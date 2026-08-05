"""Deterministic field-null replay for a new beacon acquisition method."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Callable

import numpy as np

from .analysis import ANALYSIS_SCHEMA, analyze_exact_window
from .artifact import BeaconCapture
from .calibration import build_calibration

NULL_REPLAY_SCHEMA = "leo-tracker.starlink-beacon-null-replay/v1"
HOST_TEMPERATURE_PATH = Path("/sys/class/thermal/thermal_zone0/temp")


def _thermal_backoff(maximum_c: float, resume_c: float,
                     progress: Callable[[dict], None] | None) -> None:
    if resume_c >= maximum_c:
        raise ValueError("thermal resume temperature must be below the maximum")
    while True:
        try:
            temperature_c = int(HOST_TEMPERATURE_PATH.read_text()) / 1000
        except (OSError, ValueError):
            return
        if temperature_c < maximum_c:
            return
        if progress:
            progress({"state": "thermal_backoff", "host_temperature_c": temperature_c,
                      "resume_below_c": resume_c})
        while temperature_c > resume_c:
            time.sleep(15)
            try:
                temperature_c = int(HOST_TEMPERATURE_PATH.read_text()) / 1000
            except (OSError, ValueError):
                return


def _strict_negative_sources(storage_root: Path) -> list[tuple[Path, dict]]:
    reports = Path(storage_root) / "reports"
    result = []
    counters = ("exact_candidate_count", "exact_qualified_count",
                "single_receiver_candidate_count", "single_receiver_qualified_count",
                "followup_trigger_count")
    for path in reports.glob("*.json"):
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("schema") != ANALYSIS_SCHEMA:
            continue
        if any(report.get("summary", {}).get(key, 0) for key in counters):
            continue
        try:
            followup = json.loads((reports / "followups" / path.name).read_text())
        except (OSError, json.JSONDecodeError):
            followup = {}
        if followup.get("confirmation", {}).get("confirmed"):
            continue
        capture = Path(report.get("capture", ""))
        if not capture.is_dir():
            continue
        result.append((path, report))
    return sorted(result, key=lambda item: item[0].stat().st_mtime_ns, reverse=True)


def replay_null_calibration(storage_root: Path, output_dir: Path, *,
                            acquisition_method: str = "pss_symbolwise_v2",
                            capture_limit: int = 6, checks_per_capture: int = 12,
                            window_s: float = .01,
                            maximum_host_temperature_c: float = 75,
                            resume_host_temperature_c: float = 70,
                            progress: Callable[[dict], None] | None = None) -> dict:
    """Replay stratified windows from conservatively selected field negatives.

    Results use the normal analysis schema, allowing the ordinary calibration
    builder to summarize them without introducing a second scoring path.
    Existing per-capture results are reused, making an interrupted run safe to
    resume.
    """
    if capture_limit <= 0 or checks_per_capture <= 0 or window_s <= 0:
        raise ValueError("capture limit, check count, and window must be positive")
    if resume_host_temperature_c >= maximum_host_temperature_c:
        raise ValueError("thermal resume temperature must be below the maximum")
    storage_root = Path(storage_root).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = _strict_negative_sources(storage_root)[:capture_limit]
    if not sources:
        raise ValueError("no retained strict-negative beacon captures are available")
    completed, reused = [], []
    for source_path, source_report in sources:
        output = output_dir / source_path.name
        try:
            existing = json.loads(output.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
        if (existing.get("schema") == ANALYSIS_SCHEMA and
                existing.get("analysis", {}).get("exact_acquisition_method") ==
                acquisition_method and
                existing.get("null_replay", {}).get("checks_per_capture") ==
                checks_per_capture):
            reused.append(str(output))
            if progress:
                progress({"state": "reused", "report": str(output)})
            continue
        capture_path = Path(source_report["capture"])
        capture = BeaconCapture.open(capture_path, verify=True)
        rate = float(capture.manifest["sample_rate_hz"])
        duration_s = capture.manifest["captured_samples_per_receiver"] / rate
        if duration_s < window_s:
            continue
        starts = np.linspace(0, duration_s - window_s,
                             checks_per_capture, endpoint=True)
        region = capture.manifest.get("metadata", {}).get("region", "center")
        if region not in ("lower-edge", "upper-edge"):
            continue
        edge = region.removesuffix("-edge")
        checks = []
        for start_s in starts:
            _thermal_backoff(maximum_host_temperature_c,
                             resume_host_temperature_c, progress)
            start_sample = round(float(start_s) * rate)
            values = capture.read_window(start_sample, round(window_s * rate))
            checks.append(analyze_exact_window(values, rate, edge=edge,
                start_sample=start_sample, acquisition_method=acquisition_method))
        sampled_s = sum(item["duration_s"] for item in checks)
        report = {"schema": ANALYSIS_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "capture": str(capture_path.resolve()),
            "capture_manifest": capture.manifest,
            "analysis": {"exact_acquisition_method": acquisition_method,
                         "acquisition_span_hz": 0,
                         "exact_subband_rate_hz": min(rate, 2_500_000.0),
                         "exact_window_s": window_s,
                         "sampling_strategy": "stratified_field_null"},
            "null_replay": {"schema": NULL_REPLAY_SCHEMA,
                            "source_analysis": str(source_path.resolve()),
                            "source_selection": "strict_no-trigger_negative",
                            "checks_per_capture": checks_per_capture},
            "exact_checks": checks,
            "summary": {"exact_check_count": len(checks),
                        "exact_sampled_time_s": sampled_s,
                        "exact_temporal_coverage_fraction": sampled_s / duration_s,
                        "exact_candidate_count": sum(item["candidate"] for item in checks),
                        "exact_qualified_count": sum(item["qualified"] for item in checks),
                        "single_receiver_candidate_count": sum(
                            sum(item["receiver_candidates"]) for item in checks),
                        "single_receiver_qualified_count": sum(
                            sum(item["receiver_qualified"]) for item in checks),
                        "followup_trigger_count": sum(item["followup_trigger"] for item in checks)}}
        temporary = output.with_suffix(output.suffix + ".next")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        temporary.replace(output)
        completed.append(str(output))
        if progress:
            progress({"state": "completed", "report": str(output),
                      "check_count": len(checks)})
    calibration_path = output_dir / "calibration.json"
    calibration = build_calibration(output_dir, calibration_path)
    result = {"schema": NULL_REPLAY_SCHEMA,
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "storage_root": str(storage_root), "output_dir": str(output_dir),
              "acquisition_method": acquisition_method,
              "selected_capture_count": len(sources),
              "completed_reports": completed, "reused_reports": reused,
              "calibration": str(calibration_path),
              "method_calibration": calibration.get("acquisition_methods", {}).get(
                  acquisition_method, {})}
    summary = output_dir / "replay-summary.json"
    temporary = summary.with_suffix(".json.next")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.replace(summary)
    return result
