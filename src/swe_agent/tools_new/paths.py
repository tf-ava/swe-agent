"""检查工具访问的路径是否在会话工作目录内。"""

from pathlib import Path


SENSITIVE_FILES = {".env", ".env.local", ".env.production"}


def is_protected_path(relative_path: Path) -> bool:
    return relative_path.name.casefold() in SENSITIVE_FILES or any(
        part.casefold() == ".git" for part in relative_path.parts
    )


def resolve_workspace_path(workspace_root: Path, path: str) -> tuple[Path, Path]:
    """返回解析后的绝对路径和相对于工作目录的路径。"""
    if not path:
        raise ValueError("path must not be empty")

    root = workspace_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path is outside workspace")

    relative_path = target.relative_to(root)
    if is_protected_path(relative_path):
        raise ValueError("path is protected")

    return target, relative_path


def is_path_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()
