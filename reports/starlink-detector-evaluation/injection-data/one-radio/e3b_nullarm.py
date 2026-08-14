"""E3b -- the null the *report* actually calibrates on, on a genuinely empty channel.

The repository builds two different cross-edge nulls and they are not the same
statistic:

  (a) ``survey_comparison.conditioned_comparison`` thresholds on
      ``cross_edge_score`` -- the opposite edge's template evaluated at points
      the TARGET-edge detectors selected.  Nothing maximised the opposite-edge
      statistic, so it is an unselected draw compared against a selected one.

  (b) ``cross_radio.null_thresholds`` -- the one the published d, f and the
      5.47-6.74% per-cell figures rest on -- thresholds on ``score`` from a
      separate ``cross-edge-null`` ARM, which runs ``search_observation`` with
      the opposite edge's own bank and takes its own ``distinct_points``.  That
      one maximises on both sides and is properly matched.

E3 measured (a).  This measures (b): TX off, the same empty channel, scored
with the upper edge as its own target exactly as the null arm does.
"""
from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
from rig import Rig, TX_OFF_DB

OUT = Path(__file__).resolve().parent
PROBES = 300


def _score_upper(job):
    import numpy as np
    import pipeline
    tag, receiver, index, raw = job
    flat = np.frombuffer(raw, np.int16).astype(np.float32)
    samples = (flat[0::2] + 1j * flat[1::2]).astype(np.complex64)
    scored = pipeline.score_probe(samples, edge="upper", null_edge=None)
    scored.update(tag=tag, receiver=receiver, index=index)
    return scored


if __name__ == "__main__":
    out = OUT / "e3b_nullarm.jsonl"
    rig = Rig()
    try:
        rig.configure()
        rig.open_rx(runner.PROBE)
        rig.set_tx2_gain(TX_OFF_DB)
        time.sleep(0.3)
        rig.flush_rx(3)
        out.write_text(json.dumps(
            {"record": "header", "experiment": "E3b-null-arm",
             "probe_samples": runner.PROBE,
             "arm": "cross-edge-null arm construction: upper edge as its own "
                    "target, own bank, own points, on a TX-off channel"}) + "\n")
        pool = mp.get_context("spawn").Pool(runner.WORKERS, initializer=runner._init)
        try:
            done = 0
            while done < PROBES:
                jobs = []
                for _ in range(min(runner.CHUNK, PROBES - done)):
                    rx1, rx2 = rig.capture()
                    for receiver, samples in ((1, rx1), (2, rx2)):
                        jobs.append(("nullarm", receiver, done, runner.pack(samples)))
                    done += 1
                rows = pool.map(_score_upper, jobs, chunksize=1)
                with out.open("a") as handle:
                    for row in rows:
                        handle.write(json.dumps(row) + "\n")
            print(f"[E3b] {PROBES} probes", flush=True)
        finally:
            pool.close()
            pool.join()
    finally:
        rig.set_tx2_gain(TX_OFF_DB)
        print("final tx2", rig.state()["tx2_gain_db"], flush=True)
        rig.close()
