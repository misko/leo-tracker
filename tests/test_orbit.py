from datetime import datetime, timezone
import math

import pytest

from leo_tracker.orbit import Observer, look_angle, parse_tle, predicted_doppler_hz, propagate_ecef, propagate_teme
from leo_tracker.orbit.propagation import ECEFState


VANGUARD = """VANGUARD 1
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 331.5174 1849677 331.7664  19.3264 10.82419157413661"""


def test_parse_tle_epoch_provenance_and_digest():
    retrieved = datetime(2026, 7, 31, tzinfo=timezone.utc)
    tle = parse_tle(VANGUARD, source="fixture:sgp4-verification", retrieved_at=retrieved)
    assert tle.norad_id == 5
    assert tle.name == "VANGUARD 1"
    assert tle.epoch == datetime(2000, 6, 27, 18, 50, 19, 733568, tzinfo=timezone.utc)
    assert tle.provenance.source == "fixture:sgp4-verification"
    assert len(tle.sha256) == 64


def test_parse_rejects_corruption_and_naive_retrieval_time():
    with pytest.raises(ValueError, match="checksum"):
        parse_tle(VANGUARD[:-1] + "8")
    with pytest.raises(ValueError, match="UTC"):
        parse_tle(VANGUARD, retrieved_at=datetime(2020, 1, 1))


def test_published_sgp4_epoch_vector():
    state = propagate_teme(parse_tle(VANGUARD), datetime(2000, 6, 27, 18, 50, 19, 733568, tzinfo=timezone.utc))
    # Vallado verification TLE, evaluated at its element epoch with WGS-72.
    expected_r = (6297.67002280, -3423.95422294, -4.23460253)
    expected_v = (3.704542814, 5.551523941, 4.530508331)
    assert state.position_km == pytest.approx(expected_r, abs=2e-5)
    assert state.velocity_km_s == pytest.approx(expected_v, abs=2e-8)
    assert state.frame == "TEME"


def test_ecef_velocity_matches_position_finite_difference():
    tle = parse_tle(VANGUARD)
    t = datetime(2000, 6, 27, 19, tzinfo=timezone.utc)
    from datetime import timedelta
    before = propagate_ecef(tle, t - timedelta(milliseconds=50))
    after = propagate_ecef(tle, t + timedelta(milliseconds=50))
    state = propagate_ecef(tle, t)
    derivative = tuple((b-a)/0.1 for a, b in zip(before.position_km, after.position_km))
    # SGP4's reported osculating velocity is not the exact finite-difference
    # derivative of its independently reconstructed positions; agreement is
    # nevertheless comfortably sub-m/s.
    assert state.velocity_km_s == pytest.approx(derivative, abs=7e-4)
    assert state.frame == "ITRF_APPROX"


def test_observer_wgs84_and_topocentric_geometry():
    observer = Observer(0, 0, 0)
    assert observer.ecef_km() == pytest.approx((6378.137, 0, 0), abs=1e-9)
    overhead = ECEFState(datetime(2020, 1, 1, tzinfo=timezone.utc), (6478.137, 0, 0), (-1, 0, 0))
    look = look_angle(observer, overhead)
    assert look.elevation_deg == pytest.approx(90)
    assert look.range_km == pytest.approx(100)
    assert look.range_rate_km_s == pytest.approx(-1)


def test_range_rate_matches_finite_difference_of_range():
    tle = parse_tle(VANGUARD)
    observer = Observer(37.4, -122.1, 20)
    t = datetime(2000, 6, 27, 19, tzinfo=timezone.utc)
    from datetime import timedelta
    dt = timedelta(milliseconds=50)
    before = look_angle(observer, propagate_ecef(tle, t-dt)).range_km
    after = look_angle(observer, propagate_ecef(tle, t+dt)).range_km
    rate = look_angle(observer, propagate_ecef(tle, t)).range_rate_km_s
    assert rate == pytest.approx((after-before)/0.1, abs=1e-5)


def test_doppler_sign_and_scaling():
    assert predicted_doppler_hz(10e9, -1) > 0
    assert predicted_doppler_hz(20e9, 1) == pytest.approx(2 * predicted_doppler_hz(10e9, 1))
    with pytest.raises(ValueError):
        predicted_doppler_hz(0, 1)
