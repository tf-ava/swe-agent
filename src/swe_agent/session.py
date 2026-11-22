"""定义持久化的会话数据，不包含数据库操作或运行中的任务对象。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from swe_agent.config import SessionConfig


class SessionStatus(StrEnum):
    """会话最近一次执行的状态；空会话从 idle 开始。"""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class Session:
    """从数据库读取的会话快照；修改该对象不会自动写回数据库。"""

    session_id: str
    workspace_root: Path
    config: SessionConfig
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    summary: str | None
    summary_start_id: int | None
    summary_end_id: int | None
    token_usage: int
    pending_toolcalls: list[dict[str, Any]]


@dataclass(slots=True)
class StoredMessage:
    """历史消息及其存储信息；数据库 ID 不属于发给模型的消息内容。"""

    message_id: int
    session_id: str
    message: dict[str, Any]
    created_at: datetime
