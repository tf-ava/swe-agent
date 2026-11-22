"""验证配置行为，不使用真实密钥、真实数据库，也不调用模型。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

import swe_agent.config as config_module
from swe_agent.config import (
    AppPaths,
    ConfigError,
    SessionConfig,
    apply_config_overrides,
    load_app_config,
    resolve_app_paths,
)


TEMPLATE = Path(config_module.__file__).with_name("config.example.toml")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.paths = AppPaths(self.root / "agent-data")

    def write_config(self, content=None):
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text(
            TEMPLATE.read_text(encoding="utf-8") if content is None else content,
            encoding="utf-8",
        )

    def test_default_paths_do_not_depend_on_working_directory_or_create_files(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            Path, "home", return_value=self.root
        ):
            paths = resolve_app_paths()
        self.assertEqual(paths.data_dir, self.root / ".sweagent")
        self.assertEqual(paths.env_file, paths.data_dir / ".env")
        self.assertEqual(paths.database_file, paths.data_dir / "sessions.sqlite3")
        self.assertFalse(paths.data_dir.exists())

    def test_startup_environment_selects_data_directory(self):
        with patch.dict(os.environ, {"SWE_AGENT_HOME": str(self.paths.data_dir)}):
            self.assertEqual(resolve_app_paths(), self.paths)
        self.assertFalse(self.paths.data_dir.exists())

    def test_invalid_home_is_not_replaced_with_default(self):
        for value in ("", "  ", "relative/path"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"SWE_AGENT_HOME": value}
            ):
                with self.assertRaises(ConfigError):
                    resolve_app_paths()

    def test_missing_file_does_not_initialize_resources(self):
        with self.assertRaisesRegex(ConfigError, "swe-agent init"):
            load_app_config(self.paths)
        self.assertFalse(self.paths.data_dir.exists())

    def test_template_is_complete_and_environment_does_not_override_it(self):
        self.write_config()
        with patch.dict(os.environ, {"MODEL": "unexpected", "MAX_STEPS": "99"}):
            config = load_app_config(self.paths)
        self.assertEqual(config.session_defaults.model, "provider/model")
        self.assertEqual(config.session_defaults.max_steps, 15)
        self.assertFalse(self.paths.database_file.exists())
        self.assertFalse(self.paths.env_file.exists())

    def test_bad_toml_and_missing_or_unknown_fields_are_reported(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        cases = (
            ("[broken", "Invalid TOML"),
            ("", "session_defaults"),
            (template.replace("max_steps = 15", ""), "max_steps"),
            (template + "\nmax_step = 15\n", "max_step"),
            ("unexpected = 1\n" + template, "unexpected"),
            (template.replace("max_steps = 15", 'max_steps = "15"'), "max_steps"),
            (template.replace("max_steps = 15", "max_steps = true"), "max_steps"),
            (template.replace("max_steps = 15", "max_steps = 0"), "max_steps"),
        )
        for content, expected in cases:
            with self.subTest(expected=expected, content=content):
                self.write_config(content)
                with self.assertRaises(ConfigError) as caught:
                    load_app_config(self.paths)
                self.assertIn(expected, str(caught.exception))
                self.assertIn(str(self.paths.config_file), str(caught.exception))

    def test_value_validation(self):
        self.write_config()
        original = load_app_config(self.paths).session_defaults
        for overrides in (
            {"model": " "},
            {"compress_model": ""},
            {"base_url": "not-a-url"},
            {"base_url": "ftp://example.com"},
            {"base_url": "https://example.com:bad"},
            {"api_key_env": "not an environment variable"},
            {"context_token_limit": -1},
            {"keep_recent_messages": 0},
            {"max_steps": None},
            {"unknown_option": 1},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    apply_config_overrides(original, overrides)

    def test_override_produces_independent_validated_snapshot(self):
        self.write_config()
        original = load_app_config(self.paths).session_defaults
        turn = apply_config_overrides(original, {"model": "provider/other", "max_steps": 2})
        self.assertEqual(turn.model, "provider/other")
        self.assertEqual(turn.max_steps, 2)
        self.assertEqual(turn.compress_model, original.compress_model)
        self.assertEqual(original.model, "provider/model")
        self.assertEqual(original.max_steps, 15)
        self.assertEqual(load_app_config(self.paths).session_defaults, original)
        self.assertIsNot(apply_config_overrides(original, {}), original)
        with self.assertRaises(ValidationError):
            turn.max_steps = 20

    def test_saved_configuration_validates_without_global_defaults(self):
        self.write_config()
        saved = load_app_config(self.paths).session_defaults.model_dump_json()
        self.paths.config_file.unlink()
        restored = SessionConfig.model_validate_json(saved)
        self.assertEqual(restored.max_steps, 15)


if __name__ == "__main__":
    unittest.main()
