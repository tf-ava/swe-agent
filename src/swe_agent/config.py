"""定义配置结构和数据路径；导入模块或读取配置时不会创建文件。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Mapping
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)


class ConfigError(ValueError):
    """应用路径或配置文件无效。"""


@dataclass(frozen=True, slots=True)
class AppPaths:
    """应用共用的数据路径，与各个 session 的工作目录无关。"""

    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.toml"

    @property
    def env_file(self) -> Path:
        return self.data_dir / ".env"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "sessions.sqlite3"


def resolve_app_paths() -> AppPaths:
    """先从启动环境确定数据目录，再由调用方读取配置和 .env 文件。"""
    configured = os.environ.get("SWE_AGENT_HOME")
    if configured is None:
        data_dir = Path.home() / ".sweagent"
    else:
        if not configured.strip():
            raise ConfigError("SWE_AGENT_HOME must not be empty")
        data_dir = Path(configured).expanduser()
        if not data_dir.is_absolute():
            raise ConfigError("SWE_AGENT_HOME must be an absolute path")

    return AppPaths(data_dir=data_dir.resolve())


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInteger = Annotated[int, Field(gt=0)]


class SessionConfig(BaseModel):
    """完整的 session 配置；所有字段必填，不使用默认值兜底。

    session 持久化配置和单次运行的独立配置快照共用这一结构，
    创建后均不允许直接修改字段。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    model: NonEmptyString
    base_url: NonEmptyString
    api_key_env: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    compress_model: NonEmptyString
    context_token_limit: PositiveInteger
    keep_recent_messages: PositiveInteger
    max_steps: PositiveInteger

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            _ = parsed.port  # 读取该属性时会同时检查端口是否合法。
        except ValueError as exc:
            raise ValueError("base_url must be a valid HTTP(S) URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(char.isspace() for char in value)
        ):
            raise ValueError("base_url must be a valid HTTP(S) URL")
        return value


class AppConfig(BaseModel):
    """配置文件的内容；其中的默认配置只在创建 session 时复制使用。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    session_defaults: SessionConfig


def load_app_config(paths: AppPaths) -> AppConfig:
    """读取并校验 config.toml，不创建或修复文件和目录。

    MODEL、MAX_STEPS 等普通环境变量不参与配置合并。
    密钥读取将在模型执行接入本模块时单独实现。
    """
    try:
        content = paths.config_file.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration file not found: {paths.config_file}. "
            "Run 'swe-agent init' first."
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read configuration file: {paths.config_file}") from exc

    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {paths.config_file}: {exc}") from exc

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        # 只报告出错字段和原因，不回显可能包含敏感信息的输入值。
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
        raise ConfigError(f"Invalid configuration in {paths.config_file}: {details}") from exc


def apply_config_overrides(
    config: SessionConfig, overrides: Mapping[str, object]
) -> SessionConfig:
    """合并覆盖参数并重新校验，返回独立快照，不修改原配置。

    调用方只能传入用户显式指定的选项。None 不代表省略参数，
    对这些必填配置字段传入 None 会校验失败。
    """
    return SessionConfig.model_validate(config.model_dump() | dict(overrides))
