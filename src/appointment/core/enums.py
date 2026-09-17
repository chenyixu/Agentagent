"""受控枚举。

设计稿 §6.2：会话状态、任务状态、业务状态不能混成一个字段。这里把三类状态
分别定义，并用不同的枚举承载。
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------------------
# 任务状态（设计稿 §6.2 与 §18.2）
# --------------------------------------------------------------------------
class TaskState(StrEnum):
    """业务任务状态。一个 task 可跨多轮 reply，与 SDK 的规划任务无关。"""

    COLLECTING = "COLLECTING"
    SEARCHING = "SEARCHING"
    PROPOSED = "PROPOSED"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    COMMITTING = "COMMITTING"
    SUCCEEDED = "SUCCEEDED"
    # 旁路
    NEEDS_REPLAN = "NEEDS_REPLAN"
    WAITING_USER = "WAITING_USER"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    WAITING_RESULT = "WAITING_RESULT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"


TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)

#: 处于这些状态的任务持久等待用户或外部，不占用活跃执行位置（设计稿 §3.3）。
PARKED_TASK_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.WAITING_USER,
        TaskState.WAITING_CONFIRMATION,
        TaskState.WAITING_EXTERNAL,
        TaskState.WAITING_RESULT,
        TaskState.HUMAN_TAKEOVER,
    }
)


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_TASK_STATES


# --------------------------------------------------------------------------
# 槽位（设计稿 §2.1）
# --------------------------------------------------------------------------
class SlotOp(StrEnum):
    """槽位补丁操作。未出现该槽位表示不修改。"""

    SET = "SET"
    CLEAR = "CLEAR"
    NO_PREFERENCE = "NO_PREFERENCE"


class SlotIntent(StrEnum):
    """持久槽位记录的意图。CLEAR 是明确撤销，NO_PREFERENCE 是交由系统选择。"""

    VALUE = "VALUE"
    CLEARED = "CLEARED"
    NO_PREFERENCE = "NO_PREFERENCE"


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


class SlotName(StrEnum):
    STORE = "store"
    SERVICE = "service"
    TIME_WINDOW = "time_window"
    RESOURCE_PREFERENCE = "resource_preference"
    BUDGET = "budget"
    DURATION = "duration"


# --------------------------------------------------------------------------
# 方案 / 占位 / 占用 / 订单（设计稿 §5）
# --------------------------------------------------------------------------
class ProposalAction(StrEnum):
    CREATE = "CREATE"
    RESCHEDULE = "RESCHEDULE"
    CANCEL = "CANCEL"


class ProposalStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"
    COMMITTED = "COMMITTED"
    EXPIRED = "EXPIRED"


class HoldState(StrEnum):
    HELD = "HELD"
    BOOKED = "BOOKED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class AllocationState(StrEnum):
    """resource_allocation 状态。

    HELD 必须 hold_id/expires_at 且 appointment_id 为空；
    BOOKED 必须 appointment_id 且 expires_at 为空。
    """

    HELD = "HELD"
    BOOKED = "BOOKED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


#: 参与排他约束的状态（设计稿 §5.1）。
BLOCKING_ALLOCATION_STATES: frozenset[AllocationState] = frozenset(
    {AllocationState.HELD, AllocationState.BOOKED}
)


class AppointmentStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"


class FulfillmentStatus(StrEnum):
    """履约可达性。DISRUPTED 不等于取消（设计稿 §5.1）。"""

    READY = "READY"
    DISRUPTED = "DISRUPTED"


# --------------------------------------------------------------------------
# 确认 / 操作（设计稿 §5.2）
# --------------------------------------------------------------------------
class ConfirmationStatus(StrEnum):
    ISSUED = "ISSUED"
    CONFIRMED = "CONFIRMED"
    CONSUMED = "CONSUMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class OperationStatus(StrEnum):
    REGISTERED = "REGISTERED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_FINAL = "FAILED_FINAL"
    #: 效果未查清，不是普通可重试失败。保留原键并先查主库。
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# 等待（设计稿 §5.4）
# --------------------------------------------------------------------------
class WaitingKind(StrEnum):
    CLARIFICATION = "CLARIFICATION"
    CONFIRMATION = "CONFIRMATION"
    EXTERNAL = "EXTERNAL"


class WaitingStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class HandoffStatus(StrEnum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    CLOSED = "CLOSED"


# --------------------------------------------------------------------------
# 异步交付（设计稿 §6）
# --------------------------------------------------------------------------
class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    #: 仅说明消费者已持久受理，不代表用户已收到通知。
    PROCESSED = "PROCESSED"
    DEAD_LETTER = "DEAD_LETTER"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    #: 供应商受理，不等于送达。
    ACCEPTED = "ACCEPTED"
    DELIVERED = "DELIVERED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    UNKNOWN = "UNKNOWN"
    SUPERSEDED = "SUPERSEDED"


class ReconcileStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    GAVE_UP = "GAVE_UP"


class ReceiptProcessingStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    #: 回调先到、暂时无法映射时持久入隔离队列。
    QUARANTINED = "QUARANTINED"


# --------------------------------------------------------------------------
# 工具与模型（设计稿 §9、§5.2）
# --------------------------------------------------------------------------
class ToolStatus(StrEnum):
    OK = "OK"
    PENDING = "PENDING"
    ERROR = "ERROR"
    #: 效果未查清，不是普通可重试失败。
    UNKNOWN = "UNKNOWN"


class UsageStatus(StrEnum):
    RESERVED = "RESERVED"
    REPORTED = "REPORTED"
    ESTIMATED = "ESTIMATED"
    #: 缺失 usage 标记 unknown，不计为零。
    UNKNOWN = "UNKNOWN"


class PermissionMode(StrEnum):
    DEFAULT = "DEFAULT"
    EXPLORE = "EXPLORE"
    ASK = "ASK"
    DONT_ASK = "DONT_ASK"


class AgentRole(StrEnum):
    RECEPTION = "reception"
    CONSULTANT = "consultant"
    SYSTEM = "system"


class ExecutionEndReason(StrEnum):
    COMPLETED = "COMPLETED"
    WAITING = "WAITING"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    LOOP_DETECTED = "LOOP_DETECTED"
    ERROR = "ERROR"
    SUPERSEDED = "SUPERSEDED"


class Intent(StrEnum):
    """入口分类结果。分类不授予任何权限（设计稿 §5）。"""

    CONSULT = "consult"
    BOOK = "book"
    MODIFY = "modify"
    CANCEL = "cancel"
    HANDOFF = "handoff"
    UNKNOWN = "unknown"


class ErrorCode(StrEnum):
    """统一错误枚举（设计稿 §9）。

    对无权访问的对象统一返回 NOT_FOUND，避免泄漏存在性。
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    SLOT_CONFLICT = "SLOT_CONFLICT"
    HOLD_EXPIRED = "HOLD_EXPIRED"
    STALE_PROPOSAL = "STALE_PROPOSAL"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    HUMAN_TAKEOVER_ACTIVE = "HUMAN_TAKEOVER_ACTIVE"
    LEASE_LOST = "LEASE_LOST"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# --------------------------------------------------------------------------
# 事件（设计稿 §2.3）
# --------------------------------------------------------------------------
class TaskEventType(StrEnum):
    TASK_STATE_CHANGED = "task_state_changed"
    SLOTS_UPDATED = "slots_updated"
    CLARIFICATION_REQUIRED = "clarification_required"
    CANDIDATES_READY = "candidates_ready"
    PROPOSAL_PUBLISHED = "proposal_published"
    PROPOSAL_INVALIDATED = "proposal_invalidated"
    APPOINTMENT_COMMITTED = "appointment_committed"
    APPOINTMENT_UPDATED = "appointment_updated"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    OPERATION_UNKNOWN = "operation_unknown"
    HANDOFF_CHANGED = "handoff_changed"
    ASSISTANT_MESSAGE = "assistant_message"
    REPLY_END = "reply_end"
    ERROR = "error"


#: 由已提交事务产生的持久业务事件。不能由 ReplyEndEvent 推断（设计稿 §9）。
BUSINESS_EVENT_TYPES: frozenset[TaskEventType] = frozenset(
    {
        TaskEventType.PROPOSAL_PUBLISHED,
        TaskEventType.PROPOSAL_INVALIDATED,
        TaskEventType.APPOINTMENT_COMMITTED,
        TaskEventType.APPOINTMENT_UPDATED,
        TaskEventType.APPOINTMENT_CANCELLED,
        TaskEventType.HANDOFF_CHANGED,
    }
)


class Channel(StrEnum):
    SMS = "sms"
    WECHAT = "wechat"
    EMAIL = "email"
    PUSH = "push"


class DisruptionStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVING = "RESOLVING"
    RESOLVED = "RESOLVED"


class DisruptionItemStatus(StrEnum):
    PENDING = "PENDING"
    NOTIFIED = "NOTIFIED"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
