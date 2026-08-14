"""E1 -- smoke test.  Reproduce the bench numbers before anything else runs.

Target from the bench notes: TX2 -20 dB, RX manual 40 dB, LO 1190312500,
fs 5 MS/s, bw 5 MHz -> RX rms 83.6 counts, peak 461, correlation peak/median 60.3.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import numpy as np
from scipy.signal import fftconvolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig import Rig, levels, pilot_stream, TX_OFF_DB, FS_HZ, RX_PEAK_CEILING

OUT = Path(__file__).resolve().parent
PROBE = 100_000


def correlation_peak_to_median(samples: np.ndarray, sample_rate_hz: float,
                               edge: str = "lower") -> dict:
    """|matched filter| peak over its median, against the transmitted frame."""
    from leo_tracker.radio.beacon.pilots import edge_pilot_frame
    template = np.asarray(edge_pilot_frame(sample_rate_hz, edge), np.complex64)
    template = template / np.sqrt(np.sum(np.abs(template) ** 2))
    values = np.asarray(samples, np.complex64)
    corr = np.abs(fftconvolve(values, np.conj(template[::-1]), mode="valid"))
    median = float(np.median(corr))
    return {"peak": float(corr.max()),
            "median": median,
            "peak_to_median": float(corr.max() / median) if median > 0 else 0.0,
            "peak_index": int(np.argmax(corr)),
            "lags": int(corr.size)}


def main() -> int:
    rig = Rig()
    record = {"experiment": "E1-smoke", "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                             time.gmtime()),
              "rig": "cabled loopback TX2 -> tee -> 2x30 dB -> RX1,RX2",
              "probe_samples": PROBE, "ladder": []}
    try:
        record["config"] = rig.configure()
        wave = pilot_stream()
        record["tx_waveform"] = {
            "samples": int(wave.size),
            "frames": 3,
            "digital_peak": float(np.abs(wave).max()),
            "digital_rms": float(np.sqrt(np.mean(np.abs(wave) ** 2))),
            "source": "leo_tracker.radio.beacon.pilots.edge_pilot_frame(5e6,'lower')"}
        rig.open_rx(PROBE)

        # TX genuinely off first: the noise floor of this exact configuration.
        rig.set_tx2_gain(TX_OFF_DB)
        rig.start_tx(wave)
        time.sleep(0.3)
        rig.flush_rx(3)
        rx1, rx2 = rig.capture()
        off = {"tx2_gain_db": TX_OFF_DB,
               "rx1": levels(rx1), "rx2": levels(rx2),
               "rx1_corr": correlation_peak_to_median(rx1, FS_HZ),
               "rx2_corr": correlation_peak_to_median(rx2, FS_HZ)}
        record["tx_off"] = off
        print("TX off:", off["rx1"]["rms"], off["rx1"]["peak"],
              off["rx1_corr"]["peak_to_median"], flush=True)

        # Walk up in <=10 dB steps, re-measuring each rung.
        ladder = [-85.0, -80.0, -75.0, -70.0, -65.0, -60.0, -55.0, -50.0,
                  -45.0, -40.0, -35.0, -30.0, -25.0, -20.0]
        for gain in ladder:
            rig.set_tx2_gain(gain)
            time.sleep(0.25)
            rig.flush_rx(2)
            rx1, rx2 = rig.capture()
            l1, l2 = levels(rx1), levels(rx2)
            rung = {"tx2_gain_db": gain, "rx1": l1, "rx2": l2,
                    "rx1_corr": correlation_peak_to_median(rx1, FS_HZ),
                    "rx2_corr": correlation_peak_to_median(rx2, FS_HZ)}
            record["ladder"].append(rung)
            (OUT / "e1_smoke.json").write_text(json.dumps(record, indent=2))
            print(f"TX2 {gain:+7.2f} dB  rx1 rms {l1['rms']:8.2f} peak {l1['peak']:8.1f}"
                  f"  p/m {rung['rx1_corr']['peak_to_median']:7.2f}"
                  f" | rx2 rms {l2['rms']:8.2f} peak {l2['peak']:8.1f}"
                  f"  p/m {rung['rx2_corr']['peak_to_median']:7.2f}", flush=True)
            if max(l1["peak"], l2["peak"]) > RX_PEAK_CEILING:
                record["aborted"] = f"RX peak exceeded {RX_PEAK_CEILING} at {gain} dB"
                break

        final = record["ladder"][-1] if record["ladder"] else None
        record["verdict"] = {
            "reference": {"rms": 83.6, "peak": 461.0, "peak_to_median": 60.3},
            "measured_rx1": None if final is None else
                {"rms": final["rx1"]["rms"], "peak": final["rx1"]["peak"],
                 "peak_to_median": final["rx1_corr"]["peak_to_median"]},
            "passed": bool(final is not None
                           and final["rx1_corr"]["peak_to_median"] >= 8.0)}
    finally:
        try:
            rig.set_tx2_gain(TX_OFF_DB)
        finally:
            record["final_state"] = rig.state()
            rig.close()
            (OUT / "e1_smoke.json").write_text(json.dumps(record, indent=2))
            print("TX2 returned to", record["final_state"]["tx2_gain_db"], "dB", flush=True)
    return 0 if record.get("verdict", {}).get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
