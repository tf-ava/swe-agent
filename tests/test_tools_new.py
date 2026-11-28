"""通过新版工具目录验证 Responses API 格式和真实文件操作。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swe_agent.tools_new.catalog import BUILTIN_TOOLS


class FileToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.temp_root = Path(temp_dir.name)
        self.workspace_root = self.temp_root / "工作目录"
        self.workspace_root.mkdir()
        self.path = "示例.txt"
        self.content = "第一行\n第二行\n第三行\n"
        (self.workspace_root / self.path).write_text(self.content, encoding="utf-8")

    async def execute_call(self, arguments, *, name="read_file"):
        call = {
            "type": "function_call",
            "id": "fc_file",
            "call_id": "call_file",
            "name": name,
            "arguments": arguments,
        }
        spec = BUILTIN_TOOLS[call["name"]]
        item = await spec.execute(self.workspace_root, call["call_id"], call["arguments"])

        self.assertEqual(set(item), {"type", "call_id", "output"})
        self.assertEqual(item["type"], "function_call_output")
        self.assertEqual(item["call_id"], call["call_id"])
        self.assertIsInstance(item["output"], str)
        return json.loads(item["output"])

    def test_schema_is_a_strict_responses_tool(self):
        spec = BUILTIN_TOOLS["read_file"]
        schema = spec.schema
        self.assertEqual(set(schema), {"type", "name", "description", "strict", "parameters"})
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["name"], "read_file")
        self.assertIs(schema["strict"], True)
        self.assertEqual(spec.effect, "read")
        self.assertEqual(spec.risk_level, "low")

        parameters = schema["parameters"]
        properties = parameters["properties"]
        self.assertEqual(parameters["type"], "object")
        self.assertIs(parameters["additionalProperties"], False)
        self.assertEqual(set(properties), {"path", "start_line", "end_line"})
        self.assertCountEqual(parameters["required"], properties)
        self.assertEqual(properties["path"]["type"], "string")
        for name in ("start_line", "end_line"):
            with self.subTest(name=name):
                self.assertCountEqual(
                    properties[name]["anyOf"], [{"type": "integer"}, {"type": "null"}]
                )
                self.assertNotIn("default", properties[name])

    async def test_read_full_utf8_file(self):
        arguments = {"path": self.path, "start_line": None, "end_line": None}
        result = await self.execute_call(json.dumps(arguments))
        self.assertEqual(
            result,
            {"success": True, "path": self.path, "content": self.content, "total_lines": 3},
        )

    async def test_read_inclusive_line_ranges(self):
        cases = (
            (2, 2, "第二行"),
            (2, None, "第二行\n第三行"),
            (None, 2, "第一行\n第二行"),
        )
        for start_line, end_line, expected in cases:
            with self.subTest(start_line=start_line, end_line=end_line):
                arguments = {"path": self.path, "start_line": start_line, "end_line": end_line}
                result = await self.execute_call(json.dumps(arguments))
                self.assertTrue(result["success"])
                self.assertEqual(result["content"], expected)
                self.assertEqual(result["total_lines"], 3)

    async def test_read_explicit_build_files_and_env_example(self):
        files = {
            "src/build/config.py": "enabled = True\n",
            "dist/manifest.json": "{}\n",
            ".env.example": "EXAMPLE=\n",
        }
        for path, content in files.items():
            with self.subTest(path=path):
                file_path = self.workspace_root / path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")

                arguments = {"path": path, "start_line": None, "end_line": None}
                result = await self.execute_call(json.dumps(arguments))
                self.assertEqual(
                    result,
                    {"success": True, "path": str(Path(path)), "content": content, "total_lines": 1},
                )

    async def test_invalid_arguments_return_an_output_item(self):
        valid = {"path": self.path, "start_line": None, "end_line": None}
        invalid_arguments = (
            "{",
            json.dumps([]),
            json.dumps({"path": self.path, "start_line": None}),
            json.dumps(valid | {"path": ""}),
            json.dumps(valid | {"extra": True}),
            json.dumps(valid | {"start_line": True}),
            json.dumps(valid | {"start_line": "2"}),
            json.dumps(valid | {"start_line": 0}),
            json.dumps(valid | {"start_line": 3, "end_line": 2}),
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                result = await self.execute_call(arguments)
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "invalid tool arguments")
                self.assertTrue(result["details"])
                self.assertNotIn("input", result["details"][0])

    async def test_protected_paths_are_rejected(self):
        for path in (".git/config", ".env", ".env.local", ".env.production"):
            with self.subTest(path=path):
                file_path = self.workspace_root / path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("EXAMPLE=value\n", encoding="utf-8")

                arguments = {"path": path, "start_line": None, "end_line": None}
                result = await self.execute_call(json.dumps(arguments))
                self.assertEqual(result, {"success": False, "error": "path is protected", "path": path})

    async def test_file_errors_return_output_items(self):
        outside = self.temp_root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (self.workspace_root / "binary").write_bytes(b"\xff")
        cases = (
            ("../outside.txt", "path is outside workspace"),
            (str(outside), "path is outside workspace"),
            ("missing.txt", "file not found"),
            (".", "not a file"),
            ("binary", "file is not valid UTF-8"),
        )
        for path, error in cases:
            with self.subTest(path=path):
                arguments = {"path": path, "start_line": None, "end_line": None}
                result = await self.execute_call(json.dumps(arguments))
                self.assertEqual(result, {"success": False, "error": error, "path": path})

    def test_write_schema_is_a_strict_responses_tool(self):
        spec = BUILTIN_TOOLS["write_file"]
        schema = spec.schema
        self.assertEqual(set(schema), {"type", "name", "description", "strict", "parameters"})
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["name"], "write_file")
        self.assertIs(schema["strict"], True)
        self.assertEqual(spec.effect, "write")
        self.assertEqual(spec.risk_level, "medium")

        parameters = schema["parameters"]
        self.assertEqual(parameters["type"], "object")
        self.assertIs(parameters["additionalProperties"], False)
        self.assertCountEqual(parameters["required"], ["path", "content"])
        self.assertEqual(
            {name: prop["type"] for name, prop in parameters["properties"].items()},
            {"path": "string", "content": "string"},
        )

    async def test_write_creates_overwrites_and_clears_file(self):
        path = "src/新文件.txt"
        file_path = self.workspace_root / path
        self.assertFalse(file_path.parent.exists())
        for content in ("新建内容\n", "覆盖内容\r\n", ""):
            with self.subTest(content=content):
                arguments = json.dumps({"path": path, "content": content})
                result = await self.execute_call(arguments, name="write_file")
                self.assertEqual(result, {"success": True, "path": str(Path(path))})
                self.assertEqual(file_path.read_bytes(), content.encode("utf-8"))

                read_arguments = json.dumps({"path": path, "start_line": None, "end_line": None})
                read_result = await self.execute_call(read_arguments)
                self.assertTrue(read_result["success"])
                self.assertEqual(read_result["content"], content.replace("\r\n", "\n"))

    async def test_invalid_write_arguments_leave_files_untouched(self):
        cases = (
            {"path": self.path},
            {"path": self.path, "content": 7},
            {"path": "", "content": "new"},
            {"path": "new/file.txt", "content": "new", "extra": True},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = await self.execute_call(json.dumps(arguments), name="write_file")
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "invalid tool arguments")
                self.assertEqual((self.workspace_root / self.path).read_text("utf-8"), self.content)
                self.assertFalse((self.workspace_root / "new").exists())

    async def test_invalid_write_targets_leave_files_untouched(self):
        outside = self.temp_root / "outside.txt"
        outside.write_text("original", encoding="utf-8")
        protected = self.workspace_root / ".env"
        protected.write_text("original", encoding="utf-8")
        git_dir = self.workspace_root / ".git"
        git_dir.mkdir()
        cases = (
            ("../outside.txt", "path is outside workspace"),
            (".env", "path is protected"),
            (".git/config", "path is protected"),
            (".", "not a file"),
        )
        for path, error in cases:
            with self.subTest(path=path):
                arguments = json.dumps({"path": path, "content": "new"})
                result = await self.execute_call(arguments, name="write_file")
                self.assertEqual(result, {"success": False, "error": error, "path": path})

        self.assertEqual(outside.read_text("utf-8"), "original")
        self.assertEqual(protected.read_text("utf-8"), "original")
        self.assertFalse((git_dir / "config").exists())

    async def test_write_cleans_up_after_replacement_fails(self):
        arguments = json.dumps({"path": self.path, "content": "new"})
        with patch.object(Path, "replace", side_effect=PermissionError("replacement failed")):
            result = await self.execute_call(arguments, name="write_file")

        self.assertEqual(result, {"success": False, "error": "replacement failed", "path": self.path})
        self.assertEqual((self.workspace_root / self.path).read_text("utf-8"), self.content)
        self.assertEqual(list(self.workspace_root.iterdir()), [self.workspace_root / self.path])

    def test_new_tools_publish_strict_schemas_and_effects(self):
        cases = {
            "list_dir": ({"path", "recursive"}, "read", "low"),
            "patch_check": ({"path", "content", "context_lines"}, "read", "low"),
            "delete_file": ({"path"}, "write", "high"),
            "inspect_project": ({"path"}, "read", "low"),
        }
        for name, (required, effect, risk_level) in cases.items():
            with self.subTest(name=name):
                spec = BUILTIN_TOOLS[name]
                self.assertEqual(spec.schema["type"], "function")
                self.assertEqual(spec.schema["name"], name)
                self.assertIs(spec.schema["strict"], True)
                self.assertIs(spec.schema["parameters"]["additionalProperties"], False)
                self.assertCountEqual(spec.schema["parameters"]["required"], required)
                self.assertEqual((spec.effect, spec.risk_level), (effect, risk_level))

    async def test_list_dir_lists_one_level_or_recurses_with_ignores(self):
        for path in (
            "src/pkg/main.py", "src/build/config.py", "dist/manifest.json",
            "node_modules/dependency.txt", ".git/config", ".env", ".env.example",
        ):
            file_path = self.workspace_root / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("example\n", encoding="utf-8")

        result = await self.execute_call(
            json.dumps({"path": None, "recursive": None}), name="list_dir"
        )
        self.assertEqual(result, {
            "success": True, "path": ".", "files": [".env.example", self.path], "dirs": ["src"],
        })

        result = await self.execute_call(
            json.dumps({"path": None, "recursive": True}), name="list_dir"
        )
        self.assertEqual(result, {
            "success": True,
            "path": ".",
            "files": [".env.example", str(Path("src/pkg/main.py")), self.path],
            "dirs": ["src", str(Path("src/pkg"))],
        })

        result = await self.execute_call(
            json.dumps({"path": "dist", "recursive": False}), name="list_dir"
        )
        self.assertEqual(
            result,
            {"success": True, "path": "dist", "files": [str(Path("dist/manifest.json"))], "dirs": []},
        )

    async def test_patch_check_preserves_actual_line_endings_in_preview(self):
        original = b"same\r\nold"
        file_path = self.workspace_root / self.path
        file_path.write_bytes(original)
        arguments = {"path": self.path, "content": "same\nnew\n", "context_lines": None}

        result = await self.execute_call(json.dumps(arguments), name="patch_check")

        self.assertTrue(result["success"])
        self.assertTrue(result["exists"])
        self.assertIn("-same\r\n-old\n\\ No newline at end of file\n+same\n+new\n", result["diff"])
        self.assertEqual(file_path.read_bytes(), original)

    async def test_patch_check_previews_creation_without_making_directories(self):
        arguments = {"path": "new/file.txt", "content": "hello\n", "context_lines": 0}

        result = await self.execute_call(json.dumps(arguments), name="patch_check")

        self.assertTrue(result["success"])
        self.assertFalse(result["exists"])
        self.assertTrue(result["diff"].startswith("--- /dev/null\n+++ b/new/file.txt\n"))
        self.assertIn("+hello\n", result["diff"])
        self.assertFalse((self.workspace_root / "new").exists())

    async def test_delete_file_only_removes_the_requested_file(self):
        path = self.workspace_root / "remove.txt"
        path.write_text("sample\n", encoding="utf-8")
        arguments = json.dumps({"path": path.name})

        result = await self.execute_call(arguments, name="delete_file")

        self.assertEqual(result, {"success": True, "path": path.name})
        self.assertFalse(path.exists())
        self.assertEqual((self.workspace_root / self.path).read_text("utf-8"), self.content)
        result = await self.execute_call(arguments, name="delete_file")
        self.assertEqual(result, {"success": False, "error": "file not found", "path": path.name})

    async def test_new_tools_reject_protected_and_outside_paths(self):
        protected = self.workspace_root / ".env"
        protected.write_text("example\n", encoding="utf-8")
        outside = self.temp_root / "outside.txt"
        outside.write_text("example\n", encoding="utf-8")
        cases = (
            ("list_dir", {"path": "..", "recursive": True}, "path is outside workspace"),
            ("inspect_project", {"path": ".git"}, "path is protected"),
            ("patch_check", {"path": ".env", "content": "", "context_lines": None}, "path is protected"),
            ("delete_file", {"path": ".env"}, "path is protected"),
            ("delete_file", {"path": "../outside.txt"}, "path is outside workspace"),
            ("delete_file", {"path": "."}, "not a file"),
        )
        for name, arguments, error in cases:
            with self.subTest(name=name, path=arguments["path"]):
                result = await self.execute_call(json.dumps(arguments), name=name)
                self.assertEqual(result, {"success": False, "error": error, "path": arguments["path"]})
        self.assertEqual(protected.read_text("utf-8"), "example\n")
        self.assertEqual(outside.read_text("utf-8"), "example\n")

    async def test_inspect_project_only_reports_top_level_marker_files(self):
        project = self.workspace_root / "project"
        (project / "nested").mkdir(parents=True)
        (project / "nested/package.json").touch()
        (project / "go.mod").mkdir()
        (project / "pyproject.toml").touch()
        (project / "Cargo.toml").touch()

        result = await self.execute_call(json.dumps({"path": "project"}), name="inspect_project")

        self.assertEqual(result, {
            "success": True,
            "path": "project",
            "project_types": ["python", "rust"],
            "markers": {"python": ["pyproject.toml"], "rust": ["Cargo.toml"]},
        })
