"""在工作目录中的 Git 仓库执行查询。"""

from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .execution import execute_tool
from .paths import resolve_workspace_path
from .process import run_process
from .spec import ToolSpec, function_tool_schema


class GitPathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str | None = Field(description="工作目录内的仓库根路径。传 null 表示会话工作目录。")


async def _run_git(
    workspace_root: Path, path: str | None, arguments: tuple[str, ...], output_key: str
) -> dict[str, Any]:
    path = "." if path is None else path
    try:
        directory, relative_path = resolve_workspace_path(workspace_root, path)
        if not directory.is_dir():
            return {"success": False, "error": "not a directory", "path": path}

        result = await run_process(
            (
                "git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
                "--git-dir=.git", "--work-tree=.", *arguments,
            ),
            cwd=directory,
            timeout=30,
        )
    except TimeoutError:
        return {"success": False, "error": "git command timed out", "path": path}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    return {
        "success": result.returncode == 0,
        "path": str(relative_path),
        output_key: result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


async def git_status(workspace_root: Path, path: str | None) -> dict[str, Any]:
    return await _run_git(workspace_root, path, ("status", "--short"), "status")


async def git_diff(workspace_root: Path, path: str | None) -> dict[str, Any]:
    return await _run_git(workspace_root, path, ("diff", "--no-ext-diff", "--no-textconv"), "diff")


GIT_STATUS = ToolSpec(
    schema=function_tool_schema(
        "git_status",
        "查看指定 Git 仓库的简短状态，包括已暂存、未暂存和未跟踪的文件。",
        GitPathArgs,
    ),
    execute=partial(execute_tool, GitPathArgs, git_status),
    effect="read",
    risk_level="low",
)


GIT_DIFF = ToolSpec(
    schema=function_tool_schema(
        "git_diff",
        "查看指定 Git 仓库已跟踪文件的未暂存差异。",
        GitPathArgs,
    ),
    execute=partial(execute_tool, GitPathArgs, git_diff),
    effect="read",
    risk_level="low",
)
