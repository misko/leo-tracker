from datetime import datetime, timezone

import pytest

from leo_tracker.orbit import Observer, parse_tle
from leo_tracker.passes import predict_passes, sample_track


VANGUARD = """VANGUARD 1
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 331.5174 1849677 331.7664  19.3264 10.82419157413661"""


def test_passes_are_ordered_and_crossings_are_refined():
    passes = predict_passes(
        parse_tle(VANGUARD), Observer(37.4, -122.1, 20),
        datetime(2000, 6, 27, tzinfo=timezone.utc),
        datetime(2000, 6, 28, tzinfo=timezone.utc),
        horizon_deg=10, step_seconds=60,
    )
    assert passes
    for item in passes:
        assert item.rise.time <= item.culmination.time <= item.set.time
        # Complete passes have numerically refined crossings.
        if item.rise.time.hour or item.rise.time.minute:
            assert item.rise.look.elevation_deg == pytest.approx(10, abs=0.002)
        assert item.culmination.look.elevation_deg >= item.rise.look.elevation_deg


def test_pass_prediction_validates_time_and_parameters():
    tle, observer = parse_tle(VANGUARD), Observer(0, 0)
    t = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        predict_passes(tle, observer, t, t)
    with pytest.raises(ValueError):
        predict_passes(tle, observer, t, t.replace(day=2), step_seconds=0)
    with pytest.raises(ValueError, match="UTC"):
        predict_passes(tle, observer, t.replace(tzinfo=None), t)


def test_sample_track_includes_exact_endpoints_and_propagated_interior():
    tle, observer = parse_tle(VANGUARD), Observer(37.4, -122.1, 20)
    start = datetime(2000, 6, 27, tzinfo=timezone.utc)
    end = start.replace(minute=2, second=5)
    track = sample_track(tle, observer, start, end, step_seconds=60)
    assert [sample.time for sample in track] == [start, start.replace(minute=1),
                                                  start.replace(minute=2), end]
    assert len({sample.look.range_km for sample in track}) == 4
