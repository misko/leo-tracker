#!/usr/bin/env python3
"""snapshot: freeze the corpus once, so all six figures share one census.

Every previous pass in this project measured its own population while the
importer was still writing, and the report then had to reconcile figures that
disagreed about how much data existed.  Radio collection is paused, so the
sweep share is frozen, but ``leo-sync-import.timer`` is still draining a
backlog into the survey corpus -- the scored-sidecar count moved 2330 -> 2335
while this directory was being read.

So the population is pinned HERE, once, and written to ``snapshot.json``:

  * the sweep directories on the QNAP scan share,
  * the corpus entries,
  * the corpus entries that carry BOTH ``scores.json`` and ``manifest.json``.

Every downstream extractor reads that list rather than re-globbing, so a
sidecar that lands mid-run cannot change one figure and not another.  Re-run
this at the end to measure drift; ``snapshot.py --verify`` does exactly that
without overwriting the frozen list.

Read-only: /mnt/qnap01 and /mnt/leo-nvme are never written.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import sys

SCANS = "/mnt/qnap01/mouse9911/leo-scans"
CORPUS = "/mnt/qnap01/mouse9911/leo/surveys/corpus"
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("REFRESH_WORK", os.path.join(os.path.dirname(HERE), "work"))
OUT = os.path.join(WORK, "snapshot.json")


def measure() -> dict:
    sweeps = sorted(os.path.basename(p)
                    for p in glob.glob(os.path.join(SCANS, "sync-*"))
                    if os.path.isdir(p))
    committed = [name for name in sweeps
                 if os.path.exists(os.path.join(SCANS, name, "sweep.json"))]
    entries = sorted(os.path.basename(p)
                     for p in glob.glob(os.path.join(CORPUS, "sync-*"))
                     if os.path.isdir(p))
    scored, scored_no_manifest = [], []
    for name in entries:
        base = os.path.join(CORPUS, name)
        if not os.path.isfile(os.path.join(base, "scores.json")):
            continue
        if os.path.isfile(os.path.join(base, "manifest.json")):
            scored.append(name)
        else:
            scored_no_manifest.append(name)
    return {
        "measured_utc": dt.datetime.now(dt.timezone.utc)
                          .isoformat(timespec="seconds"),
        "sweeps_on_share": len(sweeps),
        "sweeps_committed": len(committed),
        "corpus_entries": len(entries),
        "scored_sidecars": len(scored),
        "scored_without_manifest": len(scored_no_manifest),
        "first_scored": scored[0] if scored else None,
        "last_scored": scored[-1] if scored else None,
        "scored_digest": hashlib.sha256(
            "\n".join(scored).encode()).hexdigest()[:16],
        "sweeps": sweeps,
        "scored": scored,
    }


def summary(state: dict) -> dict:
    return {key: value for key, value in state.items()
            if key not in ("sweeps", "scored")}


def load() -> dict:
    with open(OUT) as handle:
        return json.load(handle)


def main(argv: list[str]) -> int:
    state = measure()
    if "--verify" in argv:
        try:
            frozen = load()
        except OSError:
            print("no frozen snapshot to verify against", file=sys.stderr)
            return 1
        drift = {
            "frozen": summary(frozen),
            "now": summary(state),
            "delta": {key: state[key] - frozen[key]
                      for key in ("sweeps_on_share", "sweeps_committed",
                                  "corpus_entries", "scored_sidecars")},
            "scored_added": sorted(set(state["scored"]) - set(frozen["scored"])),
            "scored_removed": sorted(set(frozen["scored"]) - set(state["scored"])),
            "sweeps_added": sorted(set(state["sweeps"]) - set(frozen["sweeps"])),
        }
        drift["identical"] = (not drift["scored_added"]
                              and not drift["scored_removed"]
                              and not drift["sweeps_added"])
        print(json.dumps(drift, indent=2))
        return 0
    os.makedirs(WORK, exist_ok=True)
    with open(OUT, "w") as handle:
        json.dump(state, handle)
    print(json.dumps(summary(state), indent=2))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
