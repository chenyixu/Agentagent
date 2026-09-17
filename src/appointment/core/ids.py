"""标识生成。

设计稿 §6.1：主键使用 UUID 等成熟生成方案，不使用单纯毫秒时间戳生成预约 ID。
"""

from __future__ import annotations

import uuid

from ..core.enums import TaskEventType


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def new_id_str() -> str:
    return str(uuid.uuid4())


def deterministic_id(*parts: str) -> uuid.UUID:
    """由业务键派生稳定 UUID（仅用于幂等场景，如一个逻辑投递的稳定键）。"""

    return uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts))


def idempotency_key(
    *, tenant_id: str, task_id: str, proposal_version: int, action: str
) -> str:
    """一次用户确认对应的稳定业务幂等键。

    设计稿 §8.2：业务幂等键由应用为一次用户确认分配，例如
    ``tenant + task + proposal_version + action``；客户端重试、模型修复、
    执行器接管必须复用同一键，不能每次生成新 UUID。
    """

    return f"{tenant_id}:{task_id}:{proposal_version}:{action}"


def sse_sequence_key(event_type: TaskEventType) -> str:  # pragma: no cover - 语义占位
    """保留位：业务事件与 SDK 事件共用 task_event 序号空间。"""

    return event_type.value
