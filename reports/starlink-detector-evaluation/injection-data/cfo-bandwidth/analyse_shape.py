"""Turn the measured noise spectra into the two edges the factorial is read against.

The predicted clipping offset for a cell is not ``(B_RX - 1,875,000)/2``.  That
formula assumes the analog corner sits exactly where it was asked to, and this
radio's own noise says it does not: the AD9361 places its baseband filter above
the requested corner, so a 2.5 MHz request measures ~3.1 MHz wide.  Two edges
therefore come out of this file, and the smaller of them is what a cell can pass:

``analog``   the measured -3 dB half-corner minus the block's 937,500 Hz
             half-occupancy
``digital``  the same, computed from the half-corner measured with the filter
             wide open at that rate -- which is the FIR-and-decimation window,
             not Fs/2, because ``filter_fir_en`` reads 1 on every arm here

Both are *measured* on this radio rather than assumed, so a cliff that lands on
neither cannot be waved through as "roughly where the filter is".
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

OCCUPIED_HALF_WIDTH_HZ = 937_500.0
PILOT_CENTRE_HALF_WIDTH_HZ = 820_312.5


def read(path: Path) -> list[dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def summarise(rows: list[dict]) -> dict:
    shapes = [row for row in rows if row["kind"] == "shape"]
    header = next((row for row in rows if row["kind"] == "header"), {})

    # The widest bandwidth at each rate is the digital window on its own: the
    # analog corner is far outside it, so whatever limits the spectrum there is
    # the FIR and the decimation.
    widest: dict = {}
    for row in shapes:
        rate = row["sample_rate_requested_hz"]
        current = widest.get(rate)
        if current is None or (row["rx_bandwidth_requested_hz"]
                               > current["rx_bandwidth_requested_hz"]):
            widest[rate] = row

    cells = []
    for row in shapes:
        rate = row["sample_rate_requested_hz"]
        digital_half = widest[rate]["half_corner_mean_hz"]
        analog_half = row["half_corner_mean_hz"]
        effective = min(value for value in (analog_half, digital_half)
                        if value is not None)
        cells.append({
            "sample_rate_requested_hz": rate,
            "sample_rate_readback_hz": row["sample_rate_readback_hz"],
            "rx_bandwidth_requested_hz": row["rx_bandwidth_requested_hz"],
            "rx_bandwidth_readback_hz": row["rx_bandwidth_readback_hz"],
            "readback_matched_request": (
                row["rx_bandwidth_readback_hz"] == row["rx_bandwidth_requested_hz"]
                and row["sample_rate_readback_hz"] == row["sample_rate_requested_hz"]),
            "rx_fir_en": row["rx_fir_en"],
            "measured_half_corner_hz": analog_half,
            "measured_bandwidth_hz": row["measured_bandwidth_hz"],
            "measured_over_requested": (None if analog_half is None else
                                        row["measured_bandwidth_hz"]
                                        / row["rx_bandwidth_requested_hz"]),
            "digital_half_window_hz": digital_half,
            "effective_half_window_hz": effective,
            "noise_power_dbfs": row["power_dbfs"],
            # Where the occupied block first touches the edge, and where the
            # outermost pilot CENTRES do.  Detection dies somewhere between:
            # losing the shoulder of the band costs SNR, losing a pilot centre
            # costs a correlator tap.
            "predicted_clip_offset_hz": effective - OCCUPIED_HALF_WIDTH_HZ,
            "predicted_pilot_loss_offset_hz": effective - PILOT_CENTRE_HALF_WIDTH_HZ,
            "nominal_analog_edge_hz": (row["rx_bandwidth_requested_hz"]
                                       - 2 * OCCUPIED_HALF_WIDTH_HZ) / 2,
            "nominal_digital_edge_hz": (rate - 2 * OCCUPIED_HALF_WIDTH_HZ) / 2,
        })
    return {"schema": "leo-tracker.cfo-bandwidth-filter-shape/v1",
            "source": {key: value for key, value in header.items()
                       if key != "context"},
            "context": header.get("context"),
            "geometry": {"occupied_half_width_hz": OCCUPIED_HALF_WIDTH_HZ,
                         "pilot_centre_half_width_hz": PILOT_CENTRE_HALF_WIDTH_HZ},
            "cells": cells}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw")
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args()
    payload = summarise(read(Path(arguments.raw)))
    Path(arguments.out).write_text(json.dumps(payload, indent=1))
    print(f"{'Fs':>6} {'B req':>7} {'B back':>8} {'measured':>9} {'ratio':>6} "
          f"{'digital':>8} {'effective':>10} {'clip CFO':>9}")
    for cell in payload["cells"]:
        print(f"{cell['sample_rate_requested_hz']/1e6:6.2f} "
              f"{cell['rx_bandwidth_requested_hz']/1e6:7.2f} "
              f"{cell['rx_bandwidth_readback_hz']/1e6:8.2f} "
              f"{cell['measured_bandwidth_hz']/1e6:9.3f} "
              f"{cell['measured_over_requested']:6.2f} "
              f"{cell['digital_half_window_hz']/1e3:8.0f} "
              f"{cell['effective_half_window_hz']/1e3:10.0f} "
              f"{cell['predicted_clip_offset_hz']/1e3:9.0f}")
    print(f"-> {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
