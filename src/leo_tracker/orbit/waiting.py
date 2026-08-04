"""Selection of the next usable recording window."""
from __future__ import annotations

from datetime import datetime


def select_window(schedule: dict, now: datetime, *, min_remaining_seconds: float = 0) -> dict:
    if min_remaining_seconds < 0:
        raise ValueError("minimum remaining time cannot be negative")
    for entry in schedule.get("entries", []):
        end = datetime.fromisoformat(entry["record_end"].replace("Z", "+00:00"))
        if (end - now).total_seconds() >= min_remaining_seconds:
            return entry
    raise RuntimeError("schedule contains no future recording window")
