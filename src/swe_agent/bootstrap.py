"""组织应用初始化；配置解析和数据库操作分别由各自模块负责。"""

from importlib.resources import files
from pathlib import Path

from swe_agent.config import AppPaths, load_app_config
from swe_agent.session_store import initialize_database


def _create_text_file_if_missing(path: Path, content: str) -> None:
    """只创建缺失文件；排他创建防止重复执行时覆盖用户内容。"""
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
    except FileExistsError:
        if not path.is_file():
            raise OSError(f"Expected a file: {path}")


def initialize_app(paths: AppPaths) -> None:
    """显式创建或检查应用资源，不加载 .env，不校验密钥或请求模型。

    初始化按顺序执行，不承诺多个文件之间的整体回滚。
    失败后保留已完成的资源，修正问题后可再次运行。
    """
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    if not paths.config_file.exists():
        template = files("swe_agent").joinpath("config.example.toml").read_text(encoding="utf-8")
        _create_text_file_if_missing(paths.config_file, template)
    elif not paths.config_file.is_file():
        raise OSError(f"Expected a file: {paths.config_file}")
    config = load_app_config(paths)

    # 先校验配置，再初始化数据库；不兼容的数据库交由存储模块拒绝。
    initialize_database(paths)

    # 使用配置中的密钥变量名；已有 .env 原样保留，不读取或显示其内容。
    if not paths.env_file.exists():
        _create_text_file_if_missing(
            paths.env_file,
            "# 请填写模型服务密钥；密钥变量名应与 config.toml 中的 api_key_env 一致。\n"
            f"{config.session_defaults.api_key_env}=\n",
        )
    elif not paths.env_file.is_file():
        raise OSError(f"Expected a file: {paths.env_file}")
