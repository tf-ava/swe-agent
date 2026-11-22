"""验证会话命令的用户可见行为；只使用临时目录和真实的临时数据库。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from swe_agent.bootstrap import initialize_app
from swe_agent.cli_new import app
from swe_agent.config import AppPaths, load_app_config
from swe_agent.session_store import SessionStore


class SessionCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.paths = AppPaths(self.root / "agent-data")
        self.workspace = self.root / "项目目录"
        self.workspace.mkdir()
        initialize_app(self.paths)
        self.store = SessionStore(self.paths)
        self.runner = CliRunner()

    def invoke(self, *arguments, paths=None):
        target = self.paths if paths is None else paths
        return self.runner.invoke(
            app, ["session", *arguments],
            env={"SWE_AGENT_HOME": str(target.data_dir)}, terminal_width=200,
        )

    def create(self, session_id="demo"):
        result = self.invoke("create", session_id, "--path", str(self.workspace))
        self.assertEqual(result.exit_code, 0, result.output)
        return self.store.get_session(session_id)

    def test_create_copies_defaults_and_leaves_history_empty(self):
        defaults = load_app_config(self.paths).session_defaults
        session = self.create()
        self.assertEqual(session.config, defaults)
        self.assertEqual(session.workspace_root, self.workspace)
        self.assertEqual(self.store.load_messages("demo"), [])
        content = self.paths.config_file.read_text(encoding="utf-8").replace(
            "max_steps = 15", "max_steps = 30"
        )
        self.paths.config_file.write_text(content, encoding="utf-8")
        self.assertEqual(self.create("later").config.max_steps, 30)
        self.assertEqual(self.store.get_session("demo").config.max_steps, 15)

    def test_create_defaults_to_actual_current_directory(self):
        result = subprocess.run(
            [sys.executable, "-m", "swe_agent.cli_new", "session", "create", "demo"],
            cwd=self.workspace, env=dict(os.environ, SWE_AGENT_HOME=str(self.paths.data_dir)),
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.store.get_session("demo").workspace_root, self.workspace)

    def test_duplicate_id_and_invalid_workspace_do_not_create_or_replace(self):
        original = self.create()
        result = self.invoke("create", "demo", "--path", str(self.root))
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("already exists", result.output)
        result = self.invoke("create", "other", "--path", str(self.root / "missing"))
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.store.list_sessions(), [original])

    def test_config_updates_all_supported_fields_without_touching_other_data(self):
        original = self.create()
        self.store.append_message("demo", {"role": "user", "content": "保留消息"})
        history = self.store.load_messages("demo")
        global_config = self.paths.config_file.read_bytes()
        result = self.invoke(
            "config", "demo", "--model", "provider/other", "--base-url", "https://new.example/v1",
            "--api-key-env", "CUSTOM_API_KEY", "--compress-model", "provider/summary",
            "--context-token-limit", "10000", "--keep-recent-messages", "6", "--max-steps", "3",
        )
        self.assertEqual(result.exit_code, 0, result.output)
        updated = self.store.get_session("demo")
        self.assertEqual(updated.config.model_dump(), {
            "model": "provider/other", "base_url": "https://new.example/v1",
            "api_key_env": "CUSTOM_API_KEY", "compress_model": "provider/summary",
            "context_token_limit": 10000, "keep_recent_messages": 6, "max_steps": 3,
        })
        self.assertEqual(updated.workspace_root, original.workspace_root)
        self.assertEqual(updated.created_at, original.created_at)
        self.assertEqual(self.store.load_messages("demo"), history)
        self.assertEqual(self.paths.config_file.read_bytes(), global_config)

    def test_partial_config_patch_keeps_unspecified_fields(self):
        original = self.create()
        result = self.invoke("config", "demo", "--max-steps", "2")
        self.assertEqual(result.exit_code, 0, result.output)
        expected = original.config.model_dump() | {"max_steps": 2}
        self.assertEqual(self.store.get_session("demo").config.model_dump(), expected)

    def test_invalid_config_patch_is_rejected_atomically(self):
        original = self.create()
        for arguments in (
            ("--max-steps", "0"),
            ("--model", ""),
            ("--model", "provider/unsaved", "--base-url", "invalid"),
            ("--api-key-env", "invalid variable"),
            ("--unknown-option", "value"),
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke("config", "demo", *arguments)
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(self.store.get_session("demo"), original)

    def test_config_without_options_does_not_write(self):
        original = self.create()
        result = self.invoke("config", "demo")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("session show", result.output)
        self.assertEqual(self.store.get_session("demo"), original)

    def test_show_displays_saved_attributes_without_history_or_secrets(self):
        original = self.create()
        self.store.append_message("demo", {"role": "user", "content": "history-not-for-show"})
        self.paths.env_file.write_text("SWE_AGENT_API_KEY=not-for-display\n", encoding="utf-8")
        with patch.object(SessionStore, "load_messages", side_effect=AssertionError("不应读取历史")):
            result = self.invoke("show", "demo")
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["session_id"], "demo")
        self.assertEqual(data["workspace_root"], str(self.workspace))
        self.assertEqual(data["config"], original.config.model_dump())
        self.assertEqual(data["status"], "idle")
        self.assertEqual(data["token_usage"], 0)
        self.assertNotIn("history-not-for-show", result.output)
        self.assertNotIn("not-for-display", result.output)

    def test_existing_session_commands_work_without_global_config_and_env(self):
        self.create()
        self.paths.config_file.unlink()
        self.paths.env_file.unlink()
        for arguments in (("show", "demo"), ("list",), ("config", "demo", "--max-steps", "2")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.exit_code, 0, result.output)
        result = self.invoke("create", "new", "--path", str(self.workspace))
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual([session.session_id for session in self.store.list_sessions()], ["demo"])
        self.assertFalse(self.paths.config_file.exists())
        self.assertFalse(self.paths.env_file.exists())

    def test_bad_global_config_prevents_creation_but_not_query(self):
        self.create()
        self.paths.config_file.write_text("[broken", encoding="utf-8")
        result = self.invoke("create", "new", "--path", str(self.workspace))
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.invoke("show", "demo").exit_code, 0)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_unknown_session_is_never_created(self):
        for arguments in (("show", "missing"), ("config", "missing", "--max-steps", "2")):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("does not exist", result.output)
        self.assertEqual(self.store.list_sessions(), [])

    def test_uninitialized_directory_is_not_created(self):
        paths = AppPaths(self.root / "uninitialized")
        for arguments in (
            ("create", "demo"), ("show", "demo"), ("list",),
            ("config", "demo", "--max-steps", "2"),
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments, paths=paths)
                self.assertNotEqual(result.exit_code, 0)
                self.assertIn("swe-agent init", result.output)
                self.assertFalse(paths.data_dir.exists())

    def test_list_uses_update_order_pagination_and_basic_fields(self):
        for day, session_id in enumerate(("a", "b", "c"), start=1):
            with patch("swe_agent.session_store._now", return_value=f"2026-01-0{day}T00:00:00+00:00"):
                self.create(session_id)
        with patch("swe_agent.session_store._now", return_value="2026-01-04T00:00:00+00:00"):
            self.store.update_config("a", {"max_steps": 2})
        with patch.object(SessionStore, "load_messages", side_effect=AssertionError("不应读取历史")):
            first = self.invoke("list", "--limit", "2")
            second = self.invoke("list", "--limit", "2", "--offset", "2")
        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual([line.split("\t")[0] for line in first.output.splitlines()[1:]], ["a", "c"])
        self.assertEqual([line.split("\t")[0] for line in second.output.splitlines()[1:]], ["b"])
        self.assertIn(str(self.workspace), first.output)
        self.assertIn("idle", first.output)
        self.assertIn("最近更新时间", first.output)

    def test_empty_list_and_invalid_pagination(self):
        result = self.invoke("list")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("没有会话", result.output)
        for arguments in (("--limit", "0"), ("--offset", "-1")):
            with self.subTest(arguments=arguments):
                self.assertNotEqual(self.invoke("list", *arguments).exit_code, 0)
        self.assertEqual(self.store.list_sessions(), [])


if __name__ == "__main__":
    unittest.main()
