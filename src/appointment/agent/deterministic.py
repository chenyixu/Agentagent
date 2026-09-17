"""确定性状态机基线（设计稿 §4.1、§2.2）。

它是**必须有**的可运行基线：不依赖模型，只依赖结构化任务状态、已知事实与
白名单工具。作用有两个：

1. 在没有模型预算、没有网络的情况下也能把主链路跑通并做回归；
2. 作为对照基线——换 AgentScope 之后必须证明在响应质量、延迟、恢复正确性上
   有可测收益，而不是"功能清单更长"（设计稿 §2.2）。

它不做 NLU。时间与服务的解析是**有界**的：只识别写明的表达，识别不了就追问，
不猜测、不编造。这一点与生产 Agent 的要求一致——宁可追问，不要静默采用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Sequence

from ..core.enums import Intent, TaskState
from ..domain.timeutil import load_zone, resolve_local_datetime
from .ports import ToolRequest, TurnOutput, TurnRequest

#: 仅用于"这件事像不像一个时间表达"的判断，不参与任何业务时间计算。
_LIKE_A_TIME_REFERENCE = datetime(2000, 1, 1, tzinfo=timezone.utc)

#: 一天中的时段 → (起始小时, 窗口小时数)。窗口用于把"下午"这类宽表达转成可查询窗口。
PERIODS: dict[str, tuple[int, int]] = {
    "早上": (8, 4),
    "上午": (9, 3),
    "中午": (11, 3),
    "下午": (14, 4),
    "傍晚": (17, 3),
    "晚上": (18, 4),
}

DAY_WORDS: dict[str, int] = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}

WEEKDAY_WORDS: dict[str, int] = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

CN_DIGITS: dict[str, int] = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

#: "可以换"这类授权的表达。没有它们就不能生成替代候选（设计稿 §9）。
SUBSTITUTE_PATTERNS = (
    "可以换",
    "都能接受",
    "看情况",
    "谁都可以",
    "随便",
    "都行",
    "换个",
)

#: 明确拒绝替代的表达。
NO_SUBSTITUTE_PATTERNS = ("只能", "必须是", "非他不可", "不要换")

CONFIRM_PATTERNS = ("确认", "好的", "可以", "就这个", "行", "没问题", "定了", "确定")

CANCEL_PATTERNS = ("取消", "不要了", "不去了")

HANDOFF_PATTERNS = ("人工", "客服", "转人工", "投诉")

#: 从候选里挑一个的说法。"第 N 个"必须带量词，否则"第二天"会被当成"第二个"。
SELECTION_ORDINAL = re.compile(r"第\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,2})\s*个")

#: 只有一个候选时，这些答复等价于"就它了"，不存在歧义。
SINGLE_CANDIDATE_AFFIRM = ("可以", "好的", "行", "就这个", "没问题", "定了", "确定", "确认")


# ---------------------------------------------------------------------------
# 有界的中文时间/服务解析
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TimeExpression:
    """解析出的时间窗口。``window_start`` 是窗口起点，不是精确预约时刻。"""

    local_date: date
    window_start: datetime
    window_end: datetime
    desired_start: datetime
    matched_text: str

    def to_slot_value(self) -> dict[str, Any]:
        return {
            "start_at": self.window_start.isoformat(),
            "end_at": self.window_end.isoformat(),
            "desired_start": self.desired_start.isoformat(),
            "local_date": self.local_date.isoformat(),
            "matched_text": self.matched_text,
        }


def parse_cn_number(raw: str) -> int | None:
    """解析 ``1`` / ``三`` / ``十`` / ``十五`` / ``二十`` 这类小数字。"""

    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = CN_DIGITS.get(head, 1) if head else 1
        ones = CN_DIGITS.get(tail, 0) if tail else 0
        if head and head not in CN_DIGITS:
            return None
        if tail and tail not in CN_DIGITS:
            return None
        return tens * 10 + ones
    return CN_DIGITS.get(text)


def parse_time_expression(
    text: str, *, now: datetime, tz_name: str
) -> TimeExpression | None:
    """从文本中解析一个时间窗口。

    识别的形态：可选"今天/明天/后天/周X" + 可选的"上午/下午/晚上" + 可选"X点"。
    无法确定日期与时段时返回 ``None``，由调用方追问，而不是默认成"今天"。
    """

    if not text:
        return None
    tz = load_zone(tz_name)
    today = now.astimezone(tz).date()

    day: date | None = None
    matched: list[str] = []

    for word, offset in DAY_WORDS.items():
        if word in text:
            day = today + timedelta(days=offset)
            matched.append(word)
            break

    if day is None:
        weekday_match = re.search(r"下周([一二三四五六日天])", text) or re.search(
            r"周([一二三四五六日天])", text
        )
        if weekday_match:
            target = WEEKDAY_WORDS[weekday_match.group(1)]
            monday_this_week = today - timedelta(days=today.weekday())
            if "下周" in text:
                # "下周三"指的是下一周的周三，不是"今天之后的第七个周三"。
                day = monday_this_week + timedelta(days=7 + target)
            else:
                day = monday_this_week + timedelta(days=target)
                if day <= today:
                    day = day + timedelta(days=7)
            matched.append(weekday_match.group(0))

    if day is None:
        return None

    period_hour: int | None = None
    period_span = 3
    for word, (hour, span) in PERIODS.items():
        if word in text:
            period_hour = hour
            period_span = span
            matched.append(word)
            break

    hour: int | None = None
    hour_match = re.search(r"([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点:：]", text)
    if hour_match:
        hour = parse_cn_number(hour_match.group(1))
        matched.append(hour_match.group(0))
        if hour is not None and not 0 <= hour <= 24:
            hour = None

    if hour is None and period_hour is None:
        # 只说了"明天""下周三"这类日期，没说时段：给整天窗口，由营业时间在
        # 可用性查询里收敛。这比追问"几点"更少打断用户，也不会猜错时段。
        local_start = datetime.combine(day, time(0, 0))
        local_end = datetime.combine(day, time(23, 59, 59))
        return TimeExpression(
            local_date=day,
            window_start=resolve_local_datetime(local_start, tz),
            window_end=resolve_local_datetime(local_end, tz),
            desired_start=resolve_local_datetime(datetime.combine(day, time(10, 0)), tz),
            matched_text="".join(matched),
        )

    if hour is None:
        start_hour = period_hour or 10
        span = period_span
    else:
        start_hour = hour
        # "三点" 在中文口语里多半指下午，结合时段词消歧。
        if period_hour is not None:
            if period_hour >= 12 and hour < 12:
                start_hour = hour + 12
        elif hour <= 8:
            start_hour = hour + 12
        span = max(2, min(period_span, 4))

    start_hour = max(0, min(start_hour, 23))
    local_start = datetime.combine(day, time(hour=start_hour))
    window_start = resolve_local_datetime(local_start, tz)
    window_end = min(window_start + timedelta(hours=span), resolve_local_datetime(
        datetime.combine(day, time(23, 59, 59)), tz
    ))
    return TimeExpression(
        local_date=day,
        window_start=window_start,
        window_end=window_end,
        desired_start=window_start,
        matched_text="".join(matched),
    )


def resolve_service(text: str, services: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """按名称/别名解析服务。歧义时返回 ``None``（由调用方追问）。"""

    if not text:
        return None
    candidates: list[dict[str, Any]] = []
    for service in services:
        names = [str(service.get("name") or "")] + [
            str(alias) for alias in (service.get("aliases") or [])
        ]
        names = [name for name in names if name]
        if any(name and name in text for name in names):
            # 更长的匹配优先：避免"肩颈"命中而"肩颈舒缓 60 分钟"被忽略。
            candidates.append(
                (max(len(name) for name in names if name in text), service)
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_len = candidates[0][0]
    winners = [service for length, service in candidates if length == best_len]
    if len({str(s.get("service_id")) for s in winners}) != 1:
        return None
    return winners[0]


def classify_intent(text: str) -> Intent:
    """入口分类。分类不授予任何权限（设计稿 §5）。"""

    if not text:
        return Intent.UNKNOWN
    if any(word in text for word in HANDOFF_PATTERNS):
        return Intent.HANDOFF
    if any(word in text for word in CANCEL_PATTERNS):
        return Intent.CANCEL
    if any(word in text for word in ("改约", "改时间", "换个时间", "推迟", "提前到")):
        return Intent.MODIFY
    if parse_time_expression(
        text, now=_LIKE_A_TIME_REFERENCE, tz_name="UTC"
    ) is not None:
        return Intent.BOOK
    if any(word in text for word in ("预约", "订", "约", "想做个", "做一次")):
        return Intent.BOOK
    if any(
        word in text
        for word in ("怎么", "政策", "能不能", "多久", "多少钱", "注意", "吗", "?", "？")
    ):
        return Intent.CONSULT
    return Intent.UNKNOWN


def _wants_substitute(text: str) -> bool | None:
    if any(word in text for word in NO_SUBSTITUTE_PATTERNS):
        return False
    if any(word in text for word in SUBSTITUTE_PATTERNS):
        return True
    return None


def detect_candidate_selection(text: str, *, candidate_count: int) -> int | None:
    """识别用户是否**明确**选定了某个候选，返回 0 基下标。

    识别不出来就返回 ``None``。这是刻意的保守策略：用户没说"要哪个"时，运行时
    只能把候选摆出来并等待，**不得**用"默认第一个"替用户做决定——那样用户在
    未确认的情况下就被占用了资源（设计稿 §2.1）。序号越界同样返回 ``None``。
    """

    if candidate_count <= 0 or not text:
        return None

    ordinal = SELECTION_ORDINAL.search(text)
    if ordinal is not None:
        value = parse_cn_number(ordinal.group(1))
        if value is not None and 1 <= value <= candidate_count:
            return value - 1
        return None

    if candidate_count == 1 and any(
        word in text for word in SINGLE_CANDIDATE_AFFIRM
    ):
        # 只有一个候选时"可以"没有第二种解释。
        return 0
    return None


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------
class DeterministicRuntime:
    """按状态推进的确定性策略。"""

    runtime_name = "deterministic-v1"

    async def run_turn(self, request: TurnRequest) -> TurnOutput:
        handler = getattr(
            self, f"_on_{request.task_state.value.lower()}", self._on_unknown
        )
        return handler(request)

    # ---------------- 各状态的策略 ----------------
    def _on_collecting(self, request: TurnRequest) -> TurnOutput:
        patches: list[dict[str, Any]] = []
        message = request.user_message or ""

        patches = self._collect_patches(request, replace_existing=False)

        substitute = _wants_substitute(request.user_message or "")
        if substitute is not None and "resource_preference" not in request.slots:
            patches.append(
                {
                    "slot_name": "resource_preference",
                    "op": "SET",
                    "value": {"allow_substitute": substitute},
                }
            )

        if patches:
            # 本轮只补槽位；下一轮由编排器把状态推进到 SEARCHING 后再查询。
            return TurnOutput(
                slot_patches=tuple(patches),
                intent=Intent.BOOK,
                reply_text="好的，我按这个时间帮您找一下。",
            )

        known_service = request.slots.get("service", {}).get("value") or {}
        known_window = request.slots.get("time_window", {}).get("value") or {}
        missing: list[str] = []
        if not known_service:
            missing.append("想做哪个项目")
        if not known_window:
            missing.append("希望哪天、大概几点")
        if missing:
            return TurnOutput(
                clarification_question="请问" + "、".join(missing) + "？",
                intent=Intent.BOOK,
            )
        return TurnOutput(intent=Intent.UNKNOWN)

    def _collect_patches(
        self, request: TurnRequest, *, replace_existing: bool
    ) -> list[dict[str, Any]]:
        """从消息里解析槽位补丁。

        ``replace_existing=False`` 只补缺失的槽位（收集阶段）；
        ``True`` 允许覆盖已有值（"改到后天下午三点"）。只有值**真的变了**才产生
        补丁：否则一次无变化的改写也会把方案作废、把任务打回搜索。
        """

        patches: list[dict[str, Any]] = []
        message = request.user_message or ""

        known_service = (request.slots.get("service") or {}).get("value") or {}
        if replace_existing or not known_service:
            matched = resolve_service(message, list(request.facts.get("services") or []))
            if matched is not None and str(matched["service_id"]) != known_service.get(
                "service_id"
            ):
                patches.append(
                    {
                        "slot_name": "service",
                        "op": "SET",
                        "value": {
                            "service_id": str(matched["service_id"]),
                            "name": matched.get("name"),
                            "source": "deterministic_parse",
                        },
                    }
                )

        known_window = (request.slots.get("time_window") or {}).get("value") or {}
        if replace_existing or not known_window:
            now = request.facts.get("now")
            if now is None:
                # 缺参考时刻就无法把"明天"解析成绝对时间。这是编排器的错误，
                # 不能退化成"猜一个时间"。
                raise ValueError("TurnRequest.facts 缺少 now，无法解析相对时间表达")
            tz_name = str(request.facts.get("store_timezone") or "Asia/Shanghai")
            parsed = parse_time_expression(message, now=now, tz_name=tz_name)
            if parsed is not None and parsed.to_slot_value() != known_window:
                patches.append(
                    {"slot_name": "time_window", "op": "SET", "value": parsed.to_slot_value()}
                )
        return patches

    def _on_searching(self, request: TurnRequest) -> TurnOutput:
        facts = request.facts
        service_id = (request.slots.get("service", {}).get("value") or {}).get(
            "service_id"
        )
        window = request.slots.get("time_window", {}).get("value") or {}
        if not service_id or not window:
            return TurnOutput(
                clarification_question="还需要确认项目和大概时间，方便说一下吗？"
            )

        followups = facts.get("followups") or {}
        # 只认**本次活跃执行里刚取到**的事实。账本里的旧事实可能对应另一个时间窗，
        # 也可能价格已经变了：在"搜索中"这个阶段一律重新取，不用旧值凑。
        if "quote" not in request.fresh_fact_keys or not followups.get("quote"):
            return TurnOutput(
                tool_requests=(
                    ToolRequest(
                        tool_name="get_service_quote",
                        arguments={
                            "store_id": facts["store_id"],
                            "service_id": service_id,
                        },
                        rationale="需要当前价格用于方案展示",
                    ),
                ),
                intent=Intent.BOOK,
            )

        if (
            "availability" not in request.fresh_fact_keys
            or not followups.get("availability")
        ):
            preferences = request.slots.get("resource_preference", {}).get("value") or {}
            arguments: dict[str, Any] = {
                "store_id": facts["store_id"],
                "service_id": service_id,
                "window_start": window.get("start_at"),
                "window_end": window.get("end_at"),
                "desired_start": window.get("desired_start"),
                "limit": 5,
            }
            if "allow_substitute" in preferences:
                arguments["allow_substitute"] = bool(preferences["allow_substitute"])
            return TurnOutput(
                tool_requests=(
                    ToolRequest(
                        tool_name="search_availability",
                        arguments=arguments,
                        rationale="按用户时间窗查询候选",
                    ),
                ),
                intent=Intent.BOOK,
            )

        return TurnOutput(
            reply_text=self._describe_candidates(followups),
            intent=Intent.BOOK,
        )

    def _on_proposed(self, request: TurnRequest) -> TurnOutput:
        # 先看用户是不是在改条件（"改到后天下午三点"）。改了条件，现有候选就
        # 不再是他说"第一个"时看到的那个方案，必须先作废再重查。
        patches = self._collect_patches(request, replace_existing=True)
        if patches:
            return TurnOutput(
                slot_patches=tuple(patches),
                intent=Intent.BOOK,
                reply_text="好的，我按新的条件再查一遍。",
            )

        followups = request.facts.get("followups") or {}
        candidates = list(followups.get("availability", {}).get("candidates") or [])
        if not candidates:
            return TurnOutput(
                tool_requests=(
                    ToolRequest(
                        tool_name="search_availability",
                        arguments={
                            "store_id": request.facts["store_id"],
                            "service_id": (
                                request.slots.get("service", {}).get("value") or {}
                            ).get("service_id"),
                            **{
                                key: value
                                for key, value in (
                                    request.slots.get("time_window", {}).get("value") or {}
                                ).items()
                                if key in ("start_at", "end_at", "desired_start")
                            },
                        },
                        rationale="候选缺失，重新查询",
                    ),
                )
            )

        if "availability" in request.fresh_fact_keys:
            # 候选是**本轮刚算出来**的：先展示，等用户的下一条消息再占位。
            # 同一轮里替用户挑一个是最典型的"静默代替决策"。
            return TurnOutput(
                reply_text=self._describe_candidates(followups),
                intent=Intent.BOOK,
            )

        chosen_index = detect_candidate_selection(
            request.user_message or "", candidate_count=len(candidates)
        )
        if chosen_index is None:
            # 用户还没说明要哪个：再摆一次候选并要一个序号，不猜、不占位。
            return TurnOutput(
                reply_text=(
                    self._describe_candidates(followups)
                    + "\n回复序号（例如“第一个”），我就帮您把这个时段占住。"
                ),
                intent=Intent.BOOK,
            )

        candidate = candidates[chosen_index]
        quote = followups.get("quote") or {}
        if not quote.get("quote_token"):
            # 凭据不落账本，重新签发需要走一次工具调用。这里不能追问用户：
            # PROPOSED 不允许直接进 WAITING_USER，而且过期凭据是系统的事。
            return TurnOutput(
                tool_requests=(
                    ToolRequest(
                        tool_name="get_service_quote",
                        arguments={
                            "store_id": request.facts["store_id"],
                            "service_id": (
                                request.slots.get("service", {}).get("value") or {}
                            ).get("service_id"),
                        },
                        rationale="占位需要有效期内的报价凭据，重新取价",
                    ),
                ),
                intent=Intent.BOOK,
            )

        return TurnOutput(
            tool_requests=(
                ToolRequest(
                    tool_name="create_hold",
                    arguments={
                        "task_id": str(request.task_id),
                        "expected_task_version": request.task_version,
                        "store_id": request.facts["store_id"],
                        "service_id": (
                            request.slots.get("service", {}).get("value") or {}
                        ).get("service_id"),
                        "candidate_id": candidate["candidate_id"],
                        "start_at": candidate["start_at"],
                        "end_at": candidate["end_at"],
                        "resource_ids": [
                            unit["resource_id"] for unit in candidate["resources"]
                        ],
                        "quote_token": quote["quote_token"],
                    },
                    rationale="用户选定候选后创建单个占位",
                ),
            ),
            intent=Intent.BOOK,
        )

    def _on_waiting_user(self, request: TurnRequest) -> TurnOutput:
        if request.user_message:
            # 追问的答复返回后回到收集状态继续补槽位。
            return TurnOutput(intent=Intent.UNKNOWN, slot_patches=())
        return TurnOutput()

    def _on_waiting_confirmation(self, request: TurnRequest) -> TurnOutput:
        # 确认由专用接口与方案级凭据驱动，模型输入不构成授权。
        return TurnOutput(
            reply_text="方案已发出，请点击确认卡片完成预约。",
            intent=Intent.BOOK,
        )

    def _on_waiting_result(self, request: TurnRequest) -> TurnOutput:
        return TurnOutput(reply_text="这笔预约的结果还在确认中，我查到结果后马上告诉您。")

    def _on_succeeded(self, request: TurnRequest) -> TurnOutput:
        return TurnOutput(reply_text="预约已完成。")

    def _on_unknown(self, request: TurnRequest) -> TurnOutput:
        return TurnOutput()

    # ---------------- 辅助 ----------------
    @staticmethod
    def _describe_candidates(followups: dict[str, Any]) -> str:
        candidates = list(followups.get("availability", {}).get("candidates") or [])
        if not candidates:
            return "这个时间窗没有可约的时段，要不要换个时间？"
        lines = []
        for index, candidate in enumerate(candidates[:3], start=1):
            resources = "、".join(
                str(unit.get("display_name")) for unit in candidate.get("resources", [])
            )
            lines.append(f"{index}. {candidate['start_at']}（{resources}）")
        return "为您找到以下时段：\n" + "\n".join(lines)


__all__ = [
    "DeterministicRuntime",
    "TimeExpression",
    "classify_intent",
    "detect_candidate_selection",
    "parse_cn_number",
    "parse_time_expression",
    "resolve_service",
]
