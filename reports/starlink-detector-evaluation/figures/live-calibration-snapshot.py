"""Freeze the calibration that was in force when the report was written.

Section 16 says all four receivers are miscentred *today*, which is a claim about
a file on the shared store rather than about the corpus -- and that file is
rewritten by a timer. Quoting it without capturing it would leave the report's
most operational number unreproducible the moment the timer next fires, so the
artifact and the centres it resolves to are copied here verbatim.

Read only: nothing is written outside this report directory.

    python figures/live-calibration-snapshot.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from leo_tracker.radio.beacon.lnb_calibration import (  # noqa: E402
    receiver_centers)

SHARE_ROOT = Path("/mnt/qnap01/mouse9911/leo")
ARTIFACT = SHARE_ROOT / "reports" / "lnb-calibration.json"
TIMER = "leo-tracker-lnb-calibration.timer"

# Which physical port each receiver index is, per radio. Recorded here because
# the calibration artifact indexes by rx0/rx1 and every table in the report names
# the LNB, and getting that mapping wrong silently swaps two columns.
PORTS = {"pluto-19f2": ["lnb-c", "lnb-d"], "pluto-5d4d": ["lnb-a", "lnb-b"]}


def timer_next_firing() -> str | None:
    """When the daily calibration will next overwrite the artifact, if known.

    Read from `list-timers` rather than `systemctl show`: this timer is monotonic
    (`OnUnitActiveUSec=1d`), so `NextElapseUSecRealtime` is empty on it and only
    the monotonic figure is exposed as a property -- which is measured from boot
    and does not answer the question the report is asking.
    """
    try:
        done = subprocess.run(["systemctl", "list-timers", TIMER, "--all",
                               "--no-pager", "--no-legend"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in done.stdout.splitlines():
        if TIMER in line:
            # "Sat 2026-08-15 11:59:52 PDT  16h Fri ..." -- the first four
            # whitespace-separated fields are the next firing.
            fields = line.split()
            if len(fields) >= 4:
                return " ".join(fields[:4])
    return None


def main() -> int:
    calibration = json.loads(ARTIFACT.read_text())
    radios = calibration.get("radios") or {}

    resolved = {}
    for radio in sorted(radios):
        centres = receiver_centers(calibration, radio)
        entry = radios[radio]
        resolved[radio] = {
            "applied_centers_hz": list(centres),
            "ports": PORTS.get(radio),
            "mismatch_hz": entry.get("mismatch_hz"),
            "receiver_candidate_counts": entry.get("receiver_candidate_counts"),
            # Present only if somebody has written absolute centres. Its absence
            # is the finding, so it is recorded rather than omitted.
            "measured_centers_hz": entry.get("measured_centers_hz"),
            "measured": bool(entry.get("measured")),
        }

    snapshot = {
        "schema": "live-calibration-snapshot/1",
        "source": str(ARTIFACT),
        "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifact_mtime_utc": datetime.fromtimestamp(
            ARTIFACT.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "created_utc": calibration.get("created_utc"),
        "reports_examined": calibration.get("reports_examined"),
        "timer": {"unit": TIMER, "next_elapse": timer_next_firing()},
        "radios": resolved,
        "raw_artifact": calibration,
    }

    out = Path(__file__).with_suffix(".json")
    out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"wrote": str(out), "radios": sorted(resolved)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
