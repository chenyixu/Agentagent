"""浏览器侧的预约服务入口：把 AgentScope App 服务接到我们的领域层上。

跑起来::

    .venv/bin/python -m webapp.service

这一层是**适配器**，不是第二个真相来源。三条不变式：

1. **业务事实只在业务库。** AgentScope 的 Redis 存的是会话、消息、事件流；
   预约、报价、可用时段、政策的事实全部来自 ``appointment`` 包，走
   ``appointment.tools.registry.invoke_tool`` 这一个入口。浏览器看到的工具和
   编排器用的工具是同一条代码路径，不会长成两套语义。
2. **可信身份只来自服务端。** 登录名（``customer-1``）经
   :class:`~webapp.identity.IdentityRegistry` 映射成业务 UUID，再折成
   ``ToolScope``。模型可见的函数签名里没有任何身份字段——没有可传的地方，
   也就没有传错的可能。
3. **作用域必须自洽。** 框架会核对"会话记录的 (tenant, customer)"与"解析出的
   ``ToolScope``"是否相等（``_chat.py`` 里那道检查）；不相等直接 403。所以种子
   数据里的会话作用域和解析器返回的作用域必须来自同一份身份表——这里刻意让两者
   都从 :class:`IdentityRegistry` 取，避免出现两处各自硬编码的情况。

**这一版只暴露只读工具。** 浏览器可以查项目/报价/可用时段/政策，不能下单。
写入路径（占位、确认）会走 ``scoped_context_agent_tools``，因为它们需要审计关联
与幂等键；在那之前，网页这一侧不会产生任何业务副作用。

**认证是开发期模式。** ``identity_provider=None`` 时框架用 ``X-User-ID`` 请求头
当登录名，这正是 web_ui 的 setup 页在做的事。它**不是**生产认证，正式环境要换成
``identity_provider=BearerPrincipalProvider(...)``（见 ``agentscope.app._auth``）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app import ToolExposurePolicy, ToolScope, create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    ChatModelConfig,
    CredentialRecord,
    RedisStorage,
    SessionConfig,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import DeepSeekCredential

from appointment.config.settings import get_settings
from appointment.db.session import dispose_engine, get_sessionmaker

from .booking_api import install_booking_api
from .authorization import resolve_identity_for_scope, resolve_tool_scope
from .identity import BusinessIdentity, IdentityRegistry
from .prompt import APPOINTMENT_SYSTEM_PROMPT
from .tools import build_scoped_tools

# ---------------------------------------------------------------------------
# 进程级句柄
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent

logger = logging.getLogger("webapp.service")

#: 工作区根目录。放在仓库内的 ``.local/`` 下，方便连日志一起清掉。
WORKSPACE_BASEDIR = Path(
    os.getenv("APPOINTMENT_WEB_WORKSPACE_DIR", str(_REPO_ROOT / ".local" / "workspaces"))
)

#: 演示用的模型。可以用 APPOINTMENT_WEB_MODEL 覆盖成别的 DeepSeek 模型名。
DEFAULT_MODEL = os.getenv("APPOINTMENT_WEB_MODEL", "deepseek-flash")

#: DeepSeek 凭证在 storage 里的 id。它**不会**被写进 Redis（见下）。
ENV_CREDENTIAL_ID = "appointment-web-deepseek"

#: 允许跨域的来源。web_ui 的 vite dev server 默认 5173。
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def redis_settings() -> dict[str, Any]:
    """Redis 连接参数。

    默认指向本机开发用的容器（``redis``，映射在 16379），因此不设任何环境变量也
    能直接跑起来。
    """

    return {
        "host": os.getenv("APPOINTMENT_REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("APPOINTMENT_REDIS_PORT", "16379")),
        "db": int(os.getenv("APPOINTMENT_REDIS_DB", "0")),
        "password": os.getenv("APPOINTMENT_REDIS_PASSWORD") or None,
    }


_SESSION_FACTORY: async_sessionmaker[AsyncSession] = get_sessionmaker()

#: 身份表在进程内只加载一次。业务库重置后需要重启本服务（错误信息里会说明）。
_REGISTRY: IdentityRegistry | None = None


async def load_registry() -> IdentityRegistry:
    """读取（并缓存）演示身份表。"""

    global _REGISTRY
    if _REGISTRY is None:
        async with _SESSION_FACTORY() as session:
            _REGISTRY = await IdentityRegistry.load(session)
    return _REGISTRY


def loaded_registry() -> IdentityRegistry:
    """已经加载好的身份表；还没加载就说明装配顺序错了。"""

    if _REGISTRY is None:
        raise RuntimeError("身份表尚未加载：应当在 storage 的 __aenter__ 里完成")
    return _REGISTRY


# ---------------------------------------------------------------------------
# 凭证：从环境变量取，密钥不落 Redis
# ---------------------------------------------------------------------------


def deepseek_credential_from_env() -> DeepSeekCredential | None:
    """从环境变量构造 DeepSeek 凭证；没有就返回 None。"""

    settings = get_settings()
    api_key = settings.deepseek_api_key
    if api_key is None or not api_key.get_secret_value().strip():
        return None
    return DeepSeekCredential(
        id=ENV_CREDENTIAL_ID,
        name="DeepSeek（环境变量）",
        api_key=api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
    )


class AppointmentRedisStorage(RedisStorage):
    """Redis storage + 环境变量凭证 + 启动时播种演示资源。

    凭证的处理方式和 order_demo 一致：**密钥只存在于进程内存**。列表接口拿到的是
    去掉 ``api_key`` 的元数据记录，运行期解析才在内存里补上 ``SecretStr``。这样
    Redis 里不会留下明文密钥，而这个演示仍然能用真实模型跑。
    """

    # ---------------- 凭证 ----------------

    def _environment_credential_record(
        self,
        user_id: str,
        *,
        include_secret: bool,
        chat_capable: bool,
    ) -> CredentialRecord | None:
        credential = deepseek_credential_from_env()
        if credential is None or not chat_capable:
            return None
        data = credential.model_dump()
        if not include_secret:
            data.pop("api_key", None)
        return CredentialRecord(id=credential.id, user_id=user_id, data=data)

    async def list_credentials(self, user_id: str) -> list[CredentialRecord]:
        records = await super().list_credentials(user_id)
        record = self._environment_credential_record(
            user_id,
            include_secret=False,
            chat_capable=_is_chat_capable(user_id),
        )
        if record is None:
            return records
        return [item for item in records if item.id != record.id] + [record]

    async def get_credential(
        self,
        user_id: str,
        credential_id: str,
    ) -> CredentialRecord | None:
        """内存里解析环境变量凭证，不去读它在 Redis 里的副本。"""

        record = self._environment_credential_record(
            user_id,
            include_secret=True,
            chat_capable=_is_chat_capable(user_id),
        )
        if record is not None and credential_id == record.id:
            return record
        return await super().get_credential(user_id, credential_id)

    # ---------------- 播种 ----------------

    async def _seed_agent_and_session(
        self,
        identity: BusinessIdentity,
        credential: DeepSeekCredential,
    ) -> None:
        """为一个客户建一个稳定的 Agent 与 Session。

        只给**客户**建：``ToolScope`` 要求 ``customer_id`` 非空，店长在这一侧没有
        可用的业务作用域，所以店长不参与客服对话（解析器会明确拒绝，而不是给一个
        空作用域让下游猜）。
        """

        suffix = _resource_suffix(identity.user_id)
        agent_id = f"appointment-agent-{suffix}"
        session_id = f"appointment-session-{suffix}"
        expected_scope = (str(identity.tenant_id), str(identity.customer_id))

        if await self.get_agent(identity.user_id, agent_id) is None:
            await self.upsert_agent(
                identity.user_id,
                AgentRecord(
                    id=agent_id,
                    user_id=identity.user_id,
                    data=AgentData(
                        name="门店预约助理",
                        system_prompt=APPOINTMENT_SYSTEM_PROMPT,
                        context_config=ContextConfig(),
                        react_config=ReActConfig(),
                    ),
                ),
            )

        existing = await self.get_session(identity.user_id, agent_id, session_id)
        if existing is not None:
            if (existing.tenant_id, existing.customer_id) == expected_scope:
                return
            # 会话记录的作用域是不可变的，改了它等于悄悄把历史消息搬到另一个客户
            # 名下。业务库一旦重置，``seed()`` 会生成新的 UUID，这条旧会话就再也
            # 不可用了（框架那道检查必然拒绝它）。既然它已经是一份无法使用的记录，
            # 重建比留着让人对着 403 猜要好。这里只删我们按固定命名建的那一个会话，
            # 不碰同一用户名下的其他会话。
            logger.warning(
                "会话 %s 的作用域 (%s, %s) 与当前身份 (%s, %s) 不一致，"
                "按当前身份重建（通常意味着业务库被重置过）",
                session_id,
                existing.tenant_id,
                existing.customer_id,
                *expected_scope,
            )
            await self.delete_session(identity.user_id, agent_id, session_id)

        await self.upsert_session(
            user_id=identity.user_id,
            agent_id=agent_id,
            session_id=session_id,
            # 作用域必须与 tool_scope_resolver 的返回值逐字相同，否则框架那道
            # "会话作用域不可访问" 的检查会把这次对话挡掉。
            tenant_id=expected_scope[0],
            customer_id=expected_scope[1],
            config=SessionConfig(
                workspace_id=f"appointment-workspace-{suffix}",
                name="门店预约（只读）",
                chat_model_config=ChatModelConfig(
                    type="deepseek_chat",
                    credential_id=credential.id,
                    model=DEFAULT_MODEL,
                    parameters={
                        "max_tokens": 1024,
                        "temperature": 0,
                        "thinking_enable": False,
                    },
                ),
            ),
        )

    async def __aenter__(self) -> "AppointmentRedisStorage":
        await super().__aenter__()

        registry = await load_registry()
        credential = deepseek_credential_from_env()
        if credential is None:
            # 没有密钥时不播种会话：一个"有会话但一开口就报凭证错误"的界面比
            # 一个明确说"没配密钥"的启动日志更难排查。
            return self

        for identity in registry.all():
            if identity.customer_id is None:
                continue
            await self._seed_agent_and_session(identity, credential)
        return self


def _resource_suffix(user_id: str) -> str:
    """把登录名变成 Redis 里安全的资源后缀。"""

    return "".join(char if char.isalnum() else "-" for char in user_id)


def _is_chat_capable(user_id: str) -> bool:
    """这个登录名能不能在这个界面里对话。

    只有客户能：店长没有 ``customer_id``，给不出 ``ToolScope``。
    """

    registry = _REGISTRY
    if registry is None:
        return False
    for identity in registry.all():
        if identity.user_id == user_id:
            return identity.customer_id is not None
    return False


# ---------------------------------------------------------------------------
# 作用域解析：登录名 -> ToolScope
# ---------------------------------------------------------------------------


async def appointment_tool_scope_resolver(
    user_id: str,
    agent_id: str,
    session_id: str,
) -> ToolScope:
    """框架回调：登录名 → 业务作用域。

    判定本身在 :func:`webapp.authorization.resolve_tool_scope`；这里只负责把身份表
    取出来。``agent_id`` / ``session_id`` 刻意不参与判定——业务作用域只取决于认证
    出来的人，不取决于他访问的是哪个 agent 或会话。拿它们做条件就等于把"访问控制"
    交给了请求参数。
    """

    return resolve_tool_scope(await load_registry(), user_id)


# ---------------------------------------------------------------------------
# 工具装配：ToolScope -> 只读业务工具
# ---------------------------------------------------------------------------


async def appointment_scoped_read_tools(scope: ToolScope) -> list[Any]:
    """由框架核实过的作用域装配只读工具。

    框架只把 ``ToolScope``（tenant + customer）交过来，所以这里要**反查**身份拿到
    登录名/角色/门店。反查失败就拒绝：那种情况只可能来自"业务库重置了但 Redis 里
    还留着旧会话"，静默降级会让浏览器看到另一个客户的数据。
    """

    identity = resolve_identity_for_scope(
        await load_registry(), scope.tenant_id, scope.customer_id
    )
    return build_scoped_tools(
        identity=identity,
        session_factory=_SESSION_FACTORY,
    )


#: 模型可见的最终工具面：只有业务工具，框架工具（Bash/Read/团队/计划等）全部抑制。
#:
#: 两件事一起做才成立——``allowed_tool_names`` 是最后一道具体工具的允许清单，
#: 那些 ``allow_*`` 开关负责让延时来源根本不构造。只设前者的话，工作区工具仍会被
#: 建出来再被过滤掉，白白多一份依赖和一份出错面。
APPOINTMENT_TOOL_EXPOSURE_POLICY = ToolExposurePolicy(
    allowed_tool_names=frozenset(
        {"get_service_quote", "search_availability", "search_knowledge"}
    ),
    allow_workspace_tools=False,
    allow_planning_tools=False,
    allow_background_tools=False,
    allow_schedule_tools=False,
    allow_team_tools=False,
    allow_middleware_tools=False,
    allow_channel_tools=False,
    allow_skills=False,
    allow_mcps=False,
)


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------

_STORAGE = AppointmentRedisStorage(**redis_settings())
_MESSAGE_BUS = RedisMessageBus(**redis_settings())

app = create_app(
    storage=_STORAGE,
    message_bus=_MESSAGE_BUS,
    workspace_manager=LocalWorkspaceManager(basedir=str(WORKSPACE_BASEDIR)),
    # 身份：开发期用 X-User-ID 头（web_ui 的 setup 页就是这么做的）。
    # 生产要换成 identity_provider=BearerPrincipalProvider(...)。
    identity_provider=None,
    tool_scope_resolver=appointment_tool_scope_resolver,
    scoped_extra_agent_tools=appointment_scoped_read_tools,
    # scoped_context_agent_tools 留给写入工具：它们需要 requester 与幂等键。
    tool_exposure_policy=APPOINTMENT_TOOL_EXPOSURE_POLICY,
    enable_index_worker=False,
    enable_channel_worker=False,
    enable_scheduler=False,
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
    title="星颜美业 · 预约助理",
)
install_booking_api(app)

# 框架的 lifespan 管进程级资源，但没有给业务方留注入点，所以这里包一层：
# 先读身份表并打印横幅，再跑框架自己的生命周期，退出时把业务库连接池关掉
# （不关的话本地看不出来，上到部署就是 socket 泄漏）。
_FRAMEWORK_LIFESPAN = app.router.lifespan_context


@asynccontextmanager
async def _appointment_lifespan(application: Any) -> AsyncIterator[Any]:
    application.state.appointment_identity_registry = await _load_registry_or_explain()
    print(describe_startup(), flush=True)
    async with _FRAMEWORK_LIFESPAN(application) as state:
        try:
            yield state
        finally:
            await dispose_engine()


app.router.lifespan_context = _appointment_lifespan


@app.get("/demo/identities", include_in_schema=False)
async def demo_identities() -> dict[str, Any]:
    """列出本机可登录的演示身份。

    仅用于本地演示：登录页需要知道该填什么用户名，而"等一下它会告诉你"比让人对着
    403 猜要好。**只在开发期身份模式下开放**——一旦配了 ``identity_provider``，
    这个接口就没有必要存在，直接 404，免得它变成生产环境的用户名枚举面。
    """

    if app.state.identity_provider is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    registry = loaded_registry()
    return {
        "mode": "development",
        "chat_capable": [
            {"user_id": i.user_id, "display_name": i.display_name, "role": i.role}
            for i in sorted(registry.all(), key=lambda i: i.user_id)
            if i.customer_id is not None
        ],
        "not_chat_capable": [
            {
                "user_id": i.user_id,
                "display_name": i.display_name,
                "role": i.role,
                "reason": "该角色没有客户作用域，不参与顾客侧对话",
            }
            for i in sorted(registry.all(), key=lambda i: i.user_id)
            if i.customer_id is None
        ],
    }


async def _load_registry_or_explain() -> IdentityRegistry:
    """读身份表；失败时给出能直接照做的下一步。

    放在 lifespan 里而不是模块导入期执行：导入不该产生数据库连接，否则连
    ``--help`` 这类一次性调用都会去连库。
    """

    try:
        return await load_registry()
    except Exception as exc:  # pylint: disable=broad-except
        raise RuntimeError(
            "无法读取业务库身份表。请先运行 "
            ".venv/bin/python scripts/dev_reset.py 灌入种子数据。"
        ) from exc


def describe_startup() -> str:
    """启动横幅：把"这一版做什么、不做什么"直接印出来。"""

    registry = _REGISTRY
    credential = deepseek_credential_from_env()
    lines = [
        "",
        "  星颜美业 · 预约助理（浏览器演示）",
        "",
        f"  模型        {'DeepSeek ' + DEFAULT_MODEL if credential else '未配置（缺 DEEPSEEK_API_KEY）'}",
        f"  Redis       {redis_settings()['host']}:{redis_settings()['port']}"
        f"/{redis_settings()['db']}",
        f"  工作区      {WORKSPACE_BASEDIR}",
        "  工具面      只读：get_service_quote / search_availability / search_knowledge",
        "  认证        开发期模式（X-User-ID 头），不是生产认证",
    ]
    if registry is not None:
        lines.append("")
        lines.append("  可登录的演示身份：")
        lines.append(registry.describe())
    lines.append("")
    lines.append("  用法：web_ui 的 setup 页填本服务地址，用户名填上面的登录名。")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        # 默认 8010，不是 3000：本机的 3000 已经被 observability 那套 Grafana
        # 占着。web_ui 的 setup 页接受任意地址，填 http://127.0.0.1:8010 即可。
        port=int(os.getenv("PORT", "8010")),
        reload=False,
    )


if __name__ == "__main__":
    main()
