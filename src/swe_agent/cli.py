from __future__ import annotations

import os
from dotenv import load_dotenv

import asyncio
from pathlib import Path
from typing import Optional, Any, Callable

import typer

from swe_agent.llm import ask as agent_ask
from swe_agent.prompt import SYSTEM_PROMPT
from swe_agent.sqlite_session_manager import SQLiteSessionManager

from swe_agent.tools import (
    git_diff,
    git_status,
    inspect_project,
    list_dir,
    patch_check,
    read_file,
    run_command,
    search_text,
    write_file,
    delete_file,
)
from swe_agent.workspace import Workspace


load_dotenv()

MODEL = os.getenv(
    "MODEL"
)

CONTEXT_TOKEN_LIMIT = int(os.getenv(
    "CONTEXT_TOKEN_LIMIT",
    "120000"
))

KEEP_RECENT_MESSAGES = int(os.getenv(
    "KEEP_RECENT_MESSAGES",
    "12"
))


app = typer.Typer(
    name="swe-agent",
    help="A minimal SWE Agent CLI.",
)




def resolve_workspace(path: Optional[str]) -> Path:
    workspace = Path(path).expanduser() if path else Path.cwd()
    workspace = workspace.resolve()

    if not workspace.exists():
        raise typer.BadParameter(f"workspace does not exist: {workspace}")

    if not workspace.is_dir():
        raise typer.BadParameter(f"workspace is not a directory: {workspace}")

    return workspace


def create_session_manager(
    workspace: Path,
) -> SQLiteSessionManager:
    return SQLiteSessionManager(
        workspace,
        context_token_limit=CONTEXT_TOKEN_LIMIT,
        keep_recent_messages=KEEP_RECENT_MESSAGES,
    )


def bind_workspace_tools(
        workspace: Workspace
        ) -> dict[str, Callable[..., Any]]:
    return {
        "list_dir": lambda **kwargs: list_dir(workspace, **kwargs),
        "read_file": lambda **kwargs: read_file(workspace, **kwargs),
        "search_text": lambda **kwargs: search_text(workspace, **kwargs),
        "patch_check": lambda **kwargs: patch_check(workspace, **kwargs),
        "write_file": lambda **kwargs: write_file(workspace, **kwargs),
        "delete_file": lambda **kwargs: delete_file(workspace, **kwargs),
        "inspect_project": lambda **kwargs: inspect_project(workspace, **kwargs),
        "run_command": lambda **kwargs: run_command(workspace, **kwargs),
        "git_status": lambda **kwargs: git_status(workspace, **kwargs),
        "git_diff": lambda **kwargs: git_diff(workspace, **kwargs),
    }


@app.command()
def ask(
    question: str = typer.Argument(
        ...,
        help="The coding question or task for the agent.",
    ),
    session_id: str = typer.Option(
        ...,
        "--session-id",
        "-s",
        help="Session id. Required.",
    ),
    path: Optional[str] = typer.Option(
        None,
        "--path",
        "-p",
        help="Workspace path. Defaults to current working directory.",
    ),
    model: str = typer.Option(
        MODEL,
        "--model",
        "-m",
        help="Model name passed to LiteLLM.",
    ),
    max_steps: int = typer.Option(
        15,
        "--max-steps",
        help="Maximum agent loop steps for this ask.",
    ),
):
    workspace_path = resolve_workspace(path)
    workspace = Workspace(workspace_path)
    manager = create_session_manager(workspace_path)
    tool_functions = bind_workspace_tools(workspace)

    result = asyncio.run(
        agent_ask(
            question=question,
            tools=None,
            session_id=session_id,
            sqlitesessionmanager=manager,
            tool_functions=tool_functions,
            model=model,
            max_steps=max_steps,
        )
    )

    typer.echo(result.get("content") or "")

    if result.get("status"):
        typer.echo(f"\nstatus: {result['status']}")

    if result.get("token_usage") is not None:
        typer.echo(f"token_usage: {result['token_usage']}")

    if result.get("pending_toolcalls"):
        typer.echo("\npending_toolcalls:")
        for index, tool_call in enumerate(result["pending_toolcalls"], start=1):
            function = tool_call.get("function") or {}
            typer.echo(f"{index}. {function.get('name', '<unknown>')}")
            typer.echo(f"   arguments: {function.get('arguments', '{}')}")


@app.command()
def trace(
    session_id: str = typer.Option(
        ...,
        "--session-id",
        "-s",
        help="Session id. Required.",
    ),
    path: Optional[str] = typer.Option(
        None,
        "--path",
        "-p",
        help="Workspace path. Defaults to current working directory.",
    ),
):
    workspace_path = resolve_workspace(path)
    manager = create_session_manager(workspace_path)

    session = manager.load_or_create(
        session_id,
        {"role": "system", "content": SYSTEM_PROMPT},
    )

    typer.echo(f"session_id: {session.session_id}")
    typer.echo(f"status: {session.status}")
    typer.echo(f"token_usage: {session.token_usage}")
    typer.echo(f"context_too_long: {session.context_too_long}")

    if session.notice:
        typer.echo(f"notice: {session.notice}")

    if session.pending_toolcalls:
        typer.echo("\npending_toolcalls:")
        for index, tool_call in enumerate(session.pending_toolcalls, start=1):
            function = tool_call.get("function") or {}
            typer.echo(f"{index}. {function.get('name', '<unknown>')}")
            typer.echo(f"   arguments: {function.get('arguments', '{}')}")

    typer.echo("\nmessages:")
    for index, message in enumerate(session.messages, start=1):
        role = message.get("role", "<unknown>")
        content = message.get("content")

        typer.echo(f"\n[{index}] {role}")

        if isinstance(content, str):
            typer.echo(content)
        else:
            typer.echo(repr(content))

        tool_calls = message.get("tool_calls")
        if tool_calls:
            typer.echo("tool_calls:")
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                typer.echo(f"- {function.get('name', '<unknown>')}")
                typer.echo(f"  arguments: {function.get('arguments', '{}')}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()