"""Compact, unattended four-hour summaries of the live Starlink watch."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_review(root: Path, *, now: datetime | None = None,
                 hours: float = 4.0) -> dict[str, Any]:
    root = Path(root)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = now-timedelta(hours=hours)
    reports: list[tuple[Path, dict]] = []
    candidates: list[dict] = []
    for path in sorted((root/"wide").glob("chunk-*.json")):
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        starts = [item.get("start_utc") for item in report.get("candidates", [])
                  if item.get("start_utc")]
        report_time = max((_time(value) for value in starts), default=None)
        if report_time is None and report.get("source"):
            try:
                with np.load(report["source"], allow_pickle=False) as stored:
                    report_time = datetime.fromtimestamp(
                        int(stored["utc_ns"][-1])/1e9, timezone.utc)
            except (OSError, KeyError, ValueError):
                pass
        if report_time is not None and report_time >= since:
            reports.append((path, report))
            for item in report.get("candidates", []):
                if item.get("start_utc") and _time(item["start_utc"]) >= since:
                    copied = dict(item)
                    copied["_bin_width_hz"] = report.get("bin_width_hz", 3_750.0)
                    candidates.append(copied)
    doppler = [item for item in candidates if item.get("doppler_candidate_qualified")]
    spacing_targets = {"starlink_leakage_tone_hz": 43_949.5,
                       "starlink_ofdm_subcarrier_hz": 234_375.0}
    spacing_matches = []
    for item in doppler:
        tolerance = float(item.get("_bin_width_hz", 3_750.0))
        for spacing in item.get("common_internal_peak_spacings_hz", []):
            for name, target in spacing_targets.items():
                if abs(float(spacing)-target) <= tolerance:
                    spacing_matches.append({"start_utc": item["start_utc"],
                        "observed_hz": float(spacing), "target": name,
                        "error_hz": float(spacing)-target})
    status = {}
    try: status = json.loads((root/"status.json").read_text())
    except (OSError, json.JSONDecodeError): pass
    latest_temperature = {}
    chunks = sorted((root/"chunks").glob("chunk-*.npz"), key=lambda p: p.stat().st_mtime)
    if chunks:
        try:
            with np.load(chunks[-1], allow_pickle=False) as stored:
                identity = json.loads(str(stored["identity_json"]))
            latest_temperature = {key: identity.get(key) for key in
                ("host_temperature_c", "radio_temperature_c")}
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
    return {"schema": "leo-tracker.periodic-review/v1",
        "generated_utc": now.isoformat().replace("+00:00", "Z"),
        "window_start_utc": since.isoformat().replace("+00:00", "Z"),
        "window_hours": hours, "scanner_status": status,
        "latest_temperature": latest_temperature,
        "wide_report_count": len(reports), "candidate_count": len(candidates),
        "moving_rf_count": sum(bool(x.get("moving_rf_qualified")) for x in candidates),
        "doppler_candidate_count": len(doppler),
        "leo_like_count": sum(bool(x.get("leo_like_qualified")) for x in candidates),
        "orbital_shape_count": sum(bool(x.get("orbital_shape_qualified")) for x in candidates),
        "spacing_matches": spacing_matches,
        "doppler_candidates": [{key: item.get(key) for key in (
            "start_utc", "stop_utc", "duration_s", "bounding_width_hz",
            "mean_drift_hz_s", "internal_translation_path_correlation",
            "common_internal_peak_spacings_hz", "leo_like_qualified",
            "orbital_shape_qualified", "measurement_warnings")} for item in doppler]}


def write_review(root: Path, output_dir: Path, *, now: datetime | None = None,
                 hours: float = 4.0) -> Path:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    report = build_review(root, now=now, hours=hours)
    timestamped = output_dir/f"review-{now:%Y%m%dT%H%M%SZ}.json"
    payload = json.dumps(report, indent=2, sort_keys=True)+"\n"
    timestamped.write_text(payload)
    (output_dir/"latest.json").write_text(payload)
    return timestamped


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--hours", type=float, default=4.0)
    args = parser.parse_args()
    path = write_review(args.root, args.output_dir, hours=args.hours)
    print(json.dumps({"review": str(path)}))
