#!/usr/bin/env python3
"""Summarize signal-strength proxies in Starlink continuous-track reports.

The script accepts a tar stream containing ``leo-tracker.starlink-continuous-track``
JSON reports.  It intentionally reports normalized matched-filter measurements,
not calibrated RF power: the track artifacts do not contain event-local off-signal
PSD or an absolute receiver calibration.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import random
import re
import statistics
import sys
import tarfile
from typing import Any, Iterable


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def percentile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if finite(value))
    if not ordered:
        return None
    position = probability * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    clean = [float(value) for value in values if finite(value)]
    return {
        "n": len(clean),
        "p05": percentile(clean, 0.05),
        "p25": percentile(clean, 0.25),
        "median": percentile(clean, 0.50),
        "p75": percentile(clean, 0.75),
        "p95": percentile(clean, 0.95),
        "mean": statistics.fmean(clean) if clean else None,
    }


def correlation_snr_db(score: float) -> float:
    """Return the formal S/(N) proxy used by frame_tracking.py, in dB."""
    bounded = min(max(float(score), 1e-9), 1.0 - 1e-9)
    snr = bounded * bounded / max(1.0 - bounded * bounded, 1e-12)
    return 10.0 * math.log10(snr)


def rms_dbfs(rms: float, full_scale: float) -> float:
    return 20.0 * math.log10(max(float(rms), 1e-12) / float(full_scale))


def radio_label(report: dict[str, Any]) -> str:
    capture = Path(report.get("capture") or "").name
    match = re.search(r"pluto-(19f2|5d4d)", capture)
    if match:
        return f"pluto-{match.group(1)}"
    identity = (report.get("capture_manifest") or {}).get("identity") or {}
    return str(identity.get("serial") or identity.get("uri") or "legacy-radio")


def fit_slope(rows: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    if len(rows) < 3:
        return None, None
    times = [row[0] for row in rows]
    values = [row[1] for row in rows]
    center = statistics.fmean(times)
    mean_value = statistics.fmean(values)
    denominator = sum((time - center) ** 2 for time in times)
    if denominator <= 0:
        return None, None
    slope = sum((time - center) * (value - mean_value)
                for time, value in rows) / denominator
    intercept = mean_value - slope * center
    rms = math.sqrt(statistics.fmean(
        (value - (intercept + slope * time)) ** 2 for time, value in rows))
    return slope, rms


def event_row(report_name: str, report: dict[str, Any], track: dict[str, Any]
              ) -> dict[str, Any] | None:
    observations = track.get("observations") or []
    if not observations:
        return None
    dual = []
    consensus = []
    exact_scores: list[float] = []
    control_scores: list[float] = []
    margins: list[float] = []
    weakest_margins: list[float] = []
    sigmas: list[float] = []
    for observation in observations:
        receivers = observation.get("receivers") or []
        is_dual = bool(receivers) and all(receiver.get("valid") for receiver in receivers)
        if is_dual:
            dual.append(observation)
            local_margins = []
            for receiver in receivers:
                exact = receiver.get("exact_score")
                control = receiver.get("control_score")
                margin = receiver.get("score_margin")
                sigma = receiver.get("formal_sigma_hz")
                if finite(exact):
                    exact_scores.append(float(exact))
                if finite(control):
                    control_scores.append(float(control))
                if finite(margin):
                    margins.append(float(margin))
                    local_margins.append(float(margin))
                if finite(sigma):
                    sigmas.append(float(sigma))
            if local_margins:
                weakest_margins.append(min(local_margins))
        combined = observation.get("consensus") or {}
        if (combined.get("valid") and finite(combined.get("apparent_doppler_hz"))
                and finite(observation.get("time_s"))):
            consensus.append((float(observation["time_s"]),
                              float(combined["apparent_doppler_hz"])))
    slope, fit_rms = fit_slope(consensus)
    times = [float(item["time_s"]) for item in observations if finite(item.get("time_s"))]
    dual_times = [float(item["time_s"]) for item in dual if finite(item.get("time_s"))]
    manifest = report.get("capture_manifest") or {}
    metadata = manifest.get("metadata") or {}
    signal = report.get("signal") or {}
    stats = (manifest.get("sample_statistics") or {}).get("receivers") or []
    full_scale = float((manifest.get("sample_statistics") or {}).get(
        "adc_nominal_full_scale") or 2048.0)
    capture_rms = [rms_dbfs(item["rms_magnitude"], full_scale)
                   for item in stats if finite(item.get("rms_magnitude"))]
    exact_snr = [correlation_snr_db(value) for value in exact_scores]
    discrimination = [20.0 * math.log10(max(exact, 1e-12) / max(control, 1e-12))
                      for exact, control in zip(exact_scores, control_scores)]
    capture_id = Path(report.get("capture") or report_name).name
    created = str(report.get("created_utc") or "")
    date = created[:10] if len(created) >= 10 else "unknown"
    row = {
        "event_id": f"{capture_id}::{track.get('track_id')}",
        "report": report_name,
        "capture_id": capture_id,
        "track_id": track.get("track_id"),
        "date": date,
        "radio": radio_label(report),
        "channel": metadata.get("channel_number"),
        "edge": str(metadata.get("region") or signal.get("edge") or "").replace("-edge", ""),
        "observation_mode": metadata.get("observation_mode"),
        "gain_mode": manifest.get("gain_mode") or metadata.get("assigned_gain_mode"),
        "configured_gain_db": manifest.get("configured_gain_db"),
        "sample_rate_hz": manifest.get("sample_rate_hz") or signal.get("sample_rate_hz"),
        "nominal_rf_hz": metadata.get("nominal_rf_hz") or signal.get("nominal_rf_hz"),
        "observation_count": len(observations),
        "dual_valid_count": len(dual),
        "consensus_count": len(consensus),
        "duration_s": max(times) - min(times) if len(times) >= 2 else 0.0,
        "dual_duration_s": max(dual_times) - min(dual_times) if len(dual_times) >= 2 else 0.0,
        "doppler_drift_hz_s": slope,
        "doppler_fit_rms_hz": fit_rms,
        "seed_score": (track.get("seed") or {}).get("score"),
        "best_weak_receiver_margin": max(weakest_margins) if weakest_margins else None,
        "median_weak_receiver_margin": percentile(weakest_margins, 0.5),
        "median_score_margin": percentile(margins, 0.5),
        "median_exact_correlation": percentile(exact_scores, 0.5),
        "median_control_correlation": percentile(control_scores, 0.5),
        "median_template_discrimination_db": percentile(discrimination, 0.5),
        "median_matched_filter_snr_proxy_db": percentile(exact_snr, 0.5),
        "median_formal_frequency_sigma_hz": percentile(sigmas, 0.5),
        "capture_rms_rx0_dbfs": capture_rms[0] if len(capture_rms) > 0 else None,
        "capture_rms_rx1_dbfs": capture_rms[1] if len(capture_rms) > 1 else None,
        "_doppler_times_s": [item[0] for item in consensus],
        "_doppler_hz": [item[1] for item in consensus],
    }
    row["dual_valid"] = row["dual_valid_count"] >= 2
    row["strong"] = (row["dual_valid"] and finite(row["best_weak_receiver_margin"])
                     and float(row["best_weak_receiver_margin"]) >= 0.10)
    row["long"] = row["strong"] and float(row["dual_duration_s"]) >= 5.0
    row["slope_qualified"] = row["dual_valid"] and finite(slope) and len(consensus) >= 3
    return row


def read_reports() -> list[tuple[str, dict[str, Any]]]:
    reports = []
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".json"):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            try:
                report = json.loads(extracted.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if str(report.get("schema") or "").startswith(
                    "leo-tracker.starlink-continuous-track/"):
                reports.append((Path(member.name).name, report))
    return reports


def deduplicate(reports: list[tuple[str, dict[str, Any]]]
                ) -> list[tuple[str, dict[str, Any]]]:
    best: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, report in reports:
        capture = str(report.get("capture") or name)
        candidate = (name, report)
        if capture not in best or len(report.get("tracks") or []) > len(
                best[capture][1].get("tracks") or []):
            best[capture] = candidate
    return sorted(best.values(), key=lambda item: item[0])


def bootstrap_capture_median(rows: list[dict[str, Any]], field: str,
                             repetitions: int = 2000) -> dict[str, float | None]:
    by_capture: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if finite(row.get(field)):
            by_capture.setdefault(str(row["capture_id"]), []).append(row)
    captures = sorted(by_capture)
    if not captures:
        return {"low": None, "high": None}
    rng = random.Random(20260817)
    medians = []
    for _ in range(repetitions):
        sample = [rng.choice(captures) for _ in captures]
        values = [float(row[field]) for capture in sample for row in by_capture[capture]]
        medians.append(float(statistics.median(values)))
    return {"low": percentile(medians, 0.025), "high": percentile(medians, 0.975)}


def grouped(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        values.setdefault(str(row.get(field)), []).append(row)
    return {
        key: {
            "events": len(group),
            "median_matched_filter_snr_proxy_db": percentile(
                (row["median_matched_filter_snr_proxy_db"] for row in group), 0.5),
            "median_score_margin": percentile(
                (row["median_score_margin"] for row in group), 0.5),
            "median_duration_s": percentile((row["dual_duration_s"] for row in group), 0.5),
        }
        for key, group in sorted(values.items())
    }


def capture_row(name: str, report: dict[str, Any], event_rows: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = report.get("capture_manifest") or {}
    metadata = manifest.get("metadata") or {}
    signal = report.get("signal") or {}
    stats = (manifest.get("sample_statistics") or {}).get("receivers") or []
    full_scale = float((manifest.get("sample_statistics") or {}).get(
        "adc_nominal_full_scale") or 2048.0)
    capture_rms = [rms_dbfs(item["rms_magnitude"], full_scale)
                   for item in stats if finite(item.get("rms_magnitude"))]
    capture_id = Path(report.get("capture") or name).name
    created = str(report.get("created_utc") or "")
    return {
        "capture_id": capture_id,
        "report": name,
        "date": created[:10] if len(created) >= 10 else "unknown",
        "radio": radio_label(report),
        "channel": metadata.get("channel_number"),
        "edge": str(metadata.get("region") or signal.get("edge") or "").replace("-edge", ""),
        "gain_mode": manifest.get("gain_mode") or metadata.get("assigned_gain_mode"),
        "configured_gain_db": manifest.get("configured_gain_db"),
        "sample_rate_hz": manifest.get("sample_rate_hz") or signal.get("sample_rate_hz"),
        "capture_rms_rx0_dbfs": capture_rms[0] if len(capture_rms) > 0 else None,
        "capture_rms_rx1_dbfs": capture_rms[1] if len(capture_rms) > 1 else None,
        "track_count": len(event_rows),
        "dual_valid_track_count": sum(bool(row["dual_valid"]) for row in event_rows),
        "strong_slope_track_count": sum(
            bool(row["strong"] and row["slope_qualified"]) for row in event_rows),
    }


def capture_background_comparison(captures: list[dict[str, Any]]) -> dict[str, Any]:
    answer = {}
    gain_modes = sorted({str(row["gain_mode"]) for row in captures})
    for gain_mode in ["all", *gain_modes]:
        selected = (captures if gain_mode == "all" else
                    [row for row in captures if str(row["gain_mode"]) == gain_mode])
        no_track = [row for row in selected if int(row["track_count"]) == 0]
        detected = [row for row in selected if int(row["strong_slope_track_count"]) > 0]
        entry = {"no_track_captures": len(no_track), "strong_slope_captures": len(detected)}
        for receiver in (0, 1):
            field = f"capture_rms_rx{receiver}_dbfs"
            baseline = distribution(row[field] for row in no_track)
            occupied = distribution(row[field] for row in detected)
            entry[field] = {
                "no_track": baseline,
                "strong_slope": occupied,
                "median_difference_db": (
                    float(occupied["median"]) - float(baseline["median"])
                    if finite(occupied["median"]) and finite(baseline["median"]) else None),
            }
        answer[gain_mode] = entry
    return answer


def stratified_capture_background(captures: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare detected/no-track RMS within like hardware and observing states."""
    fields = ("date", "gain_mode", "radio", "channel", "edge")
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in captures:
        groups.setdefault(tuple(str(row[field]) for field in fields), []).append(row)
    result = {"strata": list(fields), "minimum_captures_per_class": 5}
    for receiver in (0, 1):
        rms_field = f"capture_rms_rx{receiver}_dbfs"
        differences = []
        for group in groups.values():
            baseline = [float(row[rms_field]) for row in group
                        if int(row["track_count"]) == 0 and finite(row[rms_field])]
            detected = [float(row[rms_field]) for row in group
                        if int(row["strong_slope_track_count"]) > 0 and finite(row[rms_field])]
            if len(baseline) >= 5 and len(detected) >= 5:
                differences.append(float(statistics.median(detected)
                                         - statistics.median(baseline)))
        rng = random.Random(20260817 + receiver)
        bootstrap = sorted(statistics.median(rng.choices(
            differences, k=len(differences))) for _ in range(20_000))
        median = float(statistics.median(differences))
        result[f"rx{receiver}"] = {
            "stratum_count": len(differences),
            "median_difference_db": median,
            "median_power_ratio": 10.0 ** (median / 10.0),
            "stratum_bootstrap_95pct_ci_db": {
                "low": percentile(bootstrap, 0.025),
                "high": percentile(bootstrap, 0.975),
            },
            "median_after_excluding_abs_difference_over_5db": statistics.median(
                value for value in differences if abs(value) <= 5.0),
        }
    return result


def representative_examples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [row for row in rows if row["strong"] and row["slope_qualified"]
                and float(row["dual_duration_s"]) >= 5.0
                and finite(row["median_matched_filter_snr_proxy_db"])]
    strengths = [float(row["median_matched_filter_snr_proxy_db"]) for row in eligible]
    targets = [
        ("strong long track", percentile(strengths, 0.99)),
        ("typical long track", percentile(strengths, 0.50)),
        ("weak long track", percentile(strengths, 0.05)),
    ]
    chosen = []
    for label, target in targets:
        available = [row for row in eligible if row not in chosen]
        row = min(available, key=lambda item: (
            abs(float(item["median_matched_filter_snr_proxy_db"]) - float(target)),
            float(item["doppler_fit_rms_hz"]),
            str(item["event_id"])))
        row["_example_label"] = label
        chosen.append(row)
    public_fields = ("event_id", "date", "radio", "channel", "edge",
                     "dual_duration_s", "doppler_drift_hz_s", "doppler_fit_rms_hz",
                     "median_matched_filter_snr_proxy_db", "median_score_margin")
    return [{"label": row["_example_label"],
             **{field: row[field] for field in public_fields}} for row in chosen]


def save_figures(output_dir: Path, rows: list[dict[str, Any]], captures: list[dict[str, Any]],
                 examples: list[dict[str, Any]]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    main = [row for row in rows if row["strong"] and row["slope_qualified"]]
    strength = np.asarray([float(row["median_matched_filter_snr_proxy_db"])
                           for row in main], dtype=float)
    figure, axis = plt.subplots(figsize=(9.2, 4.8), constrained_layout=True)
    bins = np.linspace(float(np.percentile(strength, 0.5)),
                       float(np.percentile(strength, 99.5)), 55)
    axis.hist(strength, bins=bins, color="#2878b5", alpha=.82, edgecolor="white",
              linewidth=.35)
    median = float(np.median(strength))
    axis.axvline(median, color="#c44e52", lw=2,
                 label=f"median {median:.2f} dB")
    axis.axvspan(float(np.percentile(strength, 25)), float(np.percentile(strength, 75)),
                 color="#dd8452", alpha=.16, label="middle 50%")
    axis.set(title=f"Matched-filter strength across {len(main):,} strong Doppler tracks",
             xlabel="Per-track median matched-filter SNR proxy (dB)",
             ylabel="Track count")
    axis.grid(axis="y", alpha=.22)
    axis.legend(frameon=False)
    figure.savefig(output_dir / "strength-distribution.png", dpi=180)
    plt.close(figure)

    fields = ("date", "gain_mode", "radio", "channel", "edge")
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in captures:
        groups.setdefault(tuple(str(row[field]) for field in fields), []).append(row)
    differences = {0: [], 1: []}
    for group in groups.values():
        for receiver in (0, 1):
            field = f"capture_rms_rx{receiver}_dbfs"
            baseline = [float(row[field]) for row in group
                        if int(row["track_count"]) == 0 and finite(row[field])]
            detected = [float(row[field]) for row in group
                        if int(row["strong_slope_track_count"]) > 0 and finite(row[field])]
            if len(baseline) >= 5 and len(detected) >= 5:
                differences[receiver].append(float(np.median(detected) - np.median(baseline)))
    figure, axis = plt.subplots(figsize=(8.2, 4.8), constrained_layout=True)
    clipped = 0
    for receiver, color in ((0, "#2878b5"), (1, "#dd8452")):
        values = np.asarray(differences[receiver])
        visible = values[np.abs(values) <= 2.5]
        clipped += int(len(values) - len(visible))
        jitter = np.linspace(-.10, .10, len(visible))
        axis.scatter(np.full(len(visible), receiver) + jitter, visible, s=30,
                     color=color, alpha=.64, edgecolor="none")
        axis.plot([receiver - .20, receiver + .20], [np.median(values)] * 2,
                  color="#222222", lw=3)
        axis.text(receiver, np.median(values) + .35,
                  f"median {np.median(values):+.3f} dB", ha="center", fontsize=10)
    axis.axhline(0, color="#555555", lw=1)
    axis.set_xticks([0, 1], ["RX0", "RX1"])
    axis.set_ylim(-2.5, 3.0)
    axis.set(title="Broadband RMS lift in matched detection/no-track strata",
             ylabel="Strong-track minus no-track median RMS (dB)")
    axis.text(.99, .02, f"{clipped} hardware-state outliers beyond +/-2.5 dB omitted",
              transform=axis.transAxes, ha="right", va="bottom", fontsize=9,
              color="#555555")
    axis.grid(axis="y", alpha=.22)
    figure.savefig(output_dir / "background-lift.png", dpi=180)
    plt.close(figure)

    figure, axes = plt.subplots(1, 2, figsize=(11.5, 4.8), constrained_layout=True)
    for axis, field, order, title in (
        (axes[0], "channel", ["1", "2", "3", "4"], "By channel"),
        (axes[1], "date", sorted({str(row["date"]) for row in main}), "By date"),
    ):
        for index, key in enumerate(order):
            values = np.asarray([float(row["median_matched_filter_snr_proxy_db"])
                                 for row in main if str(row[field]) == key])
            quantiles = np.percentile(values, [5, 25, 50, 75, 95])
            axis.plot([index, index], [quantiles[0], quantiles[4]],
                      color="#777777", lw=1.2)
            axis.plot([index, index], [quantiles[1], quantiles[3]],
                      color="#2878b5", lw=7, solid_capstyle="butt")
            axis.scatter(index, quantiles[2], color="#c44e52", s=32, zorder=3)
        labels = [key if field == "channel" else key[5:] for key in order]
        axis.set_xticks(range(len(order)), labels, rotation=35 if field == "date" else 0)
        axis.set(title=title, xlabel="Channel" if field == "channel" else "2026 date (MM-DD)",
                 ylabel="Median SNR proxy per track (dB)")
        axis.grid(axis="y", alpha=.22)
    figure.suptitle("Matched-filter strength distributions by channel and date")
    figure.savefig(output_dir / "configuration-strength.png", dpi=180)
    plt.close(figure)

    lookup = {str(row["event_id"]): row for row in rows}
    figure, axes = plt.subplots(3, 1, figsize=(10.2, 8.6), constrained_layout=True)
    for axis, example in zip(axes, examples, strict=True):
        row = lookup[str(example["event_id"])]
        times = np.asarray(row["_doppler_times_s"], dtype=float)
        frequency = np.asarray(row["_doppler_hz"], dtype=float)
        elapsed = times - times[0]
        relative = (frequency - frequency[0]) / 1000.0
        fit = (float(row["doppler_drift_hz_s"]) * elapsed) / 1000.0
        axis.scatter(elapsed, relative, s=18, color="#2878b5", alpha=.78,
                     label="consensus Doppler")
        axis.plot(elapsed, fit, color="#c44e52", lw=1.8, label="linear fit")
        axis.set(title=(f"{example['label']}: {row['event_id']}  |  "
                        f"SNR {float(row['median_matched_filter_snr_proxy_db']):.2f} dB, "
                        f"drift {float(row['doppler_drift_hz_s']) / 1000:.2f} kHz/s"),
                 xlabel="Elapsed time (s)", ylabel="Relative Doppler (kHz)")
        axis.grid(alpha=.22)
    axes[0].legend(frameon=False, loc="best")
    figure.savefig(output_dir / "example-tracks.png", dpi=180)
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    arguments = parser.parse_args()
    reports_all = read_reports()
    reports = deduplicate(reports_all)
    rows = []
    rows_by_capture: dict[str, list[dict[str, Any]]] = {}
    for name, report in reports:
        for track in report.get("tracks") or []:
            row = event_row(name, report, track)
            if row is not None:
                rows.append(row)
                rows_by_capture.setdefault(str(report.get("capture") or name), []).append(row)
    rows.sort(key=lambda row: (str(row["date"]), str(row["event_id"])))
    captures = [capture_row(name, report, rows_by_capture.get(
        str(report.get("capture") or name), [])) for name, report in reports]
    dual = [row for row in rows if row["dual_valid"]]
    strong = [row for row in rows if row["strong"]]
    slope = [row for row in rows if row["slope_qualified"]]
    strong_slope = [row for row in rows if row["strong"] and row["slope_qualified"]]
    examples = representative_examples(rows)
    strength_fields = [
        "median_matched_filter_snr_proxy_db",
        "median_template_discrimination_db",
        "median_exact_correlation",
        "median_control_correlation",
        "median_score_margin",
        "best_weak_receiver_margin",
        "median_formal_frequency_sigma_hz",
        "dual_duration_s",
        "doppler_drift_hz_s",
        "doppler_fit_rms_hz",
        "capture_rms_rx0_dbfs",
        "capture_rms_rx1_dbfs",
    ]
    summary = {
        "schema": "leo-tracker.doppler-signal-strength-summary/v1",
        "source": {
            "tar_members_read": len(reports_all),
            "unique_capture_reports": len(reports),
            "selection": "all *narrow*.json reports supplied on stdin",
        },
        "cohorts": {
            "all_tracks": len(rows),
            "dual_valid": len(dual),
            "strong": len(strong),
            "strong_slope": len(strong_slope),
            "long": sum(bool(row["long"]) for row in rows),
            "slope_qualified": len(slope),
        },
        "definitions": {
            "dual_valid": "both receivers valid in at least two track observations",
            "strong": "dual_valid and best weaker-receiver exact-minus-control margin >= 0.10",
            "long": "strong and dual-valid duration >= 5 seconds",
            "slope_qualified": "dual_valid with at least three valid consensus Doppler samples",
            "matched_filter_snr_proxy": "10log10(rho^2/(1-rho^2)); rho is normalized exact-template correlation",
            "template_discrimination": "20log10(exact-template correlation / rolled-template control correlation)",
        },
        "dual_valid_distributions": {field: distribution(row[field] for row in dual)
                                     for field in strength_fields},
        "strong_distributions": {field: distribution(row[field] for row in strong)
                                  for field in strength_fields},
        "slope_qualified_distributions": {field: distribution(row[field] for row in slope)
                                           for field in strength_fields},
        "strong_slope_distributions": {field: distribution(row[field] for row in strong_slope)
                                        for field in strength_fields},
        "cluster_bootstrap_95pct_ci": {
            "dual_valid_median_matched_filter_snr_proxy_db": bootstrap_capture_median(
                dual, "median_matched_filter_snr_proxy_db"),
            "strong_median_matched_filter_snr_proxy_db": bootstrap_capture_median(
                strong, "median_matched_filter_snr_proxy_db"),
        },
        "strong_by_date": grouped(strong, "date"),
        "strong_by_radio": grouped(strong, "radio"),
        "strong_by_channel": grouped(strong, "channel"),
        "strong_by_edge": grouped(strong, "edge"),
        "strong_by_gain_mode": grouped(strong, "gain_mode"),
        "capture_background_comparison": capture_background_comparison(captures),
        "stratified_capture_background": stratified_capture_background(captures),
        "representative_examples": examples,
    }
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    fields = [field for field in rows[0] if not field.startswith("_")] if rows else []
    with (arguments.output_dir / "events.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    strongest = sorted(
        (row for row in slope if finite(row["median_matched_filter_snr_proxy_db"])),
        key=lambda row: float(row["median_matched_filter_snr_proxy_db"]), reverse=True)[:50]
    with (arguments.output_dir / "strongest_50.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(strongest)
    capture_fields = list(captures[0]) if captures else []
    with (arguments.output_dir / "captures.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=capture_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(captures)
    with (arguments.output_dir / "summary.json").open("w") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    save_figures(arguments.output_dir, rows, captures, examples)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
