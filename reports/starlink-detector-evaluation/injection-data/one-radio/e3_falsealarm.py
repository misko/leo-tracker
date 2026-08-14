"""E3 -- the true false-alarm rate on a genuinely empty channel.

TX2 at -89.75 dB, everything else identical to the detection runs.  Two arms:

  ``off``   the DAC still streaming the cyclic pilot buffer through the
            minimum attenuator, which is what every other run calls "TX off"
  ``dark``  the TX DMA buffer not pushed at all -- no waveform anywhere

``dark`` exists to prove ``off`` is empty rather than merely quiet.  If the two
score alike then -89.75 dB is a true null and every threshold below rests on it.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
from rig import Rig, pilot_stream, TX_OFF_DB

OUT = Path(__file__).resolve().parent
PROBES_OFF = 300
PROBES_DARK = 60

if __name__ == "__main__":
    rig = Rig()
    try:
        rig.configure()
        rig.open_rx(runner.PROBE)
        wave = pilot_stream()

        conditions = [{"tag": "off", "tx2_gain_db": TX_OFF_DB, "transmitting": False}]
        runner.run("E3-false-alarm", conditions, probes=PROBES_OFF, rig=rig,
                   wave_for=lambda c: wave, out_path=OUT / "e3_off.jsonl",
                   meta={"arm": "TX2 at -89.75 dB, cyclic pilot buffer running"})

        # Now genuinely dark: cancel the TX DMA buffer entirely.
        rig.stop_tx()
        time.sleep(0.3)
        rig.flush_rx(3)
        pool_out = OUT / "e3_dark.jsonl"
        import multiprocessing as mp
        pool = mp.get_context("spawn").Pool(runner.WORKERS, initializer=runner._init)
        pool_out.write_text(json.dumps(
            {"record": "header", "experiment": "E3-dark", "probe_samples": runner.PROBE,
             "arm": "TX DMA buffer not pushed at all"}) + "\n")
        try:
            done = 0
            while done < PROBES_DARK:
                jobs = []
                for _ in range(min(runner.CHUNK, PROBES_DARK - done)):
                    rx1, rx2 = rig.capture()
                    for receiver, samples in ((1, rx1), (2, rx2)):
                        jobs.append(("dark", receiver, done, runner.pack(samples)))
                    done += 1
                for row in pool.map(runner._score, jobs, chunksize=1):
                    row.update(tx2_gain_db=TX_OFF_DB, transmitting=False,
                               total_power=None, signal_power=None)
                    with pool_out.open("a") as handle:
                        handle.write(json.dumps(row) + "\n")
            print(f"[E3] dark: {PROBES_DARK} probes", flush=True)
        finally:
            pool.close()
            pool.join()
    finally:
        rig.set_tx2_gain(TX_OFF_DB)
        print("final tx2", rig.state()["tx2_gain_db"], flush=True)
        rig.close()
