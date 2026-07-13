from __future__ import annotations

import os

import inspect
import json
from typing import Any

import typer
from dotenv import load_dotenv
from litellm import acompletion

from swe_agent.sqlite_session_manager import SQLiteSessionManager
from swe_agent.tools import TOOLS, TOOL_SCHEMAS

from swe_agent.prompt import SYSTEM_PROMPT

load_dotenv()

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY"
)

BASE_URL = os.getenv(
    "BASE_URL"
)



RUNNING = "running"
FINISHED = "get_finish_result"
PENDING_TOOLCALL = "pending_toolcall"

CONFIRM_TOOLS = {"write_file", "run_command", "delete_file"}




async def ask(
    question: str,
    tools: list[dict[str, Any]] | None,
    session_id: str,
    sqlitesessionmanager: SQLiteSessionManager,
    tool_functions: dict[str, Any],
    *,
    model: str,
    max_steps: int = 8,
) -> dict[str, Any]:
    if not model:
        raise typer.BadParameter("model is required. Set MODEL in .env or pass --model.")
    tool_schemas = tools or TOOL_SCHEMAS
    system_message = {"role": "system", "content": SYSTEM_PROMPT}

    session = sqlitesessionmanager.load_or_create(
        session_id,
        system_message,
        status=RUNNING,
    )
    session_id = session.session_id
    final_notice: str | None = None

    if session.status == PENDING_TOOLCALL:
        session = await _handle_pending(
            question=question,
            tool_functions=tool_functions,
            session=session,
            sqlitesessionmanager=sqlitesessionmanager,
            system_message=system_message,
        )
    else:
        user_message = {"role": "user", "content": question}
        sqlitesessionmanager.append_message(session_id, user_message)
        sqlitesessionmanager.update_session(session_id, status=RUNNING)

        session = sqlitesessionmanager.load_or_create(
            session_id,
            system_message,
        )

    for _ in range(max_steps):
        session = await _compress_if_needed(
            session=session,
            sqlitesessionmanager=sqlitesessionmanager,
        )

        if session.context_too_long:
            final_notice = session.notice or "当前对话过长，请切换新的 session。"
            break

        session = await step(
            model=model,
            messages=session.messages,
            tool_schemas=tool_schemas,
            tool_functions=tool_functions,
            session_id=session_id,
            sqlitesessionmanager=sqlitesessionmanager,
        )

        if session.context_too_long:
            final_notice = session.notice or "当前对话过长，请切换新的 session。"
            break

        if session.status in {FINISHED, PENDING_TOOLCALL}:
            break
    else:
        sqlitesessionmanager.update_session(session_id, status=FINISHED)
        session = sqlitesessionmanager.load_or_create(
            session_id,
            system_message,
        )
        final_notice = "已达到最大执行步数，任务暂停。"

    return _result(session, final_notice)


async def step(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    tool_functions: dict[str, Any],
    session_id: str,
    sqlitesessionmanager: SQLiteSessionManager,
):
    assistant_message, token_usage = await generate(
        model=model,
        messages=messages,
        tools=tool_schemas,
    )

    sqlitesessionmanager.append_message(session_id, assistant_message)
    sqlitesessionmanager.update_session(
        session_id,
        model_token_usage=token_usage,
    )

    tool_calls = assistant_message.get("tool_calls") or []

    if not tool_calls:
        sqlitesessionmanager.update_session(session_id, status=FINISHED)
        return sqlitesessionmanager.load_or_create(
            session_id,
            {"role": "system", "content": SYSTEM_PROMPT},
        )

    normal_tool_calls, confirm_tool_calls = _split_tool_calls(tool_calls)

    if normal_tool_calls:
        await act(
            tool_calls=normal_tool_calls,
            tool_functions=tool_functions,
            session_id=session_id,
            sqlitesessionmanager=sqlitesessionmanager,
        )

    if confirm_tool_calls:
        sqlitesessionmanager.set_pending(session_id, confirm_tool_calls)
        sqlitesessionmanager.update_session(session_id, status=PENDING_TOOLCALL)

        return sqlitesessionmanager.load_or_create(
            session_id,
            {"role": "system", "content": SYSTEM_PROMPT},
        )

    return sqlitesessionmanager.load_or_create(
        session_id,
        {"role": "system", "content": SYSTEM_PROMPT},
    )


async def generate(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    response = await acompletion(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        base_url=BASE_URL,
        api_key=OPENROUTER_API_KEY
    )

    message = response.choices[0].message
    assistant_message = message.model_dump(exclude_none=True)

    usage = getattr(response, "usage", None)
    token_usage = int(getattr(usage, "total_tokens", 0) or 0)

    return assistant_message, token_usage


async def act(
    *,
    tool_calls: list[dict[str, Any]],
    tool_functions: dict[str, Any],
    session_id: str,
    sqlitesessionmanager: SQLiteSessionManager,
) -> None:
    for tool_call in tool_calls:
        result_message = await _run_tool_call(
            tool_call,
            tool_functions,
        )
        sqlitesessionmanager.append_message(session_id, result_message)


async def _handle_pending(
    *,
    question: str,
    tool_functions: dict[str, Any],
    session,
    sqlitesessionmanager: SQLiteSessionManager,
    system_message: dict[str, Any],
):
    session_id = session.session_id
    pending = session.pending_toolcalls

    allowed = question.strip().lower() in {
        "y",
        "yes",
        "确认",
        "同意",
        "执行",
    }

    sqlitesessionmanager.delete_pending(session_id)
    sqlitesessionmanager.update_session(session_id, status=RUNNING)

    if allowed:
        await act(
            tool_calls=pending,
            tool_functions=tool_functions,
            session_id=session_id,
            sqlitesessionmanager=sqlitesessionmanager,
        )
    else:
        for tool_call in pending:
            rejected = {
                "success": False,
                "error": "user rejected this pending tool call",
                "user_input": question,
            }
            sqlitesessionmanager.append_message(
                session_id,
                _tool_message(tool_call, rejected),
            )

    user_message = {
        "role": "user",
        "content": question,
    }
    sqlitesessionmanager.append_message(session_id, user_message)

    return sqlitesessionmanager.load_or_create(
        session_id,
        system_message,
    )


async def _run_tool_call(
    tool_call: dict[str, Any],
    tool_functions: dict[str, Any],
) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    name = function.get("name")
    raw_args = function.get("arguments") or "{}"

    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        result = {"success": False, "error": f"invalid tool arguments: {exc}"}
        return _tool_message(tool_call, result)

    tool = tool_functions.get(name)

    if tool is None:
        result = {"success": False, "error": f"unknown tool: {name}"}
        return _tool_message(tool_call, result)

    try:
        value = tool(**args)
        if inspect.isawaitable(value):
            value = await value
        result = value
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    return _tool_message(tool_call, result)


def _tool_message(
    tool_call: dict[str, Any],
    result: Any,
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", ""),
        "content": json.dumps(result, ensure_ascii=False),
    }


def _split_tool_calls(
    tool_calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    for index, tool_call in enumerate(tool_calls):
        if _needs_confirm(tool_call):
            return tool_calls[:index], tool_calls[index:]

    return tool_calls, []


def _needs_confirm(tool_call: dict[str, Any]) -> bool:
    name = (tool_call.get("function") or {}).get("name")
    return name in CONFIRM_TOOLS


def _last_assistant_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""


async def _compress_if_needed(
    *,
    session,
    sqlitesessionmanager: SQLiteSessionManager,
):
    if (
        sqlitesessionmanager.context_token_count(session.messages)
        <= sqlitesessionmanager.context_token_limit
    ):
        return session

    return await sqlitesessionmanager.compress_session(session.session_id)


def _result(session, content: str | None = None) -> dict[str, Any]:
    if content is None and session.status == PENDING_TOOLCALL:
        content = "有工具调用需要确认。确认执行请输入：确认 / y / yes；拒绝则输入其他内容。"

    return {
        "session_id": session.session_id,
        "status": session.status,
        "content": content if content is not None else _last_assistant_content(session.messages),
        "token_usage": session.token_usage,
        "messages": session.messages,
        "pending_toolcalls": session.pending_toolcalls,
        "context_too_long": session.context_too_long,
        "notice": session.notice,
    }