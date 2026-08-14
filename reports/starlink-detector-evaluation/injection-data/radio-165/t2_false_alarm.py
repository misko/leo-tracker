"""T2 -- the false-alarm rate of all eight detectors on a GENUINELY empty channel.

TX sits at -89.75 dB on both ports for the whole run.  The channel is a closed
cable with no antenna, so this is not "sky with no target code in it" -- it is
sky with no sky.  That is the difference from the cross-edge null the corpus
calibrates on, which is only target-code-free and may hold real energy.

Each probe is scored through the repository's own path, unmodified and imported
rather than reimplemented:

    survey_scoring.search_observation   the searchers propose ~7 points
    survey_scoring.distinct_points      collapse claims naming the same place
    survey_scoring.confirm_points       the eight confirmers score every point,
                                        under the target template AND the
                                        opposite edge's (the corpus's null arm)

Every point therefore yields, per method, a target-arm score and a cross-edge
score from the same IQ.  On a dead channel BOTH are null, which is exactly the
test: a threshold drawn from the cross-edge arm at 1% should fire on 1% of
target-arm points if the two arms are exchangeable.

One JSON object per observation is appended as it completes, so a libiio
teardown segfault at interpreter exit cannot cost the run.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig165 import (ARM_NAME, FS_HZ, OUT, PROBE_MS, PROBE_SAMPLES,  # noqa: E402
                    Rig, TX_OFF_DB, appender)

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import survey_scoring as ss  # noqa: E402

PROBES = int(sys.argv[1]) if len(sys.argv) > 1 else 150
EDGE, NULL_EDGE = "lower", "upper"
RESULTS = OUT / "t2_scores-165.jsonl"


def score(samples: np.ndarray, banks: dict) -> dict:
    """One observation, scored exactly as the corpus scores one."""
    observation = ss.search_observation(samples, FS_HZ, edge=EDGE, banks=banks)
    points = ss.distinct_points(observation["certificates"], FS_HZ)
    confirmed = ss.confirm_points(samples, FS_HZ, points, edge=EDGE,
                                  null_edge=NULL_EDGE)
    # Only what the false-alarm arithmetic reads, so a long run stays small.
    return {
        "points": [{"point_id": point["point_id"],
                    "epoch_sample": point["epoch_sample"],
                    "cfo_hz": point["cfo_hz"],
                    "claimed_by": point["claimed_by"],
                    "methods": {name: {"score": value["score"],
                                       "cross_edge_score": value["cross_edge_score"],
                                       "control_score": value["control_score"],
                                       "control_epoch": value["control_epoch"]}
                                for name, value in point["methods"].items()}}
                   for point in confirmed],
        "searchers": {certificate["method"]: certificate["score"]
                      for certificate in observation["certificates"]},
        "coarse": {name: row["peak_to_median"]
                   for name, row in observation["coarse"].items()},
    }


def main() -> int:
    write = appender(RESULTS)
    rig = None
    try:
        ss.warm(float(FS_HZ))
        banks = ss._banks(EDGE, float(FS_HZ))
        rig = Rig()
        rig.all_tx_off()
        write({"kind": "header", "arm": ARM_NAME, "sample_rate_hz": float(FS_HZ),
               "probe_ms": PROBE_MS, "probe_samples": PROBE_SAMPLES,
               "tx1_gain_db": TX_OFF_DB, "tx2_gain_db": TX_OFF_DB,
               "rx_gain_db": rig.rx_gain_db, "edge": EDGE, "null_edge": NULL_EDGE,
               "probes_requested": PROBES,
               "note": "closed cable, no antenna, TX off on both ports"})
        start = time.time()
        for probe in range(PROBES):
            block = rig.capture(discard=1)
            for receiver in (0, 1):
                values = np.asarray(block[receiver], np.complex64)
                magnitude = np.abs(values)
                record = {"kind": "observation", "probe": probe,
                          "receiver": receiver + 1,
                          "rms_counts": float(np.sqrt(np.mean(magnitude ** 2))),
                          "peak_counts": float(magnitude.max()),
                          **score(values, banks)}
                write(record)
            done = (probe + 1) * 2
            rate = (time.time() - start) / done
            print(f"probe {probe + 1}/{PROBES}  {done} observations  "
                  f"{rate:.2f} s/obs  eta {rate * (PROBES - probe - 1) * 2 / 60:.1f} min",
                  flush=True)
        return 0
    except Exception:                                        # noqa: BLE001
        write({"kind": "error", "traceback": traceback.format_exc()})
        traceback.print_exc()
        return 1
    finally:
        if rig is not None:
            rig.close()
        print("TX confirmed at -89.75 dB on both channels", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
