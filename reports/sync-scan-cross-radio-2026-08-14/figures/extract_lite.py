"""Compact mirror of the scored sync corpus.

The real sidecars are ~1.6 MB each (adjudication blocks, per-frame score arrays,
raw certificates).  This host has 4 GB of RAM and a live collector on it, so the
full corpus cannot be held in memory at once.  This script streams every
``sync-*/scores.json`` and writes back only the fields the cross-radio estimator
in ``leo_tracker.radio.beacon.cross_radio`` actually reads, preserving the exact
nested shape so the real module can be run against the mirror unmodified.

Nothing is aggregated, filtered or rounded here.  Every score written is copied
verbatim from the corpus.  ``manifest.json`` is copied byte for byte.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

CORPUS = Path("/mnt/qnap01/mouse9911/leo/surveys/corpus")
LITE = Path(os.environ.get(
    "LITE_ROOT",
    "/tmp/claude-1000/-home-satpi01-leo-tracker/"
    "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/lite"))

# Entry-level keys _entry() reads off the scores document.
SCORE_KEYS = ("schema", "capture", "radio_id", "sample_rate_hz",
              "samples_per_tuning", "probe_ms", "pilot_guard_hz",
              "receiver_centers_hz", "truth_available", "calibration_applied")

# Observation-level keys the join, the thresholds and the geometry read.
OBS_KEYS = ("arm", "template_edge", "null_direction", "iq_index", "channel",
            "region", "edge", "receiver", "receiver_label", "utc")


def compact(scores: dict) -> dict:
    out = {key: scores[key] for key in SCORE_KEYS if key in scores}
    observations = []
    for observation in scores.get("observations") or []:
        row = {key: observation.get(key) for key in OBS_KEYS}
        points = []
        for point in observation.get("points") or []:
            methods = {}
            for method, values in (point.get("methods") or {}).items():
                # ``observation_fires`` reads only ``score``; ``frame_max`` is
                # kept because it is the alternative combiner the report names.
                methods[method] = {"score": values.get("score"),
                                   "frame_max": values.get("frame_max")}
            points.append({"point_id": point.get("point_id"),
                           "epoch_sample": point.get("epoch_sample"),
                           "cfo_hz": point.get("cfo_hz"),
                           "methods": methods})
        row["points"] = points
        observations.append(row)
    out["observations"] = observations
    return out


def main() -> int:
    LITE.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for directory in sorted(CORPUS.glob("sync-*")):
        scores_path = directory / "scores.json"
        manifest_path = directory / "manifest.json"
        if not scores_path.is_file() or not manifest_path.is_file():
            skipped += 1
            continue
        try:
            scores = json.loads(scores_path.read_text())
        except (OSError, ValueError):
            skipped += 1
            continue
        target = LITE / directory.name
        target.mkdir(exist_ok=True)
        (target / "scores.json").write_text(json.dumps(compact(scores)))
        shutil.copyfile(manifest_path, target / "manifest.json")
        del scores
        written += 1
    print(json.dumps({"written": written, "skipped_no_scores": skipped,
                      "lite_root": str(LITE)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
