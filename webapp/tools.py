"""浏览器侧的业务工具：模型只看得到业务参数。

这里的每一层都在守同一条线：**可信身份不经过模型**。

- 模型可见的函数签名里只有业务参数（项目名称、时间、条数）。没有 tenant、没有
  customer、没有 store——它们在工具**构造时**由服务端从 :class:`ToolScope` 绑定，
  被闭包捕获。模型即使被告知"请传 tenant_id"，也没有那个参数可传。
- 工具体不重新实现业务规则，而是调用 ``appointment.tools.registry.invoke_tool``。
  白名单、参数校验、权限、领域规则都在那一处收口，和编排器走的是同一条路。
  这样"浏览器看到的工具"和"编排器看到的工具"不会长成两套语义。
- 项目名称走 ``resolve_service_by_text``：只做名称/别名匹配，不足以判断时返回空，
  由模型追问。这正是设计稿 §7 第 3 步的行为，不在这里做模糊猜测。

**当前只暴露只读工具**。``create_hold`` / ``confirm_appointment`` 这类写工具会走
``scoped_context_agent_tools``（多拿 requester 与 idempotency_key），因为它们需要
审计关联与幂等键；在那之前，浏览器这一侧不会产生任何业务副作用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool, ToolBase

from appointment.core.enums import ToolStatus
from appointment.domain.catalog import resolve_service_by_text
from appointment.domain.context import TrustedContext
from appointment.domain.timeutil import load_zone
from appointment.seed import STORE_TIMEZONE
from appointment.tools.registry import invoke_tool

from .identity import BusinessIdentity

#: 演示用的发布标签。真实环境来自发布清单。
DEMO_RELEASE_ID = "release-local-1"

_READ_ONLY_PERMISSION = PermissionDecision(
    behavior=PermissionBehavior.ALLOW,
    message="只读查询：查询门店项目、报价、可用时段与政策。",
    decision_reason="These tools read catalog, availability and policy facts only.",
)


def _parse_moment(raw: str, *, tz_name: str = STORE_TIMEZONE) -> datetime:
    """把模型给的时间解析成带时区的时刻。

    边界刻意宽容一点：模型很可能给出不带偏移的 ``2026-09-18T14:00``，直接拒绝会
    变成难以理解的失败。不带偏移时按门店时区解释——这是唯一一个能被本地演示
    接受的解释，且它对应"用户说的下午三点"这个真实语义。
    """

    text = (raw or "").strip().replace("Z", "+00:00")
    if not text:
        raise ValueError("时间不能为空")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间 {raw!r}，请用 ISO 8601（如 2026-09-18T14:00+08:00）") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=load_zone(tz_name))
    return moment


class ScopedAppointmentTools:
    """为一个已认证客户装配的预约工具集。

    实例本身持有闭包捕获的 ``identity``，因此工具的"身份"在装配那一刻就固定了，
    与模型后续说什么无关。
    """

    def __init__(
        self,
        *,
        identity: BusinessIdentity,
        session_factory: async_sessionmaker[AsyncSession],
        release_id: str = DEMO_RELEASE_ID,
    ) -> None:
        self._identity = identity
        self._session_factory = session_factory
        self._release_id = release_id

    # ---------------- 内部：构造可信上下文并走统一入口 ----------------

    def _context(self, *, request_id: str) -> TrustedContext:
        """由服务端身份构造可信上下文。工具参数里没有这些字段的位置。"""

        return TrustedContext(
            tenant_id=self._identity.tenant_id,
            actor_id=self._identity.actor_id,
            role=self._identity.role,
            request_id=request_id,
            release_id=self._release_id,
            customer_id=self._identity.customer_id,
            store_scopes=(self._identity.store_id,),
        )

    async def _resolve_service(self, session: AsyncSession, text: str) -> list[Any]:
        return await resolve_service_by_text(
            session,
            tenant_id=self._identity.tenant_id,
            store_id=self._identity.store_id,
            text=text,
        )

    async def _call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """统一的"开事务 → 调白名单工具 → 归一化结果"路径。

        每次工具调用用独立的会话，与 AgentScope 的会话存储（Redis）无关：业务事实
        只在业务库里，两边不共享事务。
        """

        request_id = f"webapp:{self._identity.user_id}:{uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            result = await invoke_tool(
                session,
                self._context(request_id=request_id),
                tool_name,
                arguments,
                now=now,
            )
            await session.rollback()  # 只读路径不留下任何未提交状态

        payload: dict[str, Any] = dict(result.data or {})
        payload["ok"] = result.status is ToolStatus.OK
        if result.status is not ToolStatus.OK:
            # 失败必须说成失败，不能描述成"没有号"（设计稿 §11）。
            payload["error_code"] = (
                None if result.error_code is None else result.error_code.value
            )
            payload["error_message"] = result.error_message
        return payload

    # ---------------- 模型可见：只读工具 ----------------

    def build(self) -> list[ToolBase]:
        tools: list[ToolBase] = [
            self._service_quote_tool(),
            self._availability_tool(),
            self._knowledge_tool(),
        ]
        return tools

    def _service_quote_tool(self) -> ToolBase:
        identity = self._identity

        async def get_service_quote(service: str) -> dict[str, Any]:
            """查询本项目在当前门店的当前价格与时长。只读，不锁价。"""

            async with self._session_factory() as session:
                matches = await self._resolve_service(session, service)
            if not matches:
                return {
                    "ok": False,
                    "matched": False,
                    "message": f"没有找到与「{service}」对应的项目，请向用户确认项目名称。",
                }
            if len(matches) > 1:
                return {
                    "ok": False,
                    "matched": False,
                    "ambiguous": [
                        {"service_id": str(item.id), "name": item.name}
                        for item in matches
                    ],
                    "message": "该项目名称对应多个服务，请让用户确认具体是哪一个。",
                }
            resolved = matches[0]
            return await self._call(
                "get_service_quote",
                {"store_id": str(identity.store_id), "service_id": str(resolved.id)},
            )

        return FunctionTool(
            get_service_quote,
            name="get_service_quote",
            description=(
                "查询一个服务项目在本门店的当前价格与时长。只传项目名称，"
                "不要传门店号或租户号。项目名称对应多个服务时会返回候选，"
                "此时应当询问用户具体项目。"
            ),
            is_read_only=True,
            permission=_READ_ONLY_PERMISSION,
        )

    def _availability_tool(self) -> ToolBase:
        identity = self._identity

        async def search_availability(
            service: str,
            window_start: str,
            window_end: str,
            limit: int = 5,
        ) -> dict[str, Any]:
            """查询某项目在时间窗口内的可用时段。只读快照，不构成资源保证。

            ``window_start`` / ``window_end`` 用 ISO 8601。不带时区偏移时按门店
            本地时区（Asia/Shanghai）解释。
            """

            try:
                start_at = _parse_moment(window_start)
                end_at = _parse_moment(window_end)
                wanted = max(1, min(int(limit), 10))
            except ValueError as exc:
                return {"ok": False, "error_code": "VALIDATION_ERROR", "error_message": str(exc)}

            async with self._session_factory() as session:
                matches = await self._resolve_service(session, service)
            if not matches:
                return {
                    "ok": False,
                    "matched": False,
                    "message": f"没有找到与「{service}」对应的项目，请向用户确认项目名称。",
                }
            if len(matches) > 1:
                return {
                    "ok": False,
                    "matched": False,
                    "ambiguous": [
                        {"service_id": str(item.id), "name": item.name}
                        for item in matches
                    ],
                    "message": "该项目名称对应多个服务，请让用户确认具体是哪一个。",
                }

            return await self._call(
                "search_availability",
                {
                    "store_id": str(identity.store_id),
                    "service_id": str(matches[0].id),
                    "window_start": start_at.isoformat(),
                    "window_end": end_at.isoformat(),
                    "limit": wanted,
                },
            )

        return FunctionTool(
            search_availability,
            name="search_availability",
            description=(
                "查询某个服务项目在给定时间窗口内的可用时段候选。只传项目名称与"
                "时间窗口，不要传门店号或租户号。结果是只读快照，不代表已经占位；"
                "要占位必须走确认流程。窗口通常取用户说的'某天下午'这一类范围。"
            ),
            is_read_only=True,
            permission=_READ_ONLY_PERMISSION,
        )

    def _knowledge_tool(self) -> ToolBase:
        identity = self._identity

        async def search_knowledge(query: str, top_k: int = 3) -> dict[str, Any]:
            """检索门店政策、服务说明与注意事项。只读。"""

            return await self._call(
                "search_knowledge",
                {
                    "query": query,
                    "store_id": str(identity.store_id),
                    "top_k": max(1, min(int(top_k), 8)),
                },
            )

        return FunctionTool(
            search_knowledge,
            name="search_knowledge",
            description=(
                "检索本门店的取消改约政策、服务说明与到店注意事项。用于回答"
                "'取消要收费吗''要注意什么'这类问题。检索不到证据时必须如实说"
                "不知道，不要根据常识编造政策。"
            ),
            is_read_only=True,
            permission=_READ_ONLY_PERMISSION,
        )


def build_scoped_tools(
    *,
    identity: BusinessIdentity,
    session_factory: async_sessionmaker[AsyncSession],
) -> list[ToolBase]:
    """装配一个客户作用域下的预约工具集。"""

    return ScopedAppointmentTools(
        identity=identity, session_factory=session_factory
    ).build()
