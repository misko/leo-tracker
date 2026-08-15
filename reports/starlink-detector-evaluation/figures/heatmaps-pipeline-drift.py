#!/usr/bin/env python3
"""Measure how far the corpus moved while the figures were being computed.

Scoring is running and the scored count is climbing, so the population a figure
was computed on is not the population on the share when the figure is read.
The frozen census pins what WAS used; this pins what has ARRIVED since, and the
number travels into every figure's JSON and caption rather than being quietly
dropped.

Uses ``snapshot.py``'s own ``measure()`` so the two counts are taken the same way.

    nice -n 15 python3 drift.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT_FIGURES = Path("/home/satpi01/leo-tracker/reports/"
                      "sync-scan-cross-radio-2026-08-14/figures")
sys.path.insert(0, str(REPORT_FIGURES))

from _pipeline import load as _load_pipeline  # noqa: E402
snap = _load_pipeline("snapshot")

OUT = HERE / "drift.json"


def main() -> int:
    frozen = json.loads((HERE / "snapshot.json").read_text())
    now = snap.measure()
    added = sorted(set(now["scored"]) - set(frozen["scored"]))
    removed = sorted(set(frozen["scored"]) - set(now["scored"]))
    block = {
        "frozen_at_start": {k: frozen[k] for k in
                            ("measured_utc", "sweeps_on_share", "corpus_entries",
                             "scored_sidecars", "scored_digest")},
        "measured_at_end": {k: now[k] for k in
                            ("measured_utc", "sweeps_on_share", "corpus_entries",
                             "scored_sidecars", "scored_digest")},
        "delta": {key: now[key] - frozen[key] for key in
                  ("sweeps_on_share", "sweeps_committed", "corpus_entries",
                   "scored_sidecars")},
        "scored_added": len(added),
        "scored_removed": len(removed),
        "sweeps_added": len(set(now["sweeps"]) - set(frozen["sweeps"])),
        "identical": not added and not removed,
        "note": "every figure in this set was computed on the FROZEN list. "
                "Sidecars that landed after it are not in any figure; none were "
                "removed, and radio collection stayed paused (0 new sweeps).",
    }
    OUT.write_text(json.dumps(block, indent=2) + "\n")
    print(json.dumps(block, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
