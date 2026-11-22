"""新版本命令入口；仅处理命令参数、功能调用和终端输出。"""

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from swe_agent.bootstrap import initialize_app
from swe_agent.config import ConfigError, load_app_config, resolve_app_paths
from swe_agent.session_store import SessionStore, SessionStoreError


app = typer.Typer(
    name="swe-agent",
    help="SWE Agent 命令行工具（重构中）。",
    no_args_is_help=True,
)
session_app = typer.Typer(help="创建、配置和查询会话。", no_args_is_help=True)
app.add_typer(session_app, name="session")


@app.callback()
def root() -> None:
    """保留根命令层级，后续逐步注册 ask 和 chat 命令。"""


@app.command()
def init() -> None:
    """初始化数据目录、配置文件、密钥占位文件和数据库。"""
    try:
        paths = resolve_app_paths()
        initialize_app(paths)
    except (ConfigError, SessionStoreError, OSError) as exc:
        typer.echo(f"初始化失败：{exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("初始化完成，已有配置和会话数据已保留。")
    typer.echo(f"数据目录：{paths.data_dir}")
    typer.echo(f"配置文件：{paths.config_file}")
    typer.echo(f"密钥文件：{paths.env_file}")
    typer.echo(f"数据库：{paths.database_file}")
    typer.echo("请编辑 config.toml 中的模型和服务地址，并在 .env 中填写密钥。")
    typer.echo("可使用 session create 创建空会话；ask 和 chat 将在后续检查点接入。")


def _exit_session_error(exc: Exception) -> NoReturn:
    """统一展示会话操作错误；配置校验失败时只输出字段和原因。"""
    if isinstance(exc, ValidationError):
        message = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
    else:
        message = str(exc)
    typer.echo(f"会话操作失败：{message}", err=True)
    raise typer.Exit(code=1) from exc


@session_app.command("create")
def session_create(
    session_id: Annotated[str, typer.Argument(help="新会话的唯一 ID。")],
    path: Annotated[Path | None, typer.Option("--path", "-p", help="工作目录，默认当前目录。")] = None,
) -> None:
    """使用全局默认配置创建空会话，不请求模型。"""
    try:
        paths = resolve_app_paths()
        store = SessionStore(paths)
        defaults = load_app_config(paths).session_defaults
        session = store.create_session(session_id, path if path is not None else Path.cwd(), defaults)
    except (SessionStoreError, OSError, ValueError) as exc:
        _exit_session_error(exc)
    typer.echo(f"已创建会话：{session.session_id}")
    typer.echo(f"工作目录：{session.workspace_root}")


@session_app.command("config")
def session_config(
    session_id: Annotated[str, typer.Argument(help="已有会话的 ID。")],
    model: Annotated[str | None, typer.Option(help="主模型标识。")] = None,
    base_url: Annotated[str | None, typer.Option(help="模型服务的 HTTP(S) 地址。")] = None,
    api_key_env: Annotated[str | None, typer.Option(help="密钥环境变量的名称，不是密钥值。")] = None,
    compress_model: Annotated[str | None, typer.Option(help="上下文压缩模型标识。")] = None,
    context_token_limit: Annotated[int | None, typer.Option(min=1, help="上下文压缩阈值。")] = None,
    keep_recent_messages: Annotated[int | None, typer.Option(min=1, help="压缩时保留的近期消息数。")] = None,
    max_steps: Annotated[int | None, typer.Option(min=1, help="单次运行最大步数。")] = None,
) -> None:
    """修改明确指定的配置项；全部校验通过后才保存。"""
    options = {
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "compress_model": compress_model,
        "context_token_limit": context_token_limit,
        "keep_recent_messages": keep_recent_messages,
        "max_steps": max_steps,
    }
    # 只排除未传入的参数，让空字符串等无效输入正常进入配置校验。
    overrides = {name: value for name, value in options.items() if value is not None}
    if not overrides:
        raise typer.BadParameter("请至少指定一个配置项；查看配置请使用 session show。")
    try:
        store = SessionStore(resolve_app_paths())
        store.update_config(session_id, overrides)
    except (SessionStoreError, OSError, ValueError) as exc:
        _exit_session_error(exc)
    typer.echo(f"已更新会话配置：{session_id}")
    typer.echo("修改项：" + ", ".join(overrides))


@session_app.command("show")
def session_show(
    session_id: Annotated[str, typer.Argument(help="已有会话的 ID。")],
) -> None:
    """展示会话属性和持久化配置，不加载对话历史。"""
    try:
        session = SessionStore(resolve_app_paths()).get_session(session_id)
    except (SessionStoreError, OSError, ValueError) as exc:
        _exit_session_error(exc)
    data = {
        "session_id": session.session_id,
        "workspace_root": str(session.workspace_root),
        "status": session.status.value,
        "created_at": session.created_at.astimezone().isoformat(),
        "updated_at": session.updated_at.astimezone().isoformat(),
        "token_usage": session.token_usage,
        "config": session.config.model_dump(),
    }
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


@session_app.command("list")
def session_list(
    limit: Annotated[int, typer.Option(min=1, help="最多显示的会话数量。")] = 20,
    offset: Annotated[int, typer.Option(min=0, help="跳过排序后的前多少条记录。")] = 0,
) -> None:
    """按最近更新时间倒序展示会话基本信息，不生成或读取对话总结。"""
    try:
        sessions = SessionStore(resolve_app_paths()).list_sessions(limit=limit, offset=offset)
    except (SessionStoreError, OSError, ValueError) as exc:
        _exit_session_error(exc)
    if not sessions:
        typer.echo("当前范围内没有会话。")
        return
    typer.echo("session_id\t状态\t工作目录\t最近更新时间")
    for session in sessions:
        updated_at = session.updated_at.astimezone().isoformat(timespec="seconds")
        typer.echo(f"{session.session_id}\t{session.status.value}\t{session.workspace_root}\t{updated_at}")


def main() -> None:
    """供安装后的 swe-agent 命令调用。"""
    app()


if __name__ == "__main__":
    main()
