from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    return datetime.now(UTC)


def floor_hour(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(minute=0, second=0, microsecond=0)


def hour_range(start: datetime, end: datetime) -> list[datetime]:
    """[start, end) 内的整点（UTC）。"""
    cur, out = floor_hour(start), []
    while cur < end:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


def local_day_window(tz: ZoneInfo | str, day: date) -> tuple[datetime, datetime]:
    """区域本地日历日 → UTC 时间窗 [start, end)。日报按区域时区切“天”。"""
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def local_today(tz: ZoneInfo | str) -> date:
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    return datetime.now(tz).date()
