"""Regular-session calendar helpers, shared by friction pricing and risk vetoes.

Calendar-naive throughout: assumes a standard 09:30-16:00 America/New_York
session every day. Half-days, holidays, and market-closed detection are
Phase 8 (market calendar hardening) — BUILD_SPEC §16.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
PENALTY_WINDOW = timedelta(minutes=5)


def in_open_or_close_window(ts: datetime, penalty_window: timedelta = PENALTY_WINDOW) -> bool:
    """True in the first/last few minutes of the regular session (BUILD_SPEC §9.2)."""
    local = ts.astimezone(NY)
    open_dt = datetime.combine(local.date(), SESSION_OPEN, tzinfo=NY)
    close_dt = datetime.combine(local.date(), SESSION_CLOSE, tzinfo=NY)
    in_open_window = open_dt <= local < open_dt + penalty_window
    in_close_window = close_dt - penalty_window <= local < close_dt
    return in_open_window or in_close_window


def minutes_until_session_close(ts: datetime) -> float:
    """Minutes until 16:00 ET. Negative once the session has closed."""
    local = ts.astimezone(NY)
    close_dt = datetime.combine(local.date(), SESSION_CLOSE, tzinfo=NY)
    return (close_dt - local).total_seconds() / 60


def start_of_trading_day(ts: datetime) -> datetime:
    """The UTC instant corresponding to local midnight in America/New_York.

    Used to bucket "today's" trades for the risk engine's daily-loss check.
    """
    local = ts.astimezone(NY)
    midnight = datetime.combine(local.date(), time.min, tzinfo=NY)
    return midnight.astimezone(UTC)
