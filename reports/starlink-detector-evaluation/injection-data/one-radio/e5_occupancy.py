"""E5 -- put a known occupancy through the coincidence estimator.

RX1 and RX2 see the same injected pilot through the tee with largely
independent receiver noise -- structurally what the model assumes of two radios
on one sky -- except that here the occupancy is SET rather than inferred.  A
seeded draw decides, per probe, whether the pilot is transmitted at all; the
repository's own ``cross_radio.solve_coincidence`` is then asked to recover it.

Usage: e5_occupancy.py <tx2_gain_db> [f_true] [probes]
"""
from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
from rig import Rig, pilot_stream, TX_OFF_DB

OUT = Path(__file__).resolve().parent
SEED = 20260814


if __name__ == "__main__":
    gain = float(sys.argv[1]) if len(sys.argv) > 1 else -70.0
    f_true = float(sys.argv[2]) if len(sys.argv) > 2 else 0.30
    probes = int(sys.argv[3]) if len(sys.argv) > 3 else 400

    rng = np.random.default_rng(SEED)
    occupied = rng.random(probes) < f_true
    out = OUT / "e5_occupancy.jsonl"

    rig = Rig()
    try:
        rig.configure()
        rig.open_rx(runner.PROBE)
        wave = pilot_stream()
        rig.start_tx(wave)
        tx_fft_conj, tx_energy = runner.tx_reference(wave)
        out.write_text(json.dumps(
            {"record": "header", "experiment": "E5-occupancy",
             "probe_samples": runner.PROBE, "tx2_gain_db": gain,
             "f_true": float(occupied.mean()), "f_requested": f_true,
             "probes": probes, "seed": SEED,
             "occupancy_draw": "independent Bernoulli per probe; the pilot is "
                               "transmitted or the transmitter is at -89.75 dB",
             "note": ("RX1 and RX2 share one Pluto LO, so their noise is not "
                      "fully independent -- measured common-mode power is "
                      "reported beside the estimate")}) + "\n")

        pool = mp.get_context("spawn").Pool(runner.WORKERS, initializer=runner._init)
        try:
            done = 0
            while done < probes:
                jobs, extras = [], []
                for _ in range(min(runner.CHUNK, probes - done)):
                    want = bool(occupied[done])
                    rig.set_tx2_gain(gain if want else TX_OFF_DB)
                    time.sleep(0.05)
                    rig.flush_rx(1)
                    rx1, rx2 = rig.capture()
                    cross = complex(np.vdot(np.asarray(rx1, np.complex128),
                                            np.asarray(rx2, np.complex128))
                                    / rx1.size)
                    for receiver, samples in ((1, rx1), (2, rx2)):
                        jobs.append((f"p{done}", receiver, done,
                                     runner.pack(samples)))
                        extras.append({**runner.snr_terms(samples, tx_fft_conj,
                                                          tx_energy),
                                       "occupied": want,
                                       "cross_receiver_power": abs(cross)})
                    done += 1
                rows = pool.map(runner._score, jobs, chunksize=1)
                with out.open("a") as handle:
                    for row, extra in zip(rows, extras):
                        row.update(extra)
                        row.update(transmitting=extra["occupied"],
                                   tx2_gain_db=gain if extra["occupied"] else TX_OFF_DB)
                        handle.write(json.dumps(row) + "\n")
                print(f"[E5] {done}/{probes}", flush=True)
        finally:
            pool.close()
            pool.join()
    finally:
        rig.set_tx2_gain(TX_OFF_DB)
        print("final tx2", rig.state()["tx2_gain_db"], flush=True)
        rig.close()
