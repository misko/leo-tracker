"""Preserving survey probes before retention removes them.

The probes live inside capture directories, and both reclamation paths remove
those whole. Measured on the live system, a probe survives about three hours.
Everything the detector plan proposes to measure depends on probes outliving
that, so this runs on the analysis host and copies them somewhere retention
does not reach.
"""
import json

import pytest

from leo_tracker.radio.beacon.survey_corpus import (CORPUS_SCHEMA, entry_path,
                                                    reasons_for, sample,
                                                    corpus_status)


def _capture(root, name, *, peak=1.2, anchors=0, iq=b"\x01\x02\x03\x04",
             state="complete", survey=True):
    """A capture directory shaped the way the live system writes one."""
    directory = root / "captures" / name
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "leo-tracker.pre-dwell-survey/v1", "state": state,
        "threshold": 1.33, "active_count": 0, "active": [],
        "sample_order": [[4, "lower-edge"]],
        "tunings": [{"channel": 4, "region": "lower-edge",
                     "if_center_hz": 1.7e9, "rf_center_hz": 1.1e10,
                     "receivers": [{"receiver": 0, "active": False,
                                    "peak_to_median": peak,
                                    "anchor_agreement": anchors,
                                    "anchor_count": 8}]}]}
    manifest = {"state": "complete", "identity": {"radio_id": "pluto-x"},
                "metadata": {"channel_number": 4, "region": "lower-edge",
                             **({"pre_dwell_survey": record} if survey else {})},
                "survey_iq": {"path": "survey.ci16", "bytes": len(iq)}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "survey.ci16").write_bytes(iq)
    return directory


def test_a_strong_probe_is_kept_for_being_strong(tmp_path):
    _capture(tmp_path, "ch4-narrow-a", peak=2.3, anchors=5)

    assert "strong" in reasons_for(
        json.loads((tmp_path / "captures/ch4-narrow-a/manifest.json").read_text()),
        random_keep=False)


def test_a_quiet_probe_is_kept_only_by_the_random_draw(tmp_path):
    """The corpus must not consist solely of what the current detector likes."""
    manifest = json.loads(
        _capture(tmp_path, "ch4-narrow-b", peak=1.1, anchors=0)
        .joinpath("manifest.json").read_text())

    assert reasons_for(manifest, random_keep=False) == []
    assert reasons_for(manifest, random_keep=True) == ["random"]


def test_every_reason_is_recorded_not_just_the_first(tmp_path):
    """Selection bias can only be corrected for if it is written down."""
    manifest = json.loads(
        _capture(tmp_path, "ch4-narrow-c", peak=2.3, anchors=5)
        .joinpath("manifest.json").read_text())

    assert reasons_for(manifest, random_keep=True) == ["strong", "random"]


def test_sampling_copies_the_iq_and_its_manifest(tmp_path):
    _capture(tmp_path, "ch4-narrow-d", peak=2.3, anchors=5, iq=b"probe-bytes")

    outcome = sample(tmp_path, tmp_path / "corpus", random_fraction=0.0)

    entry = entry_path(tmp_path / "corpus", "ch4-narrow-d")
    assert outcome["kept"] == 1
    assert (entry / "survey.ci16").read_bytes() == b"probe-bytes"
    assert json.loads((entry / "manifest.json").read_text())["state"] == "complete"


def test_the_copy_records_a_digest_of_what_it_copied(tmp_path):
    """A corpus that cannot prove what it holds is not evidence."""
    _capture(tmp_path, "ch4-narrow-e", peak=2.3, anchors=5, iq=b"exactly-this")

    sample(tmp_path, tmp_path / "corpus", random_fraction=0.0)

    entry = json.loads(
        (entry_path(tmp_path / "corpus", "ch4-narrow-e") / "corpus.json").read_text())
    assert entry["schema"] == CORPUS_SCHEMA
    assert entry["reasons"] == ["strong"]
    assert entry["bytes"] == len(b"exactly-this")
    assert len(entry["sha256"]) == 64


def test_sampling_twice_copies_nothing_the_second_time(tmp_path):
    """It runs on a timer beside every analysis; it must be cheap to repeat."""
    _capture(tmp_path, "ch4-narrow-f", peak=2.3, anchors=5)

    first = sample(tmp_path, tmp_path / "corpus", random_fraction=0.0)
    second = sample(tmp_path, tmp_path / "corpus", random_fraction=0.0)

    assert first["kept"] == 1 and second["kept"] == 0
    assert second["already_held"] == 1


def test_a_partial_copy_is_never_mistaken_for_a_probe(tmp_path):
    """Interrupted mid-copy, the next run must redo it rather than trust it."""
    _capture(tmp_path, "ch4-narrow-g", peak=2.3, anchors=5, iq=b"whole")
    corpus = tmp_path / "corpus"
    entry = entry_path(corpus, "ch4-narrow-g")
    entry.mkdir(parents=True)
    (entry / "survey.ci16").write_bytes(b"trunc")     # no corpus.json: incomplete

    outcome = sample(tmp_path, corpus, random_fraction=0.0)

    assert outcome["kept"] == 1
    assert (entry / "survey.ci16").read_bytes() == b"whole"
    assert not list(entry.glob("*.partial"))


def test_a_capture_without_a_survey_is_skipped(tmp_path):
    _capture(tmp_path, "ch4-narrow-h", survey=False)

    outcome = sample(tmp_path, tmp_path / "corpus", random_fraction=1.0)

    assert outcome["kept"] == 0 and outcome["no_survey"] == 1


def test_a_failed_survey_is_skipped(tmp_path):
    """A failed survey has no probes; its IQ would be an empty exhibit."""
    _capture(tmp_path, "ch4-narrow-i", state="failed", peak=2.3)

    outcome = sample(tmp_path, tmp_path / "corpus", random_fraction=1.0)

    assert outcome["kept"] == 0


def test_the_corpus_stops_growing_at_its_budget(tmp_path):
    """Bounded by design: 12.8 MB a probe against a share that is 85% full."""
    for index in range(5):
        _capture(tmp_path, f"ch4-narrow-cap{index}", peak=2.3, anchors=5,
                 iq=b"0123456789")

    outcome = sample(tmp_path, tmp_path / "corpus", random_fraction=0.0,
                     budget_bytes=25)

    assert outcome["kept"] == 2
    assert outcome["budget_reached"] is True


def test_status_reports_what_is_held_against_what_exists(tmp_path):
    _capture(tmp_path, "ch4-narrow-j", peak=2.3, anchors=5)
    _capture(tmp_path, "ch4-narrow-k", peak=1.1)
    sample(tmp_path, tmp_path / "corpus", random_fraction=0.0)

    status = corpus_status(tmp_path, tmp_path / "corpus")

    assert status["held"] == 1
    assert status["available"] == 2
    assert status["by_reason"] == {"strong": 1}


def test_a_probe_survives_its_capture_being_removed(tmp_path):
    """The whole point. Retention removes the capture directory outright."""
    import shutil

    directory = _capture(tmp_path, "ch4-narrow-l", peak=2.3, anchors=5,
                         iq=b"survives")
    sample(tmp_path, tmp_path / "corpus", random_fraction=0.0)

    shutil.rmtree(directory)

    entry = entry_path(tmp_path / "corpus", "ch4-narrow-l")
    assert (entry / "survey.ci16").read_bytes() == b"survives"


def test_sampling_never_raises_on_a_damaged_capture(tmp_path):
    """It runs beside analysis; one bad directory must not stop the sweep."""
    _capture(tmp_path, "ch4-narrow-m", peak=2.3, anchors=5)
    broken = tmp_path / "captures" / "ch4-narrow-broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json")
    (broken / "survey.ci16").write_bytes(b"x")

    outcome = sample(tmp_path, tmp_path / "corpus", random_fraction=0.0)

    assert outcome["kept"] == 1
    assert outcome["unreadable"] == 1
