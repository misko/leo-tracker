"""Compact mirror of the scored sync corpus.

The real sidecars are ~1.6 MB each (adjudication blocks, per-frame score arrays,
raw certificates) and there are 2,339 of them, 4.7 GB in all.  This host has
4 GB of RAM and other services on it, so the full corpus cannot be held in
memory at once.  This script streams every ``sync-*/scores.json`` and writes
back only the fields the cross-radio estimator in
``leo_tracker.radio.beacon.cross_radio`` actually reads, preserving the exact
nested shape so the real module can be run against the mirror unmodified.

Nothing is aggregated, filtered or rounded here.  Every score written is copied
verbatim from the corpus.  ``manifest.json`` is copied byte for byte.

CHANGED FOR THE FULL-CORPUS PASS (2026-08-14), plumbing only:
  * the directory list comes from ``snapshot.py``'s frozen census instead of a
    live glob, because ``leo-sync-import.timer`` is still draining and a glob
    taken per-script would give each figure a different population;
  * a worker pool, because one pass over 4.7 GB serially is ~4 minutes;
  * the mirror is the frozen snapshot for EVERY downstream extractor, not only
    for negative-control and method-correlation.  cfo-cliff/port-bias
    (extract.py) and f-strata (extract_cells.py) read it too, so all six
    figures are computed from one identical set of bytes.
The ``compact`` projection itself is unchanged.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("REFRESH_WORK", HERE.parent / "work"))
CORPUS = Path("/mnt/qnap01/mouse9911/leo/surveys/corpus")
LITE = Path(os.environ.get("LITE_ROOT", WORK / "lite"))
SNAPSHOT = WORK / "snapshot.json"

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


def mirror_one(name: str) -> str:
    source = CORPUS / name
    try:
        scores = json.loads((source / "scores.json").read_text())
    except (OSError, ValueError):
        return "unreadable"
    target = LITE / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "scores.json").write_text(json.dumps(compact(scores)))
    shutil.copyfile(source / "manifest.json", target / "manifest.json")
    return "written"


def main() -> int:
    with open(SNAPSHOT) as handle:
        frozen = json.load(handle)
    names = frozen["scored"]
    LITE.mkdir(parents=True, exist_ok=True)
    tally = {"written": 0, "unreadable": 0}
    with Pool(3) as pool:
        for index, status in enumerate(pool.imap_unordered(mirror_one, names,
                                                           chunksize=8)):
            tally[status] += 1
            if (index + 1) % 400 == 0:
                print(f"  {index + 1}/{len(names)}", file=sys.stderr, flush=True)
    print(json.dumps({**tally, "requested": len(names),
                      "snapshot_digest": frozen["scored_digest"],
                      "lite_root": str(LITE)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
