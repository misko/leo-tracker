"""E2 -- detection probability against measured SNR, all eight detectors.

The ladder is dense where the recon put the transition (TX2 -78 to -62 dB,
SNR -23 to -8 dB) and coarse either side, so the probes are spent where the
curve has shape rather than spread evenly over 70 dB of nothing happening.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
from rig import Rig, pilot_stream, TX_OFF_DB

OUT = Path(__file__).resolve().parent
PROBES = 80

LADDER = ([-85.0, -80.0]
          + [-78.0, -76.0, -74.0, -72.0, -70.0, -68.0, -66.0, -64.0, -62.0]
          + [-60.0, -55.0, -50.0, -45.0, -40.0, -30.0, -20.0])

if __name__ == "__main__":
    import numpy as np
    # The receiver warms over a 35 minute run -- the E3 null drifted +0.22 dB
    # in five minutes -- so the rungs are visited in a shuffled order and the
    # TX-off reference is taken three times through the run.  A monotonic
    # ladder would put that drift straight onto the SNR axis.
    order = list(LADDER)
    np.random.default_rng(20260814).shuffle(order)
    third = len(order) // 3
    sequence = ([TX_OFF_DB] + order[:third] + [TX_OFF_DB]
                + order[third:2 * third] + [TX_OFF_DB] + order[2 * third:])

    rig = Rig()
    try:
        rig.configure()
        rig.open_rx(runner.PROBE)
        wave = pilot_stream()
        conditions = [{"tag": f"g{g:.2f}#{i}", "tx2_gain_db": g,
                       "transmitting": g > TX_OFF_DB}
                      for i, g in enumerate(sequence)]
        runner.run("E2-roc", conditions, probes=PROBES, rig=rig,
                   wave_for=lambda c: wave, out_path=OUT / "e2_roc.jsonl",
                   meta={"ladder_db": LADDER, "visit_order_db": sequence,
                         "order": "shuffled, with three TX-off references, so "
                                  "receiver thermal drift cannot alias onto the "
                                  "SNR axis",
                         "snr_estimator": "coherent projection onto the known "
                                          "cyclic TX stream, debiased by the "
                                          "mean over lags"})
    finally:
        rig.set_tx2_gain(TX_OFF_DB)
        print("final tx2", rig.state()["tx2_gain_db"], flush=True)
        rig.close()
