"""The sky corpus's own coarse peak-to-median on the 80ms-5.00MSps arm.

Gives the cable's empty-channel coarse statistic something to be read against:
the same bank, the same rate, the same probe length, on sky instead of on a
terminated cable.  Both the target and the cross-edge null arms are collected,
because the interesting comparison is whether sky's *null* arm sits above a
channel that genuinely holds nothing.
"""
from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig165 import OUT  # noqa: E402

CORPUS = "/mnt/qnap01/mouse9911/leo/surveys/corpus"
WANT_ARM = "80ms-5.00MSps"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 300


def main() -> int:
    values: dict = defaultdict(lambda: defaultdict(list))
    entries = 0
    for path in sorted(glob.glob(f"{CORPUS}/sync-*")):
        if entries >= LIMIT:
            break
        entry = Path(path)
        if not (entry / "scores.json").exists():
            continue
        try:
            manifest = json.loads((entry / "manifest.json").read_text())
            record = (manifest.get("metadata", {}).get("pre_dwell_survey", {})
                      .get("synchronised_scan") or {})
            if (record.get("arm") or {}).get("name") != WANT_ARM:
                continue
            scores = json.loads((entry / "scores.json").read_text())
        except Exception:                                    # noqa: BLE001
            continue
        entries += 1
        for observation in scores.get("observations") or []:
            arm = observation.get("arm") or "unknown"
            for name, row in (observation.get("coarse") or {}).items():
                value = row.get("peak_to_median")
                if value is not None:
                    values[arm][name].append(float(value))

    payload = {"corpus": CORPUS, "arm": WANT_ARM, "entries": entries,
               "deployed_gate": 1.33,
               "repo_clean_null_p99_80ms": 1.137, "coarse": {}}
    for arm in sorted(values):
        for name in sorted(values[arm]):
            data = np.asarray(values[arm][name])
            payload["coarse"][f"{arm}/{name}"] = {
                "n": int(data.size), "mean": float(data.mean()),
                "p50": float(np.percentile(data, 50)),
                "p99": float(np.percentile(data, 99)),
                "max": float(data.max()),
                "over_deployed_gate": float((data > 1.33).mean())}
            print(f"{arm:20} coarse-{name}  n={data.size:6d} "
                  f"mean={data.mean():.4f} p50={np.percentile(data, 50):.4f} "
                  f"p99={np.percentile(data, 99):.4f} max={data.max():.4f} "
                  f">1.33: {(data > 1.33).mean() * 100:.2f}%")
    (OUT / "sky_coarse_null-165.json").write_text(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
