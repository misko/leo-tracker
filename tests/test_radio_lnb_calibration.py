import json

import pytest

from leo_tracker.radio.beacon.lnb_calibration import (ALERT_THRESHOLD_HZ,
                                                      CALIBRATION_SCHEMA,
                                                      compare_calibration,
                                                      load_calibration,
                                                      measure_mismatch,
                                                      receiver_centers,
                                                      write_calibration)


def _report(path, *, radio="pluto-a", labels=("lnb-a", "lnb-b"), pairs=(),
            singles=()):
    """A narrow report carrying per-receiver acquisition offsets."""
    def check(rx0, rx1, candidate):
        return {"candidate": candidate, "receivers": [
            {"acquisition": {"exact_match": {"frequency_offset_hz": rx0}}},
            {"acquisition": {"exact_match": {"frequency_offset_hz": rx1}}}]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "capture_manifest": {"identity": {"radio_id": radio,
                                          "receiver_labels": list(labels)}},
        "exact_checks": [check(a, b, True) for a, b in pairs]
                        + [check(a, b, False) for a, b in singles]}))


def test_mismatch_is_the_paired_difference(tmp_path):
    """Differencing the receivers cancels Doppler and the shared tuner error."""
    reports = tmp_path / "reports"
    for index in range(50):
        # A large common term stands in for Doppler; it must not survive.
        doppler = 1000.0 * index
        _report(reports / f"ch4-lower-edge-narrow-{index:03d}.json",
                pairs=[(400_000.0 + doppler, doppler)])

    result = measure_mismatch(reports)

    assert result["schema"] == CALIBRATION_SCHEMA
    entry = result["radios"]["pluto-a"]
    assert entry["measured"] is True
    assert entry["mismatch_hz"] == pytest.approx(400_000.0, abs=1.0)
    assert entry["receiver_labels"] == ["lnb-a", "lnb-b"]


def test_only_dual_candidates_are_measured(tmp_path):
    """A single-receiver hit says nothing about the pair's disagreement."""
    reports = tmp_path / "reports"
    for index in range(50):
        _report(reports / f"ch4-lower-edge-narrow-{index:03d}.json",
                pairs=[(100_000.0, 0.0)], singles=[(999_999.0, 0.0)])

    entry = measure_mismatch(reports)["radios"]["pluto-a"]

    assert entry["mismatch_hz"] == pytest.approx(100_000.0, abs=1.0)


def test_too_few_pairs_is_reported_rather_than_guessed(tmp_path):
    """A median over a handful of pairs is noise, and must not be stored."""
    reports = tmp_path / "reports"
    _report(reports / "ch4-lower-edge-narrow-000.json", pairs=[(1.0, 0.0)])

    entry = measure_mismatch(reports)["radios"]["pluto-a"]

    assert entry["measured"] is False
    assert entry["sample_count"] < 40


def test_centring_anchors_on_the_port_that_detects_more():
    """The freely-detecting port is the trustworthy reference.

    A port outside the search only matches when Doppler carries it inside, so
    its own estimate is biased toward the boundary. Anchoring on its healthy
    twin and placing it by the measured difference corrects it fully, where
    splitting the difference would leave it half outside.
    """
    calibration = {"radios": {"pluto-a": {
        "measured": True, "mismatch_hz": 435_000.0,
        "receiver_candidate_counts": [184, 1631]}}}

    assert receiver_centers(calibration, "pluto-a") == (435_000.0, 0.0)


def test_centring_follows_whichever_port_is_healthier():
    """The offset port can be either one; the rule must not assume an index."""
    calibration = {"radios": {"pluto-a": {
        "measured": True, "mismatch_hz": -435_000.0,
        "receiver_candidate_counts": [1631, 184]}}}

    assert receiver_centers(calibration, "pluto-a") == (0.0, 435_000.0)


def test_a_matched_pair_is_left_alone():
    """Two ports that agree need no correction beyond their small difference."""
    calibration = {"radios": {"pluto-a": {
        "measured": True, "mismatch_hz": 3_755.0,
        "receiver_candidate_counts": [1239, 960]}}}

    assert receiver_centers(calibration, "pluto-a") == (0.0, -3_755.0)


def test_an_unmeasured_radio_gets_no_correction():
    assert receiver_centers({"radios": {}}, "pluto-a") == (0.0, 0.0)


def test_thermal_wander_does_not_raise_an_alert():
    """The value moves a few kHz diurnally; alerting on that is noise."""
    before = {"radios": {"r": {"measured": True, "mismatch_hz": 435_000.0}}}
    after = {"radios": {"r": {"measured": True, "mismatch_hz": 443_000.0}}}

    assert compare_calibration(before, after) == []


def test_a_moved_cable_raises_an_alert():
    """A swap shifts the mismatch at once, which no timer alone would catch."""
    before = {"radios": {"r": {"measured": True, "mismatch_hz": 435_000.0}}}
    after = {"radios": {"r": {"measured": True, "mismatch_hz": 5_000.0}}}

    alerts = compare_calibration(before, after)

    assert len(alerts) == 1
    assert alerts[0]["radio_id"] == "r"
    assert alerts[0]["shift_hz"] == pytest.approx(-430_000.0)
    assert abs(alerts[0]["shift_hz"]) > ALERT_THRESHOLD_HZ


def test_exchanged_ports_are_named_as_such():
    """A sign flip means relabelling, not recalibration."""
    before = {"radios": {"r": {"measured": True, "mismatch_hz": 435_000.0}}}
    after = {"radios": {"r": {"measured": True, "mismatch_hz": -434_000.0}}}

    alerts = compare_calibration(before, after)

    assert alerts[0]["ports_exchanged"] is True
    assert "exchanged" in alerts[0]["reason"]


def test_a_first_measurement_is_not_an_alert():
    """There is nothing to compare against on the first run."""
    assert compare_calibration({}, {"radios": {
        "r": {"measured": True, "mismatch_hz": 435_000.0}}}) == []


def test_calibration_round_trips_through_storage(tmp_path):
    value = {"schema": CALIBRATION_SCHEMA,
             "radios": {"r": {"measured": True, "mismatch_hz": 1.0}}}

    write_calibration(tmp_path, value)

    assert load_calibration(tmp_path) == value


def test_a_missing_calibration_reads_as_empty(tmp_path):
    """A first run must not fail for want of a previous measurement."""
    assert load_calibration(tmp_path) == {}
