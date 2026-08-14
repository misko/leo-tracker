#!/usr/bin/env python3
"""Consolidate everything the two opening figures need into ONE cached extract.

Written once to ``summary/cache/opening-figures.npz`` so that neither figure
re-reads the corpus and neither can measure a different population than the
other.  Four concurrent agents share this box; this is the whole of my read
footprint after the extractors in ``summary/opening/`` have run.

Inputs, all already on disk from those extractors:

  work/snapshot.json      the frozen census (report figures' own snapshot.py)
  work/corpus_pairs.json  pairs joined by cross_radio's own rules + skew
  work/sweeps.json        every committed sweep.json off the read-only share
  work/pilot_spectrum.json the one measured spectrum + the repo's own reference

The one thing measured HERE rather than copied is the skew provenance: each
paired entry's manifest carries a ``skew_basis`` string, and the figure's
central caveat rests on what it says.  Every paired entry is read and the
strings are counted, so "stamped at barrier release" is a census over the
corpus rather than a sentence quoted from a brief.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")

from leo_tracker.radio.beacon.cross_radio import DESIGN_MAX_SKEW_MS  # noqa: E402
from leo_tracker.radio.beacon.synchronised_scan import (  # noqa: E402
    sweep_skew_event,
)

CORPUS = Path("/mnt/qnap01/mouse9911/leo/surveys/corpus")
SCANS = Path("/mnt/qnap01/mouse9911/leo-scans")
HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
CACHE = HERE.parent / "cache" / "opening-figures.npz"


def main() -> int:
    snapshot = json.loads((WORK / "snapshot.json").read_text())
    pairs = json.loads((WORK / "corpus_pairs.json").read_text())
    sweeps = {row["sweep"]: row
              for row in json.loads((WORK / "sweeps.json").read_text())["sweeps"]}
    spectrum = json.loads((WORK / "pilot_spectrum.json").read_text())

    # ---- skew, one row per paired tuning ---------------------------------
    skew, sweep_of, slot, geometry, matched = [], [], [], [], []
    for pair in pairs["pairs"]:
        per_tuning = (pair.get("skew_ms") or {}).get("per_tuning") or []
        for index, value in enumerate(per_tuning):
            if value is None:
                continue
            skew.append(float(value))
            sweep_of.append(pair["paired_sweep"])
            slot.append(index)
            geometry.append(pair["geometry"])
            matched.append(bool(pair["matched_arm"]))

    # ---- what the corpus itself says the stamp means ---------------------
    bases: collections.Counter = collections.Counter()
    for pair in pairs["pairs"]:
        for name in pair["entries"]:
            manifest = json.loads((CORPUS / name / "manifest.json").read_text())
            record = (manifest["metadata"]["pre_dwell_survey"]
                      .get("synchronised_scan") or {})
            bases[str(record.get("skew_basis"))] += 1

    # ---- and what the repository's own guard will certify ----------------
    events: collections.Counter = collections.Counter()
    for name in sorted({f"sync-{value}" for value in sweep_of}):
        try:
            events[sweep_skew_event(
                json.loads((SCANS / name / "sweep.json").read_text()))] += 1
        except ValueError as exc:
            events[f"REFUSED: {exc}"] += 1

    schemas = collections.Counter(row["schema"] for row in sweeps.values())

    # How the one opened capture was chosen: rank 1 of this many, by the
    # deployed coarse detector's own score.  Kept so the figure can say what
    # it was picked out of instead of asserting "a real capture".
    ranked = json.loads((WORK / "highrate_rank.json").read_text())
    scores = sorted((row["coarse_peak_to_median"].get("E") or 0.0)
                    for row in ranked["observations"])
    arm_null = json.loads((WORK / "arm_null.json").read_text())

    provenance = {
        "selection": {
            "arm": ranked["arm"],
            "entries": ranked["entries"],
            "target_observations_ranked": len(scores),
            "ranked_by": "coarse['E'] peak-to-median, the deployed survey bank",
            "chosen_rank": 1,
            "chosen_value": scores[-1],
            "runner_up": scores[-2] if len(scores) > 1 else None,
            "median_of_ranked": float(np.median(scores)),
            "arm_coarse_E_null_threshold":
                (arm_null["coarse_null"].get("E") or {}).get("threshold"),
            "arm_coarse_E_null_n": (arm_null["coarse_null"].get("E") or {}).get("n"),
            "false_alarm_rate": arm_null["false_alarm_rate"],
            "excluded_receivers": arm_null["excluded_receivers"],
        },
        "skew_basis_in_manifests": dict(bases),
        "sweep_skew_event": {key: value for key, value in events.items()},
        "sweep_schemas": dict(schemas),
        "design_max_skew_ms": DESIGN_MAX_SKEW_MS,
        "census": {key: value for key, value in snapshot.items()
                   if key not in ("sweeps", "scored")},
        "pair_census": pairs["census"],
        "pairs": len(pairs["pairs"]),
        "skew_mismatches_manifest_vs_share": len(pairs["skew_mismatches"]),
        "spectrum_meta": {key: value for key, value in spectrum.items()
                          if not isinstance(value, list)
                          or len(value) <= 16},
    }

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        CACHE,
        skew_ms=np.array(skew, dtype=np.float64),
        skew_sweep=np.array(sweep_of),
        skew_slot=np.array(slot, dtype=np.int16),
        skew_geometry=np.array(geometry),
        skew_matched_arm=np.array(matched),
        freqs_hz=np.array(spectrum["freqs_hz"], dtype=np.float64),
        spectrum=np.array(spectrum["spectrum"], dtype=np.float64),
        reference_spectrum=np.array(spectrum["reference_spectrum"],
                                    dtype=np.float64),
        pilot_offsets_hz=np.array(spectrum["pilot_offsets_hz"],
                                  dtype=np.float64),
        provenance=np.array(json.dumps(provenance)),
    )
    print(json.dumps({"tunings": len(skew), "cache": str(CACHE),
                      "bytes": CACHE.stat().st_size,
                      **provenance}, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
