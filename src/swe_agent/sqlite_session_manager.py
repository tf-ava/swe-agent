from __future__ import annotations

import os
from dotenv import load_dotenv

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from swe_agent.prompt import COMPRESS_PROMPT

load_dotenv()

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY"
)

BASE_URL = os.getenv(
    "BASE_URL"
)

COMPRESS_MODEL = os.getenv(
    "COMPRESS_MODEL"
)


MODULE_VERSION = "2026.07.12-reviewed-1"



Message = dict[str, Any]
#status 有 running,pending_toolcall,get_finish_result

@dataclass(slots=True)
class Session:
    session_id: str
    status: str
    messages: list[Message]
    token_usage: int
    pending_toolcalls: list[dict[str, Any]]
    context_too_long: bool = False
    notice: str | None = None
ROLES = {"system", "user", "assistant", "tool", "function"}
PRIVATE_PREFIX = "_session_manager_"
SUMMARY_PREFIX = "以下是此前对话的压缩总结：\n\n"


class SQLiteSessionManager:
    """SWE Agent Session 持久化与上下文压缩。"""

    def __init__(
        self,
        project_path: str | Path,
        *,
        context_token_limit: int,
        keep_recent_messages: int = 12,
        model: str = COMPRESS_MODEL,
        summary_model: str | None = None,
    ) -> None:
        if context_token_limit <= 0 or keep_recent_messages <= 0:
            raise ValueError("context_token_limit and keep_recent_messages must be positive")

        self.project_path = Path(project_path).expanduser().resolve()
        self.data_path = self.project_path / ".sweagent"
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_path / "sessions.sqlite3"

        self.model = model
        if not self.model:
            raise ValueError("COMPRESS_MODEL is required")
        self.summary_model = summary_model or model
        self.context_token_limit = context_token_limit
        self.keep_recent_messages = keep_recent_messages
        self._init_db()

    def load_or_create(
        self,
        session_id: str | None,
        system_message: Message,
        *,
        status: str = "running",
    ) -> Session:
        """存在则加载，不存在则创建后加载。

        session_id 为 None 时自动生成一个新 ID。
        system_message 只会在创建新 Session 时写入。
        """
        self._validate_message(system_message)
        if system_message["role"] != "system":
            raise ValueError("the first message must be a system message")

        session_id = session_id or uuid.uuid4().hex

        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

            if exists is None:
                content, extra = self._encode_message(system_message)
                with conn:
                    # 创建时因外键约束必须先插入 sessions，再插入第一条 message。
                    conn.execute(
                        "INSERT INTO sessions(id, status, token_usage) VALUES (?, ?, 0)",
                        (session_id, status),
                    )
                    conn.execute(
                        """
                        INSERT INTO messages(
                            session_id, role, content, extra_json, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            system_message["role"],
                            content,
                            extra,
                            self._now(),
                        ),
                    )

        return self._load_session(session_id)

    def append_message(
        self,
        session_id: str,
        message: Message,
    ) -> int:
        """把一条新消息写入数据库，并返回新消息的数据库 ID。

        本方法只修改 messages 表，不计算上下文 token，也不修改
        sessions.token_usage。数据库写入成功后，调用方才能执行
        runtime_messages.append(message)。
        """
        self._validate_message(message)
        content, extra = self._encode_message(message)

        with self._connect() as conn, conn:
            self._get_session(conn, session_id)
            cursor = conn.execute(
                """
                INSERT INTO messages(
                    session_id, role, content, extra_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message["role"],
                    content,
                    extra,
                    self._now(),
                ),
            )
            return int(cursor.lastrowid)

    def update_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        model_token_usage: int | None = None,
    ) -> int:
        """更新状态，或累计一次真实 LLM 调用返回的 token 用量。

        model_token_usage 必须来自模型响应的 usage.total_tokens。
        本方法是 sessions.token_usage 的唯一更新入口。
        """
        if status is None and model_token_usage is None:
            with self._connect() as conn:
                return int(self._get_session(conn, session_id)["token_usage"])

        if model_token_usage is not None and model_token_usage < 0:
            raise ValueError("model_token_usage cannot be negative")

        updates: list[str] = []
        values: list[Any] = []

        if status is not None:
            updates.append("status = ?")
            values.append(status)

        if model_token_usage is not None:
            updates.append("token_usage = token_usage + ?")
            values.append(model_token_usage)

        values.append(session_id)

        with self._connect() as conn, conn:
            self._get_session(conn, session_id)
            conn.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            row = conn.execute(
                "SELECT token_usage FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            return int(row["token_usage"])

    async def compress_session(self, session_id: str) -> Session:
        """在调用方已判断上下文超限后执行两层压缩。"""
        # 第一层只修改工具消息，不修改 sessions.token_usage。
        self._compress_tool_results(session_id)
        loaded = self._load_session(session_id)

        # 这是当前上下文长度的即时估算，不是累计模型调用 token_usage。
        if self._context_token_count(loaded.messages) <= self.context_token_limit:
            loaded.context_too_long = False
            return loaded

        batch = self._build_summary_batch(session_id)
        if batch is None:
            return self._too_long_result(loaded)

        new_summary, summary_token_usage = await self._summarize(
            batch["messages"]
        )
        summary = (
            new_summary
            if not batch["old_summary"]
            else f"{batch['old_summary']}\n\n{new_summary}"
        )

        with self._connect() as conn, conn:
            self._get_session(conn, session_id)
            conn.execute(
                """
                UPDATE sessions
                SET summary = ?,
                    summary_start_id = ?,
                    summary_end_id = ?
                WHERE id = ?
                """,
                (
                    summary,
                    batch["summary_start_id"],
                    batch["summary_end_id"],
                    session_id,
                ),
            )

        # 总结模型也是一次真实 LLM 调用，只能通过 update_session 累计。
        self.update_session(
            session_id,
            model_token_usage=summary_token_usage,
        )

        loaded = self._load_session(session_id)
        if self._context_token_count(loaded.messages) > self.context_token_limit:
            return self._too_long_result(loaded)

        loaded.context_too_long = False
        return loaded


    def set_pending(
        self,
        session_id: str,
        pending_toolcalls: list[dict[str, Any]],
    ) -> None:
        """设置当前 session 等待处理的 tool calls。"""

        with self._connect() as conn, conn:
            self._get_session(conn, session_id)

            conn.execute(
                """
                UPDATE sessions
                SET pending_toolcalls = ?
                WHERE id = ?
                """,
                (
                    self._dump(pending_toolcalls),
                    session_id,
                ),
            )


    def delete_pending(
        self,
        session_id: str,
    ) -> None:
        """删除当前 session 的 pending tool calls。"""

        with self._connect() as conn, conn:
            self._get_session(conn, session_id)

            conn.execute(
                """
                UPDATE sessions
                SET pending_toolcalls = '[]'
                WHERE id = ?
                """,
                (session_id,),
            )

        
    def context_token_count(self, messages: list[Message]) -> int:
        return self._context_token_count(messages)


    def _load_session(self, session_id: str) -> Session:
        with self._connect() as conn:
            session = self._get_session(conn, session_id)
            rows = self._get_message_rows(conn, session_id)

        if not rows:
            messages: list[Message] = []
        elif session["summary"] is None:
            messages = [self._decode_message(row) for row in rows]
        else:
            summary_end = int(session["summary_end_id"])
            messages = [self._decode_message(rows[0])]
            messages.append(
                {
                    "role": "system",
                    "content": SUMMARY_PREFIX + str(session["summary"]),
                }
            )

            # 最近用户输入若已被 summary 覆盖，则在 summary 后重新插入。
            # 若它仍在 summary_end_id 之后的最近消息中，则不重复插入。
            latest_user = next(
                (row for row in reversed(rows) if row["role"] == "user"),
                None,
            )
            if latest_user is not None and int(latest_user["id"]) <= summary_end:
                messages.append(self._decode_message(latest_user))

            messages.extend(
                self._decode_message(row)
                for row in rows
                if int(row["id"]) > summary_end
            )

        return Session(
            session_id=session_id,
            status=str(session["status"]),
            messages=messages,
            token_usage=int(session["token_usage"]),
            pending_toolcalls=json.loads(session["pending_toolcalls"]),
        )

    def _compress_tool_results(self, session_id: str) -> None:
        compressed_key = PRIVATE_PREFIX + "tool_result_compressed"
        original_key = PRIVATE_PREFIX + "original_content"

        with self._connect() as conn, conn:
            self._get_session(conn, session_id)
            rows = conn.execute(
                """
                SELECT id, role, content, extra_json
                FROM messages
                WHERE session_id = ? AND role IN ('tool', 'function')
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()

            for row in rows:
                extra = self._load_extra(row["extra_json"])
                if extra.get(compressed_key):
                    continue

                extra[compressed_key] = True
                extra[original_key] = self._load_content(row["content"])
                placeholder = (
                    f"[工具结果已省略；原始内容保存在 SQLite message_id={row['id']}]"
                )
                conn.execute(
                    """
                    UPDATE messages
                    SET content = ?, extra_json = ?
                    WHERE id = ? AND session_id = ?
                    """,
                    (
                        self._dump(placeholder),
                        self._dump(extra),
                        row["id"],
                        session_id,
                    ),
                )

    def _build_summary_batch(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            session = self._get_session(conn, session_id)
            rows = self._get_message_rows(conn, session_id)

        if len(rows) <= 1:
            return None

        previous_end = session["summary_end_id"]
        candidates = [
            row
            for row in rows[1:]  # 第一条 system 永远保留
            if previous_end is None or int(row["id"]) > int(previous_end)
        ]
        if len(candidates) <= self.keep_recent_messages:
            return None

        keep_start = len(candidates) - self.keep_recent_messages

        # 避免最近保留区从孤立的 tool/function 结果开始。
        while keep_start > 0 and candidates[keep_start]["role"] in {"tool", "function"}:
            keep_start -= 1
        if keep_start == 0:
            return None

        compressed_rows = candidates[:keep_start]

        start_id = (
            int(session["summary_start_id"])
            if session["summary_start_id"] is not None
            else int(compressed_rows[0]["id"])
        )

        return {
            "messages": [self._decode_message(row) for row in compressed_rows],
            "old_summary": session["summary"],
            "summary_start_id": start_id,
            "summary_end_id": int(compressed_rows[-1]["id"]),
        }

    async def _summarize(
        self,
        messages: list[Message],
    ) -> tuple[str, int]:
        try:
            from litellm import acompletion
        except ImportError as exc:
            raise RuntimeError("please install litellm") from exc

        payload = {"messages_to_summarize": messages}

        response = await acompletion(
            model=self.summary_model,
            messages=[
                {
                    "role": "system",
                    "content": COMPRESS_PROMPT
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            base_url=BASE_URL,
            api_key=OPENROUTER_API_KEY
        )

        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("summary model returned empty content")

        return content.strip(), self._response_total_tokens(response)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        summary TEXT,
                        summary_start_id INTEGER,
                        summary_end_id INTEGER,
                        token_usage INTEGER NOT NULL DEFAULT 0,
                        pending_toolcalls TEXT NOT NULL DEFAULT '[]'
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT,
                        extra_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(session_id)
                            REFERENCES sessions(id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_messages_session_id_id
                    ON messages(session_id, id);
                    """
                )
                self._ensure_column(
                    conn,
                    "sessions",
                    "pending_toolcalls",
                    "TEXT NOT NULL DEFAULT '[]'",
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _get_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT id, status, summary, summary_start_id,
                   summary_end_id, token_usage, pending_toolcalls
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"session does not exist: {session_id}")
        return row

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _get_message_rows(
        conn: sqlite3.Connection,
        session_id: str,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT id, role, content, extra_json, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()

    def _encode_message(self, message: Message) -> tuple[str | None, str]:
        content = message.get("content")
        extra = {k: v for k, v in message.items() if k not in {"role", "content"}}
        return (
            None if content is None else self._dump(content),
            self._dump(extra),
        )

    def _decode_message(self, row: sqlite3.Row) -> Message:
        return {
            "role": str(row["role"]),
            "content": self._load_content(row["content"]),
            **self._public_extra(self._load_extra(row["extra_json"])),
        }

    @staticmethod
    def _public_extra(extra: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in extra.items()
            if not key.startswith(PRIVATE_PREFIX)
        }

    def _context_token_count(self, messages: list[Message]) -> int:
        """即时估算 messages 的上下文长度；与 sessions.token_usage 无关。"""
        if not messages:
            return 0
        try:
            from litellm import token_counter
        except ImportError as exc:
            raise RuntimeError("please install litellm") from exc
        return int(token_counter(model=self.model, messages=messages))

    @staticmethod
    def _response_total_tokens(response: Any) -> int:
        """读取 LiteLLM/OpenAI 风格响应中的 usage.total_tokens。"""
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        if usage is None:
            return 0

        total_tokens = getattr(usage, "total_tokens", None)
        if total_tokens is None and isinstance(usage, dict):
            total_tokens = usage.get("total_tokens")
        return int(total_tokens or 0)

    @staticmethod
    def _validate_message(message: Message) -> None:
        if not isinstance(message, dict):
            raise TypeError("message must be a dict")
        if message.get("role") not in ROLES:
            raise ValueError(f"unsupported role: {message.get('role')!r}")
        if "content" not in message:
            raise ValueError("message must contain content")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load_content(value: str | None) -> Any:
        return None if value is None else json.loads(value)

    @staticmethod
    def _load_extra(value: str) -> dict[str, Any]:
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise ValueError("extra_json must be a JSON object")
        return loaded

    @staticmethod
    def _too_long_result(loaded: Session) -> Session:
        loaded.context_too_long = True
        loaded.notice = "当前对话过长，请创建或切换到新的 session。"
        return loaded

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()


SqliteSessionManager = SQLiteSessionManager
