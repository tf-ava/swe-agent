"""按名称提供 v0.2 内置工具。"""

from .command_tools import RUN_COMMAND
from .file_tools import DELETE_FILE, LIST_DIR, PATCH_CHECK, READ_FILE, SEARCH_TEXT, WRITE_FILE
from .git_tools import GIT_DIFF, GIT_STATUS
from .project_tools import INSPECT_PROJECT


BUILTIN_TOOLS = {
    spec.schema["name"]: spec
    for spec in (
        READ_FILE, WRITE_FILE, LIST_DIR, PATCH_CHECK, DELETE_FILE, INSPECT_PROJECT,
        SEARCH_TEXT, GIT_STATUS, GIT_DIFF, RUN_COMMAND,
    )
}
