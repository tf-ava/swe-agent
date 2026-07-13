from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from swe_agent.workspace import Workspace


DEFAULT_IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}

SENSITIVE_FILES = {
    ".env",
    ".env.local",
    ".env.production",
}


def should_ignore(path: Path) -> bool:
    if path.name in SENSITIVE_FILES:
        return True

    for part in path.parts:
        if part in DEFAULT_IGNORE_DIRS:
            return True

    return False


async def list_dir(
    workspace: Workspace,
    path: str = ".",
    recursive: bool = False,
):
    root = workspace.resolve_path(path)

    if not root.exists():
        return {"success": False, "error": "directory not found", "path": path}

    if not root.is_dir():
        return {"success": False, "error": "not a directory", "path": path}

    files = []
    dirs = []

    iterator = root.rglob("*") if recursive else root.iterdir()

    for p in iterator:
        if should_ignore(p):
            continue

        rel = str(p.relative_to(workspace.root))

        if p.is_file():
            files.append(rel)
        elif p.is_dir():
            dirs.append(rel)

    return {
        "success": True,
        "path": str(root.relative_to(workspace.root)),
        "files": sorted(files),
        "dirs": sorted(dirs),
    }


async def read_file(
    workspace: Workspace,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
):
    file_path = workspace.resolve_path(path)

    if not file_path.exists():
        return {"success": False, "error": "file not found", "path": path}

    if not file_path.is_file():
        return {"success": False, "error": "not a file", "path": path}

    if should_ignore(file_path):
        return {"success": False, "error": "file is ignored", "path": path}

    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    if start_line is not None:
        start = max(start_line - 1, 0)
        end = end_line if end_line is not None else len(lines)
        content = "\n".join(lines[start:end])

    return {
        "success": True,
        "path": str(file_path.relative_to(workspace.root)),
        "content": content,
        "total_lines": len(lines),
    }


async def search_text(
    workspace: Workspace,
    query: str,
    path: str = ".",
    max_results: int = 20,
):
    root = workspace.resolve_path(path)

    if not root.exists():
        return {"success": False, "error": "path not found", "path": path}

    cmd = [
        "rg",
        query,
        str(root),
        "--line-number",
        "--no-heading",
        "--color",
        "never",
    ]

    result = subprocess.run(
        cmd,
        cwd=workspace.root,
        capture_output=True,
        text=True,
    )

    if result.returncode not in (0, 1):
        return {"success": False, "error": result.stderr}

    matches = []

    for line in result.stdout.splitlines():
        if len(matches) >= max_results:
            break

        parts = line.split(":", 2)
        if len(parts) != 3:
            continue

        file_path = Path(parts[0]).resolve()

        try:
            rel_path = str(file_path.relative_to(workspace.root))
        except ValueError:
            continue

        matches.append(
            {
                "file": rel_path,
                "line": int(parts[1]),
                "content": parts[2],
            }
        )

    return {
        "success": True,
        "query": query,
        "matches": matches,
        "count": len(matches),
    }


async def patch_check(
    workspace: Workspace,
    path: str,
    content: str,
    context_lines: int = 3,
):
    file_path = workspace.resolve_path(path)

    if file_path.exists() and not file_path.is_file():
        return {"success": False, "error": "not a file", "path": path}

    old_content = ""
    if file_path.exists():
        old_content = file_path.read_text(encoding="utf-8")

    rel_path = str(file_path.relative_to(workspace.root))

    diff = difflib.unified_diff(
        old_content.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile=f"{rel_path} (current)",
        tofile=f"{rel_path} (new)",
        n=context_lines,
    )

    return {
        "success": True,
        "path": rel_path,
        "exists": file_path.exists(),
        "diff": "".join(diff),
    }


async def write_file(
    workspace: Workspace,
    path: str,
    content: str,
):
    file_path = workspace.resolve_path(path)

    if should_ignore(file_path):
        return {"success": False, "error": "file is ignored", "path": path}

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    return {
        "success": True,
        "path": str(file_path.relative_to(workspace.root)),
    }


async def delete_file(
    workspace: Workspace,
    path: str,
):
    """
    删除文件。

    注意：
    - 路径必须在 workspace 内。
    - 调用前应由 agent 展示说明或 diff。
    - 该工具必须走用户确认流程。
    """
    file_path = workspace.resolve_path(path)

    if not file_path.exists():
        return {
            "path": path,
            "success": False,
            "error": "file not found",
        }

    if not file_path.is_file():
        return {
            "path": path,
            "success": False,
            "error": "not a file",
        }

    if should_ignore(file_path):
        return {
            "path": path,
            "success": False,
            "error": "file is ignored",
        }

    file_path.unlink()

    return {
        "path": str(file_path.relative_to(workspace.root)),
        "success": True,
    }