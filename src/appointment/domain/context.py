"""可信调用上下文。

设计稿 §5.1 第 1 步与 §9：tenant_id、actor_id、可信授权上下文由服务端注入，
不能作为可被模型随意改写的工具输入。角色与门店范围来自接入层解析的身份，
工具参数里没有它们的位置。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..core.enums import ErrorCode
from ..core.errors import DomainError

#: 角色能力表。默认拒绝：只开放当前任务阶段需要的动作。
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "customer": frozenset(
        {
            "task:read",
            "task:write",
            "task:confirm",
            "appointment:read",
            "appointment:create",
            "appointment:reschedule",
            "appointment:cancel",
            "knowledge:read",
            "catalog:read",
        }
    ),
    "staff": frozenset(
        {
            "task:read",
            "appointment:read",
            "appointment:complete",
            "appointment:no_show",
            "knowledge:read",
            "catalog:read",
        }
    ),
    "store_manager": frozenset(
        {
            "task:read",
            "task:takeover",
            "appointment:read",
            "appointment:complete",
            "appointment:no_show",
            "appointment:cancel",
            "calendar:write",
            "shift:write",
            "knowledge:read",
            "catalog:read",
            "disruption:resolve",
        }
    ),
    "pricing_admin": frozenset({"catalog:read", "price:write", "knowledge:read"}),
    "worker": frozenset({"job:consume", "notification:send", "allocation:expire"}),
    "system": frozenset(
        {
            "task:read",
            "task:write",
            "task:confirm",
            "appointment:read",
            "appointment:create",
            "appointment:reschedule",
            "appointment:cancel",
            "knowledge:read",
            "catalog:read",
            "job:consume",
            "notification:send",
            "allocation:expire",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class TrustedContext:
    """由服务端构造，不来自请求体或模型输出。"""

    tenant_id: UUID
    actor_id: UUID
    role: str
    request_id: str
    release_id: str
    #: 客户身份。员工可以没有 customer_id，访问目标客户依赖后端权限。
    customer_id: UUID | None = None
    store_scopes: tuple[UUID, ...] = ()
    #: 执行权。Agent/自动接管者必须验证自身 fence 与执行权；不把前端伪造的
    #: fence 当凭据。
    epoch: int | None = None
    fencing_token: int | None = None
    trace_id: str | None = None

    def permissions(self) -> frozenset[str]:
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def can(self, permission: str) -> bool:
        return permission in self.permissions()

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise DomainError(
                ErrorCode.PERMISSION_DENIED,
                f"角色 {self.role} 不具备权限 {permission}",
            )

    def require_customer(self) -> UUID:
        if self.customer_id is None:
            raise DomainError(
                ErrorCode.PERMISSION_DENIED, "该操作需要客户身份"
            )
        return self.customer_id

    def require_store_scope(self, store_id: UUID) -> None:
        """员工只能操作所属门店。限制只看 store_scopes，不看请求参数。"""

        if self.role in ("customer", "system", "worker"):
            return
        if self.store_scopes and store_id not in self.store_scopes:
            # 对外统一 NOT_FOUND，避免泄漏其他门店对象的存在性。
            raise DomainError(ErrorCode.NOT_FOUND, "对象不存在或无权访问")

    def for_worker(self) -> "TrustedContext":
        return self
