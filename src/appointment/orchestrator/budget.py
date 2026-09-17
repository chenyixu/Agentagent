"""回合预算与循环止损（设计稿 §5.1 第 6 步、§11）。

设计稿明确：**不能只靠"最大轮数"止损**。相同工具与参数连续失败、或连续多轮没有
状态进展时，必须立即退出循环、追问或转人工。因此这里同时跟踪三类信号：

- 计数上限：工具调用次数、咨询委派次数、结构化输出修复次数；
- 预算：活跃执行时长（不含等待用户）；
- 无进展：同一 (工具名, 参数哈希) 重复出现，或槽位/状态连续无变化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ..config.settings import Settings
from ..core.enums import ExecutionEndReason
from ..core.hashing import content_hash


@dataclass(slots=True)
class TurnBudget:
    """一次活跃执行的预算账本。"""

    max_tool_calls: int = 6
    max_consult_delegations: int = 2
    max_timestamps_repairs: int = 1
    budget_seconds: float = 20.0
    #: 同一 (工具, 参数) 重复出现的容忍次数。超过即判定为循环。
    repeat_tolerance: int = 2

    tool_calls: int = 0
    consult_delegations: int = 0
    repairs: int = 0
    no_progress_rounds: int = 0
    seen_calls: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings) -> "TurnBudget":
        return cls(
            max_tool_calls=settings.max_tool_calls,
            max_consult_delegations=settings.max_consult_delegations,
            budget_seconds=settings.turn_budget_seconds,
        )

    # ---------------- 计数 ----------------
    def can_call_tool(self) -> bool:
        return self.tool_calls < self.max_tool_calls

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def can_consult(self) -> bool:
        return self.consult_delegations < self.max_consult_delegations

    def record_consult(self) -> None:
        self.consult_delegations += 1

    def can_repair(self) -> bool:
        return self.repairs < self.max_timestamps_repairs

    def record_repair(self) -> None:
        self.repairs += 1

    # ---------------- 循环检测 ----------------
    def observe_call(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """登记一次工具调用，返回 ``True`` 表示这是重复调用。

        用参数内容哈希而不是参数对象本身做键：同一语义的不同字段顺序不应被当成
        进展，而同一工具查不同时段是合法的新调用。
        """

        key = f"{tool_name}:{content_hash(arguments) if arguments else '-'}"
        count = self.seen_calls.get(key, 0) + 1
        self.seen_calls[key] = count
        return count > 1

    def is_looping(self) -> bool:
        return any(count > self.repeat_tolerance for count in self.seen_calls.values())

    def observe_progress(self, *, progressed: bool) -> None:
        self.no_progress_rounds = 0 if progressed else self.no_progress_rounds + 1

    def is_stalled(self, *, limit: int = 2) -> bool:
        return self.no_progress_rounds >= limit

    # ---------------- 预算 ----------------
    def elapsed(self, *, started_at, now) -> timedelta:
        return now - started_at

    def over_time(self, *, started_at, now) -> bool:
        return self.elapsed(started_at=started_at, now=now) > timedelta(
            seconds=self.budget_seconds
        )

    def stop_reason(
        self, *, started_at, now
    ) -> ExecutionEndReason | None:
        """返回应当立即结束活跃执行的原因，``None`` 表示可以继续。"""

        if self.is_looping():
            return ExecutionEndReason.LOOP_DETECTED
        if self.is_stalled():
            return ExecutionEndReason.LOOP_DETECTED
        if self.over_time(started_at=started_at, now=now):
            return ExecutionEndReason.BUDGET_EXHAUSTED
        if not self.can_call_tool():
            return ExecutionEndReason.BUDGET_EXHAUSTED
        return None

    def snapshot(self) -> dict[str, Any]:
        """诊断快照。仅用于诊断，不承担跨等待续跑权威（设计稿 §11）。"""

        return {
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "consult_delegations": self.consult_delegations,
            "repairs": self.repairs,
            "no_progress_rounds": self.no_progress_rounds,
            "distinct_calls": len(self.seen_calls),
            "repeated_calls": sum(
                1 for count in self.seen_calls.values() if count > 1
            ),
        }


__all__ = ["TurnBudget"]
