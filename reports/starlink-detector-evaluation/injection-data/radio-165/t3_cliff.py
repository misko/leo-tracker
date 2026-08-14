"""T3 -- does detection really collapse between 350 and 400 kHz?

On sky the collapse is measured against a BIAS-CORRECTED ESTIMATE of the
offset, produced by the same pipeline whose sensitivity is in question.  Here
the offset is IMPOSED: the pilot frame is multiplied by exp(2j*pi*f*t) before
it reaches the DAC, so f is known by construction and no estimator sits between
the axis and the answer.  That is what makes this settle the question rather
than restate it.

The rig's own carrier offset is ~0 by construction -- TX and RX run from one
XO, and T1 measured the coarse bank returning 0 Hz on the loopback -- so the
offset the detectors face IS the offset imposed here.

Two TX gains are swept over the same offsets.  If the cliff sits at the same
place at both, it is structural: a property of the search, not of sensitivity.
If it moves with power, it is an SNR effect and the sky reading needs rethinking.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig165 import (ARM_NAME, FS_HZ, OUT, PROBE_MS, PROBE_SAMPLES,  # noqa: E402
                    Rig, appender, snap_offset_hz, tx_waveform)

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import survey_scoring as ss  # noqa: E402

EDGE, NULL_EDGE = "lower", "upper"
RESULTS = OUT / "t3_cliff-165.jsonl"

#: Dense through 250-500 kHz, where the sky corpus puts the collapse, coarse
#: outside it.  0 is included as the reference every offset is read against.
OFFSETS_HZ = [0, 50_000, 100_000, 150_000, 200_000, 250_000, 275_000, 300_000,
              325_000, 350_000, 375_000, 400_000, 425_000, 450_000, 475_000,
              500_000, 550_000, 600_000, 700_000, 800_000]

PROBES_PER_POINT = 3
RX_PEAK_CEILING = 1500.0


def score(samples: np.ndarray, banks: dict) -> dict:
    observation = ss.search_observation(samples, FS_HZ, edge=EDGE, banks=banks)
    points = ss.distinct_points(observation["certificates"], FS_HZ)
    confirmed = ss.confirm_points(samples, FS_HZ, points, edge=EDGE,
                                  null_edge=NULL_EDGE)
    return {
        "points": [{"point_id": point["point_id"],
                    "epoch_sample": point["epoch_sample"],
                    "cfo_hz": point["cfo_hz"],
                    "claimed_by": point["claimed_by"],
                    "methods": {name: {"score": value["score"],
                                       "cross_edge_score": value["cross_edge_score"]}
                                for name, value in point["methods"].items()}}
                   for point in confirmed],
        "searchers": {certificate["method"]: certificate["score"]
                      for certificate in observation["certificates"]},
        "coarse": {name: row["peak_to_median"]
                   for name, row in observation["coarse"].items()},
        "coarse_offset_hz": {name: row["frequency_offset_hz"]
                             for name, row in observation["coarse"].items()},
    }


def main() -> int:
    gains = [float(value) for value in (sys.argv[1:] or ["-20", "-50"])]
    write = appender(RESULTS)
    rig = None
    try:
        ss.warm(float(FS_HZ))
        banks = ss._banks(EDGE, float(FS_HZ))
        rig = Rig()
        write({"kind": "header", "arm": ARM_NAME, "sample_rate_hz": float(FS_HZ),
               "probe_ms": PROBE_MS, "probe_samples": PROBE_SAMPLES,
               "rx_gain_db": rig.rx_gain_db, "tx_port": 2, "edge": EDGE,
               "null_edge": NULL_EDGE, "offsets_hz": OFFSETS_HZ,
               "tx_gains_db": gains, "probes_per_point": PROBES_PER_POINT,
               "note": "CABLED LOOPBACK TX2 -> split -> RX1+RX2, offset imposed "
                       "on the waveform before the DAC"})
        start, done = time.time(), 0
        total = len(gains) * len(OFFSETS_HZ) * PROBES_PER_POINT * 2
        for gain in gains:
            for nominal in OFFSETS_HZ:
                offset = snap_offset_hz(nominal)
                wave = tx_waveform(offset_hz=offset)
                status = rig.load(wave)
                rig.set_tx_gain(tx2_db=gain)
                for probe in range(PROBES_PER_POINT):
                    block = rig.capture(discard=1)
                    for receiver in (0, 1):
                        values = np.asarray(block[receiver], np.complex64)
                        magnitude = np.abs(values)
                        record = {"kind": "observation", "tx_gain_db": gain,
                                  "offset_nominal_hz": nominal,
                                  "offset_hz": offset, "probe": probe,
                                  "receiver": receiver + 1,
                                  "rms_counts": float(np.sqrt(np.mean(magnitude ** 2))),
                                  "peak_counts": float(magnitude.max()),
                                  "load_attempts": status["attempts"],
                                  **score(values, banks)}
                        write(record)
                        done += 1
                        if magnitude.max() > RX_PEAK_CEILING:
                            write({"kind": "abort", "reason": "rx peak ceiling",
                                   "peak_counts": float(magnitude.max())})
                            raise RuntimeError("RX peak ceiling exceeded")
                rig.all_tx_off()
                elapsed = time.time() - start
                print(f"gain {gain:+.0f} dB  offset {nominal / 1e3:6.1f} kHz  "
                      f"{done}/{total}  eta "
                      f"{(elapsed / done) * (total - done) / 60:.1f} min", flush=True)
        return 0
    except Exception:                                        # noqa: BLE001
        write({"kind": "error", "traceback": traceback.format_exc()})
        traceback.print_exc()
        return 1
    finally:
        if rig is not None:
            rig.close()
        print("TX returned to -89.75 dB on both channels", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
