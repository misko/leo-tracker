"""CLI for acquiring a provenance-bearing observer fix."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .nmea import acquire_fix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leo-gps")
    parser.add_argument("--device", default="/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--min-satellites", type=int, default=4)
    parser.add_argument("--max-hdop", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    fix = asdict(acquire_fix(args.device, args.timeout_seconds,
                             min_satellites=args.min_satellites, max_hdop=args.max_hdop))
    fix["timestamp"] = fix["timestamp"].isoformat().replace("+00:00", "Z")
    fix["schema"] = "leo-tracker.gps-fix/v1"
    text = json.dumps(fix, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
