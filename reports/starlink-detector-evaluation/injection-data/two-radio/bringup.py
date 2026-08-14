"""Bring both rigs up, prove the cable is on TX2, and reproduce the .183 anchor."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from rig import Rig, rms, FS_HZ, TX_IDLE_DB          # noqa: E402

from leo_tracker.radio.beacon.pilots import edge_pilot_frame   # noqa: E402

OUT = Path(__file__).parent / "bringup.json"
PROBE_N = 100_000          # 20 ms at 5 MS/s


def peak_median(x: np.ndarray, fs: float) -> float:
    """Cheap fold statistic: frame-folded power peak over its median."""
    period = 6667
    n = (x.size // period) * period
    folded = np.abs(x[:n].reshape(-1, period)).mean(axis=0)
    return float(folded.max() / np.median(folded))


def main():
    frame = edge_pilot_frame(FS_HZ, "lower")
    report = {"frame_samples": int(frame.size), "probe_samples": PROBE_N,
              "rigs": {}}
    rigs = []
    try:
        for uri, label in (("ip:192.168.1.183", "r183"), ("ip:192.168.1.165", "r165")):
            r = Rig(uri, label).configure()
            r.open_rx(PROBE_N)
            rigs.append(r)
            row = {"state": r.state(), "serial": r.serial}

            # 1. floor with no TX buffer at all
            r.capture()
            floor = [rms(r.capture()) for _ in range(3)]
            row["floor_no_tx_rms"] = floor

            # 2. floor with TX parked at the idle attenuation (buffer running)
            r.start_tx(frame, 0.3048)
            r.tx_gain(TX_IDLE_DB)
            r.capture()
            parked = [rms(r.capture()) for _ in range(3)]
            row["floor_tx_parked_rms"] = parked

            # 3. THE TRAP: drive TX1's DMA while the cable is on TX2
            r.stop_tx()
            i_ch = r.txdma.find_channel("voltage0", True)
            q_ch = r.txdma.find_channel("voltage1", True)
            for ch in r.txdma.channels:
                if ch.id.startswith("voltage"):
                    ch.enabled = ch.id in ("voltage0", "voltage1")
            import iio
            scaled = frame / np.max(np.abs(frame)) * (0.3048 * 32767.0)
            inter = np.empty(scaled.size * 2, dtype=np.int16)
            inter[0::2] = np.rint(scaled.real).astype(np.int16)
            inter[1::2] = np.rint(scaled.imag).astype(np.int16)
            buf = iio.Buffer(r.txdma, scaled.size, True)
            buf.write(bytearray(inter.tobytes()))
            buf.push()
            r.tx_gain(-20.0)                      # phy TX2 open, DMA on TX1
            r.capture()
            wrong = [rms(r.capture()) for _ in range(3)]
            row["tx1_dma_wrong_port_rms"] = wrong
            del buf

            # 4. the real thing: TX2 DMA, TX2 phy at -20 dB, drive 0.3048
            r.start_tx(frame, 0.3048)
            r.tx_gain(-20.0)
            r.capture()
            caps = [r.capture() for _ in range(3)]
            row["tx2_dma_m20db_rms"] = [rms(c) for c in caps]
            row["tx2_dma_m20db_peak_median"] = [peak_median(c, FS_HZ) for c in caps]
            row["tx2_gain_readback_db"] = r.tx_gain_read()
            r.tx_gain(TX_IDLE_DB)
            report["rigs"][label] = row
            print(label, json.dumps(row, indent=1)[:1200], flush=True)
    finally:
        for r in rigs:
            r.close()
        OUT.write_text(json.dumps(report, indent=1))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
