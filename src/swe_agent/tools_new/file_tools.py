"""在会话工作目录内执行文件操作。"""

import difflib
import json
import os
import stat
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .execution import execute_tool
from .paths import SENSITIVE_FILES, is_path_link, is_protected_path, resolve_workspace_path
from .process import run_process
from .spec import ToolSpec, function_tool_schema


IGNORED_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}


class ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(description="工作目录内的文件路径，建议使用相对路径，例如 src/main.py。")
    start_line: int | None = Field(description="从第几行开始，传 null 时从首行开始。")
    end_line: int | None = Field(description="读到第几行（含），传 null 时读到文件末尾。")

    @model_validator(mode="after")
    def validate_read_arguments(self) -> Self:
        if not self.path:
            raise ValueError("path must not be empty")
        if any(line is not None and line < 1 for line in (self.start_line, self.end_line)):
            raise ValueError("line numbers must be positive")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.start_line > self.end_line
        ):
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


async def read_file(
    workspace_root: Path, path: str, start_line: int | None, end_line: int | None
) -> dict[str, Any]:
    try:
        file_path, relative_path = resolve_workspace_path(workspace_root, path)
        if not file_path.is_file():
            error = "not a file" if file_path.exists() else "file not found"
            return {"success": False, "error": error, "path": path}

        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"success": False, "error": "file is not valid UTF-8", "path": path}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    lines = content.splitlines()
    if start_line is not None or end_line is not None:
        content = "\n".join(lines[(start_line or 1) - 1 : end_line])

    return {
        "success": True,
        "path": str(relative_path),
        "content": content,
        "total_lines": len(lines),
    }


READ_FILE = ToolSpec(
    schema=function_tool_schema(
        "read_file",
        "读取工作目录内的 UTF-8 文本文件。行号从 1 开始，包含首尾行；"
        "两个行号都传 null 时读取全文。返回相对路径、文本和文件总行数。",
        ReadFileArgs,
    ),
    execute=partial(execute_tool, ReadFileArgs, read_file),
    effect="read",
    risk_level="low",
)


class WriteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(description="工作目录内的目标文件路径，例如 src/main.py。")
    content: str = Field(description="文件的完整新内容。传空字符串可清空文件。")

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        if not path:
            raise ValueError("path must not be empty")
        return path


async def write_file(workspace_root: Path, path: str, content: str) -> dict[str, Any]:
    try:
        file_path, relative_path = resolve_workspace_path(workspace_root, path)
        try:
            existing_mode = file_path.stat().st_mode
        except FileNotFoundError:
            existing_mode = None
        else:
            if not stat.S_ISREG(existing_mode):
                return {"success": False, "error": "not a file", "path": path}

        file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=file_path.parent, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)

            # 覆盖时保留已有文件的基本权限位，包括脚本的可执行位。
            if existing_mode is not None and os.name == "posix":
                temporary_path.chmod(existing_mode & 0o777)
            temporary_path.replace(file_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    return {"success": True, "path": str(relative_path)}


WRITE_FILE = ToolSpec(
    schema=function_tool_schema(
        "write_file",
        "以 UTF-8 写入工作目录内的文件，按需创建父目录。已有文件会被完整覆盖。",
        WriteFileArgs,
    ),
    execute=partial(execute_tool, WriteFileArgs, write_file),
    effect="write",
    risk_level="medium",
)


class ListDirArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str | None = Field(description="工作目录内的目录路径。传 null 表示会话工作目录。")
    recursive: bool | None = Field(description="是否递归列出子目录。传 null 时只列出一层。")


async def list_dir(workspace_root: Path, path: str | None, recursive: bool | None) -> dict[str, Any]:
    path = "." if path is None else path
    try:
        directory, relative_path = resolve_workspace_path(workspace_root, path)
        if not directory.is_dir():
            error = "not a directory" if directory.exists() else "directory not found"
            return {"success": False, "error": error, "path": path}

        files = []
        dirs = []
        pending = [directory]
        while pending:
            for entry in pending.pop().iterdir():
                relative_entry = relative_path / entry.relative_to(directory)
                if is_protected_path(relative_entry) or is_path_link(entry):
                    continue
                if entry.is_dir():
                    if entry.name.casefold() in IGNORED_DIRS:
                        continue
                    dirs.append(str(relative_entry))
                    if recursive:
                        pending.append(entry)
                elif entry.is_file():
                    files.append(str(relative_entry))
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    return {"success": True, "path": str(relative_path), "files": sorted(files), "dirs": sorted(dirs)}


LIST_DIR = ToolSpec(
    schema=function_tool_schema(
        "list_dir",
        "列出工作目录下的文件和子目录，可递归。自动跳过受保护条目和常见的构建、依赖、缓存目录。",
        ListDirArgs,
    ),
    execute=partial(execute_tool, ListDirArgs, list_dir),
    effect="read",
    risk_level="low",
)


class PatchCheckArgs(WriteFileArgs):
    context_lines: int | None = Field(description="每处差异周围保留的行数。传 null 时使用 3 行。")

    @field_validator("context_lines")
    @classmethod
    def validate_context_lines(cls, context_lines: int | None) -> int | None:
        if context_lines is not None and context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        return context_lines


async def patch_check(
    workspace_root: Path, path: str, content: str, context_lines: int | None
) -> dict[str, Any]:
    try:
        file_path, relative_path = resolve_workspace_path(workspace_root, path)
        exists = file_path.exists()
        old_content = ""
        if exists:
            if not file_path.is_file():
                return {"success": False, "error": "not a file", "path": path}
            with file_path.open(encoding="utf-8", newline="") as file:
                old_content = file.read()
    except UnicodeDecodeError:
        return {"success": False, "error": "file is not valid UTF-8", "path": path}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    diff_path = relative_path.as_posix()
    diff_lines = difflib.unified_diff(
        StringIO(old_content).readlines(),
        StringIO(content).readlines(),
        fromfile=f"a/{diff_path}" if exists else "/dev/null",
        tofile=f"b/{diff_path}",
        n=3 if context_lines is None else context_lines,
    )
    diff = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in diff_lines
    )
    return {"success": True, "path": str(relative_path), "exists": exists, "diff": diff}


PATCH_CHECK = ToolSpec(
    schema=function_tool_schema(
        "patch_check",
        "预览向工作目录内的文件写入完整内容会产生的 unified diff，适用于新建和覆盖文件。",
        PatchCheckArgs,
    ),
    execute=partial(execute_tool, PatchCheckArgs, patch_check),
    effect="read",
    risk_level="low",
)


class DeleteFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(description="工作目录内要删除的单个普通文件的路径。")


async def delete_file(workspace_root: Path, path: str) -> dict[str, Any]:
    try:
        file_path, relative_path = resolve_workspace_path(workspace_root, path)
        if is_path_link(workspace_root / path):
            return {"success": False, "error": "path is a link", "path": path}
        if not file_path.is_file():
            error = "not a file" if file_path.exists() else "file not found"
            return {"success": False, "error": error, "path": path}
        file_path.unlink()
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    return {"success": True, "path": str(relative_path)}


DELETE_FILE = ToolSpec(
    schema=function_tool_schema(
        "delete_file",
        "删除工作目录内指定的一个普通文件。目录和链接无法通过此工具删除。",
        DeleteFileArgs,
    ),
    execute=partial(execute_tool, DeleteFileArgs, delete_file),
    effect="write",
    risk_level="high",
)


class SearchTextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(description="要搜索的正则表达式。")
    path: str | None = Field(description="要搜索的文件或目录。传 null 表示会话工作目录。")
    max_results: int | None = Field(description="最多返回的匹配行数。传 null 时返回最多 20 行。")

    @field_validator("query")
    @classmethod
    def validate_query(cls, query: str) -> str:
        if not query:
            raise ValueError("query must not be empty")
        return query

    @field_validator("max_results")
    @classmethod
    def validate_max_results(cls, max_results: int | None) -> int | None:
        if max_results is not None and max_results <= 0:
            raise ValueError("max_results must be positive")
        return max_results


async def search_text(
    workspace_root: Path, query: str, path: str | None, max_results: int | None
) -> dict[str, Any]:
    path = "." if path is None else path
    max_results = 20 if max_results is None else max_results
    try:
        target, relative_path = resolve_workspace_path(workspace_root, path)
        if not target.is_dir() and not target.is_file():
            return {"success": False, "error": "not a file or directory", "path": path}

        command = ["rg", "--no-config", "--json", "--max-count", str(max_results)]
        for name in sorted(IGNORED_DIRS | {".git"}):
            command.extend(("--iglob", f"!{name}/"))
        for name in sorted(SENSITIVE_FILES):
            command.extend(("--iglob", f"!{name}"))
        command.extend(("-e", query, "--", relative_path.as_posix()))

        result = await run_process(command, cwd=workspace_root, timeout=30)
        if result.returncode not in (0, 1):
            error = result.stderr.strip() or f"rg exited with code {result.returncode}"
            return {"success": False, "error": error, "path": path}

        matches = []
        for line in result.stdout.split("\n"):
            if not line:
                continue
            event = json.loads(line)
            if event["type"] != "match":
                continue
            data = event["data"]
            match_path = data["path"].get("text")
            content = data["lines"].get("text")
            if match_path is None or content is None:
                continue
            try:
                _, relative_match = resolve_workspace_path(workspace_root, match_path)
            except ValueError:
                continue

            matches.append({
                "file": str(relative_match),
                "line": data["line_number"],
                "content": content.rstrip("\r\n"),
            })
            if len(matches) == max_results:
                break
    except TimeoutError:
        return {"success": False, "error": "rg command timed out", "path": path}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    return {"success": True, "query": query, "matches": matches, "count": len(matches)}


SEARCH_TEXT = ToolSpec(
    schema=function_tool_schema(
        "search_text",
        "按正则表达式搜索工作目录内的文本，返回匹配文件、行号和行内容。",
        SearchTextArgs,
    ),
    execute=partial(execute_tool, SearchTextArgs, search_text),
    effect="read",
    risk_level="low",
)
