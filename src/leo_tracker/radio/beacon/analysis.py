"""Windowed, replayable Starlink beacon structure analysis."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

from .artifact import BeaconCapture
from .acquisition import acquire_exact_receiver
from .structure import analyze_frame_period

ANALYSIS_SCHEMA = "leo-tracker.starlink-beacon-analysis/v1"
DUAL_MATCH_MARGIN = .01
DUAL_SYMBOL_MARGIN = .005
DUAL_EPOCH_DELTA_SAMPLES = 20
SINGLE_MATCH_MARGIN = .015
SINGLE_SYMBOL_MARGIN = .01


def analyze_exact_window(values: np.ndarray, source_rate_hz: float, *, edge: str,
                         start_sample: int = 0, acquisition_span_hz: float = 0,
                         acquisition_step_hz: float = 500_000,
                         exact_subband_rate_hz: float = 2_500_000) -> dict:
    """Apply all exact/control gates to one paired-IQ window."""
    paired = np.asarray(values, np.complex64)
    if paired.ndim != 2 or paired.shape[1] != 2:
        raise ValueError("exact window must have shape (samples, 2 receivers)")
    receivers = []
    for receiver in range(2):
        exact = acquire_exact_receiver(paired[:, receiver], source_rate_hz,
            edge=edge, acquisition_span_hz=acquisition_span_hz,
            acquisition_step_hz=acquisition_step_hz,
            subband_rate_hz=exact_subband_rate_hz)
        receivers.append({"receiver": receiver, **exact})
    exact_rate = receivers[0]["acquisition"]["subband_rate_hz"]
    period = exact_rate / 750
    epoch_difference = abs(receivers[0]["acquisition"]["selected_epoch_sample"] -
                           receivers[1]["acquisition"]["selected_epoch_sample"])
    epoch_difference = min(epoch_difference, period - epoch_difference)
    pss_epoch_difference = abs(receivers[0]["pss"]["epoch_sample"] -
                               receivers[1]["pss"]["epoch_sample"])
    pss_epoch_difference = min(pss_epoch_difference, period - pss_epoch_difference)
    cfo_difference = abs(receivers[0]["pilot"]["frequency_offset_hz"] -
                         receivers[1]["pilot"]["frequency_offset_hz"])
    margins = [item["pilot"]["score_margin"] for item in receivers]
    coherences = [item["pilot"]["coherence"] for item in receivers]
    match_margins = [item["acquisition"]["match_score_margin"] for item in receivers]
    receiver_candidates = [match_margins[index] >= SINGLE_MATCH_MARGIN and
                           margins[index] >= SINGLE_SYMBOL_MARGIN
                           for index in range(2)]
    receiver_qualified = [match_margins[index] >= .03 and margins[index] >= .02
                          and coherences[index] >= .05 for index in range(2)]
    candidate = (min(match_margins) >= DUAL_MATCH_MARGIN and
                 min(margins) >= DUAL_SYMBOL_MARGIN and
                 epoch_difference <= DUAL_EPOCH_DELTA_SAMPLES)
    qualified = (min(match_margins) >= .03 and min(margins) >= .02 and
                 min(coherences) >= .05 and epoch_difference <= 8)
    followup_trigger = bool(any(receiver_candidates) or
                            (epoch_difference <= DUAL_EPOCH_DELTA_SAMPLES and
                             max(match_margins) >= DUAL_MATCH_MARGIN))
    return {"start_sample": int(start_sample), "start_s": start_sample / source_rate_hz,
            "duration_s": paired.shape[0] / source_rate_hz, "receivers": receivers,
            "epoch_difference_samples": epoch_difference,
            "pss_epoch_difference_samples": pss_epoch_difference,
            "cfo_difference_hz": cfo_difference, "candidate": bool(candidate),
            "qualified": bool(qualified), "receiver_candidates": receiver_candidates,
            "receiver_qualified": receiver_qualified, "followup_trigger": followup_trigger}


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
                    exact_interval_s: float = 60.0, exact_window_s: float = .1,
                    acquisition_span_hz: float = 0,
                    acquisition_step_hz: float = 500_000,
                    exact_subband_rate_hz: float = 2_500_000,
                    exact_start_s: float = 0,
                    exact_stop_s: float | None = None) -> dict:
    """Analyze independent windows without loading a complete long capture into RAM."""
    if min(window_s, maximum_analysis_rate_hz, exact_interval_s, exact_window_s,
           acquisition_step_hz, exact_subband_rate_hz) <= 0:
        raise ValueError("window lengths, intervals, and analysis rate must be positive")
    if acquisition_span_hz < 0:
        raise ValueError("acquisition span must be nonnegative")
    if exact_start_s < 0 or (exact_stop_s is not None and exact_stop_s <= exact_start_s):
        raise ValueError("exact replay interval must have 0 <= start < stop")
    capture = BeaconCapture.open(capture_path, verify=True)
    source_rate = float(capture.manifest["sample_rate_hz"])
    decimation = max(1, int(np.floor(source_rate / maximum_analysis_rate_hz)))
    analysis_rate = source_rate / decimation
    window_samples = max(1, round(window_s * analysis_rate))
    windows: list[dict] = []
    exact_checks: list[dict] = []
    next_exact_sample = round(exact_start_s * source_rate)
    exact_stop_sample = (capture.manifest["captured_samples_per_receiver"]
                         if exact_stop_s is None else round(exact_stop_s * source_rate))
    exact_pending: list[np.ndarray] = []
    analysis_index = 0
    carry = np.empty((0, 2), np.complex64)
    region = capture.manifest.get("metadata", {}).get("region", "center")
    edge = region.removesuffix("-edge") if region in ("lower-edge", "upper-edge") else None
    for record, values in capture.chunks():
        chunk_stop = record.first_sample_index + record.sample_count
        while edge and next_exact_sample < chunk_stop and next_exact_sample < exact_stop_sample:
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
            exact_checks.append(analyze_exact_window(exact_values, source_rate, edge=edge,
                start_sample=next_exact_sample, acquisition_span_hz=acquisition_span_hz,
                acquisition_step_hz=acquisition_step_hz,
                exact_subband_rate_hz=exact_subband_rate_hz))
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
                     "exact_window_s": exact_window_s, "exact_edge": edge,
                     "acquisition_span_hz": acquisition_span_hz,
                     "acquisition_step_hz": acquisition_step_hz,
                     "exact_subband_rate_hz": min(source_rate, exact_subband_rate_hz),
                     "exact_start_s": exact_start_s, "exact_stop_s": exact_stop_s},
        "windows": windows,
        "exact_checks": exact_checks,
        "doppler_track": doppler_track,
        "summary": {"window_count": len(windows), "qualified_window_count": len(qualified),
                    "qualified_fraction": len(qualified) / len(windows) if windows else 0.0,
                    "exact_check_count": len(exact_checks),
                    "exact_candidate_count": sum(item["candidate"] for item in exact_checks),
                    "exact_qualified_count": sum(item["qualified"] for item in exact_checks),
                    "single_receiver_candidate_count": sum(
                        sum(item["receiver_candidates"]) for item in exact_checks),
                    "single_receiver_qualified_count": sum(
                        sum(item["receiver_qualified"]) for item in exact_checks),
                    "followup_trigger_count": sum(item["followup_trigger"] for item in exact_checks),
                    "doppler_track_qualified": doppler_track["qualified"]},
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    return report
