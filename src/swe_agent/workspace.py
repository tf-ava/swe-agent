from __future__ import annotations

from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or Path.cwd()).expanduser().resolve()

        if not self.root.exists():
            raise ValueError(f"workspace does not exist: {self.root}")

        if not self.root.is_dir():
            raise ValueError(f"workspace is not a directory: {self.root}")

    def resolve_path(self, path: str | Path = ".") -> Path:
        raw_path = Path(path).expanduser()

        if raw_path.is_absolute():
            target = raw_path.resolve()
        else:
            target = (self.root / raw_path).resolve()

        if target != self.root and self.root not in target.parents:
            raise ValueError(f"path is outside workspace: {path}")

        return target

    def relative_path(self, path: str | Path) -> str:
        return str(self.resolve_path(path).relative_to(self.root))