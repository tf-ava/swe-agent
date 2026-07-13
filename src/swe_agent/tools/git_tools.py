from __future__ import annotations

import subprocess

from swe_agent.workspace import Workspace


async def git_status(
    workspace: Workspace,
    path: str = ".",
):
    root = workspace.resolve_path(path)

    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=root,
        capture_output=True,
        text=True,
    )

    return {
        "success": result.returncode == 0,
        "path": str(root.relative_to(workspace.root)),
        "status": result.stdout,
        "error": result.stderr,
    }


async def git_diff(
    workspace: Workspace,
    path: str = ".",
):
    root = workspace.resolve_path(path)

    result = subprocess.run(
        ["git", "diff"],
        cwd=root,
        capture_output=True,
        text=True,
    )

    return {
        "success": result.returncode == 0,
        "path": str(root.relative_to(workspace.root)),
        "diff": result.stdout,
        "error": result.stderr,
    }