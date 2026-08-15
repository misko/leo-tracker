"""Print the factorial as one table per sample rate.

Each row is an RX bandwidth, each column a carrier offset. Read across a row and
the analog filter is the only thing changing; read down a column and the digital
window is; read the same column across the three tables and neither is. A limit
that is real has to move when its own knob moves.

The right-hand margin carries the offset at which that row's block is predicted
to start clipping, taken from ``filter_shape``'s MEASURED corner rather than
from ``(B_RX - 1,875,000)/2`` -- on this radio the analog filter sits about 1.2x
wider than requested, so the nominal formula understates every analog edge and a
cliff compared against it would look closer to the filter than it is.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def table(cells: list[dict], method: str, rate: float, edges: dict,
          field: str = "pd") -> str:
    rows = [cell for cell in cells if cell["sample_rate_requested_hz"] == rate]
    offsets = sorted({cell["cfo_requested_hz"] for cell in rows})
    bandwidths = sorted({cell["rx_bandwidth_requested_hz"] for cell in rows})
    index = {(cell["rx_bandwidth_requested_hz"], cell["cfo_requested_hz"]): cell
             for cell in rows}
    width = 7
    digital = edges.get((rate, max(bandwidths)))
    out = [f"Fs = {rate/1e6:g} MS/s   detector {method}   "
           f"measured digital window "
           f"{'?' if digital is None else format(digital/1e3, '.0f')} kHz half"]
    out.append("  B_RX " + "".join(f"{value/1e3:>{width}.0f}" for value in offsets)
               + "    kHz offset")
    for bandwidth in bandwidths:
        line = f"{bandwidth/1e6:5.2f} "
        for offset in offsets:
            cell = index.get((bandwidth, offset))
            value = None if cell is None else (cell[field] or {}).get(method)
            line += f"{'':>{width}}" if value is None else f"{value:>{width}.2f}"
        edge = edges.get((rate, bandwidth))
        note = ("" if edge is None else
                f"    clips beyond {(edge - 937_500)/1e3:+.0f} kHz")
        out.append(line + note)
    out.append("  power, dBFS")
    for bandwidth in bandwidths:
        line = f"{bandwidth/1e6:5.2f} "
        for offset in offsets:
            cell = index.get((bandwidth, offset))
            line += (f"{'':>{width}}" if cell is None
                     else f"{cell['power_dbfs']:>{width}.1f}")
        out.append(line)
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary")
    parser.add_argument("--method", default="full-frame-full")
    parser.add_argument("--field", default="pd")
    parser.add_argument("--shape", default=None,
                        help="filter_shape summary, for the measured edges")
    arguments = parser.parse_args()
    payload = json.loads(Path(arguments.summary).read_text())
    edges = {}
    if arguments.shape:
        for cell in json.loads(Path(arguments.shape).read_text())["cells"]:
            edges[(cell["sample_rate_requested_hz"],
                   cell["rx_bandwidth_requested_hz"])] = \
                cell["effective_half_window_hz"]
    cells = payload["cells"]
    for rate in sorted({cell["sample_rate_requested_hz"] for cell in cells}):
        print(table(cells, arguments.method, rate, edges, arguments.field))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
