from __future__ import annotations

from swe_agent.workspace import Workspace


PROJECT_FILES = {
    "python": ["pyproject.toml", "requirements.txt", "setup.py"],
    "node": ["package.json"],
    "rust": ["Cargo.toml"],
    "go": ["go.mod"],
    "java": ["pom.xml", "build.gradle"],
}


async def inspect_project(
    workspace: Workspace,
    path: str = ".",
):
    root = workspace.resolve_path(path)

    if not root.exists():
        return {"success": False, "error": "path not found", "path": path}

    if not root.is_dir():
        return {"success": False, "error": "not a directory", "path": path}

    files = [item.name for item in root.iterdir()]
    detected = []

    for language, markers in PROJECT_FILES.items():
        for marker in markers:
            if marker in files:
                detected.append(language)
                break

    return {
        "success": True,
        "root": str(root.relative_to(workspace.root)),
        "project_types": sorted(set(detected)),
        "files": sorted(files),
    }