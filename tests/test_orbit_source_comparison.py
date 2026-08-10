import json

import pytest

from leo_tracker.orbit.source_comparison import (COMPARISON_SCHEMA,
                                                 compare_source_associations)


def _association(path, tracks):
    """Write an association artifact holding the given qualified tracks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "leo-tracker.starlink-tle-association/v2",
        "associations": [
            {"track_id": track_id, "qualified": record is not None,
             "duration_s": 30.0,
             **({} if record is None else {"stability": {"primary": {
                 "best_norad_id": record[0], "best_name": f"STARLINK-{record[0]}",
                 "holdout_residual_rms_hz": record[1],
                 "margin_to_second_hz": record[2],
                 "epoch_adjustment_s": record[3]}}}),
             "candidates": []}
            for track_id, record in tracks.items()]}))


def test_comparison_pairs_tracks_and_scores_identity_agreement(tmp_path):
    """Providers are comparable only on tracks both actually scored.

    Aggregating each provider's qualified set separately would confound element
    quality with which tracks each happened to see.
    """
    root = tmp_path / "reports"
    _association(root / "associations/space-track/rec-a.json",
                 {"track-000": (57622, 120.0, 900.0, -0.5),
                  "track-001": (63700, 200.0, 400.0, 0.4)})
    _association(root / "associations/huggingface/rec-a.json",
                 {"track-000": (57622, 130.0, 850.0, -1.4),
                  "track-001": (11111, 210.0, 300.0, 1.6)})
    # Only one provider qualified this recording at all.
    _association(root / "associations/space-track/rec-b.json",
                 {"track-000": (64668, 90.0, 1200.0, -0.2)})
    _association(root / "associations/huggingface/rec-b.json", {"track-000": None})

    result = compare_source_associations(
        root, sources=("space-track", "huggingface"))

    assert result["schema"] == COMPARISON_SCHEMA
    assert result["paired_track_count"] == 2
    assert result["identity_agreement_count"] == 1
    assert result["identity_disagreement_count"] == 1
    assert result["only_qualified_by"]["space-track"] == 1
    assert result["only_qualified_by"]["huggingface"] == 0
    # The disagreement is reported in full so it can be investigated, not just counted.
    clash = result["disagreements"][0]
    assert clash["recording"] == "rec-a" and clash["track_id"] == "track-001"
    assert clash["space-track"]["norad_id"] == 63700
    assert clash["huggingface"]["norad_id"] == 11111


def test_comparison_metrics_cover_only_the_paired_tracks(tmp_path):
    """A provider must not look better for having qualified an easier track."""
    root = tmp_path / "reports"
    _association(root / "associations/space-track/rec-a.json",
                 {"track-000": (57622, 100.0, 900.0, -0.5)})
    _association(root / "associations/huggingface/rec-a.json",
                 {"track-000": (57622, 300.0, 500.0, -1.5)})
    # An unpaired, very clean space-track result must not move its medians.
    _association(root / "associations/space-track/rec-solo.json",
                 {"track-000": (64668, 1.0, 9999.0, 0.0)})

    result = compare_source_associations(
        root, sources=("space-track", "huggingface"))

    metrics = result["metrics"]
    assert result["paired_track_count"] == 1
    assert metrics["space-track"]["qualified_total"] == 2
    assert metrics["space-track"]["holdout_residual_rms_hz"]["median"] == 100.0
    assert metrics["huggingface"]["holdout_residual_rms_hz"]["median"] == 300.0
    # Epoch adjustment is compared in absolute value; sign is not a quality.
    assert metrics["space-track"]["absolute_epoch_adjustment_s"]["median"] == 0.5
    assert metrics["huggingface"]["absolute_epoch_adjustment_s"]["median"] == 1.5


def test_comparison_tolerates_absent_and_unreadable_artifacts(tmp_path):
    root = tmp_path / "reports"
    _association(root / "associations/space-track/rec-a.json",
                 {"track-000": (57622, 100.0, 900.0, -0.5)})
    (root / "associations/space-track/broken.json").write_text("{not json")
    # huggingface has produced nothing yet; that is a normal early state.

    result = compare_source_associations(
        root, sources=("space-track", "huggingface"))

    assert result["paired_track_count"] == 0
    assert result["metrics"]["space-track"]["qualified_total"] == 1
    assert result["metrics"]["huggingface"]["qualified_total"] == 0
    assert result["metrics"]["space-track"]["holdout_residual_rms_hz"] is None


def test_comparison_requires_two_distinct_sources(tmp_path):
    with pytest.raises(ValueError, match="two distinct sources"):
        compare_source_associations(tmp_path, sources=("space-track", "space-track"))
