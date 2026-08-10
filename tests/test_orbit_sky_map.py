from datetime import datetime, timezone

from leo_tracker.orbit.sky_map import summarize_daily


def _utc(day, hour):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


def test_daily_summary_counts_confirmed_windows_tracks_and_unique_satellites():
    beacons = [_utc(8, 6), _utc(8, 8), _utc(9, 6)]
    associations = [
        {"times": [_utc(8, 6)], "norad_id": 100},
        {"times": [_utc(8, 9)], "norad_id": 100},
        {"times": [_utc(8, 10)], "norad_id": 200},
        {"times": [_utc(9, 8)], "norad_id": 300},
    ]

    rows = summarize_daily(beacons, associations,
                           timezone_name="America/Los_Angeles")

    assert rows == [
        {"date": "2026-08-07", "confirmed_beacon_windows": 1,
         "qualified_satellite_tracks": 1, "unique_satellites": 1,
         "norad_ids": [100]},
        {"date": "2026-08-08", "confirmed_beacon_windows": 2,
         "qualified_satellite_tracks": 2, "unique_satellites": 2,
         "norad_ids": [100, 200]},
        {"date": "2026-08-09", "confirmed_beacon_windows": 0,
         "qualified_satellite_tracks": 1, "unique_satellites": 1,
         "norad_ids": [300]},
    ]
