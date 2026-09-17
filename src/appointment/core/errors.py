"""领域错误与统一结果契约。

设计稿 §2.2 定义的工具结果字段：
    status、data、error_code、retryable、operation_id、observed_at、
    schema_version、request_id

关键语义区分：
- ``ERROR`` 是确定的失败，可按策略重试；
- ``UNKNOWN`` 是效果未查清，必须先查原操作，不能换键重发；
- 写操作的重试永远复用原幂等键。
"""

from __future__ import annotations

from typing import Any, Mapping

from .enums import ErrorCode, ToolStatus

SCHEMA_VERSION = 1


class DomainError(Exception):
    """领域服务抛出的受控错误。

    Attributes:
        code: 统一错误码。
        message: 面向开发者的说明，不直接展示给用户。
        retryable: 是否允许按指定策略重试。写操作永远复用原幂等键。
        operation_id: 已登记操作时携带，便于提交结果不明时查单。
        details: 结构化补充信息，不得包含其他客户的占用细节。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        operation_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operation_id = operation_id
        self.details: dict[str, Any] = dict(details or {})

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"DomainError(code={self.code.value!r}, message={self.message!r}, "
            f"retryable={self.retryable!r}, operation_id={self.operation_id!r})"
        )


# --------------------------------------------------------------------------
# 常用构造器：让调用点读起来就是业务语义
# --------------------------------------------------------------------------
def validation_error(message: str, **details: Any) -> DomainError:
    return DomainError(ErrorCode.VALIDATION_ERROR, message, details=details)


def permission_denied(message: str = "无权执行该操作") -> DomainError:
    return DomainError(ErrorCode.PERMISSION_DENIED, message)


def not_found(message: str = "对象不存在或无权访问") -> DomainError:
    """统一用 NOT_FOUND 表达"不存在"与"无权访问"，避免泄漏存在性。"""

    return DomainError(ErrorCode.NOT_FOUND, message)


def version_conflict(message: str, **details: Any) -> DomainError:
    return DomainError(ErrorCode.VERSION_CONFLICT, message, details=details)


def slot_conflict(message: str, **details: Any) -> DomainError:
    return DomainError(ErrorCode.SLOT_CONFLICT, message, details=details)


def hold_expired(message: str = "占位已过期，请重新确认时段") -> DomainError:
    return DomainError(ErrorCode.HOLD_EXPIRED, message)


def stale_proposal(message: str, **details: Any) -> DomainError:
    return DomainError(ErrorCode.STALE_PROPOSAL, message, details=details)


def idempotency_mismatch(
    message: str = "同一幂等键使用了不同参数",
) -> DomainError:
    return DomainError(ErrorCode.IDEMPOTENCY_MISMATCH, message)


def confirmation_required(message: str = "缺少有效的方案级确认凭据") -> DomainError:
    return DomainError(ErrorCode.CONFIRMATION_REQUIRED, message)


def dependency_unavailable(message: str, **details: Any) -> DomainError:
    return DomainError(
        ErrorCode.DEPENDENCY_UNAVAILABLE, message, retryable=True, details=details
    )


def human_takeover_active(message: str = "任务已被人工接管") -> DomainError:
    return DomainError(ErrorCode.HUMAN_TAKEOVER_ACTIVE, message)


def lease_lost(message: str = "执行租约已失效") -> DomainError:
    return DomainError(ErrorCode.LEASE_LOST, message)


def budget_exhausted(message: str = "任务预算不足") -> DomainError:
    return DomainError(ErrorCode.BUDGET_EXHAUSTED, message)


def unknown_outcome(
    message: str, *, operation_id: str | None = None
) -> DomainError:
    """写入结果不明。调用方必须先查原操作，不能重新规划一笔替代写入。"""

    return DomainError(
        ErrorCode.UNKNOWN_OUTCOME, message, operation_id=operation_id
    )
