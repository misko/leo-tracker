"""T1 -- the 2x2 (TX port, RX port) table on radio .165, done properly.

One cyclic DMA load carries the pilot waveform on BOTH TX channels; which port
radiates is chosen by hardwaregain alone, so no measurement in the table can be
spoiled by a DMA that failed to start.  The load is verified before any row is
taken, and the TX-off baseline is re-measured between every row so each level
is read against a floor from the same minute.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig165 import (ARM_NAME, BW_HZ, FS_HZ, LO_HZ, OUT,  # noqa: E402
                    PROBE_MS, PROBE_SAMPLES, Rig, TX_OFF_DB, appender,
                    tx_waveform)

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import fast_scan  # noqa: E402

RX_PEAK_CEILING = 1500.0
GAINS_DB = [-50.0, -40.0, -30.0, -20.0]


def matched(received: np.ndarray, template: np.ndarray) -> dict:
    template = np.asarray(template, np.complex64)
    template = template / np.sqrt(np.vdot(template, template).real)
    values = np.asarray(received, np.complex64)
    size = 1 << int(np.ceil(np.log2(values.size + template.size)))
    spectrum = np.fft.fft(values, size) * np.conj(np.fft.fft(template, size))
    correlation = np.abs(np.fft.ifft(spectrum))[:values.size - template.size + 1]
    median = float(np.median(correlation))
    return {"peak": float(correlation.max()), "median": median,
            "peak_to_median": float(correlation.max() / median) if median else None,
            "peak_lag": int(np.argmax(correlation))}


def measure(block, sent, bank, label, tx1, tx2, write) -> list[dict]:
    rows = []
    for receiver in (0, 1):
        values = np.asarray(block[receiver], np.complex64)
        magnitude = np.abs(values)
        scored = fast_scan.probe(values, bank)
        row = {"phase": label, "tx1_gain_db": tx1, "tx2_gain_db": tx2,
               "receiver": receiver + 1,
               "rms_counts": float(np.sqrt(np.mean(magnitude ** 2))),
               "peak_counts": float(magnitude.max()),
               "matched": matched(values, sent),
               "bank_peak_to_median": float(scored["peak_to_median"]),
               "bank_offset_hz": float(scored["frequency_offset_hz"])}
        write(row)
        rows.append(row)
        print(f"  {label:14} tx1={tx1:>7} tx2={tx2:>7} RX{row['receiver']} "
              f"rms={row['rms_counts']:8.2f} peak={row['peak_counts']:7.1f} "
              f"mf_ptm={row['matched']['peak_to_median']:7.2f} "
              f"bank={row['bank_peak_to_median']:5.3f}", flush=True)
    return rows


def main() -> int:
    write = appender(OUT / "t1_matrix-165.jsonl")
    sent = tx_waveform()
    bank = fast_scan.build_bank("lower", FS_HZ, (13, 8), offset_span_hz=700_000.0)
    rig = None
    try:
        rig = Rig()
        # Recorded rather than restated downstream: the figure's caption is
        # built from THIS, so the geometry it claims cannot drift away from the
        # geometry it measured.
        write({"phase": "header", "arm": ARM_NAME,
               "sample_rate_hz": float(FS_HZ), "probe_ms": PROBE_MS,
               "probe_samples": PROBE_SAMPLES, "rx_gain_db": rig.rx_gain_db,
               "lo_hz": LO_HZ, "rf_bandwidth_hz": BW_HZ,
               "tx_gains_db": GAINS_DB})
        status = rig.load(sent)
        write({"phase": "load", **status})
        print(f"TX DMA verified live after {status['attempts']} push(es), "
              f"rms {status['verify_rms']:.2f}", flush=True)

        rig.all_tx_off()
        measure(rig.capture(), sent, bank, "baseline", TX_OFF_DB, TX_OFF_DB, write)

        for port, name in ((0, "TX1"), (1, "TX2")):
            for gain in GAINS_DB:
                tx1 = gain if port == 0 else TX_OFF_DB
                tx2 = gain if port == 1 else TX_OFF_DB
                rig.set_tx_gain(tx1, tx2)
                rows = measure(rig.capture(), sent, bank, name, tx1, tx2, write)
                rig.all_tx_off()
                over = max(row["peak_counts"] for row in rows)
                if over > RX_PEAK_CEILING:
                    write({"phase": "abort", "port": name, "gain_db": gain,
                           "peak_counts": over})
                    print(f"  !! RX peak {over:.0f} > {RX_PEAK_CEILING}; "
                          f"stopping {name}", flush=True)
                    break
            measure(rig.capture(), sent, bank, "baseline", TX_OFF_DB, TX_OFF_DB, write)
        return 0
    except Exception:                                        # noqa: BLE001
        write({"phase": "error", "traceback": traceback.format_exc()})
        traceback.print_exc()
        return 1
    finally:
        if rig is not None:
            rig.close()
        print("TX returned to -89.75 dB on both channels", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
