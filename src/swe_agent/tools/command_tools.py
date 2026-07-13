from __future__ import annotations

import asyncio
import shlex

from swe_agent.workspace import Workspace


BLOCK_COMMANDS = {
    "rm",
    "del",
    "shutdown",
    "format",
    "mkfs",
    "rmdir",
}


def _is_blocked_command(command: str) -> bool:
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return True

    if not parts:
        return True

    return parts[0].lower() in BLOCK_COMMANDS


async def run_command(
    workspace: Workspace,
    command: str,
    cwd: str = ".",
    timeout: int = 120,
):
    if _is_blocked_command(command):
        return {
            "success": False,
            "command": command,
            "error": "blocked command",
        }

    workdir = workspace.resolve_path(cwd)

    if not workdir.is_dir():
        return {
            "success": False,
            "command": command,
            "cwd": cwd,
            "error": "cwd is not a directory",
        }

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )

        return {
            "success": True,
            "command": command,
            "cwd": str(workdir.relative_to(workspace.root)),
            "exit_code": process.returncode,
            "stdout": stdout.decode(errors="ignore"),
            "stderr": stderr.decode(errors="ignore"),
        }

    except asyncio.TimeoutError:
        return {
            "success": False,
            "command": command,
            "cwd": str(workdir.relative_to(workspace.root)),
            "error": "timeout",
        }