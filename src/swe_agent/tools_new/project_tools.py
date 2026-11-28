"""用工作目录中的标志文件识别项目类型。"""

from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .execution import execute_tool
from .paths import is_path_link, resolve_workspace_path
from .spec import ToolSpec, function_tool_schema


PROJECT_MARKERS = {
    "python": ("pyproject.toml", "requirements.txt", "setup.py"),
    "node": ("package.json",),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "java": ("pom.xml", "build.gradle"),
}


class InspectProjectArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str | None = Field(description="要探查的目录路径。传 null 表示会话工作目录。")


async def inspect_project(workspace_root: Path, path: str | None) -> dict[str, Any]:
    path = "." if path is None else path
    try:
        directory, relative_path = resolve_workspace_path(workspace_root, path)
        if not directory.is_dir():
            error = "not a directory" if directory.exists() else "directory not found"
            return {"success": False, "error": error, "path": path}

        markers = {}
        for project_type, candidates in PROJECT_MARKERS.items():
            found = []
            for name in candidates:
                marker = directory / name
                if not is_path_link(marker) and marker.is_file():
                    found.append(name)
            if found:
                markers[project_type] = found
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc), "path": path}

    return {
        "success": True,
        "path": str(relative_path),
        "project_types": sorted(markers),
        "markers": markers,
    }


INSPECT_PROJECT = ToolSpec(
    schema=function_tool_schema(
        "inspect_project",
        "检查指定目录顶层的标志文件，返回识别的项目类型及对应标志文件。",
        InspectProjectArgs,
    ),
    execute=partial(execute_tool, InspectProjectArgs, inspect_project),
    effect="read",
    risk_level="low",
)
