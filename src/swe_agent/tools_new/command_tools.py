"""在会话工作目录中执行项目命令。"""

from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import execute_tool
from .paths import resolve_workspace_path
from .process import run_process
from .spec import ToolSpec, function_tool_schema


class RunCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(description="交给本机默认 shell 执行的命令。")
    cwd: str | None = Field(description="工作目录内启动命令的位置。传 null 表示会话工作目录。")
    timeout: int | None = Field(description="超时秒数。传 null 时使用 120 秒。")

    @field_validator("command")
    @classmethod
    def validate_command(cls, command: str) -> str:
        if not command.strip():
            raise ValueError("command must not be empty")
        return command

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, timeout: int | None) -> int | None:
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        return timeout


async def run_command(
    workspace_root: Path, command: str, cwd: str | None, timeout: int | None
) -> dict[str, Any]:
    cwd = "." if cwd is None else cwd
    try:
        directory, relative_path = resolve_workspace_path(workspace_root, cwd)
        if not directory.is_dir():
            return {"success": False, "error": "cwd is not a directory", "cwd": cwd}

        result = await run_process(command, cwd=directory, timeout=120 if timeout is None else timeout)
    except TimeoutError:
        return {"success": False, "error": "command timed out", "cwd": cwd}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "cwd": cwd}

    return {
        "success": result.returncode == 0,
        "cwd": str(relative_path),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


RUN_COMMAND = ToolSpec(
    schema=function_tool_schema(
        "run_command",
        "在指定目录中运行项目命令或测试，返回退出码、标准输出和标准错误。",
        RunCommandArgs,
    ),
    execute=partial(execute_tool, RunCommandArgs, run_command),
    effect="command",
    risk_level="high",
)
