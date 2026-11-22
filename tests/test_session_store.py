"""在临时数据库中验证持久化边界、事务和消息隔离，不接入旧执行流程。"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

import swe_agent.config as config_module
from swe_agent.config import AppPaths, load_app_config
from swe_agent.session import SessionStatus
from swe_agent.session_store import (
    SCHEMA_VERSION,
    SessionAlreadyExistsError,
    SessionNotFoundError,
    SessionStore,
    SessionStoreError,
    initialize_database,
)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        # 路径包含 URI 特殊字符，用于验证数据库连接不会误解析文件名。
        self.paths = AppPaths(self.root / "数据 #%目录")
        self.paths.data_dir.mkdir()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        template = Path(config_module.__file__).with_name("config.example.toml")
        self.paths.config_file.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        self.config = load_app_config(self.paths).session_defaults
        initialize_database(self.paths)
        self.store = SessionStore(self.paths)

    def create(self, session_id="demo"):
        return self.store.create_session(session_id, self.workspace, self.config)

    def execute_sql(self, sql, parameters=()):
        with closing(sqlite3.connect(self.paths.database_file)) as conn, conn:
            return conn.execute(sql, parameters).fetchall()

    def test_empty_session_round_trip_without_global_config_or_credentials(self):
        self.paths.config_file.unlink()
        session = self.create()
        restored = SessionStore(self.paths).get_session("demo")
        self.assertEqual(restored, session)
        self.assertEqual(restored.workspace_root, self.workspace)
        self.assertEqual(restored.config, self.config)
        self.assertEqual(restored.status, SessionStatus.IDLE)
        self.assertEqual(restored.token_usage, 0)
        self.assertEqual(restored.pending_toolcalls, [])
        self.assertIsNone(restored.summary)
        self.assertIsNone(restored.summary_start_id)
        self.assertIsNone(restored.summary_end_id)
        self.assertIsNotNone(restored.created_at.utcoffset())
        self.assertEqual(restored.created_at, restored.updated_at)
        self.assertEqual(self.store.load_messages("demo"), [])

    def test_duplicate_and_invalid_creation_leave_original_unchanged(self):
        original = self.create()
        with self.assertRaises(SessionAlreadyExistsError):
            self.create()
        for session_id in ("", " ", " demo"):
            with self.subTest(session_id=session_id), self.assertRaises(ValueError):
                self.create(session_id)
        with self.assertRaises(ValueError):
            self.store.create_session("other", self.root / "missing", self.config)
        self.assertEqual(self.store.list_sessions(), [original])

    def test_unknown_session_operations_do_not_create_it(self):
        operations = (
            lambda: self.store.get_session("missing"),
            lambda: self.store.load_messages("missing"),
            lambda: self.store.update_config("missing", {"max_steps": 2}),
            lambda: self.store.append_message("missing", {"role": "user", "content": "你好"}),
            lambda: self.store.update_state("missing", status=SessionStatus.RUNNING),
            lambda: self.store.save_summary("missing", "总结", 1, 1),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(SessionNotFoundError):
                operation()
        self.assertEqual(self.store.list_sessions(), [])

    def test_config_patch_is_persistent_and_atomic(self):
        original = self.create()
        updated = self.store.update_config("demo", {"model": "provider/other", "max_steps": 2})
        self.assertEqual(updated.config.model, "provider/other")
        self.assertEqual(updated.config.max_steps, 2)
        self.assertEqual(updated.config.compress_model, original.config.compress_model)
        self.assertEqual(updated.workspace_root, original.workspace_root)
        self.assertEqual(updated.created_at, original.created_at)
        self.assertGreaterEqual(updated.updated_at, original.updated_at)
        self.assertEqual(original.config, self.config)
        with self.assertRaises(ValidationError):
            self.store.update_config("demo", {"model": "should-not-save", "max_steps": 0})
        with self.assertRaises(ValidationError):
            self.store.update_config("demo", {"unknown_option": 1})
        self.assertEqual(self.store.get_session("demo"), updated)

    def test_list_sessions_paginates_without_history(self):
        self.create("first")
        self.create("second")
        self.store.append_message("first", {"role": "user", "content": "历史"})
        all_sessions = self.store.list_sessions()
        self.assertEqual([session.session_id for session in all_sessions], ["first", "second"])
        self.assertEqual(self.store.list_sessions(limit=1, offset=1), [all_sessions[1]])
        self.assertFalse(hasattr(all_sessions[0], "messages"))

    def test_history_preserves_protocol_fields_and_session_isolation(self):
        self.create("a")
        self.create("b")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "检查代码"}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"path":"main.py"}',
                }},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"success":true}'},
        ]
        ids = []
        for message in messages:
            ids.append(self.store.append_message("a", message))
            self.store.append_message("b", {"role": "user", "content": "另一个会话"})
        stored = self.store.load_messages("a")
        self.assertEqual([item.message for item in stored], messages)
        self.assertEqual([item.message_id for item in stored], ids)
        self.assertTrue(all(item.session_id == "a" for item in stored))
        self.assertTrue(all(item.created_at.utcoffset() is not None for item in stored))
        self.assertEqual(
            self.store.load_messages("a", after_id=ids[0], through_id=ids[1]), [stored[1]]
        )
        self.assertEqual(self.store.load_messages("a", limit=1), stored[:1])
        self.assertEqual(self.store.load_messages("a", after_id=ids[-1]), [])

    def test_invalid_messages_do_not_change_history_or_session(self):
        original = self.create()
        for message in (
            {"role": "system", "content": "动态系统提示词不属于历史"},
            {"role": "user"},
            {"role": "tool", "content": "缺少调用 ID"},
            {"role": "user", "content": object()},
            {"role": "user", "content": float("nan")},
        ):
            with self.subTest(message=message), self.assertRaises((TypeError, ValueError)):
                self.store.append_message("demo", message)
        self.assertEqual(self.store.load_messages("demo"), [])
        self.assertEqual(self.store.get_session("demo"), original)

    def test_message_and_session_timestamp_roll_back_together(self):
        original = self.create()
        # 在第二条写操作故意触发错误，确认前面的消息插入也被回滚。
        self.execute_sql("""CREATE TRIGGER fail_update BEFORE UPDATE ON sessions
            BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(SessionStoreError):
            self.store.append_message("demo", {"role": "user", "content": "不应写入"})
        self.assertEqual(self.store.load_messages("demo"), [])
        self.assertEqual(self.store.get_session("demo"), original)

    def test_state_updates_accumulate_usage_and_clear_pending(self):
        self.create()
        pending = [{"id": "call_1", "function": {"name": "write_file", "arguments": "{}"}}]
        waiting = self.store.update_state(
            "demo", status=SessionStatus.WAITING_APPROVAL,
            token_usage_delta=10, pending_toolcalls=pending,
        )
        self.assertEqual(waiting.pending_toolcalls, pending)
        completed = self.store.update_state(
            "demo", status=SessionStatus.COMPLETED, token_usage_delta=5, pending_toolcalls=[]
        )
        self.assertEqual(completed.token_usage, 15)
        self.assertEqual(completed.pending_toolcalls, [])
        self.assertEqual(completed.status, SessionStatus.COMPLETED)
        for arguments in (
            {"token_usage_delta": -1}, {"token_usage_delta": True},
            {"status": "unknown"}, {"pending_toolcalls": ["invalid"]},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.store.update_state("demo", **arguments)
        self.assertEqual(self.store.get_session("demo"), completed)

    def test_summary_range_is_saved_atomically_without_modifying_history(self):
        self.create("a")
        self.create("b")
        first = self.store.append_message("a", {"role": "user", "content": "问题"})
        other = self.store.append_message("b", {"role": "user", "content": "其他问题"})
        last = self.store.append_message("a", {"role": "assistant", "content": "回答"})
        original_history = self.store.load_messages("a")
        session = self.store.save_summary("a", "已有结论", first, last)
        self.assertEqual((session.summary_start_id, session.summary_end_id), (first, last))
        self.assertEqual(session.summary, "已有结论")
        for start, end in ((last, first), (first, other), (first, last + 100)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.store.save_summary("a", "不能写入", start, end)
        self.assertEqual(self.store.get_session("a"), session)
        self.assertEqual(self.store.load_messages("a"), original_history)

    def test_invalid_pagination_is_rejected(self):
        self.create()
        for arguments in ({"limit": 0}, {"offset": -1}, {"limit": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.store.list_sessions(**arguments)
        for arguments in ({"limit": 0}, {"after_id": -1}, {"through_id": -1}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.store.load_messages("demo", **arguments)

    def test_initialization_is_repeatable_and_keeps_saved_data(self):
        self.create()
        self.store.append_message("demo", {"role": "user", "content": "保留"})
        before = self.store.get_session("demo")
        history = self.store.load_messages("demo")
        initialize_database(self.paths)
        self.assertEqual(self.store.get_session("demo"), before)
        self.assertEqual(self.store.load_messages("demo"), history)
        self.assertEqual(self.execute_sql("PRAGMA user_version")[0][0], SCHEMA_VERSION)

    def test_missing_database_is_not_created_by_ordinary_connections(self):
        paths = AppPaths(self.root / "uninitialized")
        with self.assertRaises(SessionStoreError):
            SessionStore(paths)
        self.assertFalse(paths.data_dir.exists())
        paths.data_dir.mkdir()
        with self.assertRaises(SessionStoreError):
            SessionStore(paths)
        self.assertFalse(paths.database_file.exists())
        # 构造后文件丢失也不能让下一次连接自动建库。
        self.paths.database_file.unlink()
        with self.assertRaises(SessionStoreError):
            self.store.list_sessions()
        self.assertFalse(self.paths.database_file.exists())

    def test_initialization_does_not_create_parent_directory(self):
        paths = AppPaths(self.root / "missing")
        with self.assertRaises(SessionStoreError):
            initialize_database(paths)
        self.assertFalse(paths.data_dir.exists())

    def test_old_schema_is_rejected_and_unchanged(self):
        paths = AppPaths(self.root / "legacy")
        paths.data_dir.mkdir()
        with closing(sqlite3.connect(paths.database_file)) as conn, conn:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO sessions VALUES ('legacy')")
        for operation in (lambda: SessionStore(paths), lambda: initialize_database(paths)):
            with self.assertRaises(SessionStoreError):
                operation()
        with closing(sqlite3.connect(paths.database_file)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM sessions").fetchall(), [("legacy",)])
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_wrong_version_missing_table_and_invalid_file_are_rejected(self):
        self.execute_sql("PRAGMA user_version = 999")
        with self.assertRaises(SessionStoreError):
            SessionStore(self.paths)
        self.execute_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.execute_sql("DROP TABLE messages")
        with self.assertRaises(SessionStoreError):
            SessionStore(self.paths)
        paths = AppPaths(self.root / "invalid")
        paths.data_dir.mkdir()
        paths.database_file.write_text("not a database", encoding="utf-8")
        with self.assertRaises(SessionStoreError):
            SessionStore(paths)

    def test_invalid_saved_config_is_reported_without_fallback(self):
        self.create()
        self.execute_sql("UPDATE sessions SET config_json = '{}' WHERE id = 'demo'")
        with self.assertRaisesRegex(SessionStoreError, "Invalid stored session"):
            self.store.get_session("demo")

    def test_foreign_key_relationship_prevents_orphan_messages(self):
        self.create()
        self.store.append_message("demo", {"role": "user", "content": "历史"})
        with closing(sqlite3.connect(self.paths.database_file)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM sessions WHERE id = 'demo'")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
