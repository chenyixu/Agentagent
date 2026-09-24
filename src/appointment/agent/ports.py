"""Agent 层的五个端口（设计稿 §4）。

分层的意义在于**依赖方向**：编排器只依赖端口，不依赖具体模型、SDK 或数据库。
因此可以在同一套业务规则下替换运行时，并用确定性基线对 SDK 方案做对照验证。

- :class:`AgentRuntimePort` —— 模型/SDK 运行时。确定性基线与 AgentScope 适配器
  都是它的实现；换框架不改业务规则。
- :class:`WorkflowPort` —— 编排进度的读写（任务快照、事件）。供 API 层与恢复用。
- :class:`BookingPort` —— 预约事实的**只读**访问。写入一律走工具边界，
  避免 Agent 绕过权限与确认凭据直接改库。
- :class:`KnowledgePort` —— 知识检索。咨询 Agent 用它，因此必须带 ACL。
- :class:`NotificationPort` —— 通知投递。Worker 用它，与业务事务分离。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable
from uuid import UUID

from ..core.clock import Clock
from ..core.enums import Intent, TaskState
from ..domain.context import TrustedContext


# ---------------------------------------------------------------------------
# AgentRuntimePort 的输入输出
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolRequest:
    """模型提出的工具调用请求。参数尚未校验，由工具边界裁决。"""

    tool_name: str
    arguments: dict[str, Any]
    #: 模型给出的调用理由。只用于诊断，不参与任何授权判断。
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """一次"活跃执行"的输入。

    刻意不包含：完整聊天历史、旧的 SDK 挂起 reply、任何授权材料。等待后需要重建
    上下文时，只从可信账本重新构建（设计稿 §5.4）。
    """

    ctx: TrustedContext
    task_id: UUID
    task_state: TaskState
    task_version: int
    slots: dict[str, Any]
    user_message: str | None
    #: 允许调用的工具名。由状态与角色推导，未列入的一律拒绝。
    allowed_tools: tuple[str, ...]
    #: 已完成工具结果的摘要（重建式恢复只注入已完成的，不注入挂起调用）。
    completed_actions: tuple[dict[str, Any], ...] = ()
    #: 已确认的业务事实（如报价、候选），供模型解释而不需要重新查询。
    facts: dict[str, Any] = field(default_factory=dict)
    #: 本次活跃执行中**刚刚**由工具算出的事实键（``quote`` / ``availability``）。
    #: 运行时用它区分"我刚查出来的候选"和"用户已经看过的候选"：前者只展示，
    #: 后者才允许把用户的答复理解成选定。
    fresh_fact_keys: frozenset[str] = frozenset()
    #: 追问/等待后的用户答复，用于恢复。
    waiting_answer: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TurnOutput:
    """模型输出。未知字段与非法枚举不得静默采用（设计稿 §5.1 第 4 步）。"""

    reply_text: str | None = None
    slot_patches: tuple[dict[str, Any], ...] = ()
    tool_requests: tuple[ToolRequest, ...] = ()
    clarification_question: str | None = None
    intent: Intent | None = None
    #: 本轮消耗的预算单位（token 折算）。缺失标记 unknown，不计为零。
    usage_units: int | None = None
    usage_status: str = "UNKNOWN"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """任务快照。上下文校验失败时用它从可信账本重建。"""

    task_id: UUID
    state: TaskState
    version: int
    epoch: int
    slots: dict[str, Any]
    current_proposal_id: UUID | None
    current_proposal_version: int | None
    store_id: UUID | None


@dataclass(frozen=True, slots=True)
class NotificationEnvelope:
    """一条待投递通知。幂等键必须由业务事件派生，不能由时间戳派生。"""

    channel: str
    recipient_ref: str
    template: str
    payload: dict[str, Any]
    idempotency_key: str
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    locale: str = "zh-CN"


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """供应商回执。ACCEPTED 只说明受理，不等于送达。"""

    provider_message_id: str
    status: str
    occurred_at: datetime
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 端口定义
# ---------------------------------------------------------------------------
@runtime_checkable
class AgentRuntimePort(Protocol):
    """模型/SDK 运行时。"""

    @property
    def runtime_name(self) -> str:
        """写入 execution_attempt 与 release manifest 的运行时标识。"""

    async def run_turn(self, request: TurnRequest) -> TurnOutput:
        """基于当前结构化任务产生下一步动作。

        实现不得读写数据库：一切业务写入都必须回到工具边界。
        """


@runtime_checkable
class WorkflowPort(Protocol):
    """编排进度的读写。"""

    async def load_snapshot(
        self, ctx: TrustedContext, *, task_id: UUID
    ) -> TaskSnapshot: ...

    async def load_events(
        self, ctx: TrustedContext, *, task_id: UUID, after_sequence: int, limit: int
    ) -> Sequence[dict[str, Any]]: ...


@runtime_checkable
class BookingPort(Protocol):
    """预约事实的只读访问。写入必须走工具边界。"""

    async def load_appointment_facts(
        self, ctx: TrustedContext, *, appointment_id: UUID
    ) -> dict[str, Any] | None: ...

    async def load_active_hold_facts(
        self, ctx: TrustedContext, *, task_id: UUID
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class KnowledgePort(Protocol):
    """知识检索。回答必须带证据；没有证据就要明确说证据不足。"""

    async def search(
        self,
        ctx: TrustedContext,
        *,
        query: str,
        store_id: UUID | None,
        top_k: int,
        now: datetime,
    ) -> dict[str, Any]: ...


@runtime_checkable
class NotificationPort(Protocol):
    """通知投递。与业务事务分离：业务事务只写 Outbox。"""

    @property
    def provider_name(self) -> str:
        """写入 provider_receipt / delivery_attempt 的供应商标识。"""

    async def send(self, envelope: NotificationEnvelope) -> DeliveryReceipt:
        """投递一条通知。

        失败必须用**同一条** :class:`NotificationEnvelope` 重试（同幂等键），
        不能换键重发。
        """

    async def query(self, provider_message_id: str) -> DeliveryReceipt | None:
        """按供应商消息 ID 查状态。用于"结果不明"时的对账，而不是盲目重发。"""


__all__ = [
    "AgentRuntimePort",
    "BookingPort",
    "DeliveryReceipt",
    "KnowledgePort",
    "NotificationEnvelope",
    "NotificationPort",
    "TaskSnapshot",
    "ToolRequest",
    "TurnOutput",
    "TurnRequest",
    "WorkflowPort",
]
