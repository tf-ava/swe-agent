"""内置工具的规格和 Responses API 函数定义。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class ToolSpec:
    schema: dict[str, Any]
    execute: Callable[[Path, str, str], Awaitable[dict[str, str]]]
    effect: Literal["read", "write", "command"]
    risk_level: Literal["low", "medium", "high"]


def function_tool_schema(
    name: str, description: str, argument_model: type[BaseModel]
) -> dict[str, Any]:
    """用按严格模式声明的参数模型组装完整工具定义。"""
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": argument_model.model_json_schema(),
    }
