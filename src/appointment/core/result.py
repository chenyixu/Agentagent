"""统一工具 / 命令结果。

设计稿 §2.2：所有工具共享同一个结果外壳，便于编排器做一致的错误分类、
重试决策与 SSE 投影。工具业务数据放在 ``data``，由各工具自己的 Schema 校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .enums import ErrorCode, ToolStatus
from .errors import SCHEMA_VERSION, DomainError


@dataclass(slots=True)
class ToolResult:
    """一个工具调用的类型化结果。"""

    status: ToolStatus
    request_id: str
    observed_at: datetime
    data: dict[str, Any] | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    retryable: bool = False
    operation_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # 约束：OK 必须有 data；ERROR 必须有 error_code。
        if self.status is ToolStatus.OK and self.data is None:
            raise ValueError("OK 结果必须携带 data")
        if self.status is ToolStatus.ERROR and self.error_code is None:
            raise ValueError("ERROR 结果必须携带 error_code")

    # ---------------- 构造器 ----------------
    @classmethod
    def ok(
        cls,
        request_id: str,
        observed_at: datetime,
        data: dict[str, Any],
        *,
        operation_id: str | None = None,
    ) -> "ToolResult":
        return cls(
            status=ToolStatus.OK,
            request_id=request_id,
            observed_at=observed_at,
            data=data,
            operation_id=operation_id,
        )

    @classmethod
    def pending(
        cls,
        request_id: str,
        observed_at: datetime,
        data: dict[str, Any],
        *,
        operation_id: str,
    ) -> "ToolResult":
        return cls(
            status=ToolStatus.PENDING,
            request_id=request_id,
            observed_at=observed_at,
            data=data,
            retryable=False,
            operation_id=operation_id,
        )

    @classmethod
    def unknown(
        cls,
        request_id: str,
        observed_at: datetime,
        message: str,
        *,
        operation_id: str | None = None,
    ) -> "ToolResult":
        """效果未查清。不是普通可重试失败。"""

        return cls(
            status=ToolStatus.UNKNOWN,
            request_id=request_id,
            observed_at=observed_at,
            error_code=ErrorCode.UNKNOWN_OUTCOME,
            error_message=message,
            retryable=False,
            operation_id=operation_id,
        )

    @classmethod
    def failure(
        cls,
        request_id: str,
        observed_at: datetime,
        error: DomainError,
    ) -> "ToolResult":
        return cls(
            status=ToolStatus.ERROR,
            request_id=request_id,
            observed_at=observed_at,
            error_code=error.code,
            error_message=error.message,
            retryable=error.retryable,
            operation_id=error.operation_id,
        )

    # ---------------- 序列化 ----------------
    @property
    def is_ok(self) -> bool:
        return self.status is ToolStatus.OK

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status.value,
            "retryable": self.retryable,
            "observed_at": self.observed_at.isoformat(),
            "schema_version": self.schema_version,
            "request_id": self.request_id,
        }
        if self.data is not None:
            payload["data"] = self.data
        if self.error_code is not None:
            payload["error_code"] = self.error_code.value
        if self.error_message is not None:
            payload["error_message"] = self.error_message
        if self.operation_id is not None:
            payload["operation_id"] = self.operation_id
        return payload


@dataclass(slots=True)
class LedgerEntry:
    """工具调用的账本记录。

    设计稿 §5.1 第 5 步："领域层验证业务参数，工具结果进入账本"。
    账本是任务预算与执行摘要的依据，不保存授权。
    """

    tool_name: str
    request_id: str
    status: ToolStatus
    observed_at: datetime
    logical_operation_id: str | None = None
    parameter_hash: str = ""
    error_code: ErrorCode | None = None
    summary: dict[str, Any] = field(default_factory=dict)
