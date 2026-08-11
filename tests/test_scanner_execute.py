"""Offline tests for band-power synthesis and the scan executor. No radio, no network."""
from pathlib import Path
import json
import math
import subprocess
import sys

import numpy as np
import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scanner.execute import (FakeScanRadio, band_power_dbfs,  # noqa: E402
                            execute_scan)
from scanner.plan import ScanPoint, plan_scan  # noqa: E402


def _tone(amplitude, offset_hz, *, sample_rate_hz=30e6, n=8192):
    t = np.arange(n) / sample_rate_hz
    return amplitude * np.exp(2j * np.pi * offset_hz * t)


def test_a_full_scale_tone_reads_zero_dbfs():
    samples = _tone(1.0, 1e6)
    dbfs, bins, outside = band_power_dbfs(
        samples, sample_rate_hz=30e6, tune_hz=2.4e9, center_hz=2.401e9,
        bandwidth_hz=200e3)
    assert dbfs == pytest.approx(0.0, abs=0.2)
    assert bins > 0 and not outside


def test_band_power_tracks_amplitude_squared():
    for amplitude, expected in ((1.0, 0.0), (0.1, -20.0), (0.01, -40.0)):
        dbfs, _, _ = band_power_dbfs(
            _tone(amplitude, 1e6), sample_rate_hz=30e6, tune_hz=2.4e9,
            center_hz=2.401e9, bandwidth_hz=200e3)
        assert dbfs == pytest.approx(expected, abs=0.2)


def test_the_result_is_independent_of_fft_size():
    samples = _tone(0.25, -2e6, n=16384)
    powers = [band_power_dbfs(samples, sample_rate_hz=30e6, tune_hz=1e9,
                              center_hz=998e6, bandwidth_hz=400e3, fft_size=size)[0]
              for size in (1024, 4096, 8192)]
    assert max(powers) - min(powers) < 0.3


def test_a_band_with_no_signal_reports_far_below_the_tone():
    samples = _tone(1.0, 1e6)
    quiet, _, _ = band_power_dbfs(samples, sample_rate_hz=30e6, tune_hz=2.4e9,
                                  center_hz=2.409e9, bandwidth_hz=200e3)
    loud, _, _ = band_power_dbfs(samples, sample_rate_hz=30e6, tune_hz=2.4e9,
                                 center_hz=2.401e9, bandwidth_hz=200e3)
    assert loud - quiet > 40


def test_a_band_off_the_edge_of_the_capture_is_flagged():
    samples = _tone(0.5, 0.0)
    _, _, outside = band_power_dbfs(samples, sample_rate_hz=30e6, tune_hz=2.4e9,
                                    center_hz=2.418e9, bandwidth_hz=1e6)
    assert outside is True


def test_two_tones_in_one_capture_are_separated_by_bandwidth():
    samples = _tone(0.5, 1e6) + _tone(0.05, 5e6)
    strong, _, _ = band_power_dbfs(samples, sample_rate_hz=30e6, tune_hz=2.4e9,
                                   center_hz=2.401e9, bandwidth_hz=200e3)
    weak, _, _ = band_power_dbfs(samples, sample_rate_hz=30e6, tune_hz=2.4e9,
                                 center_hz=2.405e9, bandwidth_hz=200e3)
    assert strong == pytest.approx(-6.0, abs=0.3)
    assert weak == pytest.approx(-26.0, abs=0.3)


def test_the_executor_sets_the_analog_bandwidth_exactly_once():
    points = [ScanPoint(2.4e9 + i * 1e6, 200e3) for i in range(6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    radio = FakeScanRadio(tones_hz=[2.4e9, 2.402e9])
    execute_scan(radio, plan)
    assert radio.configure_calls == 1
    assert radio.bandwidth_writes == [plan.analog_bandwidth_hz]


def test_results_come_back_in_the_requested_order_not_the_tune_order():
    points = [ScanPoint(1.1e9, 1e6), ScanPoint(700e6, 1e6), ScanPoint(900e6, 1e6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    report = execute_scan(FakeScanRadio(), plan)
    assert [r.center_hz for r in report.results] == [1.1e9, 700e6, 900e6]


def test_a_synthetic_tone_is_found_at_the_point_that_contains_it():
    points = [ScanPoint(2.401e9, 400e3), ScanPoint(2.405e9, 400e3)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    report = execute_scan(FakeScanRadio(tones_hz=[2.401e9], amplitude=0.2), plan)
    found, empty = report.results
    assert found.power_dbfs == pytest.approx(-13.98, abs=0.5)
    assert found.power_dbfs - empty.power_dbfs > 40
    assert empty.below_floor and not found.below_floor


def test_power_is_also_reported_input_referred_by_the_gain():
    plan = plan_scan([ScanPoint(2.401e9, 400e3)], sample_rate_hz=30e6)
    report = execute_scan(FakeScanRadio(tones_hz=[2.401e9], gain_db=40.0), plan)
    result = report.results[0]
    assert result.power_input_referred_db == pytest.approx(result.power_dbfs - 40.0, abs=1e-6)


def test_the_clipped_flag_is_carried_through_from_the_radio():
    plan = plan_scan([ScanPoint(2.401e9, 400e3)], sample_rate_hz=30e6)
    report = execute_scan(FakeScanRadio(tones_hz=[2.401e9], overloaded=True), plan)
    assert report.results[0].clipped is True
    assert report.metadata["clipped_points"] == 1


def test_an_unknown_overload_state_stays_unknown_rather_than_becoming_false():
    plan = plan_scan([ScanPoint(2.401e9, 400e3)], sample_rate_hz=30e6)
    report = execute_scan(FakeScanRadio(tones_hz=[2.401e9], overloaded=None), plan)
    assert report.results[0].clipped is None


def test_the_rssi_path_averages_over_the_dwell_in_the_power_domain():
    points = [ScanPoint(700e6, 2e6), ScanPoint(900e6, 2e6)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.metadata["measure_mode"] == "rssi"
    radio = FakeScanRadio(tones_hz=[700e6], amplitude=0.3)
    report = execute_scan(radio, plan, dwell_s=0.002)
    assert all(math.isfinite(r.power_dbfs) for r in report.results)
    assert report.results[0].power_dbfs > report.results[1].power_dbfs
    assert report.results[0].mode == "rssi"


def test_a_negative_dwell_is_refused():
    plan = plan_scan([ScanPoint(700e6, 2e6)], sample_rate_hz=30e6)
    with pytest.raises(ValueError):
        execute_scan(FakeScanRadio(), plan, dwell_s=-1e-3)


def test_the_cli_runs_offline_and_writes_a_report(tmp_path):
    out = tmp_path / "scan.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/scanner/scan.py"), "--dry-run",
         "--tones", "2401e6", "--point", "2401e6:400e3", "--point", "2409e6:400e3",
         "--sample-rate", "30e6", "--json", str(out)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "2 points -> 1 tunings" in result.stdout
    payload = json.loads(out.read_text())
    assert payload["plan"]["bandwidth_changes"] == 1
    assert len(payload["results"]) == 2
    assert payload["results"][0]["power_dbfs"] > payload["results"][1]["power_dbfs"]


def test_the_cli_refuses_an_empty_request():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/scanner/scan.py"), "--dry-run"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "nothing to scan" in result.stderr
