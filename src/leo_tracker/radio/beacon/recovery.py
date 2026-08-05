"""Idempotent post-reboot recovery for completed beacon captures."""
from __future__ import annotations

import json
from pathlib import Path

from .analysis import analyze_capture
from .artifact import SCHEMA
from .plot import plot_beacon_report
from .followup import followup_capture
from .decode import decode_followup, plot_decode_report


def recover_unanalyzed(root: Path, *, passes_path: Path | None = None,
                       exact_acquisition_method: str = "coherent_grid_v1",
                       narrow_exact_interval_s: float = 1,
                       wide_exact_interval_s: float = 5) -> dict:
    """Analyze complete artifacts that lack reports; never discard failures."""
    root = Path(root).resolve()
    captures = root / "captures"
    reports = root / "reports"
    plots = reports / "plots"
    reports.mkdir(parents=True, exist_ok=True)
    plots.mkdir(parents=True, exist_ok=True)
    decoded = reports / "decoded"
    decoded.mkdir(parents=True, exist_ok=True)
    recovered, recovered_followups, recovered_decodes, skipped, errors = [], [], [], [], []

    def decode_confirmed(capture: Path, followup_path: Path, followup: dict) -> None:
        if not followup.get("confirmation", {}).get("confirmed"):
            return
        manifest = json.loads((capture / "manifest.json").read_text())
        if float(manifest.get("sample_rate_hz", 0)) >= 5_000_000:
            return
        output = decoded / f"{capture.name}.json"
        if output.is_file():
            return
        report, arrays = decode_followup(
            capture, followup_path, output,
            symbols_output=decoded / f"{capture.name}.npz")
        plot_decode_report(report, arrays, decoded / f"{capture.name}.png")
        recovered_decodes.append({"capture": capture.name,
            "minimum_pilot_accuracy": report["combined"]["minimum_pilot_accuracy"],
            "minimum_sss_accuracy": report["combined"]["minimum_sss_accuracy"]})
    for capture in sorted(captures.iterdir() if captures.exists() else []):
        if not capture.is_dir():
            continue
        report_path = reports / f"{capture.name}.json"
        if report_path.is_file():
            followup_path = reports / "followups" / f"{capture.name}.json"
            try:
                if not followup_path.is_file():
                    followup = followup_capture(capture, report_path, followup_path,
                                                passes_path=passes_path)
                    decode_confirmed(capture, followup_path, followup)
                    recovered_followups.append({"capture": capture.name,
                                                "trigger_count": followup["trigger_count"],
                                                "confirmed": followup["confirmation"]["confirmed"]})
                else:
                    followup = json.loads(followup_path.read_text())
                    decode_confirmed(capture, followup_path, followup)
            except Exception as exc:
                errors.append({"capture": capture.name,
                               "error": f"follow-up/decode {type(exc).__name__}: {exc}"})
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
        settings = ({"exact_interval_s": wide_exact_interval_s, "exact_window_s": .01,
                     "acquisition_span_hz": 3_500_000,
                     "acquisition_step_hz": 500_000,
                     "exact_subband_rate_hz": 2_500_000}
                    if wide else {"exact_interval_s": narrow_exact_interval_s,
                                  "exact_window_s": .01})
        try:
            report = analyze_capture(capture, report_path, window_s=1,
                                     maximum_analysis_rate_hz=50_000,
                                     exact_acquisition_method=exact_acquisition_method,
                                     **settings)
            plot_beacon_report(report, plots / f"{capture.name}.png")
            followup_path = reports / "followups" / f"{capture.name}.json"
            followup = followup_capture(capture, report_path, followup_path,
                                        passes_path=passes_path)
            decode_confirmed(capture, followup_path, followup)
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
            "recovered_decodes": recovered_decodes,
            "skipped_count": len(skipped), "errors": errors}
