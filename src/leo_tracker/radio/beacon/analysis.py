"""Windowed, replayable Starlink beacon structure analysis."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

from .artifact import BeaconCapture
from .structure import analyze_frame_period
from .pilots import track_edge_pilots
from .templates import acquire_pss_epoch

ANALYSIS_SCHEMA = "leo-tracker.starlink-beacon-analysis/v1"


def summarize_doppler_track(exact_checks: list[dict]) -> dict:
    selected = [item for item in exact_checks if item.get("candidate")]
    if len(selected) < 3:
        return {"available": False, "point_count": len(selected), "qualified": False,
                "reason": "at least three exact-pilot candidate epochs are required"}
    times = np.asarray([item["start_s"] for item in selected], float)
    frequencies = [np.asarray([item["receivers"][receiver]["pilot"]["frequency_offset_hz"]
                               for item in selected], float) for receiver in range(2)]
    slopes = [float(np.polyfit(times, values, 1)[0]) for values in frequencies]
    correlation = float(np.corrcoef(frequencies[0], frequencies[1])[0, 1])
    slope_difference = abs(slopes[0] - slopes[1])
    qualified = (np.isfinite(correlation) and correlation >= .8 and
                 slope_difference <= 500 and max(map(abs, slopes)) <= 15_000)
    return {"available": True, "point_count": len(selected), "qualified": bool(qualified),
            "start_s": float(times[0]), "stop_s": float(times[-1]),
            "receiver_slopes_hz_s": slopes, "slope_difference_hz_s": slope_difference,
            "receiver_frequency_correlation": correlation,
            "maximum_physical_slope_hz_s": 15_000,
            "qualification": {"minimum_correlation": .8,
                              "maximum_slope_difference_hz_s": 500}}


def analyze_capture(capture_path: Path, output: Path, *, window_s: float = 1.0,
                    maximum_analysis_rate_hz: float = 250_000,
                    exact_interval_s: float = 60.0, exact_window_s: float = .1) -> dict:
    """Analyze independent windows without loading a complete long capture into RAM."""
    if min(window_s, maximum_analysis_rate_hz, exact_interval_s, exact_window_s) <= 0:
        raise ValueError("window lengths, intervals, and analysis rate must be positive")
    capture = BeaconCapture.open(capture_path, verify=True)
    source_rate = float(capture.manifest["sample_rate_hz"])
    decimation = max(1, int(np.floor(source_rate / maximum_analysis_rate_hz)))
    analysis_rate = source_rate / decimation
    window_samples = max(1, round(window_s * analysis_rate))
    windows: list[dict] = []
    exact_checks: list[dict] = []
    next_exact_sample = 0
    exact_pending: list[np.ndarray] = []
    analysis_index = 0
    carry = np.empty((0, 2), np.complex64)
    region = capture.manifest.get("metadata", {}).get("region", "center")
    edge = region.removesuffix("-edge") if region in ("lower-edge", "upper-edge") else None
    for record, values in capture.chunks():
        chunk_stop = record.first_sample_index + record.sample_count
        while edge and next_exact_sample < chunk_stop:
            if next_exact_sample < record.first_sample_index and not exact_pending:
                next_exact_sample = record.first_sample_index
            count = round(exact_window_s * source_rate)
            pending_count = sum(item.shape[0] for item in exact_pending)
            local_start = (0 if exact_pending else
                           max(0, next_exact_sample - record.first_sample_index))
            take = min(count - pending_count, values.shape[0] - local_start)
            if take > 0:
                exact_pending.append(values[local_start:local_start + take])
            if pending_count + take < count:
                break
            exact_values = np.concatenate(exact_pending, axis=0)
            exact_pending = []
            receivers = []
            for receiver in range(2):
                pss = acquire_pss_epoch(exact_values[:, receiver], source_rate, edge=edge)
                pilot = track_edge_pilots(exact_values[:, receiver], source_rate,
                                           pss["epoch_sample"], edge=edge)
                receivers.append({"receiver": receiver, "pss": pss, "pilot": pilot})
            period = source_rate / 750
            epoch_difference = abs(receivers[0]["pss"]["epoch_sample"] -
                                   receivers[1]["pss"]["epoch_sample"])
            epoch_difference = min(epoch_difference, period - epoch_difference)
            cfo_difference = abs(receivers[0]["pilot"]["frequency_offset_hz"] -
                                 receivers[1]["pilot"]["frequency_offset_hz"])
            pss_ratios = [item["pss"]["peak_to_median"] for item in receivers]
            margins = [item["pilot"]["score_margin"] for item in receivers]
            coherences = [item["pilot"]["coherence"] for item in receivers]
            # Independent LNB local oscillators may have a large static CFO
            # difference. Identity comes from the exact codes and common frame
            # epoch; Doppler is compared from the CFO *change* over time.
            candidate = (min(pss_ratios) >= 1.8 and min(margins) >= .005 and
                         epoch_difference <= 20)
            qualified = (min(pss_ratios) >= 2.5 and min(margins) >= .02 and
                         min(coherences) >= .05 and epoch_difference <= 8)
            exact_checks.append({"start_sample": next_exact_sample,
                "start_s": next_exact_sample / source_rate, "duration_s": exact_window_s,
                "receivers": receivers, "epoch_difference_samples": epoch_difference,
                "cfo_difference_hz": cfo_difference, "candidate": bool(candidate),
                "qualified": bool(qualified)})
            next_exact_sample += round(exact_interval_s * source_rate)
        selected = values[::decimation]
        combined = np.concatenate((carry, selected), axis=0)
        start = 0
        while start + window_samples <= combined.shape[0]:
            window = combined[start:start + window_samples]
            result = analyze_frame_period(window.T, analysis_rate)
            result["start_s"] = analysis_index / analysis_rate
            result["duration_s"] = window.shape[0] / analysis_rate
            windows.append(result)
            analysis_index += window.shape[0]
            start += window_samples
        carry = combined[start:].copy()
    qualified = [item for item in windows if item["qualified"]]
    doppler_track = summarize_doppler_track(exact_checks)
    report = {
        "schema": ANALYSIS_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "capture": str(Path(capture_path).resolve()),
        "capture_manifest": capture.manifest,
        "analysis": {"window_s": window_s, "decimation": decimation,
                     "analysis_rate_hz": analysis_rate, "exact_interval_s": exact_interval_s,
                     "exact_window_s": exact_window_s, "exact_edge": edge},
        "windows": windows,
        "exact_checks": exact_checks,
        "doppler_track": doppler_track,
        "summary": {"window_count": len(windows), "qualified_window_count": len(qualified),
                    "qualified_fraction": len(qualified) / len(windows) if windows else 0.0,
                    "exact_check_count": len(exact_checks),
                    "exact_candidate_count": sum(item["candidate"] for item in exact_checks),
                    "exact_qualified_count": sum(item["qualified"] for item in exact_checks),
                    "doppler_track_qualified": doppler_track["qualified"]},
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    return report
