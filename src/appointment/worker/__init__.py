"""Worker 与通知投递账本。

对外只暴露三样东西：工作循环、供应商端口、回执入口。内部的 ``claims`` /
``delivery`` / ``receipts`` 是职责边界而不是可以使用的方式——把租约领取抄到别处
去，fencing token 的语义就失去意义了。
"""

from __future__ import annotations

from .claims import Lease, LeaseLost, claim_jobs, claim_outbox, finish_job, finish_outbox
from .delivery import (
    DeliveryAttemptResult,
    attempt_delivery,
    corrected_template_version,
    enqueue_delivery,
    provider_idempotency_key,
    reconcile_delivery,
)
from .providers import (
    NotificationProviderPort,
    ProviderTimeout,
    SandboxProvider,
    SendOutcome,
    SendRequest,
    build_provider,
)
from .receipts import (
    ProviderAccountConfig,
    ReceiptResult,
    ingest_receipt,
    reprocess_quarantined,
    resolve_status,
    sandbox_account,
)
from .runner import (
    JOB_ALERT_MANUAL,
    JOB_NOTIFICATION_RECONCILE,
    JOB_NOTIFICATION_SEND,
    KNOWN_JOB_KINDS,
    TEMPLATE_BY_EVENT,
    Worker,
    WorkerReport,
)

__all__ = [
    "DeliveryAttemptResult",
    "JOB_ALERT_MANUAL",
    "JOB_NOTIFICATION_RECONCILE",
    "JOB_NOTIFICATION_SEND",
    "KNOWN_JOB_KINDS",
    "Lease",
    "LeaseLost",
    "NotificationProviderPort",
    "ProviderAccountConfig",
    "ProviderTimeout",
    "ReceiptResult",
    "SandboxProvider",
    "SendOutcome",
    "SendRequest",
    "TEMPLATE_BY_EVENT",
    "Worker",
    "WorkerReport",
    "attempt_delivery",
    "build_provider",
    "claim_jobs",
    "claim_outbox",
    "corrected_template_version",
    "enqueue_delivery",
    "finish_job",
    "finish_outbox",
    "ingest_receipt",
    "provider_idempotency_key",
    "reconcile_delivery",
    "reprocess_quarantined",
    "resolve_status",
    "sandbox_account",
]
