"""校验工具调用参数，将结果包装成 Responses API 输入项。"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError


def format_tool_output(call_id: str, result: dict[str, Any]) -> dict[str, str]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(result, ensure_ascii=False),
    }


async def execute_tool(
    argument_model: type[BaseModel],
    handler: Callable[..., Awaitable[dict[str, Any]]],
    workspace_root: Path,
    call_id: str,
    arguments: str,
) -> dict[str, str]:
    try:
        validated = argument_model.model_validate_json(arguments)
    except ValidationError as exc:
        result = {
            "success": False,
            "error": "invalid tool arguments",
            "details": exc.errors(include_input=False, include_context=False, include_url=False),
        }
    else:
        result = await handler(workspace_root, **validated.model_dump())

    return format_tool_output(call_id, result)
