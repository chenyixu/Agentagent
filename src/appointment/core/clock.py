"""可注入时钟。

设计稿 §8.1：PostgreSQL 的 ``now()`` 是事务起始时间，长时间等待锁后可能已经过时；
有效期裁决应使用拿锁后的实际时间点。因此领域层区分两类时间：

- ``now()``          应用侧参照时间（写审计、日志、观测）
- ``db_clock_sql()`` 事务内的数据库实际时间表达式，用于到期裁决

测试用 ``FrozenClock`` 推进受控时钟，避免用 ``sleep`` 造成脆弱测试（§13.1）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    """真实系统时钟。"""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """受控时钟，供测试推进时间。"""

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock 需要带时区的时间点")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        self._now = self._now + timedelta(**delta)
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FrozenClock 需要带时区的时间点")
        self._now = moment


#: 有效期裁决必须使用数据库实际时间，而不是事务开始时间。
#: 领域层在取得锁之后用它判断占位是否过期。
DB_NOW_SQL = "clock_timestamp()"


def ensure_aware(moment: datetime) -> datetime:
    """把 naive 时间点按 UTC 解释，避免和 timestamptz 比较时静默漂移。"""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment
