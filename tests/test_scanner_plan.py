"""Offline tests for the scan planner. No radio, no network."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from scanner.plan import ScanPoint, plan_scan  # noqa: E402


def test_points_sharing_a_span_collapse_to_one_tuning():
    points = [ScanPoint(2.401e9, 1e6), ScanPoint(2.4025e9, 200e3), ScanPoint(2.410e9, 1e6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.tunings == 1
    assert plan.metadata["grouping_saving"] == 2
    assert plan.groups[0].point_indices == (0, 1, 2)


def test_points_further_apart_than_the_span_get_their_own_tunings():
    points = [ScanPoint(700e6, 1e6), ScanPoint(900e6, 1e6), ScanPoint(1.1e9, 1e6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.tunings == 3
    assert plan.metadata["grouping_saving"] == 0


def test_the_analog_bandwidth_is_declared_once_for_the_whole_scan():
    plan = plan_scan([ScanPoint(2.4e9, 1e6), ScanPoint(2.401e9, 5e6)], sample_rate_hz=30e6)
    assert plan.metadata["bandwidth_changes"] == 1
    assert plan.analog_bandwidth_hz == 30e6


def test_uniform_isolated_points_take_the_rssi_fast_path():
    # Same bandwidth everywhere and nothing shares a tuning, so the analog filter can
    # define the band and power comes from RSSI with no IQ transferred.
    points = [ScanPoint(700e6, 2e6), ScanPoint(900e6, 2e6), ScanPoint(1.1e9, 2e6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.metadata["measure_mode"] == "rssi"
    assert plan.analog_bandwidth_hz == 2e6
    assert all(group.mode == "rssi" for group in plan.groups)


def test_mixed_bandwidths_force_digital_synthesis():
    points = [ScanPoint(700e6, 2e6), ScanPoint(900e6, 200e3)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.metadata["measure_mode"] == "fft"
    assert plan.analog_bandwidth_hz == 30e6


def test_grouped_points_never_use_the_rssi_path():
    points = [ScanPoint(2.4e9, 1e6), ScanPoint(2.401e9, 1e6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.tunings == 1
    assert plan.groups[0].mode == "fft"


def test_a_band_wider_than_one_tuning_is_rejected_with_a_useful_message():
    with pytest.raises(ValueError, match="more bandwidth than one tuning covers"):
        plan_scan([ScanPoint(2.4e9, 40e6)], sample_rate_hz=30e6)


def test_tune_centre_is_the_midpoint_of_the_group_span():
    points = [ScanPoint(2.400e9, 1e6), ScanPoint(2.410e9, 1e6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.groups[0].tune_hz == pytest.approx(2.405e9)


def test_empty_and_invalid_requests_are_refused():
    with pytest.raises(ValueError):
        plan_scan([], sample_rate_hz=30e6)
    with pytest.raises(ValueError):
        plan_scan([ScanPoint(2.4e9, 1e6)], sample_rate_hz=0)
    with pytest.raises(ValueError):
        ScanPoint(-1.0, 1e6)
    with pytest.raises(ValueError):
        ScanPoint(2.4e9, 0.0)


def test_the_timing_model_matches_the_measured_pluto_numbers():
    # The model validated on hardware to within 3%: a dwell under the tune-plus-read
    # floor is free, and beyond it the dwell dominates.
    plan = plan_scan([ScanPoint(700e6 + i * 200e6, 2e6) for i in range(4)],
                     sample_rate_hz=30e6)
    assert plan.tunings == 4
    floor = plan.estimated_seconds(0.0)
    assert floor == pytest.approx(4 * 1.822e-3, rel=0.01)
    assert plan.estimated_seconds(1e-3) == pytest.approx(floor, rel=0.01)
    assert plan.estimated_seconds(5e-3) > floor * 2


def test_spacing_inside_the_usable_span_still_groups():
    # Caught a wrong assumption in the hardware suite: 20 MHz spacing at 30 MS/s is
    # inside the 24 MHz usable span, so neighbours share a tuning and the scan uses
    # digital synthesis rather than the RSSI fast path.
    inside = plan_scan([ScanPoint(700e6 + i * 20e6, 2e6) for i in range(4)],
                       sample_rate_hz=30e6)
    assert inside.tunings < 4
    assert inside.metadata["measure_mode"] == "fft"

    outside = plan_scan([ScanPoint(700e6 + i * 50e6, 2e6) for i in range(4)],
                        sample_rate_hz=30e6)
    assert outside.tunings == 4
    assert outside.metadata["measure_mode"] == "rssi"
