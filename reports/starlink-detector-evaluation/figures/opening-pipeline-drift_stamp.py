#!/usr/bin/env python3
"""Stamp the closing census re-check into both opening figures' JSON sidecars.

The scorer is running and the scored-sidecar count grows while figures are
being computed.  Both figures were computed against ONE frozen census
(``snapshot.py``), which each sidecar already records under ``census_frozen``.
This appends the re-measurement taken after the last figure was written, so the
drift travels with the figures instead of being discovered later.

Run AFTER both figures.  Re-running a figure drops this key, which is correct:
a figure redrawn against a moved corpus has a new drift, not this one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
FIGURES = HERE.parent / "figures"
MINE = ("edge-pilots.json", "apparatus.json")


def main() -> int:
    drift = json.loads((WORK / "drift.json").read_text())
    stamp = {
        "note": "the corpus moved under these figures while they were computed; "
                "both were computed against census_frozen, which is what every "
                "number in them describes",
        "frozen": drift["frozen"],
        "rechecked": drift["now"],
        "delta": drift["delta"],
        "identical": drift["identical"],
        "scored_added": drift["scored_added"],
        "scored_removed": drift["scored_removed"],
        "sweeps_added": drift["sweeps_added"],
        "stamped_by": str(Path(__file__).resolve()),
    }
    for name in MINE:
        path = FIGURES / name
        payload = json.loads(path.read_text())
        payload["census_recheck_at_finish"] = stamp
        path.write_text(json.dumps(payload))
        print("stamped", path)
    print(json.dumps({"delta": stamp["delta"],
                      "scored_added": len(stamp["scored_added"]),
                      "sweeps_added": len(stamp["sweeps_added"])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
