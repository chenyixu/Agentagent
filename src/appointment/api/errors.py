"""领域错误 → HTTP 的映射。

只有这一个映射表：接口层不再各自判断"这个错误该返回什么码"。设计稿 §9 要求
错误枚举稳定，因此 HTTP 状态码与错误码一样属于对外契约。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..core.enums import ErrorCode
from ..core.errors import DomainError, SCHEMA_VERSION
from .identity import IdentityError

#: 错误码 → HTTP 状态码。写冲突统一 409，凭据缺失 428，依赖不可用 503。
STATUS_BY_ERROR: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.STALE_PROPOSAL: 409,
    ErrorCode.SLOT_CONFLICT: 409,
    ErrorCode.HOLD_EXPIRED: 409,
    ErrorCode.IDEMPOTENCY_MISMATCH: 409,
    ErrorCode.LEASE_LOST: 409,
    ErrorCode.HUMAN_TAKEOVER_ACTIVE: 409,
    ErrorCode.CONFIRMATION_REQUIRED: 428,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.BUDGET_EXHAUSTED: 429,
    # 效果不明不是失败：用 202 让调用方去查原操作，而不是换个键重试。
    ErrorCode.UNKNOWN_OUTCOME: 202,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


def error_body(error: DomainError) -> dict:
    return {
        "error": {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable,
            "operation_id": error.operation_id,
            "details": error.details,
        },
        "schema_version": SCHEMA_VERSION,
    }


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=STATUS_BY_ERROR.get(exc.code, 500),
        content=error_body(exc),
    )


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体形状错误也用同一个错误信封。

    FastAPI 默认返回 ``{"detail": [...]}``，那是框架的形状而不是本系统的契约；
    客户端要能用**一套**解析逻辑处理所有失败。这里把它收敛成
    ``VALIDATION_ERROR``，并把出错字段放进 ``details``，这样"请求体里塞了
    ``tenant_id``"这类越权尝试是一个可断言的契约事实，而不是一段框架文案。
    """

    details = [
        {
            "location": ".".join(str(part) for part in item.get("loc", ())),
            "type": item.get("type"),
            "message": item.get("msg"),
        }
        for item in exc.errors()
    ]
    error = DomainError(
        ErrorCode.VALIDATION_ERROR,
        "请求体不符合契约",
        details={"fields": details},
    )
    return JSONResponse(
        status_code=STATUS_BY_ERROR[ErrorCode.VALIDATION_ERROR],
        content=error_body(error),
    )


async def identity_error_handler(request: Request, exc: IdentityError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "UNAUTHENTICATED"
                if exc.status_code == 401
                else "IDENTITY_UNAVAILABLE",
                "message": exc.message,
                "retryable": False,
                "operation_id": None,
                "details": {},
            },
            "schema_version": SCHEMA_VERSION,
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """未预期错误统一收敛成 INTERNAL_ERROR。

    不回显堆栈：错误详情属于内部信息，不外泄。
    """

    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "服务内部错误",
                "retryable": True,
                "operation_id": None,
                "details": {},
            },
            "schema_version": SCHEMA_VERSION,
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(IdentityError, identity_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)


__all__ = [
    "STATUS_BY_ERROR",
    "domain_error_handler",
    "error_body",
    "identity_error_handler",
    "install_error_handlers",
    "request_validation_error_handler",
    "unhandled_error_handler",
]
