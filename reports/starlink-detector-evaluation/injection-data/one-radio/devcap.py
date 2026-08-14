"""Grab a handful of raw probes so the SNR estimator can be built against them."""
from __future__ import annotations

from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig import Rig, pilot_stream, TX_OFF_DB

OUT = Path(__file__).resolve().parent
PROBE = 100_000

rig = Rig()
try:
    rig.configure()
    wave = pilot_stream()
    rig.open_rx(PROBE)
    rig.set_tx2_gain(TX_OFF_DB)
    rig.start_tx(wave)
    time.sleep(0.3)
    rig.flush_rx(3)
    store = {"tx": wave.astype(np.complex64)}
    for gain in (TX_OFF_DB, -75.0, -60.0, -45.0, -30.0):
        rig.set_tx2_gain(gain)
        time.sleep(0.25)
        rig.flush_rx(2)
        for rep in range(2):
            rx1, rx2 = rig.capture()
            store[f"g{gain:.2f}_r{rep}_rx1"] = rx1
            store[f"g{gain:.2f}_r{rep}_rx2"] = rx2
        print("captured", gain, flush=True)
    np.savez(OUT / "devcap.npz", **store)
finally:
    rig.set_tx2_gain(TX_OFF_DB)
    print("final tx2", rig.state()["tx2_gain_db"], flush=True)
    rig.close()
