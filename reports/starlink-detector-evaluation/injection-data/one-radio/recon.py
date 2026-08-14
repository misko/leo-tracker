"""Locate the transition region so the E2 ladder spends its probes where it matters."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
from rig import Rig, pilot_stream, TX_OFF_DB

OUT = Path(__file__).resolve().parent
GAINS = [TX_OFF_DB, -85.0, -80.0, -75.0, -70.0, -65.0, -60.0, -55.0, -50.0, -40.0]

if __name__ == "__main__":
    rig = Rig()
    try:
        rig.configure()
        rig.open_rx(runner.PROBE)
        wave = pilot_stream()
        conditions = [{"tag": f"g{g:.2f}", "tx2_gain_db": g,
                       "transmitting": g > TX_OFF_DB} for g in GAINS]
        runner.run("recon", conditions, probes=6, rig=rig,
                   wave_for=lambda c: wave, out_path=OUT / "recon.jsonl")
    finally:
        rig.set_tx2_gain(TX_OFF_DB)
        print("final tx2", rig.state()["tx2_gain_db"], flush=True)
        rig.close()
