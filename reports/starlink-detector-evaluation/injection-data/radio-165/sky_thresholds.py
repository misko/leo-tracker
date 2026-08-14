"""Draw the sky corpus's own thresholds for the 80ms-5.00MSps arm.

The point is to judge this radio's empty-channel firing against the SAME
numbers the sky survey is judged against, so the thresholds are taken from the
corpus rather than re-derived: same 1% rate, same cross-edge null arm, same
per-point quantile, via the repository's own ``threshold_from``.

Only the arm that matches the rig geometry is read -- ``threshold_key`` is
``(sample_rate_hz, probe_ms)`` and a threshold from another length is a
threshold for another detector.

Also records the corpus's per-cell false-alarm rate for that arm, computed with
``cross_radio.observation_fires`` so the "any of ~7 points" rule is the
repository's rule rather than a private reimplementation.
"""
from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig165 import OUT  # noqa: E402

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402

CORPUS = "/mnt/qnap01/mouse9911/leo/surveys/corpus"
WANT_ARM = "80ms-5.00MSps"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def main() -> int:
    entries = []
    scanned = skipped_arm = unscored = 0
    for path in sorted(glob.glob(f"{CORPUS}/sync-*")):
        if len(entries) >= LIMIT:
            break
        entry = Path(path)
        manifest_path, scores_path = entry / "manifest.json", entry / "scores.json"
        if not scores_path.exists():
            unscored += 1
            continue
        scanned += 1
        try:
            manifest = json.loads(manifest_path.read_text())
            record = (manifest.get("metadata", {}).get("pre_dwell_survey", {})
                      .get("synchronised_scan") or {})
            if (record.get("arm") or {}).get("name") != WANT_ARM:
                skipped_arm += 1
                continue
            scores = json.loads(scores_path.read_text())
            entries.append(cr._entry(entry, scores, manifest))
        except Exception:                                    # noqa: BLE001
            continue
        if len(entries) % 10 == 0:
            print(f"  {len(entries)} entries on {WANT_ARM} "
                  f"({scanned} scanned)", flush=True)

    if not entries:
        print(f"no scored entries on {WANT_ARM}")
        return 1

    methods = cr.methods_in(entries)
    thresholds = cr.null_thresholds(entries)
    false_alarm = cr.cell_false_alarm(entries, thresholds)

    keys = sorted({key for _, key in thresholds})
    payload = {
        "corpus": CORPUS, "arm": WANT_ARM, "entries": len(entries),
        "scanned": scanned, "skipped_other_arm": skipped_arm,
        "unscored": unscored, "methods": methods,
        "threshold_keys": [list(key) for key in keys],
        "thresholds": {},
        "per_cell_false_alarm": {m: false_alarm[m] for m in methods
                                 if m in false_alarm},
    }
    for key in keys:
        payload["thresholds"][json.dumps(list(key))] = {
            method: thresholds[(method, key)]
            for method in methods if (method, key) in thresholds}

    (OUT / "sky_thresholds-165.json").write_text(json.dumps(payload, indent=2))
    print(f"\n{WANT_ARM}: {len(entries)} scored entries, keys {keys}")
    for key in keys:
        print(f"\nthreshold_key {key}")
        for method in methods:
            row = thresholds.get((method, key))
            if not row:
                continue
            cell = false_alarm.get(method, {})
            print(f"  {method:22} thr={row['threshold']:.4f} "
                  f"n={row['samples']:6d} supported={row['supported']} "
                  f"effective={row['effective_rate']:.4f} "
                  f"per_cell={cell.get('rate')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
