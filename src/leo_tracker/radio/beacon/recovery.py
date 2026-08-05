"""Idempotent post-reboot recovery for completed beacon captures."""
from __future__ import annotations

import json
from pathlib import Path

from .analysis import analyze_capture
from .artifact import SCHEMA
from .plot import plot_beacon_report
from .followup import followup_capture


def recover_unanalyzed(root: Path, *, passes_path: Path | None = None) -> dict:
    """Analyze complete artifacts that lack reports; never discard failures."""
    root = Path(root).resolve()
    captures = root / "captures"
    reports = root / "reports"
    plots = reports / "plots"
    reports.mkdir(parents=True, exist_ok=True)
    plots.mkdir(parents=True, exist_ok=True)
    recovered, recovered_followups, skipped, errors = [], [], [], []
    for capture in sorted(captures.iterdir() if captures.exists() else []):
        if not capture.is_dir():
            continue
        report_path = reports / f"{capture.name}.json"
        if report_path.is_file():
            followup_path = reports / "followups" / f"{capture.name}.json"
            if not followup_path.is_file():
                try:
                    followup = followup_capture(capture, report_path, followup_path,
                                                passes_path=passes_path)
                    recovered_followups.append({"capture": capture.name,
                                                "trigger_count": followup["trigger_count"],
                                                "confirmed": followup["confirmation"]["confirmed"]})
                except Exception as exc:
                    errors.append({"capture": capture.name,
                                   "error": f"follow-up {type(exc).__name__}: {exc}"})
            skipped.append(capture.name)
            continue
        try:
            manifest = json.loads((capture / "manifest.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            errors.append({"capture": capture.name, "error": f"invalid manifest: {exc}"})
            continue
        if manifest.get("schema") != SCHEMA or manifest.get("state") != "complete":
            skipped.append(capture.name)
            continue
        wide = float(manifest.get("sample_rate_hz", 0)) >= 5_000_000
        settings = ({"exact_interval_s": 5, "exact_window_s": .01,
                     "acquisition_span_hz": 3_500_000,
                     "acquisition_step_hz": 500_000,
                     "exact_subband_rate_hz": 2_500_000}
                    if wide else {"exact_interval_s": 1, "exact_window_s": .01})
        try:
            report = analyze_capture(capture, report_path, window_s=1,
                                     maximum_analysis_rate_hz=50_000, **settings)
            plot_beacon_report(report, plots / f"{capture.name}.png")
            followup_capture(capture, report_path,
                             reports / "followups" / f"{capture.name}.json",
                             passes_path=passes_path)
            recovered.append({"capture": capture.name, "mode": "wide" if wide else "narrow",
                              "candidate_count": report["summary"]["exact_candidate_count"],
                              "qualified_count": report["summary"]["exact_qualified_count"],
                              "single_receiver_candidate_count": report["summary"].get(
                                  "single_receiver_candidate_count", 0)})
        except Exception as exc:  # Preserve and report any artifact that needs intervention.
            errors.append({"capture": capture.name,
                           "error": f"{type(exc).__name__}: {exc}"})
    return {"schema": "leo-tracker.beacon-recovery/v1", "root": str(root),
            "recovered": recovered, "recovered_followups": recovered_followups,
            "skipped_count": len(skipped), "errors": errors}
