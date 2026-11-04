"""Time helpers for Phase 4.

Phase 4 timestamps (reviews, attempts, passes) are stored in UTC as ISO strings,
e.g. "2026-11-04T17:00:00+00:00". "Today" means today in config.TIMEZONE, so a review at
11 pm New York time counts for that day, not the next UTC day.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import config


def local_tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """UTC ISO string (seconds precision). `dt` must be timezone-aware."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def local_date(dt: datetime) -> date:
    """The local calendar day a UTC moment falls on."""
    return dt.astimezone(local_tz()).date()


def local_day_bounds(now: datetime | None = None) -> tuple[str, str]:
    """(start, end) of the local day containing `now`, as UTC ISO strings. End is exclusive."""
    day = local_date(now or utc_now())
    start = datetime.combine(day, time(0), tzinfo=local_tz())
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=local_tz())
    return to_iso(start), to_iso(end)


def same_local_day(a: datetime, b: datetime) -> bool:
    return local_date(a) == local_date(b)
