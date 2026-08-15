"""An absolute centre measured elsewhere must survive the daily recalibration.

`measure_mismatch` can only difference the two ports, and `write_calibration`
replaces the artifact wholesale, so before this the only way to hold an absolute
pair was to mask the timer. These cover the carry and, more importantly, the
case where carrying is the dangerous thing to do.
"""

import json

import pytest

from leo_tracker.radio.beacon.lnb_calibration import (CENTRE_DISAGREEMENT_HZ,
                                                      carry_measured_centers,
                                                      load_calibration,
                                                      receiver_centers,
                                                      write_calibration)


def artifact(mismatch_hz, centres=None, *, measured=True, created="2026-08-14T00:00:00Z"):
    entry = {"measured": measured, "mismatch_hz": mismatch_hz,
             "receiver_labels": ["lnb-a", "lnb-b"],
             "receiver_candidate_counts": [10, 20]}
    if centres is not None:
        entry["measured_centers_hz"] = list(centres)
    return {"created_utc": created, "radios": {"pluto-5d4d": entry}}


def test_absolute_centres_survive_a_recalibration():
    previous = artifact(5_154.0, [-140_204.8, -145_359.1])
    current = artifact(5_100.0)
    alerts = carry_measured_centers(previous, current)
    carried = current["radios"]["pluto-5d4d"]["measured_centers_hz"]
    assert carried == [-140_204.8, -145_359.1]
    assert alerts == []


def test_the_carried_pair_is_what_receiver_centers_returns():
    # The whole point: without the carry this resolves to (mismatch, 0.0), which
    # leaves both ports free to sit 150 kHz off together.
    previous = artifact(5_154.0, [-140_204.8, -145_359.1])
    current = artifact(5_100.0)
    carry_measured_centers(previous, current)
    assert receiver_centers(current, "pluto-5d4d") == (-140_204.8, -145_359.1)


def test_a_stale_pair_is_carried_but_alerts():
    # lnb-a's oscillator moved 567 kHz while its recorded centre stayed put.
    # Carrying silently would steer the search away from the signal for good.
    previous = artifact(5_154.0, [-140_204.8, -145_359.1])
    current = artifact(567_402.0)
    alerts = carry_measured_centers(previous, current)
    assert len(alerts) == 1
    assert alerts[0]["stale_absolute"] is True
    assert alerts[0]["radio_id"] == "pluto-5d4d"
    assert alerts[0]["measured_mismatch_hz"] == 567_402.0
    # Still applied: discarding a hand-measured absolute on one noisy run would
    # be the worse failure. It is applied loudly instead of quietly.
    assert current["radios"]["pluto-5d4d"]["measured_centers_hz"] == [-140_204.8, -145_359.1]


def test_drift_exactly_at_the_gate_is_not_an_alert():
    previous = artifact(0.0, [CENTRE_DISAGREEMENT_HZ, 0.0])
    current = artifact(0.0)
    assert carry_measured_centers(previous, current) == []


def test_drift_just_past_the_gate_alerts():
    previous = artifact(0.0, [CENTRE_DISAGREEMENT_HZ + 1.0, 0.0])
    current = artifact(0.0)
    assert len(carry_measured_centers(previous, current)) == 1


def test_nothing_to_carry_is_not_an_error():
    assert carry_measured_centers(artifact(5_154.0), artifact(5_100.0)) == []
    assert carry_measured_centers({}, artifact(5_100.0)) == []
    assert carry_measured_centers(None, artifact(5_100.0)) == []


def test_an_unmeasured_run_still_carries_without_claiming_agreement():
    # Nothing to compare against, so the pair is kept and no staleness is
    # asserted either way.
    previous = artifact(5_154.0, [-140_204.8, -145_359.1])
    current = artifact(None, measured=False)
    assert carry_measured_centers(previous, current) == []
    assert current["radios"]["pluto-5d4d"]["measured_centers_hz"] == [-140_204.8, -145_359.1]


def test_a_malformed_pair_is_ignored_rather_than_half_applied():
    for broken in ([], [1.0], [1.0, 2.0, 3.0]):
        current = artifact(5_100.0)
        carry_measured_centers(artifact(5_154.0, broken), current)
        assert "measured_centers_hz" not in current["radios"]["pluto-5d4d"]


def test_the_carry_records_where_it_came_from(tmp_path):
    previous = artifact(5_154.0, [-140_204.8, -145_359.1], created="2026-08-13T00:00:00Z")
    current = artifact(5_100.0)
    carry_measured_centers(previous, current)
    entry = current["radios"]["pluto-5d4d"]
    assert entry["measured_centers_carried_from"] == "2026-08-13T00:00:00Z"


def test_it_survives_a_round_trip_through_the_stored_artifact(tmp_path):
    # The end-to-end shape of the failure: write, reload, recalibrate, rewrite.
    write_calibration(tmp_path, artifact(5_154.0, [-140_204.8, -145_359.1]))
    previous = load_calibration(tmp_path)
    current = artifact(5_100.0)
    carry_measured_centers(previous, current)
    write_calibration(tmp_path, current)
    reloaded = load_calibration(tmp_path)
    assert receiver_centers(reloaded, "pluto-5d4d") == (-140_204.8, -145_359.1)
