#!/usr/bin/env python3
"""Wait until the next recording window in a leo-orbit schedule."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from leo_tracker.orbit.waiting import select_window


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--min-remaining-seconds", type=float, default=0)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    entry = select_window(json.loads(args.schedule.read_text()), now,
                          min_remaining_seconds=args.min_remaining_seconds)
    start = datetime.fromisoformat(entry["record_start"].replace("Z", "+00:00"))
    delay = max(0.0, (start - now).total_seconds())
    print(json.dumps({"selected": entry, "wait_seconds": delay}), flush=True)
    if not args.no_wait and delay:
        time.sleep(delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
