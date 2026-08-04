import json
from datetime import datetime, timezone

from leo_tracker.radio.periodic_review import build_review, write_review


def test_periodic_review_counts_doppler_and_spacing_matches(tmp_path):
    root = tmp_path/"watch"; (root/"wide").mkdir(parents=True)
    (root/"chunks").mkdir()
    (root/"status.json").write_text('{"stage":"capturing"}')
    report = {"candidates": [{"start_utc": "2026-08-03T03:00:00Z",
        "stop_utc": "2026-08-03T03:00:30Z", "duration_s": 30,
        "bounding_width_hz": 200_000, "mean_drift_hz_s": -2_000,
        "moving_rf_qualified": True, "doppler_candidate_qualified": True,
        "leo_like_qualified": True, "orbital_shape_qualified": False,
        "internal_translation_path_correlation": .9,
        "common_internal_peak_spacings_hz": [45_000]}]}
    (root/"wide/chunk-a.json").write_text(json.dumps(report))
    now = datetime(2026, 8, 3, 5, tzinfo=timezone.utc)
    result = build_review(root, now=now)
    assert result["doppler_candidate_count"] == 1
    assert result["leo_like_count"] == 1
    assert result["orbital_shape_count"] == 0
    assert result["spacing_matches"][0]["target"] == "starlink_leakage_tone_hz"
    path = write_review(root, tmp_path/"reviews", now=now)
    assert path.is_file()
    assert json.loads((tmp_path/"reviews/latest.json").read_text()) == result
