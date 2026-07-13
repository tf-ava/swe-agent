# tools/__init__.py


from .file_tools import (
    list_dir,
    patch_check,
    read_file,
    search_text,
    write_file,
    delete_file
)

from .project_tools import (
    inspect_project,
)

from .command_tools import (
    run_command,
)

from .git_tools import (
    git_status,
    git_diff,
)



TOOLS = {

    "list_dir": list_dir,

    "read_file": read_file,

    "search_text": search_text,

    "patch_check": patch_check,

    "write_file": write_file,

    "inspect_project": inspect_project,

    "run_command": run_command,

    "git_status": git_status,

    "git_diff": git_diff,

    "delete_file": delete_file

}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容，可递归。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件，可按行号范围读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "在项目中搜索文本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_check",
            "description": "覆盖写入文件前生成 unified diff，不修改文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "context_lines": {"type": "integer", "default": 3},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "覆盖写入文件。必须在用户确认 diff 后才能执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_project",
            "description": "探查项目类型和标志性文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "运行项目命令或测试命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "查看 git status --short。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "查看 git diff。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
            },
        },
    },
    {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "删除 workspace 内的一个文件。该工具必须在用户确认后才能执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    }
]