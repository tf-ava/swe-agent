"""独立的 SQLite 持久化模块，不读取全局配置，不调用模型。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from swe_agent.config import AppPaths, SessionConfig, apply_config_overrides
from swe_agent.session import Session, SessionStatus, StoredMessage


SCHEMA_VERSION = 1
_TABLE_COLUMNS = {
    "sessions": {
        "id", "workspace_root", "config_json", "status", "created_at", "updated_at",
        "summary", "summary_start_id", "summary_end_id", "token_usage", "pending_toolcalls",
    },
    "messages": {"id", "session_id", "role", "content_json", "extra_json", "created_at"},
}
_SCHEMA = (
    """
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY NOT NULL,
        workspace_root TEXT NOT NULL,
        config_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN ('idle', 'running', 'waiting_approval', 'completed', 'failed', 'cancelled')
        ),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        summary TEXT,
        summary_start_id INTEGER,
        summary_end_id INTEGER,
        token_usage INTEGER NOT NULL DEFAULT 0 CHECK (token_usage >= 0),
        pending_toolcalls TEXT NOT NULL DEFAULT '[]',
        CHECK (
            (summary IS NULL AND summary_start_id IS NULL AND summary_end_id IS NULL)
            OR (summary IS NOT NULL AND summary_start_id IS NOT NULL
                AND summary_end_id IS NOT NULL AND summary_start_id > 0
                AND summary_end_id >= summary_start_id)
        )
    )
    """,
    """
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
        content_json TEXT NOT NULL,
        extra_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_messages_session_id_id ON messages(session_id, id)",
)


class SessionStoreError(RuntimeError):
    """数据库不可用、结构不兼容或已保存的数据无效。"""


class SessionNotFoundError(SessionStoreError):
    """指定的会话不存在，查询操作不会自动创建会话。"""


class SessionAlreadyExistsError(SessionStoreError):
    """会话 ID 已存在，创建操作不会覆盖它。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _check_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise SessionStoreError(
            f"Unsupported database schema version: {version}; expected {SCHEMA_VERSION}"
        )
    for table, required_columns in _TABLE_COLUMNS.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not required_columns.issubset(columns):
            raise SessionStoreError(f"Invalid database table: {table}")
    index_columns = [
        row[2] for row in conn.execute("PRAGMA index_info(idx_messages_session_id_id)")
    ]
    if index_columns != ["session_id", "id"]:
        raise SessionStoreError("Missing or invalid message index")


def initialize_database(paths: AppPaths) -> None:
    """显式初始化数据库，供未来的 init 命令调用；不创建父目录。

    已初始化的兼容数据库仅检查结构。空数据库可继续初始化，
    有旧表或其他结构的数据库会报错，不迁移、不覆盖。
    """
    if not paths.data_dir.is_dir():
        raise SessionStoreError(f"Data directory does not exist: {paths.data_dir}")
    try:
        conn = sqlite3.connect(paths.database_file, timeout=5.0)
    except sqlite3.OperationalError as exc:
        raise SessionStoreError(f"Cannot open database: {paths.database_file}") from exc
    try:
        # 显式事务保证建表和版本号一起提交，并串行化重复初始化。
        conn.execute("BEGIN IMMEDIATE")
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if tables or version:
            _check_schema(conn)
        else:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        raise SessionStoreError(f"Cannot initialize database: {paths.database_file}") from exc
    finally:
        conn.close()


class SessionStore:
    """以集中数据库为边界保存数据；构造和普通操作均不自动初始化。

    每次操作独立连接。写操作使用事务，返回的 Session 不携带数据库连接，
    也不会自动加载历史。全局默认配置应由创建会话的调用方显式传入。
    """

    def __init__(self, paths: AppPaths) -> None:
        self.db_path = paths.database_file.expanduser().resolve()
        with self._connect():
            pass

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            # mode=rw 要求文件已存在，防止普通连接悄悄创建数据库。
            conn = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=5.0)
        except sqlite3.OperationalError as exc:
            raise SessionStoreError(
                f"Cannot open database: {self.db_path}. Run 'swe-agent init' first."
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            _check_schema(conn)
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except sqlite3.DatabaseError as exc:
            conn.rollback()
            raise SessionStoreError(f"Database operation failed: {self.db_path}") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _get_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Session does not exist: {session_id}")
        return row

    @staticmethod
    def _decode_session(row: sqlite3.Row) -> Session:
        try:
            config = SessionConfig.model_validate_json(row["config_json"])
            pending = json.loads(row["pending_toolcalls"])
            if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
                raise ValueError("invalid pending tool calls")
            return Session(
                session_id=row["id"],
                workspace_root=Path(row["workspace_root"]),
                config=config,
                status=SessionStatus(row["status"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                summary=row["summary"],
                summary_start_id=row["summary_start_id"],
                summary_end_id=row["summary_end_id"],
                token_usage=row["token_usage"],
                pending_toolcalls=pending,
            )
        except (ValueError, TypeError) as exc:
            raise SessionStoreError(f"Invalid stored session: {row['id']}") from exc

    def create_session(
        self, session_id: str, workspace_root: str | Path, config: SessionConfig
    ) -> Session:
        """保存完整配置并创建空会话；不写入系统提示词或对话消息。"""
        if not isinstance(session_id, str) or not session_id.strip() or session_id != session_id.strip():
            raise ValueError("session_id must be nonempty and have no surrounding whitespace")
        workspace = Path(workspace_root).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {workspace}")
        config = SessionConfig.model_validate(config.model_dump())
        with self._connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                raise SessionAlreadyExistsError(f"Session already exists: {session_id}")
            now = _now()
            conn.execute(
                """INSERT INTO sessions (
                    id, workspace_root, config_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, str(workspace), config.model_dump_json(), SessionStatus.IDLE, now, now),
            )
            return self._decode_session(self._get_row(conn, session_id))

    def get_session(self, session_id: str) -> Session:
        """读取会话属性和配置，不加载历史，不访问全局 config 文件。"""
        with self._connect() as conn:
            return self._decode_session(self._get_row(conn, session_id))

    def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[Session]:
        """按最近更新时间倒序分页读取会话，时间相同时以 ID 排序。"""
        self._check_integer(limit, minimum=1)
        self._check_integer(offset, minimum=0)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._decode_session(row) for row in rows]

    def update_config(self, session_id: str, overrides: Mapping[str, object]) -> Session:
        """在同一事务中读取、校验并保存配置；无效覆盖不会产生部分修改。"""
        with self._connect(write=True) as conn:
            session = self._decode_session(self._get_row(conn, session_id))
            config = apply_config_overrides(session.config, overrides)
            conn.execute(
                "UPDATE sessions SET config_json = ?, updated_at = ? WHERE id = ?",
                (config.model_dump_json(), _now(), session_id),
            )
            return self._decode_session(self._get_row(conn, session_id))

    def append_message(self, session_id: str, message: dict[str, Any]) -> int:
        """追加原始消息并返回数据库 ID；附加字段完整保存在 extra_json 中。"""
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("History role must be user, assistant or tool")
        if "content" not in message:
            raise ValueError("Message must contain content")
        if message["role"] == "tool" and not message.get("tool_call_id"):
            raise ValueError("Tool message must contain tool_call_id")
        content = _dump(message["content"])
        extra = _dump({key: value for key, value in message.items() if key not in {"role", "content"}})
        with self._connect(write=True) as conn:
            self._get_row(conn, session_id)
            now = _now()
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content_json, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (session_id, message["role"], content, extra, now),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            return int(cursor.lastrowid)

    def load_messages(
        self, session_id: str, *, after_id: int = 0, through_id: int | None = None,
        limit: int = 100,
    ) -> list[StoredMessage]:
        """按 ID 升序读取消息；下界不包含，上界包含，不构建模型上下文。"""
        self._check_integer(after_id, minimum=0)
        self._check_integer(limit, minimum=1)
        if through_id is not None:
            self._check_integer(through_id, minimum=0)
        with self._connect() as conn:
            self._get_row(conn, session_id)
            rows = conn.execute(
                """SELECT * FROM messages WHERE session_id = ? AND id > ?
                AND (? IS NULL OR id <= ?) ORDER BY id LIMIT ?""",
                (session_id, after_id, through_id, through_id, limit),
            ).fetchall()
            result = []
            for row in rows:
                try:
                    extra = json.loads(row["extra_json"])
                    if not isinstance(extra, dict) or {"role", "content"}.intersection(extra):
                        raise ValueError("invalid message extra fields")
                    message = {"role": row["role"], "content": json.loads(row["content_json"]), **extra}
                    result.append(StoredMessage(
                        message_id=row["id"], session_id=row["session_id"], message=message,
                        created_at=datetime.fromisoformat(row["created_at"]),
                    ))
                except (ValueError, TypeError) as exc:
                    raise SessionStoreError(f"Invalid stored message: {row['id']}") from exc
            return result

    def update_state(
        self, session_id: str, *, status: SessionStatus | None = None,
        token_usage_delta: int = 0, pending_toolcalls: list[dict[str, Any]] | None = None,
    ) -> Session:
        """原子更新执行状态、累计用量和待审批调用；空列表表示清空待审批。"""
        self._check_integer(token_usage_delta, minimum=0)
        if status is not None:
            status = SessionStatus(status)
        pending_json = None
        if pending_toolcalls is not None:
            if not isinstance(pending_toolcalls, list) or any(
                not isinstance(item, dict) for item in pending_toolcalls
            ):
                raise ValueError("pending_toolcalls must be a list of objects")
            pending_json = _dump(pending_toolcalls)
        with self._connect(write=True) as conn:
            self._get_row(conn, session_id)
            conn.execute(
                """UPDATE sessions SET status = COALESCE(?, status),
                token_usage = token_usage + ?,
                pending_toolcalls = COALESCE(?, pending_toolcalls), updated_at = ? WHERE id = ?""",
                (status, token_usage_delta, pending_json, _now(), session_id),
            )
            return self._decode_session(self._get_row(conn, session_id))

    def save_summary(
        self, session_id: str, summary: str, start_id: int, end_id: int
    ) -> Session:
        """原子保存总结和覆盖范围；只校验范围归属，不选择压缩策略。"""
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Summary must not be empty")
        self._check_integer(start_id, minimum=1)
        self._check_integer(end_id, minimum=start_id)
        with self._connect(write=True) as conn:
            self._get_row(conn, session_id)
            for message_id in (start_id, end_id):
                if not conn.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? AND id = ?",
                    (session_id, message_id),
                ).fetchone():
                    raise ValueError("Summary boundaries must belong to the session")
            conn.execute(
                """UPDATE sessions SET summary = ?, summary_start_id = ?, summary_end_id = ?,
                updated_at = ? WHERE id = ?""",
                (summary, start_id, end_id, _now(), session_id),
            )
            return self._decode_session(self._get_row(conn, session_id))

    @staticmethod
    def _check_integer(value: int, *, minimum: int) -> None:
        if type(value) is not int or value < minimum:
            raise ValueError(f"Expected an integer >= {minimum}")
