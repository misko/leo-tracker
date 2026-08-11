"""Hardware tests for the scanner. Excluded from the default offline suite.

Run with an attached Pluto+:

    LEO_SCANNER_URI=usb:1.90.5 pytest -m hardware tests/test_scanner_hardware.py
"""
from pathlib import Path
import os
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from scanner.execute import execute_scan  # noqa: E402
from scanner.plan import ScanPoint, plan_scan  # noqa: E402

pytestmark = pytest.mark.hardware

URI = os.environ.get("LEO_SCANNER_URI")
SERIAL = os.environ.get("LEO_SCANNER_SERIAL")


def _radio(**kwargs):
    if not URI:
        pytest.skip("set LEO_SCANNER_URI to run the scanner hardware tests")
    from scanner.pluto import PlutoScanRadio
    return PlutoScanRadio(URI, expect_serial=SERIAL, **kwargs)


def test_the_adapter_refuses_a_mid_scan_bandwidth_change():
    """The guard that stops a ~14.3 ms filter recalibration per point."""
    with _radio() as radio:
        radio.configure(sample_rate_hz=30e6, analog_bandwidth_hz=30e6)
        radio.configure(sample_rate_hz=30e6, analog_bandwidth_hz=30e6)   # idempotent
        with pytest.raises(RuntimeError, match="refusing to change"):
            radio.configure(sample_rate_hz=30e6, analog_bandwidth_hz=4e6)


def test_a_wrong_serial_is_refused_because_addresses_move():
    if not URI:
        pytest.skip("set LEO_SCANNER_URI")
    from scanner.pluto import PlutoScanRadio
    with pytest.raises(RuntimeError, match="expected"):
        PlutoScanRadio(URI, expect_serial="0" * 32)


def test_closing_releases_the_usb_claim_so_the_next_open_succeeds():
    first = _radio()
    first.close()
    second = _radio()
    second.close()


def test_an_rssi_scan_meets_the_documented_rate():
    """Uniform isolated points take the RSSI path; the floor is ~1.9 ms per point.

    The spacing must exceed the usable span (24 MHz at 30 MS/s), otherwise the planner
    correctly groups neighbours into one tuning and switches to digital synthesis.
    """
    points = [ScanPoint(700e6 + i * 50e6, 2e6) for i in range(20)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.metadata["measure_mode"] == "rssi"
    with _radio() as radio:
        report = execute_scan(radio, plan, dwell_s=0.0)
    assert len(report.results) == 20
    assert all(r.mode == "rssi" for r in report.results)
    per_point = report.elapsed_s / len(report.results)
    assert per_point < 4e-3, f"{per_point*1e3:.2f} ms/point is far above the 1.9 ms floor"


def test_fastlock_is_not_slower_than_retuning():
    """Measured on the bench at ~1.06 ms/point against ~1.87 ms; allow slack."""
    points = [ScanPoint(700e6 + i * 25e6, 2e6) for i in range(8)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    with _radio() as radio:
        plain = execute_scan(radio, plan, dwell_s=0.0)
    with _radio() as radio:
        stored = radio.prepare_fastlock([g.tune_hz for g in plan.groups])
        assert len(stored) == 8
        quick = execute_scan(radio, plan, dwell_s=0.0)
    assert quick.elapsed_s <= plain.elapsed_s * 1.2


def test_grouped_points_are_measured_from_one_tuning():
    points = [ScanPoint(2.400e9 + i * 1e6, 200e3) for i in range(12)]
    plan = plan_scan(points, sample_rate_hz=30e6)
    assert plan.tunings == 1
    with _radio() as radio:
        report = execute_scan(radio, plan, dwell_s=0.001)
    assert len(report.results) == 12
    assert all(r.tune_hz == plan.groups[0].tune_hz for r in report.results)


def test_the_rssi_scale_can_be_calibrated_against_the_fft_scale():
    plan = plan_scan([ScanPoint(868e6, 2e6)], sample_rate_hz=30e6)
    with _radio() as radio:
        radio.configure(sample_rate_hz=plan.sample_rate_hz,
                        analog_bandwidth_hz=plan.analog_bandwidth_hz)
        radio.tune(868e6)
        offset = radio.calibrate_rssi_offset()
        assert -200.0 < offset < 200.0
        assert radio.identity()["rssi_offset_db"] == offset
