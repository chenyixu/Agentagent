"""门店时区与本地时间处理（设计稿 §6.4）。

约定：
- 未明确时区时，预约表达默认采用已选门店时区，界面输入处与确认卡显示该口径。
- "明天"按明确的语义时区取日历日期。
- DST 不存在（gap）的本地时间拒绝并建议替代；重复出现（fold）的时间要求用户
  选择 offset，不静默猜测。
- 服务时长按实际经过的分钟计算结束 instant。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.enums import ErrorCode
from ..core.errors import DomainError

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def load_zone(tz_name: str) -> ZoneInfo:
    """IANA 时区经服务端校验，不靠普通 SQL CHECK。"""

    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"未知 IANA 时区：{tz_name}"
        ) from exc


@dataclass(frozen=True, slots=True)
class LocalWindow:
    """门店本地时间语义的一段营业窗口。"""

    start: time
    end: time

    def as_tuple(self) -> tuple[str, str]:
        return (self.start.strftime("%H:%M"), self.end.strftime("%H:%M"))


class AmbiguousLocalTime(DomainError):
    """DST 重复出现的本地时间（fold）。要求用户选择 offset。"""

    def __init__(self, local_dt: datetime) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            f"本地时间 {local_dt.isoformat()} 在该时区出现两次（夏令时回拨），"
            "请明确指定 UTC offset",
            details={"local_time": local_dt.isoformat(), "kind": "ambiguous"},
        )


class NonExistentLocalTime(DomainError):
    """DST 不存在的本地时间（gap）。拒绝并建议替代。"""

    def __init__(self, local_dt: datetime, suggestion: datetime) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            f"本地时间 {local_dt.isoformat()} 在该时区不存在（夏令时前拨），"
            f"建议改用 {suggestion.isoformat()}",
            details={
                "local_time": local_dt.isoformat(),
                "suggestion": suggestion.isoformat(),
                "kind": "nonexistent",
            },
        )


def resolve_local_datetime(local_dt: datetime, tz: ZoneInfo) -> datetime:
    """把门店本地时间解析成绝对 instant，显式处理 DST。

    通过比较两次 offset（fold=0/1）判断 gap 与 fold。
    """

    naive = local_dt.replace(tzinfo=None)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    offset_first = first.utcoffset()
    offset_second = second.utcoffset()

    if offset_first != offset_second:
        # 同一本地时间对应两个 offset 或不存在。
        if offset_first is not None and offset_second is not None:
            if offset_first > offset_second:
                # 回拨：该本地时间出现两次。
                raise AmbiguousLocalTime(naive)
            # 前拨：该本地时间不存在，建议顺延到 gap 之后。
            suggestion = naive + (offset_second - offset_first)
            raise NonExistentLocalTime(naive, suggestion)
    return first.astimezone(timezone.utc)


def to_local(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz)


def local_date_of(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


def iter_local_dates(start: datetime, end: datetime, tz: ZoneInfo) -> list[date]:
    """覆盖 [start, end) 涉及的全部本地日期。

    跨午夜时逐日覆盖，用于资源日 guard 与营业窗口判断。
    """

    first = local_date_of(start, tz)
    # end 是排他端点：若恰好落在本地午夜 00:00，则不含该日。
    last_moment = end - timedelta(microseconds=1)
    last = local_date_of(last_moment, tz)
    days: list[date] = []
    cursor = first
    while cursor <= last:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def windows_for_local_date(
    windows_by_weekday: dict[str, list[list[str]]], local_date: date
) -> list[LocalWindow]:
    """取出某一天的营业窗口。键为 mon..sun，值为 [["10:00","22:00"], ...]。"""

    key = WEEKDAY_KEYS[local_date.weekday()]
    raw = windows_by_weekday.get(key) or []
    result: list[LocalWindow] = []
    for entry in raw:
        if len(entry) != 2:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"营业窗口格式非法：{entry!r}，应为 [开始, 结束]",
            )
        result.append(
            LocalWindow(
                start=time.fromisoformat(entry[0]), end=time.fromisoformat(entry[1])
            )
        )
    return result


def window_instants(
    local_date: date, window: LocalWindow, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """把本地窗口转成绝对区间。

    结束时间小于等于开始时间时视为跨午夜，结束日顺延一天。
    """

    start_local = datetime.combine(local_date, window.start)
    end_local = datetime.combine(local_date, window.end)
    if window.end <= window.start:
        end_local = end_local + timedelta(days=1)
    start = resolve_local_datetime(start_local, tz)
    end = resolve_local_datetime(end_local, tz)
    if end <= start:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"营业窗口区间非法：{local_date} {window.as_tuple()}",
        )
    return start, end


def snap_to_step(moment: datetime, step_minutes: int) -> datetime:
    """把候选起点对齐到 step 网格，避免产生零散的奇怪时间。"""

    epoch_minutes = int(moment.timestamp() // 60)
    remainder = epoch_minutes % step_minutes
    if remainder == 0:
        return moment.replace(second=0, microsecond=0)
    return moment.replace(second=0, microsecond=0) + timedelta(
        minutes=step_minutes - remainder
    )


def format_local(moment: datetime, tz: ZoneInfo) -> str:
    """确认卡展示口径：门店本地时间 + offset。"""

    local = moment.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M %Z%z")
