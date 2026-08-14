"""Find the TX attenuation on each rig that puts detection probability in the
partial band, where coincidence carries information.

Thresholds come from a dedicated quiet block -- both TX chains parked at
-89.75 dB, which bring-up measured as indistinguishable from having no TX
buffer at all -- so every Pd here is measured against genuinely empty input.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from rig import (Rig, rms, FS_HZ, TX_IDLE_DB, cyclic_pilot_buffer,
                 guard_signals)   # noqa: E402
import score as S                                                  # noqa: E402

from leo_tracker.radio.beacon.pilots import edge_pilot_frame        # noqa: E402
from leo_tracker.radio.beacon import survey_scoring as ss           # noqa: E402
from leo_tracker.radio.beacon.survey_comparison import threshold_from  # noqa: E402

PROBE_N = 100_000                     # 20 ms at 5 MS/s
QUIET_N = 140
PER_LEVEL = 24
LEVELS = [-38.0, -44.0, -50.0, -54.0, -58.0, -62.0, -66.0, -70.0, -74.0]
OUT = HERE / "snr_sweep.json"


def collect(rigs, bank, n, tag, gains=None):
    """n captures per rig at the given TX gains, scored, returned per rig."""
    out = {r.label: [] for r in rigs}
    for r in rigs:
        r.tx_gain(TX_IDLE_DB if gains is None else gains[r.label])
    time.sleep(0.10)
    for r in rigs:
        r.capture()                                  # flush the transition
    for k in range(n):
        for r in rigs:
            x = r.capture()
            points = S.score_probe(x, float(FS_HZ), bank)
            row = {"rms": rms(x), "pk_med": S.coarse_peak_median(x, bank),
                   "scores": {name: block["score"]
                              for name, block in points[0]["methods"].items()}
                   if points else {}}
            for extra in points[1:]:                 # keep the max over points
                for name, block in extra["methods"].items():
                    if block["score"] > row["scores"].get(name, -1e9):
                        row["scores"][name] = block["score"]
            out[r.label].append(row)
        if k % 10 == 0:
            print(f"  {tag} {k}/{n}", flush=True)
    return out


def main():
    guard_signals()
    ss.warm((float(FS_HZ),))
    bank = S.banks(float(FS_HZ))
    wave = cyclic_pilot_buffer(edge_pilot_frame(FS_HZ, "lower"), FS_HZ)
    report = {"probe_samples": PROBE_N, "levels_db": LEVELS,
              "quiet_n": QUIET_N, "per_level": PER_LEVEL, "quiet": {}, "levels": {}}
    rigs = []
    try:
        for uri, label in (("ip:192.168.1.183", "r183"), ("ip:192.168.1.165", "r165")):
            r = Rig(uri, label).configure()
            r.start_tx(wave, 0.3048)
            r.open_rx(PROBE_N)
            rigs.append(r)

        print("quiet block", flush=True)
        report["quiet"] = collect(rigs, bank, QUIET_N, "quiet")

        thresholds = {}
        for label, rows in report["quiet"].items():
            names = sorted({n for row in rows for n in row["scores"]})
            thresholds[label] = {
                n: threshold_from([row["scores"].get(n) for row in rows])
                for n in names}
        report["thresholds"] = thresholds

        for gain in LEVELS:
            print(f"level {gain} dB", flush=True)
            got = collect(rigs, bank, PER_LEVEL, f"{gain}dB",
                          gains={r.label: gain for r in rigs})
            report["levels"][f"{gain:g}"] = got
            # live Pd readout
            for label, rows in got.items():
                pd = {}
                for n, t in thresholds[label].items():
                    if t["threshold"] is None:
                        continue
                    fired = sum(1 for row in rows
                                if row["scores"].get(n, -1e9) > t["threshold"])
                    pd[n] = fired / len(rows)
                mean_rms = float(np.mean([row["rms"] for row in rows]))
                print(f"   {label} rms={mean_rms:6.2f} "
                      f"Pd={ {k: round(v,2) for k,v in sorted(pd.items())} }",
                      flush=True)
            OUT.write_text(json.dumps(report))
    finally:
        for r in rigs:
            r.close()
        OUT.write_text(json.dumps(report))
        print("parked", [r.tx_gain_read() for r in rigs], flush=True)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
