"""任务状态机（设计稿 §6.2、§18.2）。

设计稿引用原型的 ``can_transition_to()`` 存在但 ``set_state()`` 未内置调用它，
因此不能声称原型已强制所有合法迁移。本实现把校验放进唯一的写入口
:func:`assert_transition`，任何状态写入都必须先通过它。

只有枚举校验是不够的：合法"旧状态→新状态"与调用主体由领域服务结合
expected version / epoch / fence 一起裁决。
"""

from __future__ import annotations

from ..core.enums import ErrorCode, TaskState
from ..core.errors import DomainError, version_conflict

_COLLECTING = TaskState.COLLECTING
_SEARCHING = TaskState.SEARCHING
_PROPOSED = TaskState.PROPOSED
_WAITING_CONFIRMATION = TaskState.WAITING_CONFIRMATION
_COMMITTING = TaskState.COMMITTING
_SUCCEEDED = TaskState.SUCCEEDED
_NEEDS_REPLAN = TaskState.NEEDS_REPLAN
_WAITING_USER = TaskState.WAITING_USER
_WAITING_EXTERNAL = TaskState.WAITING_EXTERNAL
_WAITING_RESULT = TaskState.WAITING_RESULT
_FAILED = TaskState.FAILED
_CANCELLED = TaskState.CANCELLED
_HUMAN_TAKEOVER = TaskState.HUMAN_TAKEOVER

#: 允许的状态迁移。主链路见设计稿 §18.2；旁路（等待外部、人工接管）按 §6.2
#: 与 §11 追加，并在注释中标出依据。
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    _COLLECTING: frozenset(
        {_SEARCHING, _WAITING_USER, _WAITING_EXTERNAL, _CANCELLED, _HUMAN_TAKEOVER, _FAILED}
    ),
    _SEARCHING: frozenset(
        {
            _PROPOSED,
            _WAITING_USER,
            _COLLECTING,  # 用户补充/修改必要槽位
            _WAITING_EXTERNAL,
            _CANCELLED,
            _HUMAN_TAKEOVER,
            _FAILED,
        }
    ),
    _PROPOSED: frozenset(
        {
            _WAITING_CONFIRMATION,
            _NEEDS_REPLAN,
            _SEARCHING,
            _WAITING_EXTERNAL,
            _CANCELLED,
            _HUMAN_TAKEOVER,
            _FAILED,
        }
    ),
    _WAITING_CONFIRMATION: frozenset(
        {
            _COMMITTING,
            _NEEDS_REPLAN,
            _CANCELLED,
            _WAITING_EXTERNAL,
            _HUMAN_TAKEOVER,
            _FAILED,
        }
    ),
    _COMMITTING: frozenset(
        {_SUCCEEDED, _NEEDS_REPLAN, _WAITING_RESULT, _FAILED, _HUMAN_TAKEOVER}
    ),
    # WAITING_RESULT 是任务级等待查单状态，不表示订单失败。
    _WAITING_RESULT: frozenset({_SUCCEEDED, _FAILED, _HUMAN_TAKEOVER, _NEEDS_REPLAN}),
    _NEEDS_REPLAN: frozenset(
        {_SEARCHING, _COLLECTING, _WAITING_USER, _CANCELLED, _HUMAN_TAKEOVER, _FAILED}
    ),
    _WAITING_USER: frozenset(
        {_COLLECTING, _SEARCHING, _CANCELLED, _HUMAN_TAKEOVER, _FAILED, _WAITING_EXTERNAL}
    ),
    _WAITING_EXTERNAL: frozenset(
        {_COLLECTING, _SEARCHING, _NEEDS_REPLAN, _FAILED, _HUMAN_TAKEOVER, _CANCELLED}
    ),
    _HUMAN_TAKEOVER: frozenset({_COLLECTING, _SUCCEEDED, _FAILED, _CANCELLED}),
    # 终态不可再迁移；人工结案走独立命令，不在这里复活状态。
    _SUCCEEDED: frozenset(),
    _FAILED: frozenset(),
    _CANCELLED: frozenset(),
}


def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: TaskState, target: TaskState) -> None:
    """在唯一的写入口强制校验合法迁移。"""

    if not can_transition(current, target):
        raise version_conflict(
            f"非法任务状态迁移：{current.value} → {target.value}",
            from_state=current.value,
            to_state=target.value,
        )


def transition(
    *,
    current: str,
    target: TaskState,
    expected_version: int,
    actual_version: int,
    epoch: int | None = None,
    expected_epoch: int | None = None,
) -> int:
    """校验后返回新版本号。

    设计稿 §5.1：需要用户回答时结束该执行尝试；后续输入重建上下文开启新 reply。
    因此状态写入不做"续跑"，只做 CAS 校验 + 迁移。
    """

    if expected_version != actual_version:
        raise version_conflict(
            "任务已被更新，请基于最新状态重试",
            expected_version=expected_version,
            actual_version=actual_version,
        )
    if expected_epoch is not None and epoch is not None and expected_epoch != epoch:
        raise DomainError(ErrorCode.LEASE_LOST, "任务控制权代际已变化")
    assert_transition(TaskState(current), target)
    return actual_version + 1
