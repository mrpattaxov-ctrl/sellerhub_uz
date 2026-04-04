"""Timezone-aware datetime helpers and notification window utilities."""
from __future__ import annotations

from datetime import date, datetime, timedelta, time as dt_time

from config import (
    APP_TZ,
    NOTIFICATION_INTERVAL_OPTIONS,
    NOTIFICATION_SETTINGS_DEFAULTS,
)


def _now_app_tz() -> datetime:
    return datetime.now(APP_TZ)


def _today_app_tz() -> date:
    return _now_app_tz().date()


def _app_dt(day: date, hour: int = 0, minute: int = 0, second: int = 0, microsecond: int = 0) -> datetime:
    return datetime.combine(day, dt_time(hour, minute, second, microsecond), APP_TZ)


def _app_day_start_ts(day: date) -> int:
    return int(_app_dt(day).timestamp())


def _app_day_end_ts(day: date) -> int:
    return int((_app_dt(day + timedelta(days=1)) - timedelta(seconds=1)).timestamp())


def _app_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(APP_TZ).replace(tzinfo=None)


def _notification_window_length_hours(
    window_from_hour: int,
    window_to_hour: int,
    *,
    is_24h: bool,
) -> int:
    if is_24h:
        return 24
    start_hour = max(0, min(23, int(window_from_hour)))
    end_hour = max(0, min(23, int(window_to_hour)))
    if end_hour == 0 and start_hour > 0:
        return 24 - start_hour
    if end_hour > start_hour:
        return end_hour - start_hour
    return 0


def _compatible_notification_intervals(
    window_from_hour: int,
    window_to_hour: int,
    *,
    is_24h: bool,
) -> tuple[int, ...]:
    window_length = _notification_window_length_hours(
        window_from_hour,
        window_to_hour,
        is_24h=is_24h,
    )
    if window_length <= 0:
        return (NOTIFICATION_SETTINGS_DEFAULTS["interval_hours"],)
    compatible = tuple(
        interval
        for interval in NOTIFICATION_INTERVAL_OPTIONS
        if window_length % interval == 0
    )
    return compatible or (NOTIFICATION_SETTINGS_DEFAULTS["interval_hours"],)


def _notification_daily_summary_send_hour(
    window_from_hour: int,
    window_to_hour: int,
    *,
    is_24h: bool,
) -> int:
    if is_24h or int(window_from_hour) == 0 or int(window_to_hour) == 0:
        return 0
    return max(0, min(23, int(window_from_hour)))


def _notification_interval_send_hours(
    window_from_hour: int,
    window_to_hour: int,
    *,
    is_24h: bool,
    interval_hours: int,
) -> tuple[int, ...]:
    interval = max(1, int(interval_hours))
    if interval not in NOTIFICATION_INTERVAL_OPTIONS:
        interval = NOTIFICATION_SETTINGS_DEFAULTS["interval_hours"]

    start_hour = 0 if is_24h else max(0, min(23, int(window_from_hour)))
    window_length = _notification_window_length_hours(
        window_from_hour,
        window_to_hour,
        is_24h=is_24h,
    )
    if window_length <= 0 or window_length % interval != 0:
        return ()

    daily_summary_hour = _notification_daily_summary_send_hour(
        window_from_hour,
        window_to_hour,
        is_24h=is_24h,
    )
    send_hours: list[int] = []
    for step in range(interval, window_length + 1, interval):
        absolute_hour = start_hour + step
        normalized_hour = absolute_hour % 24
        if absolute_hour == 24 and normalized_hour == daily_summary_hour:
            continue
        send_hours.append(normalized_hour)
    if not is_24h and int(window_to_hour) == 0 and start_hour > 0:
        send_hours.insert(0, start_hour)
    return tuple(send_hours)


def _recommended_window_lengths(interval_hours: int) -> tuple[int, ...]:
    interval = max(1, int(interval_hours))
    if interval not in NOTIFICATION_INTERVAL_OPTIONS:
        interval = NOTIFICATION_SETTINGS_DEFAULTS["interval_hours"]
    return tuple(length for length in range(interval, 25, interval))
