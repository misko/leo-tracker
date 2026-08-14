"""Two things the coarse sweep cannot answer.

1. LEAK CONTROL.  Occupancy is switched by moving TX2 between an operating
   attenuation and the parked -89.75 dB.  Bring-up showed the parked port reads
   the bare noise floor in rms -- but rms is a blunt instrument next to a
   detector carrying ~30 dB of fold-and-correlate gain, which still called Pd
   1.0 at an rms of 2.17 against a floor of 1.42.  If a parked TX still leaks a
   coherent pilot, the "silent" instants are not empty and f_true is a lie.
   So the parked state is compared against a genuinely dead one -- the cyclic
   buffer destroyed -- through the detectors themselves.

2. A fine attenuation sweep to land each radio's Pd in the partial band.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from rig import (Rig, rms, FS_HZ, TX_IDLE_DB, cyclic_pilot_buffer,   # noqa: E402
                 guard_signals)
import score as S                                                     # noqa: E402

from leo_tracker.radio.beacon.pilots import edge_pilot_frame          # noqa: E402
from leo_tracker.radio.beacon import survey_scoring as ss             # noqa: E402
from leo_tracker.radio.beacon.survey_comparison import threshold_from  # noqa: E402

PROBE_N = 100_000
LEAK_N = 60
FINE_N = 30
OUT = HERE / "fine_sweep.json"


def best_scores(points):
    best = {}
    for point in points:
        for name, block in point["methods"].items():
            value = float(block["score"])
            if value > best.get(name, -1e9):
                best[name] = value
    return best


def block(rigs, bank, n, tag):
    out = {r.label: [] for r in rigs}
    for r in rigs:
        r.capture()
    for k in range(n):
        for r in rigs:
            x = r.capture()
            out[r.label].append({"rms": rms(x),
                                 "scores": best_scores(S.score_probe(x, float(FS_HZ), bank))})
        if k % 20 == 0:
            print(f"  {tag} {k}/{n}", flush=True)
    return out


def main(levels):
    guard_signals()
    ss.warm((float(FS_HZ),))
    bank = S.banks(float(FS_HZ))
    wave = cyclic_pilot_buffer(edge_pilot_frame(FS_HZ, "lower"), FS_HZ)
    report = {"levels_db": levels, "leak": {}, "fine": {}}
    rigs = []
    try:
        for uri, label in (("ip:192.168.1.183", "r183"), ("ip:192.168.1.165", "r165")):
            r = Rig(uri, label).configure()
            r.open_rx(PROBE_N)
            rigs.append(r)

        # --- 1. leak control -------------------------------------------
        print("leak: TX buffer DESTROYED (true dead null)", flush=True)
        for r in rigs:
            r.stop_tx()
            r.park_tx()
        time.sleep(0.2)
        report["leak"]["dead"] = block(rigs, bank, LEAK_N, "dead")

        print("leak: TX buffer RUNNING, parked at -89.75 dB", flush=True)
        for r in rigs:
            r.start_tx(wave, 0.3048)
            r.tx_gain(TX_IDLE_DB)
        time.sleep(0.2)
        report["leak"]["parked"] = block(rigs, bank, LEAK_N, "parked")
        OUT.write_text(json.dumps(report))

        # thresholds from the DEAD null, the most conservative empty input
        thresholds = {}
        for label, rows in report["leak"]["dead"].items():
            names = sorted({n for row in rows for n in row["scores"]})
            thresholds[label] = {n: threshold_from([row["scores"].get(n) for row in rows])
                                 for n in names}
        report["thresholds_dead"] = thresholds
        for label, rows in report["leak"]["parked"].items():
            fires = {}
            for n, t in thresholds[label].items():
                if t["threshold"] is None:
                    continue
                fires[n] = sum(1 for row in rows
                               if row["scores"].get(n, -1e9) > t["threshold"]) / len(rows)
            print(f"  PARKED fire rate vs DEAD threshold {label}: "
                  f"{ {k: round(v,3) for k,v in sorted(fires.items())} }", flush=True)
            report["leak"].setdefault("parked_fire_rate", {})[label] = fires
        OUT.write_text(json.dumps(report))

        # --- 2. fine attenuation sweep ---------------------------------
        for step in range(len(levels["r183"])):
            gain = {lab: levels[lab][step] for lab in levels}
            for r in rigs:
                r.tx_gain(gain[r.label])
            time.sleep(0.1)
            got = block(rigs, bank, FINE_N, str(gain))
            report["fine"][json.dumps(gain)] = got
            for label, rows in got.items():
                pd = {}
                for n, t in thresholds[label].items():
                    if t["threshold"] is None:
                        continue
                    pd[n] = sum(1 for row in rows
                                if row["scores"].get(n, -1e9) > t["threshold"]) / len(rows)
                print(f"  {gain[label]:6.2f}dB {label} rms={np.mean([r_['rms'] for r_ in rows]):5.2f} "
                      f"mean_Pd={np.mean(list(pd.values())):.2f} "
                      f"{ {k: round(v,2) for k,v in sorted(pd.items())} }", flush=True)
            for r in rigs:
                r.tx_gain(TX_IDLE_DB)
            OUT.write_text(json.dumps(report))
    finally:
        for r in rigs:
            r.close()
        OUT.write_text(json.dumps(report))
        print("parked", [r.tx_gain_read() for r in rigs], flush=True)


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
