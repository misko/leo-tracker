"""E4 -- the 350-400 kHz cliff, against a KNOWN imposed offset.

On sky the offset is estimated, so a collapse in detection could be the
pipeline's search span or could be the estimator failing.  Here the offset is
applied to the waveform before it is transmitted -- exp(2j.pi.f.t) on the pilot
frame -- so the abscissa is imposed rather than inferred and the question is
settled.

The offsets are snapped to multiples of 250 Hz because the TX buffer is cyclic
over 20000 samples (4 ms): any other offset would step in phase at the wrap and
smear the very thing being measured.

The received power of the offset waveform is measured at every rung, so a
collapse caused by the analog 5 MHz filter rolling off at high offset can be
told apart from a collapse caused by the search span.
"""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
from rig import Rig, pilot_stream, snap_offset_hz, TX_OFF_DB

OUT = Path(__file__).resolve().parent
PROBES = int(sys.argv[2]) if len(sys.argv) > 2 else 30
# Default is well above threshold for all eight at zero offset.  A second pass
# near the detection knee answers the obvious objection to the first: offset
# tolerance could be bought with SNR, and a sweep run only at +4.7 dB could not
# tell.  Passed on the command line so both passes run the same code.
GAIN_DB = float(sys.argv[1]) if len(sys.argv) > 1 else -50.0
TAG = f"e4_offset{'' if GAIN_DB == -50.0 else f'_{abs(int(GAIN_DB))}'}"

# Dense to 500 kHz because that is where the on-sky cliff is claimed, then out
# past the coarse-E bank's own +/-700 kHz span so a collapse at the search edge
# and a collapse at 350-400 kHz cannot be confused for one another.
if GAIN_DB == -50.0:
    OFFSETS = ([snap_offset_hz(f) for f in np.arange(0, 500_001, 25_000)]
               + [snap_offset_hz(f) for f in np.arange(550_000, 1_000_001, 50_000)])
else:
    # Coarser grid: this pass only has to say whether the knee MOVES.
    OFFSETS = [snap_offset_hz(f) for f in
               (0, 100_000, 200_000, 300_000, 350_000, 400_000, 450_000,
                500_000, 600_000, 700_000, 800_000, 900_000)]

if __name__ == "__main__":
    rig = Rig()
    waves: dict = {}
    try:
        rig.configure()
        rig.open_rx(runner.PROBE)
        conditions = [{"tag": f"f{int(f)}", "tx2_gain_db": GAIN_DB,
                       "offset_hz": float(f), "transmitting": True}
                      for f in OFFSETS]

        def wave_for(condition):
            key = condition["offset_hz"]
            if key not in waves:
                waves.clear()
                waves[key] = pilot_stream(offset_hz=key)
            return waves[key]

        runner.run("E4-offset", conditions, probes=PROBES, rig=rig,
                   wave_for=wave_for, out_path=OUT / f"{TAG}.jsonl",
                   meta={"tx2_gain_db": GAIN_DB,
                         "offsets_hz": [float(f) for f in OFFSETS],
                         "offset_applied": "exp(2j*pi*f*t) on the pilot frame "
                                           "before transmission -- imposed, not "
                                           "estimated",
                         "coarse_search_spans_hz":
                             {"A": 300_000.0, "E": 700_000.0},
                         "candidate_seed_bank": "E"})
    finally:
        rig.set_tx2_gain(TX_OFF_DB)
        print("final tx2", rig.state()["tx2_gain_db"], flush=True)
        rig.close()
