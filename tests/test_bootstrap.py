"""在临时目录中验证初始化，不读取真实密钥或操作用户数据目录。"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from swe_agent.bootstrap import initialize_app
from swe_agent.cli_new import app
from swe_agent.config import AppPaths, ConfigError, load_app_config
from swe_agent.session_store import SessionStore, SessionStoreError


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.paths = AppPaths(self.root / "数据目录" / "nested")
        self.runner = CliRunner()

    def invoke(self, *arguments, home=None):
        return self.runner.invoke(
            app, list(arguments),
            env={"SWE_AGENT_HOME": str(self.paths.data_dir) if home is None else home},
            terminal_width=200,
        )

    def test_first_initialization_creates_valid_resources_without_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            initialize_app(self.paths)
        config = load_app_config(self.paths)
        self.assertEqual(config.session_defaults.max_steps, 15)
        self.assertEqual(SessionStore(self.paths).list_sessions(), [])
        self.assertIn("SWE_AGENT_API_KEY=\n", self.paths.env_file.read_text(encoding="utf-8"))

    def test_repeated_initialization_preserves_custom_configuration_credentials_and_history(self):
        initialize_app(self.paths)
        content = self.paths.config_file.read_text(encoding="utf-8").replace(
            'model = "provider/model"', 'model = "provider/custom"'
        )
        self.paths.config_file.write_text(content, encoding="utf-8")
        self.paths.env_file.write_text("SWE_AGENT_API_KEY=test-only-secret\n", encoding="utf-8")
        store = SessionStore(self.paths)
        store.create_session("demo", self.root, load_app_config(self.paths).session_defaults)
        store.append_message("demo", {"role": "user", "content": "保留历史"})
        session = store.get_session("demo")
        history = store.load_messages("demo")
        config_bytes = self.paths.config_file.read_bytes()
        env_bytes = self.paths.env_file.read_bytes()
        result = self.invoke("init")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.paths.config_file.read_bytes(), config_bytes)
        self.assertEqual(self.paths.env_file.read_bytes(), env_bytes)
        self.assertEqual(store.get_session("demo"), session)
        self.assertEqual(store.load_messages("demo"), history)
        self.assertNotIn("test-only-secret", result.output)

    def test_missing_resources_can_be_recreated_independently(self):
        initialize_app(self.paths)
        for path in (self.paths.config_file, self.paths.env_file, self.paths.database_file):
            with self.subTest(path=path):
                path.unlink()
                initialize_app(self.paths)
                self.assertTrue(path.is_file())
                self.assertEqual(SessionStore(self.paths).list_sessions(), [])

    def test_env_template_uses_configured_variable_name_without_loading_it(self):
        initialize_app(self.paths)
        content = self.paths.config_file.read_text(encoding="utf-8").replace(
            "SWE_AGENT_API_KEY", "CUSTOM_MODEL_KEY"
        )
        self.paths.config_file.write_text(content, encoding="utf-8")
        self.paths.env_file.unlink()
        with patch.dict(os.environ, {}, clear=True):
            initialize_app(self.paths)
            self.assertNotIn("CUSTOM_MODEL_KEY", os.environ)
        self.assertIn("CUSTOM_MODEL_KEY=\n", self.paths.env_file.read_text(encoding="utf-8"))

    def test_invalid_config_is_not_replaced_and_stops_before_database_creation(self):
        self.paths.data_dir.mkdir(parents=True)
        self.paths.config_file.write_text("[broken", encoding="utf-8")
        with self.assertRaises(ConfigError):
            initialize_app(self.paths)
        self.assertEqual(self.paths.config_file.read_text(encoding="utf-8"), "[broken")
        self.assertFalse(self.paths.database_file.exists())
        self.assertFalse(self.paths.env_file.exists())

    def test_incompatible_database_is_not_overwritten(self):
        self.paths.data_dir.mkdir(parents=True)
        with closing(sqlite3.connect(self.paths.database_file)) as conn, conn:
            conn.execute("CREATE TABLE legacy_data (value TEXT)")
            conn.execute("INSERT INTO legacy_data VALUES ('keep')")
        with self.assertRaises(SessionStoreError):
            initialize_app(self.paths)
        with closing(sqlite3.connect(self.paths.database_file)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM legacy_data").fetchall(), [("keep",)])
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertFalse(self.paths.env_file.exists())

    def test_directory_in_place_of_file_is_rejected(self):
        self.paths.data_dir.mkdir(parents=True)
        self.paths.config_file.mkdir()
        with self.assertRaises(OSError):
            initialize_app(self.paths)
        self.assertTrue(self.paths.config_file.is_dir())
        self.assertFalse(self.paths.database_file.exists())

    def test_cli_init_succeeds_and_reports_resource_locations(self):
        result = self.invoke("init")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("初始化完成", result.output)
        for path in (self.paths.data_dir, self.paths.config_file, self.paths.env_file, self.paths.database_file):
            self.assertIn(str(path), result.output)
        self.assertEqual(SessionStore(self.paths).list_sessions(), [])

    def test_cli_failure_returns_nonzero_and_leaves_bad_configuration(self):
        self.paths.data_dir.mkdir(parents=True)
        self.paths.config_file.write_text("", encoding="utf-8")
        result = self.invoke("init")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("初始化失败", result.output)
        self.assertIn("session_defaults", result.output)
        self.assertEqual(self.paths.config_file.read_text(encoding="utf-8"), "")

    def test_help_and_invalid_home_do_not_initialize(self):
        result = self.invoke("--help")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("init", result.output)
        self.assertFalse(self.paths.data_dir.exists())
        for home in ("", "relative/path"):
            with self.subTest(home=home):
                result = self.invoke("init", home=home)
                self.assertEqual(result.exit_code, 1)
                self.assertIn("SWE_AGENT_HOME", result.output)
        self.assertFalse(self.paths.data_dir.exists())

    def test_module_entry_does_not_load_project_env_or_old_runtime(self):
        # 在新进程中从另一个目录启动，避免模块缓存掩盖旧入口的导入副作用。
        unwanted_home = self.root / "must-not-create"
        (self.root / ".env").write_text(
            f"SWE_AGENT_HOME={unwanted_home.as_posix()}\nCONTEXT_TOKEN_LIMIT=invalid\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment["SWE_AGENT_HOME"] = str(self.paths.data_dir)
        environment["CONTEXT_TOKEN_LIMIT"] = "invalid"
        result = subprocess.run(
            [sys.executable, "-m", "swe_agent.cli_new", "init"],
            cwd=self.root, env=environment, capture_output=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.paths.database_file.is_file())
        self.assertFalse(unwanted_home.exists())


if __name__ == "__main__":
    unittest.main()
