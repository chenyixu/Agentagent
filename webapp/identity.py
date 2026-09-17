"""浏览器演示的身份映射（服务端派生，绝不来自请求）。

设计稿 §5.1 第 1 步：``tenant_id`` / ``actor_id`` / 门店范围由服务端注入，不能
作为工具输入，也不能由模型指定。这个模块把那条规则落到 AgentScope 这一侧。

三类标识必须分清，混在一起就会把边界弄丢：

1. **登录名**（``user_id``，如 ``customer-1``）——只用于在 UI 上区分"谁登录了"。
   它就是 order_demo 里 ``demo-user-a`` 的角色：一个**本地开发身份**，不代表生产
   的 JWT / 身份服务。
2. **业务主键**（``tenant_id`` / ``actor_id`` / ``customer_id`` / ``store_id``）
   ——从**业务库**读出来的 UUID。登录名到业务主键的映射只存在于服务端，在
   :func:`load_identities` 里构建，请求体里没有它的位置。
3. **ToolScope**——AgentScope 侧的工具作用域，只带 ``tenant_id`` 与
   ``customer_id``，因为那正是业务工具**最少**需要的东西。多给一个字段就等于
   多一条能被用错的路。

未知登录名一律拒绝（fail-closed），不回退到某个默认租户：给错租户的数据比
报错危险得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from appointment.db import models as m

#: 演示登录名的固定前缀。业务库里的 UUID 每次重置都会变，登录名要稳定。
CUSTOMER_LOGIN_PREFIX = "customer"
MANAGER_LOGIN = "manager"

#: 种子在 ``protected_contact_ref`` 里写下的稳定序号，形如 ``hmac:customer-1``。
#:
#: 用它给演示登录名排序，是为了让 ``customer-1`` 每次都指同一个演示客户。**这不是
#: 把联系信息当身份**——可信身份始终是业务库里的 ``tenant_id`` / ``actor_id`` /
#: ``customer_id``（见 ``Customer`` 的类注释：手机号只是受保护属性）。这里只借它
#: 的序号当作"这是种子创建的第几个客户"这个标签，因为库表里没有别的稳定序。
_SEEDED_CUSTOMER_ORDINAL = re.compile(r"^hmac:customer-(\d+)$")

#: 没有稳定序号的客户排在有序号的之后；同组之间按 UUID 排序，保证结果只依赖库里
#: 的数据本身，不依赖读取顺序或字典插入顺序。
_UNORDERED_RANK = 1 << 30


def _customer_sort_key(customer: m.Customer) -> tuple[int, str]:
    """客户的确定性排序键。

    "确定性"在这里是必须的，不是锦上添花：如果顺序由 ``created_at`` 相同时的随机
    UUID 决定，那么重置业务库之后 ``customer-1`` 可能换人，本地演示的每一步都会
    变得不可复现。
    """

    match = _SEEDED_CUSTOMER_ORDINAL.match(customer.protected_contact_ref or "")
    if match:
        return (int(match.group(1)), str(customer.id))
    return (_UNORDERED_RANK, str(customer.id))


@dataclass(frozen=True, slots=True)
class BusinessIdentity:
    """一个演示登录名对应的、由服务端持有的可信业务身份。"""

    user_id: str
    role: str
    display_name: str
    tenant_id: UUID
    actor_id: UUID
    customer_id: UUID | None
    store_id: UUID

    @property
    def login_hint(self) -> str:
        return f"{self.user_id}（{self.display_name}）"


class UnknownIdentityError(LookupError):
    """登录名不在服务端身份表里。拒绝，而不是给一个默认租户。"""


async def load_identities(session: AsyncSession) -> dict[str, BusinessIdentity]:
    """从业务库读出演示身份表。

    依赖 ``seed()`` 写入的夹具：一个租户、一个门店、两个客户、一个店长。库没灌
    种子时会抛 :class:`UnknownIdentityError` 的调用方（而不是在这里静默构造空表），
    因为"空身份表"和"忘了灌种子"是两件事，后者必须让人看见。
    """

    tenant = (await session.execute(select(m.Tenant))).scalars().first()
    if tenant is None:
        raise UnknownIdentityError(
            "业务库里没有租户：请先运行 .venv/bin/python scripts/dev_reset.py"
        )

    store = (
        await session.execute(
            select(m.Store).where(m.Store.tenant_id == tenant.id)
        )
    ).scalars().first()
    if store is None:
        raise UnknownIdentityError("业务库里没有门店：请先运行 scripts/dev_reset.py")

    customers = (
        (
            await session.execute(
                select(m.Customer).where(m.Customer.tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    )
    # 排序在 Python 侧做：种子序号藏在字符串里，SQL 侧排不出想要的次序。
    customers = sorted(customers, key=_customer_sort_key)

    identities: dict[str, BusinessIdentity] = {}
    for index, customer in enumerate(customers, start=1):
        login = f"{CUSTOMER_LOGIN_PREFIX}-{index}"
        identities[login] = BusinessIdentity(
            user_id=login,
            role="customer",
            display_name=customer.display_name,
            tenant_id=tenant.id,
            actor_id=customer.actor_id,
            customer_id=customer.id,
            store_id=store.id,
        )

    manager = (
        await session.execute(
            select(m.StaffMembership).where(
                m.StaffMembership.tenant_id == tenant.id,
                m.StaffMembership.role == "store_manager",
                m.StaffMembership.status == "ACTIVE",
            )
        )
    ).scalars().first()
    if manager is not None:
        identities[MANAGER_LOGIN] = BusinessIdentity(
            user_id=MANAGER_LOGIN,
            role="store_manager",
            display_name="店长",
            tenant_id=tenant.id,
            actor_id=manager.actor_id,
            # 员工没有 customer_id：访问哪个客户取决于后端授权，不取决于他自称是谁。
            customer_id=None,
            store_id=manager.store_id,
        )

    if not identities:
        raise UnknownIdentityError(
            "业务库里没有任何客户：请先运行 .venv/bin/python scripts/dev_reset.py"
        )
    return identities


class IdentityRegistry:
    """已加载的身份表。进程内只读，避免每次工具调用都查一遍库。

    维护两个方向的索引，因为框架的两个回调拿到的输入不同：

    - ``tool_scope_resolver(user_id, agent_id, session_id)`` 只有**登录名**，
      用 :meth:`get` 做正向查找。
    - ``scoped_extra_agent_tools(scope)`` 只有 ``ToolScope``（已核实过的
      tenant/customer），用 :meth:`by_scope` 做反向查找。

    反向索引的键用 ``str`` 而不是 ``UUID``：``ToolScope`` 的字段就是 ``str``，
    用同一种类型做键可以省掉一次"解析成 UUID 再格式化回字符串"的往返——那种往返
    一旦格式不一致（大小写、连字符）就会变成查不到，而查不到在这里意味着别人的
    工具被装配成你的。
    """

    def __init__(self, identities: dict[str, BusinessIdentity]) -> None:
        self._identities = dict(identities)
        self._scopes: dict[tuple[str, str], BusinessIdentity] = {}
        for identity in self._identities.values():
            if identity.customer_id is None:
                continue
            key = (str(identity.tenant_id), str(identity.customer_id))
            existing = self._scopes.get(key)
            if existing is not None:
                # 两个登录名映射到同一个 (tenant, customer) 时，反向查找就不再
                # 唯一。宁可在这里炸掉，也不要让"装配了谁的工具"变成一个
                # 依赖字典插入顺序的答案。
                raise UnknownIdentityError(
                    f"登录名 {existing.user_id!r} 与 {identity.user_id!r} 指向同一个"
                    f"业务作用域 {key}；反向查找不再唯一，请修正种子数据",
                )
            self._scopes[key] = identity

    @classmethod
    async def load(cls, session: AsyncSession) -> IdentityRegistry:
        return cls(await load_identities(session))

    def get(self, user_id: str) -> BusinessIdentity:
        identity = self._identities.get(user_id)
        if identity is None:
            known = "、".join(sorted(self._identities)) or "（空）"
            raise UnknownIdentityError(
                f"未知登录名 {user_id!r}；本机可用：{known}"
            )
        return identity

    def find(self, user_id: str) -> BusinessIdentity | None:
        """不抛错的查找。

        调用方要自己决定"没找到"是不是错误、以及错误长什么样（比如映射成
        HTTP 403 而不是 500）。把"要不要抛"留在调用方，这里只回答"有没有"。
        """

        return self._identities.get(user_id)

    def find_by_scope(
        self,
        tenant_id: str,
        customer_id: str,
    ) -> BusinessIdentity | None:
        """由 ``ToolScope`` 反查身份；找不到返回 ``None``。

        调用方（``webapp.authorization``）负责把"找不到"翻译成一个 HTTP 错误。
        那条路径的输入来自框架刚核实过的作用域、不是请求体，所以"找不到"只可能来自
        种子数据与运行期不一致（比如重置了业务库但 Redis 里还留着旧会话）。报错比
        静默降级好：静默降级会让浏览器看到另一个客户的数据。
        """

        return self._scopes.get((tenant_id, customer_id))

    def by_scope(self, tenant_id: str, customer_id: str) -> BusinessIdentity:
        """:meth:`find_by_scope` 的抛错版本，给"找不到就是 bug"的调用点用。"""

        identity = self.find_by_scope(tenant_id, customer_id)
        if identity is None:
            known = "、".join(sorted(self._identities)) or "（空）"
            raise UnknownIdentityError(
                f"业务作用域 (tenant={tenant_id}, customer={customer_id}) 没有对应"
                f"登录名；本机已知：{known}"
            )
        return identity

    def all(self) -> tuple[BusinessIdentity, ...]:
        return tuple(self._identities.values())

    def describe(self) -> str:
        lines = []
        for identity in sorted(self._identities.values(), key=lambda i: i.user_id):
            scope = (
                f"租户 {identity.tenant_id}"
                if identity.customer_id is None
                else f"客户 {identity.customer_id}"
            )
            lines.append(f"  {identity.login_hint}  角色={identity.role}  {scope}")
        return "\n".join(lines)
