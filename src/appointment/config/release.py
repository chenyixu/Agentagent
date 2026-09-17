"""发布清单（release_manifest）。

设计稿 §12.4：发布包固定模型配置、提示词、工具 Schema、诊断快照 Schema、知识和
规则版本及检索索引版本。重建式恢复每次显式选择 ``task.release_id`` 的
``structured_schema``，不向旧 parked reply 传 HITL 事件。

回滚改变新任务的 release 指针，不直接重放历史写入或回滚数据库订单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.hashing import content_hash

SCHEMA_VERSION = 1

#: 业务规则版本。取消窗口、费用与截止值等关键文案由结构化配置渲染，
#: 运营不手写第二份数值（设计稿 §10.1）。
RULES_V1: dict[str, Any] = {
    "rule_version": "rules-2026-09-17",
    "cancel_window_hours": 4,
    "cancel_fee_minor": 0,
    "no_show_grace_minutes": 15,
    "hold_ttl_seconds": 180,
    "booking_horizon_days": 30,
    "max_active_holds_per_customer": 1,
}

#: 委派与预算上限（设计稿 §5.1，属可测量起点而非已验证最优值）。
BUDGET_V1: dict[str, Any] = {
    "max_tool_calls": 6,
    "max_consult_delegations": 2,
    "structured_output_repairs": 1,
    "turn_budget_seconds": 20,
}

#: 确定性排序权重（设计稿 §10.3）。缺失特征需重新归一化，不虚构评价。
RECOMMENDATION_WEIGHTS_V1: dict[str, float] = {
    "skill_match": 0.35,
    "explicit_preference": 0.25,
    "time_fit": 0.20,
    "price_fit": 0.10,
    "reputation": 0.10,
}


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """平台级不可变配置。撤回发布不修改已存 manifest。"""

    release_id: str
    schema_version: int = SCHEMA_VERSION
    model_backend: str = "stub"
    model_name: str = "qwen-plus"
    prompt_version: str = "prompts-v1"
    tool_schema_version: str = "tools-v1"
    structured_schema_version: str = "structured-v1"
    snapshot_schema_version: str = "snapshot-v1"
    knowledge_version: str = "knowledge-v1"
    rule_version: str = "rules-2026-09-17"
    rules: dict[str, Any] = field(default_factory=lambda: dict(RULES_V1))
    budget: dict[str, Any] = field(default_factory=lambda: dict(BUDGET_V1))
    weights: dict[str, float] = field(
        default_factory=lambda: dict(RECOMMENDATION_WEIGHTS_V1)
    )

    def as_manifest(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "schema_version": self.schema_version,
            "model_backend": self.model_backend,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "tool_schema_version": self.tool_schema_version,
            "structured_schema_version": self.structured_schema_version,
            "snapshot_schema_version": self.snapshot_schema_version,
            "knowledge_version": self.knowledge_version,
            "rule_version": self.rule_version,
            "rules": self.rules,
            "budget": self.budget,
            "weights": self.weights,
        }

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.as_manifest())


def default_release() -> ReleaseManifest:
    from ..config.settings import get_settings

    settings = get_settings()
    return ReleaseManifest(
        release_id="release-local-1",
        model_backend=settings.model_backend,
        model_name=settings.model_name,
        rules={
            **RULES_V1,
            "hold_ttl_seconds": settings.hold_ttl_seconds,
            "max_active_holds_per_customer": settings.max_active_holds_per_customer,
        },
        budget={
            **BUDGET_V1,
            "max_tool_calls": settings.max_tool_calls,
            "max_consult_delegations": settings.max_consult_delegations,
            "turn_budget_seconds": settings.turn_budget_seconds,
        },
    )
