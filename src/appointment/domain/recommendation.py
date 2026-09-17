"""推荐排序（设计稿 §10.3）。

先执行硬过滤：租户/门店、合法服务、技能、营业和班次、资源容量、预算、用户明确
禁选项；再做排序。起步权重为技能匹配 0.35、显式偏好 0.25、时间贴合 0.20、
价格贴合 0.10、可用评价 0.10。

它只是可解释基线：缺失特征需重新归一化，不虚构评价。排序结果返回分数组成和
证据，LLM 只负责解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from ..config.release import RECOMMENDATION_WEIGHTS_V1

FEATURE_LABELS = {
    "skill_match": "技能匹配",
    "explicit_preference": "显式偏好",
    "time_fit": "时间贴合",
    "price_fit": "价格贴合",
    "reputation": "可用评价",
}


@dataclass(slots=True)
class ScoreInput:
    """一个候选的原始特征。``None`` 表示该特征缺失，需要重新归一化。"""

    skill_match: float | None = None
    explicit_preference: float | None = None
    time_fit: float | None = None
    price_fit: float | None = None
    reputation: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "skill_match": self.skill_match,
            "explicit_preference": self.explicit_preference,
            "time_fit": self.time_fit,
            "price_fit": self.price_fit,
            "reputation": self.reputation,
        }


@dataclass(slots=True)
class ScoreResult:
    score: float
    breakdown: dict[str, float]
    missing_features: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "breakdown": {k: round(v, 4) for k, v in self.breakdown.items()},
            "missing_features": self.missing_features,
            "evidence": self.evidence,
        }


def score_candidate(
    raw: ScoreInput,
    *,
    weights: Mapping[str, float] | None = None,
) -> ScoreResult:
    """归一化加权打分。

    缺失特征不按 0 计，而是把它们的权重重新分配给其余特征——否则"没有评价数据"
    会变成"评价最差"。
    """

    active_weights = dict(weights or RECOMMENDATION_WEIGHTS_V1)
    values = raw.as_dict()
    present = {
        name: value for name, value in values.items() if value is not None
    }
    missing = [name for name, value in values.items() if value is None]

    if not present:
        return ScoreResult(score=0.0, breakdown={}, missing_features=missing)

    total_weight = sum(active_weights.get(name, 0.0) for name in present)
    if total_weight <= 0:
        return ScoreResult(score=0.0, breakdown={}, missing_features=missing)

    breakdown: dict[str, float] = {}
    score = 0.0
    for name, value in present.items():
        weight = active_weights.get(name, 0.0) / total_weight
        clipped = max(0.0, min(1.0, float(value)))
        contribution = weight * clipped
        breakdown[name] = contribution
        score += contribution

    return ScoreResult(
        score=score,
        breakdown=breakdown,
        missing_features=missing,
    )


def time_fit(
    *, candidate_start: datetime, desired_start: datetime | None, window_seconds: float
) -> float | None:
    """时间贴合度。没有期望时间时该特征缺失。"""

    if desired_start is None or window_seconds <= 0:
        return None
    delta = abs((candidate_start - desired_start).total_seconds())
    return max(0.0, 1.0 - min(delta / window_seconds, 1.0))


def price_fit(*, amount_minor: int, budget_minor: int | None) -> float | None:
    """价格贴合度。用户未表达预算时该特征缺失，不虚构。"""

    if not budget_minor or budget_minor <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - (amount_minor / budget_minor)))


def explicit_preference_fit(
    *,
    resource_id: UUID,
    preferred_resource_ids: list[UUID],
    attribute_match: bool | None,
) -> float | None:
    """显式偏好贴合度。

    - 用户点名了技师且就是这位：1.0
    - 点名了别人（说明这是替代候选）：0.0
    - 只说属性（如"女技师"）：命中 1.0，未命中 0.0
    - 完全没表达偏好：特征缺失
    """

    if preferred_resource_ids:
        return 1.0 if resource_id in preferred_resource_ids else 0.0
    if attribute_match is None:
        return None
    return 1.0 if attribute_match else 0.0


def format_reasons(result: ScoreResult) -> list[str]:
    """把分数组成转成可读理由，供 Agent 解释（LLM 只解释，不生成事实）。"""

    reasons: list[str] = []
    for name, contribution in sorted(
        result.breakdown.items(), key=lambda kv: kv[1], reverse=True
    ):
        label = FEATURE_LABELS.get(name, name)
        reasons.append(f"{label}贡献 {contribution:.2f}")
    for name in result.missing_features:
        label = FEATURE_LABELS.get(name, name)
        reasons.append(f"{label}：无可用数据，未参与打分")
    return reasons
