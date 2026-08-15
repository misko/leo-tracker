"""What TX drive puts the detectors near their threshold on this cable.

The factorial's whole logic is "watch where Pd falls, and see which knob moves
it".  At the report's own -20 dB drive this path delivers ~27 dB SNR and every
detector reads Pd = 1.00 everywhere, which answers the bandwidth question in one
direction only: it can show that no bandwidth setting *creates* a cliff, but a
matrix of ones cannot show a cliff *moving*.

So the drive is measured first, and the factorial is run at a level where the
headline statistic still clears its bar at zero offset with room to fall.  Same
rig, same detectors, same thresholds as the sweep; the only thing swept is
``tx_hardwaregain``.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig_cfobw import (PROBE_MS, REF_BW_HZ, RX_GAIN_DB, Rig,  # noqa: E402
                       appender, power_row, snap_offset_hz, tx_waveform)

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import survey_scoring as ss  # noqa: E402

EDGE, NULL_EDGE = "lower", "upper"

SCRATCH = Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
               "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/cfobw")
RESULTS = SCRATCH / "drive_ladder-165.jsonl"

GAINS_DB = (-20.0, -35.0, -45.0, -50.0, -55.0, -60.0, -65.0, -70.0, -75.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=5_000_000.0)
    parser.add_argument("--bandwidth", type=float, default=5_000_000.0)
    parser.add_argument("--offsets", type=float, nargs="*", default=[0.0, 400_000.0])
    parser.add_argument("--gains", type=float, nargs="*", default=list(GAINS_DB))
    parser.add_argument("--captures", type=int, default=4)
    parser.add_argument("--out", default=str(RESULTS))
    arguments = parser.parse_args()

    write = appender(Path(arguments.out))
    rig = None
    try:
        ss.warm(arguments.rate)
        banks = ss._banks(EDGE, arguments.rate)
        rig = Rig(rx_gain_db=RX_GAIN_DB)
        rig.set_rate(arguments.rate)
        write({"kind": "header", "uri": "ip:192.168.1.165",
               "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "context": rig.context_attrs, "rx_gain_db": rig.rx_gain_db,
               "sample_rate_hz": arguments.rate,
               "rx_bandwidth_hz": arguments.bandwidth,
               "probe_ms": PROBE_MS, "gains_db": arguments.gains,
               "offsets_hz": arguments.offsets, "captures": arguments.captures})

        rig.set_rx_bandwidth(arguments.bandwidth)
        floor = float(np.median([
            power_row(np.asarray(rig.capture(discard=1)[receiver], np.complex64))["rms_counts"]
            for _ in range(3) for receiver in (0, 1)]))
        rig.set_rx_bandwidth(REF_BW_HZ)
        reference_floor = float(np.median([
            power_row(np.asarray(rig.capture(discard=1)[receiver], np.complex64))["rms_counts"]
            for _ in range(3) for receiver in (0, 1)]))
        write({"kind": "floor", "floor_rms_counts": floor,
               "reference_floor_rms_counts": reference_floor})

        for nominal in arguments.offsets:
            offset = snap_offset_hz(nominal, arguments.rate)
            wave = tx_waveform(arguments.rate, EDGE, offset)
            for gain in arguments.gains:
                # The DMA is proved at the sweep's own drive, then the gain is
                # dropped: a -75 dB port cannot pass a "did it start" check, and
                # a check it cannot pass would reject the very cells this ladder
                # exists to reach.
                rig.load(wave, -20.0, floor_rms=reference_floor)
                rig.set_rx_bandwidth(arguments.bandwidth)
                rig.set_tx_gain(tx2_db=gain)
                time.sleep(0.1)
                scores, powers = [], []
                for probe in range(arguments.captures):
                    block = rig.capture(discard=1)
                    for receiver in (0, 1):
                        values = np.asarray(block[receiver], np.complex64)
                        row = power_row(values)
                        observation = ss.search_observation(values, arguments.rate,
                                                            edge=EDGE, banks=banks)
                        points = ss.distinct_points(observation["certificates"],
                                                    arguments.rate)
                        confirmed = ss.confirm_points(values, arguments.rate, points,
                                                      edge=EDGE, null_edge=NULL_EDGE)
                        best = max((point["methods"]["full-frame-full"]["score"]
                                    for point in confirmed), default=None)
                        cross = max((point["methods"]["full-frame-full"]["cross_edge_score"]
                                     for point in confirmed), default=None)
                        scores.append(best)
                        powers.append(row["mean_power_counts2"])
                        write({"kind": "observation", "tx_gain_db": gain,
                               "offset_nominal_hz": nominal, "offset_hz": offset,
                               "probe": probe, "receiver": receiver + 1,
                               "sample_rate_hz": arguments.rate,
                               "rx_bandwidth_hz": arguments.bandwidth,
                               "full_frame_full": best,
                               "full_frame_full_cross_edge": cross,
                               "coarse_A": observation["coarse"]["A"]["peak_to_median"],
                               "coarse_E": observation["coarse"]["E"]["peak_to_median"],
                               "points": len(points), **row})
                rig.all_tx_off()
                snr = 10 * np.log10(max(np.mean(powers) - floor ** 2, 1e-12) / floor ** 2)
                print(f"CFO {nominal/1e3:6.0f} kHz  TX {gain:+6.1f} dB  "
                      f"snr {snr:6.2f} dB  full-frame-full "
                      f"{np.mean([s for s in scores if s is not None]):8.4f}  "
                      f"power {10*np.log10(np.mean(powers)/2048.0**2):7.2f} dBFS",
                      flush=True)
        return 0
    except Exception:                                        # noqa: BLE001
        write({"kind": "error", "traceback": traceback.format_exc()})
        traceback.print_exc()
        return 1
    finally:
        if rig is not None:
            write({"kind": "rest", "readback": rig.close()})


if __name__ == "__main__":
    raise SystemExit(main())
