"""Formatting helpers for showing values to people, shared across the app."""

from __future__ import annotations

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def format_duration(seconds: float) -> str:
    """
    `seconds` as its two largest units: "42s", "3m 12s", "1h 5m" or "2d 3h".

    Whole units only, rounded down, and never negative (two machines' clocks can disagree by a little,
    and a duration shown as "-3s" helps nobody). Runs last days, so it goes up to days.
    """
    whole = max(int(seconds), 0)
    days, rest = divmod(whole, _SECONDS_PER_DAY)
    hours, rest = divmod(rest, _SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, _SECONDS_PER_MINUTE)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
