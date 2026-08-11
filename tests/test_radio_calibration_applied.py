"""A missing calibration must be loud, not silently uncorrected.

The analysis falls back to zero offsets whenever the calibration cannot be
found. That is the right behaviour for a radio that has never been calibrated
and the wrong behaviour for one that has, because the two are indistinguishable
in the output: a wrong path produces the same reports as a correct one, and the
only symptom is detections quietly failing to appear.
"""
import json

import pytest

from leo_tracker.radio.beacon.lnb_calibration import (load_calibration,
                                                      receiver_centers,
                                                      write_calibration)


def _calibration(tmp_path, radio="pluto-19f2"):
    write_calibration(tmp_path, {
        "schema": "leo-tracker.lnb-calibration/v1",
        "radios": {radio: {
            "measured": True, "receiver_labels": ["lnb-c", "lnb-d"],
            "mismatch_hz": 440_000.0,
            "measured_centers_hz": [380_000.0, -60_000.0],
            "receiver_candidate_counts": [1, 1]}}})


def test_calibration_is_found_where_it_is_written(tmp_path):
    _calibration(tmp_path)
    assert receiver_centers(load_calibration(tmp_path), "pluto-19f2") == (
        380_000.0, -60_000.0)


def test_a_wrong_root_silently_yields_no_correction(tmp_path):
    """This is the failure that cost a day: it looks exactly like success."""
    _calibration(tmp_path / "shared")
    elsewhere = tmp_path / "local"
    elsewhere.mkdir()

    assert receiver_centers(load_calibration(elsewhere), "pluto-19f2") == (0.0, 0.0)


def test_calibration_status_names_why_no_correction_was_applied(tmp_path):
    """A caller must be able to tell 'never calibrated' from 'could not find it'."""
    from leo_tracker.radio.beacon.lnb_calibration import calibration_status

    _calibration(tmp_path / "shared")
    found = calibration_status(tmp_path / "shared", "pluto-19f2")
    assert found["applied"] is True
    assert found["centers_hz"] == [380_000.0, -60_000.0]

    missing = calibration_status(tmp_path / "local", "pluto-19f2")
    assert missing["applied"] is False
    assert missing["reason"] == "no calibration artifact at this root"

    unknown = calibration_status(tmp_path / "shared", "pluto-does-not-exist")
    assert unknown["applied"] is False
    assert unknown["reason"] == "radio not present in the calibration"


def test_a_calibrated_radio_reading_zero_is_reported_as_a_problem(tmp_path):
    """A radio with a large known mismatch must never quietly run uncorrected."""
    from leo_tracker.radio.beacon.lnb_calibration import calibration_status

    _calibration(tmp_path / "shared")
    status = calibration_status(tmp_path / "local", "pluto-19f2")

    assert status["applied"] is False
    assert status["centers_hz"] == [0.0, 0.0]


def test_the_watcher_points_at_the_store_that_holds_the_calibration():
    """The calibration is written beside the shared reports, not on local disk.

    Defaulting the lookup to the capture storage root sends it to a filesystem
    the artifact is never written to, which is how this failed in the field.
    """
    text = open("scripts/starlink-beacon-watch.sh").read()
    line = next(l for l in text.splitlines() if l.startswith("calibration_root="))
    assert "${storage_root}" not in line, (
        "calibration_root must not default to the capture storage root; "
        "the artifact lives with the shared reports")


def test_the_analysis_server_points_at_the_shared_store():
    text = open("scripts/starlink-analysis-server.sh").read()
    line = next(l for l in text.splitlines() if l.startswith("calibration_root="))
    assert "/mnt/qnap01/mouse9911/leo" in line


def test_the_report_records_whether_a_correction_was_applied(tmp_path):
    """An uncorrected run must be visible in its own output.

    Without this, a wrong lookup path produces reports identical to a correct
    run and the only symptom is detections quietly not appearing.
    """
    import argparse
    from leo_tracker.radio import cli

    capture = tmp_path / "cap"
    capture.mkdir()
    (capture / "manifest.json").write_text(json.dumps(
        {"identity": {"radio_id": "pluto-19f2"}}))
    _calibration(tmp_path / "shared")

    good = cli._calibration_status_for(argparse.Namespace(
        capture=capture, calibration_root=tmp_path / "shared",
        receiver_center_offsets_hz=None))
    assert good["applied"] is True
    assert good["centers_hz"] == [380_000.0, -60_000.0]
    assert good["radio_id"] == "pluto-19f2"

    wrong_root = cli._calibration_status_for(argparse.Namespace(
        capture=capture, calibration_root=tmp_path / "local",
        receiver_center_offsets_hz=None))
    assert wrong_root["applied"] is False
    assert "no calibration artifact" in wrong_root["reason"]


def test_an_explicit_override_is_recorded_as_such(tmp_path):
    """A replay testing a hypothesis must not look like a calibrated run."""
    import argparse
    from leo_tracker.radio import cli

    status = cli._calibration_status_for(argparse.Namespace(
        capture=tmp_path, calibration_root=None,
        receiver_center_offsets_hz=[1000.0, -1000.0]))

    assert status["applied"] is True and status["reason"] == "explicit override"
