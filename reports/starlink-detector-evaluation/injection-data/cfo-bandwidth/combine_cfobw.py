"""Merge the sweep arms into one machine-readable summary, one object per cell.

The arms are analysed separately and merged here rather than analysed together,
because each arm draws its own thresholds from its own TX-off null at its own
rate, bandwidth and probe length. Pooling the nulls across drive levels would be
harmless (the null does not know what the transmitter is doing) but pooling the
CELLS would not: two arms hold cells with the same (rate, bandwidth, offset) key
and merging them would average a 27 dB-margin measurement with a 4 dB one.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summaries", nargs="+",
                        help="arm_name=path/to/summary.json")
    parser.add_argument("--shape", required=True)
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args()

    shape = json.loads(Path(arguments.shape).read_text())
    measured = {(cell["sample_rate_requested_hz"],
                 cell["rx_bandwidth_requested_hz"]): cell
                for cell in shape["cells"]}

    cells, arms, errors = [], {}, []
    for item in arguments.summaries:
        name, _, path = item.partition("=")
        payload = json.loads(Path(path).read_text())
        arms[name] = {
            "source": payload["source"],
            "cells_run": payload["cells_run"],
            "cells_failed": payload["cells_failed"],
            "thresholds": payload["thresholds"],
            "cliffs": payload["cliffs"],
            "readback_mismatches": payload["readback_mismatches"],
        }
        errors.extend(payload["errors"])
        for cell in payload["cells"]:
            row = measured.get((cell["sample_rate_requested_hz"],
                                cell["rx_bandwidth_requested_hz"]), {})
            cells.append({
                "arm": name,
                **cell,
                # The measured filter, carried onto every cell so a reader never
                # has to trust the nominal formula to interpret one.
                "measured_rx_half_corner_hz": row.get("measured_half_corner_hz"),
                "measured_rx_bandwidth_hz": row.get("measured_bandwidth_hz"),
                "measured_effective_half_window_hz":
                    row.get("effective_half_window_hz"),
                "measured_clip_offset_hz": row.get("predicted_clip_offset_hz"),
            })

    payload = {
        "schema": "leo-tracker.cfo-bandwidth-factorial/v2",
        "experiment": "CFO x sample rate x RX bandwidth on ip:192.168.1.165, "
                      "carrier offset imposed on the transmitted pilot frame",
        "arms": arms,
        "cells": cells,
        "cells_run": len(cells),
        "cells_failed": sum(arm["cells_failed"] for arm in arms.values()),
        "errors": errors,
        "readback": {
            "checked": "sampling_frequency and rf_bandwidth read off the "
                       "ad9361-phy channel through raw libiio after every "
                       "write, on every cell",
            "cells_with_sample_rate_mismatch": len(
                [cell for cell in cells
                 if cell["sample_rate_readback_hz"] != cell["sample_rate_requested_hz"]]),
            "cells_with_bandwidth_mismatch": len(
                [cell for cell in cells
                 if cell["rx_bandwidth_readback_hz"] != cell["rx_bandwidth_requested_hz"]]),
            "caveat": "the AD9361 driver reports back the REQUESTED baseband "
                      "bandwidth, not the analog corner it tuned to, so a clean "
                      "readback proves the driver accepted the write and nothing "
                      "about the filter; filter_shape measures the corner off "
                      "the noise floor and finds it ~1.2x wider than requested",
        },
        "filter_shape": shape["cells"],
        "geometry": shape["geometry"],
    }
    Path(arguments.out).write_text(json.dumps(payload, indent=1))
    print(f"{payload['cells_run']} cells across {len(arms)} arms, "
          f"{payload['cells_failed']} failed, "
          f"{payload['readback']['cells_with_bandwidth_mismatch']} bandwidth "
          f"readback mismatches -> {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
