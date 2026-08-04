from datetime import datetime, timedelta, timezone

import numpy as np

from leo_tracker.radio.tle_hopping import compare_carrier_to_tles


def _catalog(start):
    points = []
    for seconds, doppler in ((0, 0), (15, -22_500), (30, -45_000)):
        points.append({"time": (start+timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
                       "expected_doppler_hz": doppler})
    return {"satellites": [{"name": "STARLINK-SYNTHETIC", "norad_id": 42,
        "passes": [{"rise": points[0],
                    "culmination": {**points[1], "elevation_deg": 70},
                    "set": points[2]}]}]}


def _candidate(start, hop_spacing):
    times = np.arange(30, dtype=float)
    predicted = -1_500*times
    indexes = (times >= 15).astype(int)
    paths = np.asarray([100_000+predicted-indexes*hop_spacing,
                       -250_000+predicted-indexes*hop_spacing])
    return {"time_s": times.tolist(), "paths_hz": paths.tolist()}, int(start.timestamp()*1e9)


def test_starlink_spacing_hop_recovers_tle_motion_and_common_index_change():
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    candidate, utc = _candidate(start, 43_949.5)
    report = compare_carrier_to_tles(candidate, utc, _catalog(start))
    best = report["candidates"][0]
    assert report["qualified_count"] == 1
    assert best["starlink_spacing_fit"]["hop_count"] == 1
    assert best["starlink_spacing_fit"]["hop_rows"] == [15]
    assert best["starlink_spacing_fit"]["joint_rms_residual_hz"] < 1
    assert best["wrong_spacing_advantage_fraction"] > .1


def test_wrong_spacing_hop_and_no_hop_do_not_claim_starlink_spacing():
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for spacing in (35_000.0, 0.0):
        candidate, utc = _candidate(start, spacing)
        report = compare_carrier_to_tles(candidate, utc, _catalog(start))
        assert report["qualified_count"] == 0
        assert "spacing controls" in " ".join(
            report["candidates"][0]["rejection_reasons"])
