"""Does one flushed buffer really separate consecutive instants?

Occupancy is switched by moving TX2's attenuation between an operating value and
the parked -89.75 dB, and the RX DMA runs continuously underneath.  If a buffer
queued before the change survives into the capture, occupied and empty instants
bleed into each other and BOTH f and Pd are wrong -- silently.

Driven at -20 dB where an occupied probe reads ~83 counts and an empty one 1.4,
so a single stale buffer is unmistakable.  Tests flush depths 0, 1 and 2.
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
from leo_tracker.radio.beacon.pilots import edge_pilot_frame         # noqa: E402

PROBE_N = 100_000
ON_DB = -20.0
N = 60
SETTLE_S = 0.03
OUT = HERE / "flushtest.json"


def main():
    guard_signals()
    wave = cyclic_pilot_buffer(edge_pilot_frame(FS_HZ, "lower"), FS_HZ)
    rng = np.random.default_rng(3)
    schedule = [int(rng.random() < 0.5) for _ in range(N)]
    report = {"schedule": schedule, "on_db": ON_DB, "settle_s": SETTLE_S,
              "flush": {}}
    rigs = []
    try:
        for uri, label in (("ip:192.168.1.183", "r183"), ("ip:192.168.1.165", "r165")):
            r = Rig(uri, label).configure()
            r.start_tx(wave, 0.3048)
            r.open_rx(PROBE_N)
            rigs.append(r)
        for flushes in (0, 1, 2):
            got = {r.label: [] for r in rigs}
            for occupied in schedule:
                for r in rigs:
                    r.tx_gain(ON_DB if occupied else TX_IDLE_DB)
                time.sleep(SETTLE_S)
                for _ in range(flushes):
                    for r in rigs:
                        r.capture()
                for r in rigs:
                    got[r.label].append(rms(r.capture()))
            report["flush"][str(flushes)] = got
            mixed = {}
            for label, values in list(got.items()):
                on = [v for v, s in zip(values, schedule) if s]
                off = [v for v, s in zip(values, schedule) if not s]
                # a clean separation means every empty probe sits at the floor
                # and every occupied one far above it
                bad = sum(1 for v in on if v < 20.0) + sum(1 for v in off if v > 20.0)
                print(f"  flush={flushes} {label} occupied min={min(on):7.2f} "
                      f"empty max={max(off):6.2f}  MIXED={bad}/{len(values)}",
                      flush=True)
                mixed[label] = bad
            report.setdefault("mixed", {})[str(flushes)] = mixed
            for r in rigs:
                r.tx_gain(TX_IDLE_DB)
            OUT.write_text(json.dumps(report))
    finally:
        for r in rigs:
            r.close()
        OUT.write_text(json.dumps(report))
        print("parked", [r.tx_gain_read() for r in rigs], flush=True)


if __name__ == "__main__":
    main()
