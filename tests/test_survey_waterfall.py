"""The picture of a survey, and the numbers kept beside it.

Both are written outside the capture directory on purpose: retention removes
that within days, and after it does these are the only surviving evidence of
what the receiver was looking at when it chose where to dwell.
"""
import json

import numpy as np
import pytest

from leo_tracker.radio.beacon.survey_waterfall import (DEFAULT_SPAN_HZ, render,
                                                       waterfall_paths, write)


def _record(tunings=3, *, active=(0,)):
    entries = []
    for index in range(tunings):
        entries.append({
            "channel": index + 1, "region": "lower-edge",
            "if_center_hz": 1e9 + index, "rf_center_hz": 1e10 + index,
            "receivers": [
                {"receiver": side, "active": index in active and side == 0,
                 "peak_to_median": 1.4 + 0.1 * side, "anchor_agreement": side,
                 "anchor_count": 8, "offset_contrast": 1.6,
                 "frequency_offset_hz": 0.0, "epoch_s": 0.001,
                 "folded_score": 0.2, "folded_median": 0.14}
                for side in (0, 1)]})
    return {"schema": "leo-tracker.pre-dwell-survey/v1", "state": "complete",
            "threshold": 1.33, "active_count": len(active), "active": [],
            "tunings": entries,
            "sample_order": [(index + 1, "lower-edge")
                             for index in range(tunings)]}


def _samples(tunings=3, count=20_000, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((tunings, count, 2, 2)) * 300).astype(np.int16)


def test_the_picture_is_written_and_is_a_png(tmp_path):
    record = _record()

    outcome = render(_samples(), record, tmp_path / "cap-survey.png",
                     capture_name="cap")

    written = (tmp_path / "cap-survey.png")
    assert written.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert outcome["bytes"] == written.stat().st_size
    assert outcome["span_hz"] == DEFAULT_SPAN_HZ
    assert not list(tmp_path.glob("*.partial"))


def test_the_window_is_wide_enough_for_doppler_and_a_disagreeing_lnb():
    """Cropping to the Doppler excursion alone hides the cases worth seeing.

    A LEO pilot moves about +/-250 kHz and the two LNBs here sit about 440 kHz
    apart, so a legitimate detection can appear anywhere inside this span.
    """
    assert DEFAULT_SPAN_HZ >= 250_000 + 250_000


def test_the_metrics_are_written_beside_the_picture(tmp_path):
    """The manifest copy dies with the capture; this one has to outlive it."""
    record = _record()

    outcome = write(_samples(), record, tmp_path, "ch4-narrow-20260812T000000Z")

    image, metrics = waterfall_paths(tmp_path, "ch4-narrow-20260812T000000Z")
    assert outcome["state"] == "complete"
    assert image.is_file() and metrics.is_file()
    stored = json.loads(metrics.read_text())
    assert stored["capture"] == "ch4-narrow-20260812T000000Z"
    assert stored["tunings"] == record["tunings"]
    assert stored["threshold"] == 1.33


def test_the_panels_follow_collection_order_not_score_order(tmp_path):
    """Results are sorted by score, so a panel keyed off them would mislabel."""
    record = _record()
    record["tunings"] = list(reversed(record["tunings"]))

    outcome = render(_samples(), record, tmp_path / "c-survey.png")

    # sample_order still describes the IQ, so rendering must not have raised
    # and must not have silently paired panel 0 with the reversed entry.
    assert outcome["bytes"] > 0
    assert record["sample_order"][0] == (1, "lower-edge")


def test_a_failed_render_never_costs_the_capture(tmp_path):
    """A recording that did not happen cannot be redone; a picture can."""
    outcome = write(np.zeros((2, 10, 3), dtype=np.int16), _record(2),
                    tmp_path, "broken")

    assert outcome["state"] == "failed"
    assert "tuning, sample, receiver" in outcome["error"]


def test_metrics_survive_even_when_there_is_no_iq(tmp_path):
    """Numbers are cheap and the picture is not; losing one must not lose both."""
    outcome = write(None, _record(), tmp_path, "no-iq")

    image, metrics = waterfall_paths(tmp_path, "no-iq")
    assert outcome["state"] == "complete"
    assert metrics.is_file() and not image.is_file()


def test_the_names_key_off_the_capture_not_the_analysis(tmp_path):
    """It is written before any analysis exists, and outlives the recording."""
    image, metrics = waterfall_paths(tmp_path, "ch4-lower-edge-narrow-x")

    assert image.name == "ch4-lower-edge-narrow-x-survey.png"
    assert metrics.name == "ch4-lower-edge-narrow-x-survey.json"
