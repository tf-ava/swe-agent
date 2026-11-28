"""用真实的 rg、Git 和本机进程验证新版工具。"""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from swe_agent.tools_new.catalog import BUILTIN_TOOLS


class ProcessToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_root = Path(temporary.name)
        self.workspace = self.temp_root / "工作 目录"
        self.workspace.mkdir()

    async def execute_call(self, name, arguments):
        call_id = "call_process"
        item = await BUILTIN_TOOLS[name].execute(self.workspace, call_id, json.dumps(arguments))
        self.assertEqual(set(item), {"type", "call_id", "output"})
        self.assertEqual(item["type"], "function_call_output")
        self.assertEqual(item["call_id"], call_id)
        return json.loads(item["output"])

    async def init_repository(self, directory):
        await asyncio.to_thread(
            subprocess.run,
            ["git", "init", "--quiet", "--template="],
            cwd=directory,
            check=True,
            env=os.environ.copy(),
        )

    def test_process_tools_publish_strict_schemas_and_effects(self):
        cases = {
            "search_text": ({"query", "path", "max_results"}, "read", "low"),
            "git_status": ({"path"}, "read", "low"),
            "git_diff": ({"path"}, "read", "low"),
            "run_command": ({"command", "cwd", "timeout"}, "command", "high"),
        }
        for name, (required, effect, risk_level) in cases.items():
            with self.subTest(name=name):
                spec = BUILTIN_TOOLS[name]
                schema = spec.schema
                self.assertEqual(schema["type"], "function")
                self.assertEqual(schema["name"], name)
                self.assertIs(schema["strict"], True)
                self.assertCountEqual(schema["parameters"]["required"], required)
                self.assertIs(schema["parameters"]["additionalProperties"], False)
                self.assertEqual((spec.effect, spec.risk_level), (effect, risk_level))

    @unittest.skipUnless(shutil.which("rg"), "本机未安装 rg")
    async def test_search_text_handles_absolute_paths_spaces_and_chinese(self):
        path = Path("源码 目录/示例.txt")
        file_path = self.workspace / path
        file_path.parent.mkdir()
        file_path.write_text("开头\n-关键字: 一\n-关键字: 二\n", encoding="utf-8")

        result = await self.execute_call(
            "search_text", {"query": "-关键字", "path": str(file_path), "max_results": 1}
        )

        self.assertEqual(result, {
            "success": True,
            "query": "-关键字",
            "matches": [{"file": str(path), "line": 2, "content": "-关键字: 一"}],
            "count": 1,
        })

    @unittest.skipUnless(shutil.which("rg"), "本机未安装 rg")
    async def test_search_text_skips_ignored_and_protected_paths(self):
        for path in ("src/normal.txt", "src/build/generated.txt", "dist/data.txt", ".git/config", ".env"):
            file_path = self.workspace / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text("needle\n", encoding="utf-8")

        arguments = {"query": "needle", "path": None, "max_results": None}
        result = await self.execute_call("search_text", arguments)
        self.assertTrue(result["success"])
        self.assertEqual([match["file"] for match in result["matches"]], [str(Path("src/normal.txt"))])

        result = await self.execute_call("search_text", arguments | {"query": "not_found"})
        self.assertEqual(result, {"success": True, "query": "not_found", "matches": [], "count": 0})

        result = await self.execute_call("search_text", arguments | {"path": ".env"})
        self.assertEqual(result, {"success": False, "error": "path is protected", "path": ".env"})

    @unittest.skipUnless(shutil.which("git"), "本机未安装 Git")
    async def test_git_status_and_diff_use_the_selected_repository(self):
        path = "项目 仓库"
        repo = self.workspace / path
        repo.mkdir()
        await self.init_repository(repo)
        (repo / "tracked.txt").write_bytes(b"old value\n")
        (repo / "staged.txt").write_bytes(b"staged only\n")
        await asyncio.to_thread(
            subprocess.run,
            ["git", "-c", "core.autocrlf=false", "add", "tracked.txt", "staged.txt"],
            cwd=repo,
            check=True,
            env=os.environ.copy(),
        )
        (repo / "tracked.txt").write_bytes(b"new value\n")
        (repo / "untracked.txt").touch()

        status = await self.execute_call("git_status", {"path": path})
        self.assertTrue(status["success"], status)
        self.assertEqual(status["path"], path)
        self.assertEqual(
            status["status"].splitlines(), ["A  staged.txt", "AM tracked.txt", "?? untracked.txt"]
        )

        diff = await self.execute_call("git_diff", {"path": path})
        self.assertTrue(diff["success"], diff)
        self.assertIn("-old value\n+new value", diff["diff"].replace("\r\n", "\n"))
        self.assertNotIn("staged only", diff["diff"])
        self.assertNotIn("untracked.txt", diff["diff"])

    @unittest.skipUnless(shutil.which("git"), "本机未安装 Git")
    async def test_git_queries_do_not_discover_a_repository_above_workspace(self):
        await self.init_repository(self.temp_root)

        result = await self.execute_call("git_status", {"path": None})

        self.assertFalse(result["success"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertTrue(result["stderr"])

    def python_command(self, *arguments):
        arguments = [sys.executable, *arguments]
        return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)

    async def test_run_command_uses_working_directory_and_returns_exit_code_and_output(self):
        cwd = "子 目录"
        directory = self.workspace / cwd
        directory.mkdir()
        (directory / "message.txt").write_text("from working directory", encoding="utf-8")
        (directory / "report.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "print(Path('message.txt').read_text(encoding='utf-8'))\n"
            "print('diagnostic', file=sys.stderr)\n"
            "raise SystemExit(int(sys.argv[1]))\n",
            encoding="utf-8",
        )

        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                arguments = {
                    "command": self.python_command("report.py", str(exit_code)),
                    "cwd": cwd,
                    "timeout": None,
                }
                result = await self.execute_call("run_command", arguments)
                self.assertEqual(result["success"], exit_code == 0)
                self.assertEqual(result["cwd"], cwd)
                self.assertEqual(result["exit_code"], exit_code)
                self.assertEqual(result["stdout"].strip(), "from working directory")
                self.assertEqual(result["stderr"].strip(), "diagnostic")

    async def test_run_command_refuses_working_directory_outside_workspace(self):
        arguments = {"command": "echo should_not_run", "cwd": "..", "timeout": None}
        result = await self.execute_call("run_command", arguments)
        self.assertEqual(result, {"success": False, "error": "path is outside workspace", "cwd": ".."})

    async def test_run_command_timeout_stops_child_process(self):
        (self.workspace / "parent.py").write_text(
            "import subprocess\n"
            "import sys\n"
            "subprocess.run([sys.executable, 'child.py'], check=True)\n",
            encoding="utf-8",
        )
        (self.workspace / "child.py").write_text(
            "import time\n"
            "from pathlib import Path\n"
            "Path('child_started').touch()\n"
            "time.sleep(2.5)\n"
            "Path('child_finished').touch()\n",
            encoding="utf-8",
        )

        result = await self.execute_call(
            "run_command", {"command": self.python_command("parent.py"), "cwd": None, "timeout": 2}
        )

        self.assertEqual(result, {"success": False, "error": "command timed out", "cwd": "."})
        self.assertTrue((self.workspace / "child_started").exists())
        await asyncio.sleep(2.7)
        self.assertFalse((self.workspace / "child_finished").exists())
