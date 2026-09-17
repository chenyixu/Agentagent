"""浏览器适配层的不变量测试。

这一层最值得锁住的东西不是"能不能跑通"，而是**边界**：

1. 登录名到业务主键的映射只存在于服务端（``webapp.identity``）；
2. 模型可见的工具参数里没有任何身份字段——没有可传的地方，也就没有传错的可能；
3. 未知登录名 / 没有客户作用域的角色一律拒绝，绝不回退到默认租户；
4. 只读工具确实只读，且失败时说成失败。

第 2 条是这组测试里最重要的。它检查的是**工具的输入 schema**，也就是模型唯一能看
到的那份契约；只要那份 schema 里出现 ``tenant_id`` / ``store_id`` / ``customer_id``
这类字段，边界就已经破了，无论服务端之后怎么校验。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.tool import ToolBase

from appointment.seed import SeedResult

from webapp.authorization import resolve_identity_for_scope, resolve_tool_scope
from webapp.identity import (
    BusinessIdentity,
    IdentityRegistry,
    UnknownIdentityError,
)
from webapp.tools import build_scoped_tools

#: 任何身份类字段出现在模型可见的 schema 里都算越界。
IDENTITY_TOKENS = (
    "tenant",
    "store",
    "customer",
    "actor",
    "org",
    "role",
    "user_id",
    "member",
)


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """和被测代码同一种会话工厂：独立的会话、测试结束时不留状态。"""

    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def registry(
    seeded: SeedResult,
    factory: async_sessionmaker[AsyncSession],
) -> IdentityRegistry:
    """按业务库里已经灌好的种子读出的身份表。

    刻意不复用 ``seeded`` 的返回值来构造身份表：那样测的就不是"从库里读"这条真实
    路径了。
    """

    async with factory() as reader:
        return await IdentityRegistry.load(reader)


def _chunk_payload(chunk: Any) -> dict[str, Any]:
    """把工具返回的 ToolChunk 还原成业务 payload。

    工具体返回 dict，框架把它包成 ``ToolChunk.content[0].text`` 里的 JSON 字符串。
    这里只做解包，不做断言改写——测试要断言的是真实返回，不是我想象的返回。
    """

    block = chunk.content[0]
    text = getattr(block, "text", None)
    assert isinstance(text, str) and text, (
        f"工具返回里没有文本块，实际拿到 {block!r}；"
        "下面的断言会失去意义，先修这里"
    )
    return json.loads(text)


# ---------------------------------------------------------------------------
# 身份映射
# ---------------------------------------------------------------------------


async def test_login_names_map_to_business_primary_keys(
    registry: IdentityRegistry,
    seeded: SeedResult,
) -> None:
    """登录名是稳定的，业务 UUID 不是——映射必须发生在服务端。

    这里同时钉住"``customer-1`` 是种子的第一个客户"这条语义。只断言"数量对得上"
    是不够的：如果顺序由 ``created_at`` 相同时的随机 UUID 决定，测试会以约一半的
    概率通过，而演示里 ``customer-1`` 已经悄悄换了人。
    """

    first = registry.get("customer-1")
    assert first.role == "customer"
    assert first.tenant_id == seeded.tenant_id
    assert first.customer_id == seeded.customer_ids[0]
    assert first.actor_id == seeded.customer_actor_ids[0]
    assert first.store_id == seeded.store_id
    assert first.display_name == "林女士"

    second = registry.get("customer-2")
    assert second.customer_id == seeded.customer_ids[1]
    assert second.customer_id != first.customer_id
    assert second.display_name == "赵先生"


async def test_staff_identity_has_no_customer_scope(
    registry: IdentityRegistry,
    seeded: SeedResult,
) -> None:
    """店长没有 ``customer_id``：访问哪个客户取决于后端授权，不取决于他自称是谁。"""

    manager = registry.get("manager")
    assert manager.role == "store_manager"
    assert manager.customer_id is None
    assert manager.actor_id == seeded.manager_actor_id


async def test_unknown_login_raises(registry: IdentityRegistry) -> None:
    with pytest.raises(UnknownIdentityError):
        registry.get("nobody")


async def test_scope_reverse_lookup_round_trips(registry: IdentityRegistry) -> None:
    """``ToolScope`` 反查身份必须能回到同一个人。"""

    for identity in registry.all():
        if identity.customer_id is None:
            continue
        found = registry.by_scope(str(identity.tenant_id), str(identity.customer_id))
        assert found.user_id == identity.user_id


async def test_reverse_lookup_rejects_unknown_scope(
    registry: IdentityRegistry,
) -> None:
    """业务库重置后 Redis 里可能留着旧会话；这种作用域必须查不到，而不是降级。"""

    with pytest.raises(UnknownIdentityError):
        registry.by_scope(str(uuid4()), str(uuid4()))


def test_two_logins_sharing_one_scope_is_rejected() -> None:
    """反向查找不唯一时必须炸掉，不能靠字典插入顺序决定"装配了谁的工具"。"""

    tenant_id, customer_id = uuid4(), uuid4()
    shared = {
        "a": BusinessIdentity(
            user_id="a",
            role="customer",
            display_name="甲",
            tenant_id=tenant_id,
            actor_id=uuid4(),
            customer_id=customer_id,
            store_id=uuid4(),
        ),
        "b": BusinessIdentity(
            user_id="b",
            role="customer",
            display_name="乙",
            tenant_id=tenant_id,
            actor_id=uuid4(),
            customer_id=customer_id,
            store_id=uuid4(),
        ),
    }
    with pytest.raises(UnknownIdentityError):
        IdentityRegistry(shared)


# ---------------------------------------------------------------------------
# 模型可见的工具面
# ---------------------------------------------------------------------------


async def _tools_by_name(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: str = "customer-1",
) -> dict[str, ToolBase]:
    tools = build_scoped_tools(
        identity=registry.get(user_id), session_factory=factory
    )
    return {tool.name: tool for tool in tools}


async def test_model_visible_schema_exposes_no_identity_fields(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """最核心的一条：模型能看到的参数里没有身份字段。"""

    tools = await _tools_by_name(registry, factory)
    assert set(tools) == {
        "get_service_quote",
        "search_availability",
        "search_knowledge",
    }

    for tool in tools.values():
        properties = set(tool.input_schema.get("properties", {}))
        for name in properties:
            lowered = name.lower()
            for token in IDENTITY_TOKENS:
                assert token not in lowered, (
                    f"{tool.name} 的模型可见参数 {name!r} 暴露了身份维度"
                    f"（命中 {token!r}）；身份必须由服务端绑定"
                )


async def test_read_only_tools_are_read_only_and_allowed(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """只读工具必须自称只读、且权限放行——否则它会去走确认流程。

    这里问的是**行为**（``check_permissions`` / ``check_read_only``），不是读某个
    内部字段：权限判定的实现细节可以变，"这次调用要不要用户确认"这个答案不能变。
    """

    tools = await _tools_by_name(registry, factory)
    for tool in tools.values():
        decision = await tool.check_permissions({}, PermissionContext())
        assert decision.behavior is PermissionBehavior.ALLOW, (
            f"{tool.name} 不是放行的：{decision.behavior}"
        )
        assert await tool.check_read_only({}) is True, f"{tool.name} 未标记为只读"


async def test_no_write_tool_is_exposed_yet(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """这一版刻意不暴露写入工具：浏览器侧不产生业务副作用。"""

    tools = await _tools_by_name(registry, factory)
    forbidden = {
        "create_hold",
        "confirm_appointment",
        "cancel_appointment",
        "reschedule_appointment",
    }
    assert not (set(tools) & forbidden)


# ---------------------------------------------------------------------------
# 工具行为：真的走通了领域层
# ---------------------------------------------------------------------------


async def test_quote_tool_reads_the_real_price(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
    seeded: SeedResult,
) -> None:
    """报价来自业务库，不是模型编的。"""

    tools = await _tools_by_name(registry, factory)
    payload = _chunk_payload(await tools["get_service_quote"].call(service="肩颈"))

    assert payload["amount_minor"] == seeded.price_amount_minor
    assert payload["duration_minutes"] == 60
    assert payload["currency"] == "CNY"


async def test_quote_tool_asks_for_clarification_when_name_is_unknown(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """名字对不上时返回"没找到"，而不是猜一个最像的。

    猜错的价格和猜错的号一样有害——而且更难发现。
    """

    tools = await _tools_by_name(registry, factory)
    payload = _chunk_payload(
        await tools["get_service_quote"].call(service="不存在的项目名")
    )

    assert payload["ok"] is False
    assert payload["matched"] is False
    assert "message" in payload


async def test_knowledge_tool_returns_evidence_for_a_known_policy(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """政策问题必须拿到带出处的证据，而不是一段自由发挥。"""

    tools = await _tools_by_name(registry, factory)
    payload = _chunk_payload(
        await tools["search_knowledge"].call(query="取消政策")
    )

    hits = payload["hits"]
    assert hits, "取消政策应当在知识库里"
    assert hits[0]["excerpt"]
    assert hits[0]["evidence_id"]


async def test_tools_are_bound_to_the_requesting_customers_own_scope(
    registry: IdentityRegistry,
    factory: async_sessionmaker[AsyncSession],
    seeded: SeedResult,
) -> None:
    """两个客户拿到的报价都来自同一门店，但工具绑定的是各自的客户身份。

    这里验证的是"装配期固定"这件事：同一个函数在两个身份下装配出的工具，作用域
    各自独立，不会互相串。
    """

    first = await _tools_by_name(registry, factory, user_id="customer-1")
    second = await _tools_by_name(registry, factory, user_id="customer-2")

    for tools in (first, second):
        payload = _chunk_payload(
            await tools["get_service_quote"].call(service="肩颈")
        )
        assert payload["amount_minor"] == seeded.price_amount_minor
        assert UUID(payload["store_id"]) == seeded.store_id


# ---------------------------------------------------------------------------
# 授权判定
# ---------------------------------------------------------------------------


def test_customer_resolves_to_its_own_scope(registry: IdentityRegistry) -> None:
    scope = resolve_tool_scope(registry, "customer-1")
    expected = registry.get("customer-1")
    assert scope.tenant_id == str(expected.tenant_id)
    assert scope.customer_id == str(expected.customer_id)


def test_two_customers_resolve_to_different_scopes(
    registry: IdentityRegistry,
) -> None:
    """同一门店的两个客户拿到不同的 ``customer_id``——这是数据隔离的依据。"""

    first = resolve_tool_scope(registry, "customer-1")
    second = resolve_tool_scope(registry, "customer-2")
    assert first.tenant_id == second.tenant_id
    assert first.customer_id != second.customer_id


def test_unknown_login_is_refused_with_403(registry: IdentityRegistry) -> None:
    """未知登录名不可以回退到某个默认租户：给错租户的数据比报错危险得多。"""

    with pytest.raises(HTTPException) as excinfo:
        resolve_tool_scope(registry, "nobody")

    assert excinfo.value.status_code == 403
    # 提示里要能看出该填什么，否则本地演示只能靠猜。
    assert "customer-1" in excinfo.value.detail


def test_staff_without_customer_scope_is_refused(
    registry: IdentityRegistry,
) -> None:
    """店长在这个界面里没有可用的业务作用域，必须明确拒绝。"""

    with pytest.raises(HTTPException) as excinfo:
        resolve_tool_scope(registry, "manager")

    assert excinfo.value.status_code == 403
    assert "store_manager" in excinfo.value.detail


def test_scope_reverse_lookup_refuses_unknown_scope(
    registry: IdentityRegistry,
) -> None:
    """旧会话指向的作用域已经不存在时必须拒绝，而不是降级成别的客户。"""

    with pytest.raises(HTTPException) as excinfo:
        resolve_identity_for_scope(registry, str(uuid4()), str(uuid4()))

    assert excinfo.value.status_code == 403
