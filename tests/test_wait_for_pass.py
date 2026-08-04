from datetime import datetime, timedelta, timezone

import pytest

from leo_tracker.orbit.waiting import select_window


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def test_select_window_skips_expired_and_keeps_active():
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    expired = {"record_start": _iso(now-timedelta(minutes=2)),
               "record_end": _iso(now-timedelta(minutes=1))}
    active = {"record_start": _iso(now-timedelta(seconds=10)),
              "record_end": _iso(now+timedelta(minutes=1))}
    assert select_window({"entries": [expired, active]}, now) is active


def test_select_window_rejects_exhausted_schedule():
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match="no future"):
        select_window({"entries": []}, now)


def test_select_window_requires_enough_remaining_time():
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    short = {"record_start": _iso(now-timedelta(minutes=1)),
             "record_end": _iso(now+timedelta(seconds=30))}
    later = {"record_start": _iso(now+timedelta(minutes=2)),
             "record_end": _iso(now+timedelta(minutes=10))}
    assert select_window({"entries": [short, later]}, now,
                         min_remaining_seconds=300) is later
