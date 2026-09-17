"""身份解析：请求头 → :class:`TrustedContext`。

三条硬规则（设计稿 §5.1 第 1 步、§12.1）：

1. **请求体与模型都不能指定自己的可信身份**。请求 Schema 里没有 tenant/actor
   的位置，并且 ``extra="forbid"`` 会让试图自带 tenant_id 的请求直接 400。
2. **pilot / production 不接受临时身份头**。这两个环境必须走 OIDC；本里程碑未接入
   OIDC 校验，因此在那两个环境里会明确报"未配置身份来源"，而不是悄悄放行开发头。
3. 角色与门店范围也来自身份解析，不来自业务参数。
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

#: 本地/测试环境的临时身份头。生产环境禁用。
TEMP_TENANT_HEADER = "X-Tenant-Id"
TEMP_ACTOR_HEADER = "X-Actor-Id"
TEMP_ROLE_HEADER = "X-Role"
TEMP_CUSTOMER_HEADER = "X-Customer-Id"
TEMP_STORE_SCOPE_HEADER = "X-Store-Scope"

REQUEST_ID_HEADER = "X-Request-Id"
RELEASE_HEADER = "X-Release-Id"

DEFAULT_RELEASE_ID = "release-local-1"

#: 允许出现在身份头里的角色。与 ``domain.context.ROLE_PERMISSIONS`` 对齐。
KNOWN_ROLES = frozenset(
    {"customer", "staff", "store_manager", "pricing_admin", "worker", "system"}
)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class IdentityError(Exception):
    """身份解析失败。接入层错误，不是领域错误。"""

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _parse_uuid(raw: str | None, *, header: str) -> UUID | None:
    if raw is None or not raw.strip():
        return None
    try:
        return UUID(raw.strip())
    except ValueError as exc:
        raise IdentityError(f"{header} 不是合法 UUID") from exc


def _parse_uuid_list(raw: str | None, *, header: str) -> tuple[UUID, ...]:
    if raw is None or not raw.strip():
        return ()
    values: list[UUID] = []
    for part in raw.split(","):
        if not part.strip():
            continue
        try:
            values.append(UUID(part.strip()))
        except ValueError as exc:
            raise IdentityError(f"{header} 含非法 UUID：{part.strip()}") from exc
    return tuple(values)


def resolve_identity(headers: dict[str, str], *, allow_temp_identity: bool):
    """把请求头解析成可信身份。

    返回 :class:`~appointment.domain.context.TrustedContext`。这里刻意保持纯函数：
    不依赖 FastAPI，方便直接单测身份边界。
    """

    from ..domain.context import TrustedContext

    lowered = {key.lower(): value for key, value in headers.items()}

    authorization = lowered.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        # OIDC 校验属于独立里程碑：乱认 token 比拒绝更危险。
        raise IdentityError(
            "本服务当前未启用 OIDC 校验；请勿在生产环境使用 Bearer 身份",
            status_code=501,
        )

    if not allow_temp_identity:
        raise IdentityError(
            "该环境不接受临时身份头（X-Actor-Id 等），必须通过 OIDC 接入",
            status_code=501,
        )

    tenant_id = _parse_uuid(lowered.get(TEMP_TENANT_HEADER.lower()), header=TEMP_TENANT_HEADER)
    actor_id = _parse_uuid(lowered.get(TEMP_ACTOR_HEADER.lower()), header=TEMP_ACTOR_HEADER)
    if tenant_id is None or actor_id is None:
        raise IdentityError(
            f"缺少可信身份头：{TEMP_TENANT_HEADER}、{TEMP_ACTOR_HEADER} 均为必填"
        )

    role = (lowered.get(TEMP_ROLE_HEADER.lower()) or "customer").strip() or "customer"
    if role not in KNOWN_ROLES:
        raise IdentityError(f"{TEMP_ROLE_HEADER} 取值非法：{role}")

    customer_id = _parse_uuid(
        lowered.get(TEMP_CUSTOMER_HEADER.lower()), header=TEMP_CUSTOMER_HEADER
    )
    if role == "customer" and customer_id is None:
        # 客户角色必须绑定客户身份：否则后续所有归属校验都无从谈起。
        raise IdentityError("客户角色必须提供 X-Customer-Id")

    request_id = (lowered.get(REQUEST_ID_HEADER.lower()) or "").strip()
    if request_id and not _REQUEST_ID_PATTERN.match(request_id):
        raise IdentityError("X-Request-Id 含非法字符（只允许字母数字与 _.:-）")
    if not request_id:
        request_id = uuid4().hex

    release_id = (lowered.get(RELEASE_HEADER.lower()) or DEFAULT_RELEASE_ID).strip()

    return TrustedContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        role=role,
        request_id=request_id,
        release_id=release_id,
        customer_id=customer_id,
        store_scopes=_parse_uuid_list(
            lowered.get(TEMP_STORE_SCOPE_HEADER.lower()), header=TEMP_STORE_SCOPE_HEADER
        ),
        trace_id=lowered.get("traceparent") or lowered.get("x-trace-id"),
    )


__all__ = [
    "DEFAULT_RELEASE_ID",
    "IdentityError",
    "KNOWN_ROLES",
    "REQUEST_ID_HEADER",
    "RELEASE_HEADER",
    "TEMP_ACTOR_HEADER",
    "TEMP_CUSTOMER_HEADER",
    "TEMP_ROLE_HEADER",
    "TEMP_STORE_SCOPE_HEADER",
    "TEMP_TENANT_HEADER",
    "resolve_identity",
]
