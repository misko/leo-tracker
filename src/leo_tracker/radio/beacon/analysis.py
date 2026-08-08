"""Windowed, replayable Starlink beacon structure analysis."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

from .artifact import BeaconCapture
from .acquisition import acquire_exact_receiver, usable_acquisition_span_hz
from .frame_tracking import conditioned_frame_observations
from .structure import analyze_frame_period
from .template_learning import load_learned_beacon

ANALYSIS_SCHEMA = "leo-tracker.starlink-beacon-analysis/v1"
DUAL_MATCH_MARGIN = .01
DUAL_SYMBOL_MARGIN = .005
DUAL_EPOCH_DELTA_SAMPLES = 20
SINGLE_MATCH_MARGIN = .015
SINGLE_SYMBOL_MARGIN = .01
LEARNED_DUAL_FRAME_MARGIN = .05
LEARNED_MAXIMUM_CFO_DIFFERENCE_HZ = 15_000

DETECTION_GATES = {
    "coherent_grid_v1": {
        "dual_match_margin": DUAL_MATCH_MARGIN,
        "dual_symbol_margin": DUAL_SYMBOL_MARGIN,
        "dual_epoch_delta_samples": DUAL_EPOCH_DELTA_SAMPLES,
        "single_match_margin": SINGLE_MATCH_MARGIN,
        "single_symbol_margin": SINGLE_SYMBOL_MARGIN,
        "qualified_match_margin": .03,
        "qualified_symbol_margin": .02,
        "qualified_coherence": .05,
        "qualified_epoch_delta_samples": 8,
    },
    # These conservative provisional gates sit above the first detector-
    # specific field null. They are versioned separately so later calibration
    # can never change the meaning of a historical report.
    "pss_symbolwise_v2": {
        "dual_match_margin": .02,
        "dual_symbol_margin": .02,
        "dual_epoch_delta_samples": 20,
        "single_match_margin": .025,
        "single_symbol_margin": .03,
        "qualified_match_margin": .05,
        "qualified_symbol_margin": .05,
        "qualified_coherence": .05,
        "qualified_epoch_delta_samples": 8,
    },
    "pilot_symbolwise_v3": {
        "dual_match_margin": .02,
        "dual_symbol_margin": .02,
        "dual_epoch_delta_samples": 20,
        "single_match_margin": .025,
        "single_symbol_margin": .03,
        "qualified_match_margin": .05,
        "qualified_symbol_margin": .05,
        "qualified_coherence": .05,
        "qualified_epoch_delta_samples": 8,
    },
}


def detection_gates(method: str) -> dict:
    try:
        return dict(DETECTION_GATES[method])
    except KeyError as exc:
        raise ValueError(f"unknown exact acquisition method: {method}") from exc


def analyze_exact_window(values: np.ndarray, source_rate_hz: float, *, edge: str,
                         start_sample: int = 0, acquisition_span_hz: float = 0,
                         acquisition_step_hz: float = 500_000,
                         exact_subband_rate_hz: float = 2_500_000,
                         acquisition_method: str = "coherent_grid_v1",
                         learned_templates: tuple[np.ndarray, np.ndarray] | None = None,
                         learned_template_source: str | None = None) -> dict:
    """Apply all exact/control gates to one paired-IQ window."""
    paired = np.asarray(values, np.complex64)
    if paired.ndim != 2 or paired.shape[1] != 2:
        raise ValueError("exact window must have shape (samples, 2 receivers)")
    gates = detection_gates(acquisition_method)
    receivers = []
    for receiver in range(2):
        exact = acquire_exact_receiver(paired[:, receiver], source_rate_hz,
            edge=edge, acquisition_span_hz=acquisition_span_hz,
            acquisition_step_hz=acquisition_step_hz,
            subband_rate_hz=exact_subband_rate_hz, method=acquisition_method)
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
    receiver_candidates = [match_margins[index] >= gates["single_match_margin"] and
                           margins[index] >= gates["single_symbol_margin"]
                           for index in range(2)]
    receiver_qualified = [match_margins[index] >= gates["qualified_match_margin"] and
                          margins[index] >= gates["qualified_symbol_margin"] and
                          coherences[index] >= gates["qualified_coherence"]
                          for index in range(2)]
    pilot_candidate = (min(match_margins) >= gates["dual_match_margin"] and
                       min(margins) >= gates["dual_symbol_margin"] and
                       epoch_difference <= gates["dual_epoch_delta_samples"])
    full_frame_evidence = {"available": False, "candidate": False,
                           "template_source": learned_template_source,
                           "receivers": []}
    if learned_templates is not None:
        if len(learned_templates) != 2:
            raise ValueError("learned templates must contain exactly two receivers")
        learned_receivers = []
        for receiver, template in enumerate(learned_templates):
            source = receivers[receiver]
            observation = conditioned_frame_observations(
                paired[:, receiver], source_rate_hz,
                epoch_sample=int(source["acquisition"]["selected_epoch_sample"]),
                coarse_cfo_hz=float(source["pilot"]["frequency_offset_hz"]),
                absolute_start_sample=start_sample, edge=edge,
                minimum_margin=0, template=template)
            best = int(np.argmax(observation["score_margin"]))
            learned_receivers.append({
                "receiver": receiver,
                "best_frame_index": best,
                "best_frame_start_sample": int(observation["frame_start_sample"][best]),
                "frequency_offset_hz": float(observation["frequency_offset_hz"][best]),
                "formal_sigma_hz": float(observation["formal_sigma_hz"][best]),
                "exact_score": float(observation["exact_score"][best]),
                "control_score": float(observation["control_score"][best]),
                "score_margin": float(observation["score_margin"][best]),
                "median_score_margin": float(np.median(observation["score_margin"])),
            })
        learned_cfo_difference = abs(
            learned_receivers[0]["frequency_offset_hz"] -
            learned_receivers[1]["frequency_offset_hz"])
        learned_candidate = bool(
            min(item["score_margin"] for item in learned_receivers) >=
                LEARNED_DUAL_FRAME_MARGIN and
            epoch_difference <= gates["dual_epoch_delta_samples"] and
            learned_cfo_difference <= LEARNED_MAXIMUM_CFO_DIFFERENCE_HZ)
        full_frame_evidence = {
            "available": True,
            "candidate": learned_candidate,
            "template_source": learned_template_source,
            "receivers": learned_receivers,
            "cfo_difference_hz": float(learned_cfo_difference),
            "gates": {"minimum_dual_frame_margin": LEARNED_DUAL_FRAME_MARGIN,
                      "maximum_epoch_delta_samples": gates[
                          "dual_epoch_delta_samples"],
                      "maximum_cfo_difference_hz":
                          LEARNED_MAXIMUM_CFO_DIFFERENCE_HZ},
        }
    learned_candidate = bool(full_frame_evidence["candidate"])
    candidate = bool(pilot_candidate or learned_candidate)
    qualified = (min(match_margins) >= gates["qualified_match_margin"] and
                 min(margins) >= gates["qualified_symbol_margin"] and
                 min(coherences) >= gates["qualified_coherence"] and
                 epoch_difference <= gates["qualified_epoch_delta_samples"])
    followup_trigger = bool(learned_candidate or any(receiver_candidates) or
                            (epoch_difference <= gates["dual_epoch_delta_samples"] and
                             max(match_margins) >= gates["dual_match_margin"]))
    return {"start_sample": int(start_sample), "start_s": start_sample / source_rate_hz,
            "duration_s": paired.shape[0] / source_rate_hz, "receivers": receivers,
            "epoch_difference_samples": epoch_difference,
            "pss_epoch_difference_samples": pss_epoch_difference,
            "cfo_difference_hz": cfo_difference, "candidate": candidate,
            "candidate_basis": ([basis for basis, present in
                                  (("published_pilot", pilot_candidate),
                                   ("learned_full_frame", learned_candidate)) if present]),
            "full_frame_evidence": full_frame_evidence,
            "qualified": bool(qualified), "receiver_candidates": receiver_candidates,
            "receiver_qualified": receiver_qualified, "followup_trigger": followup_trigger,
            "detection_gates": gates, "exact_acquisition_method": acquisition_method}


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
                    exact_acquisition_method: str = "coherent_grid_v1",
                    exact_start_s: float = 0,
                    exact_stop_s: float | None = None,
                    learned_beacon_path: Path | None = None) -> dict:
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
    learned_templates = None
    learned_template_source = None
    if learned_beacon_path is not None:
        learned_report, learned_arrays = load_learned_beacon(learned_beacon_path)
        if not learned_report.get("summary", {}).get("qualified", False):
            raise ValueError("learned beacon did not pass held-out qualification")
        if (abs(float(learned_report["sample_rate_hz"]) - source_rate) > 1e-6 or
                learned_report["region"] != region):
            raise ValueError("learned beacon rate or region does not match capture")
        learned_templates = tuple(learned_arrays[f"template_rx{receiver}"]
                                  for receiver in range(2))
        learned_template_source = str(Path(learned_beacon_path).resolve())
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
                exact_subband_rate_hz=exact_subband_rate_hz,
                acquisition_method=exact_acquisition_method,
                learned_templates=learned_templates,
                learned_template_source=learned_template_source))
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
    exact_sampled_time_s = float(sum(item["duration_s"] for item in exact_checks))
    capture_duration_s = (capture.manifest["captured_samples_per_receiver"] / source_rate)
    report = {
        "schema": ANALYSIS_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "capture": str(Path(capture_path).resolve()),
        "capture_manifest": capture.manifest,
        "analysis": {"window_s": window_s, "decimation": decimation,
                     "analysis_rate_hz": analysis_rate, "exact_interval_s": exact_interval_s,
                     "exact_window_s": exact_window_s, "exact_edge": edge,
                     "acquisition_span_hz": min(acquisition_span_hz,
                         usable_acquisition_span_hz(source_rate, exact_subband_rate_hz)),
                     "requested_acquisition_span_hz": acquisition_span_hz,
                     "acquisition_step_hz": acquisition_step_hz,
                     "exact_subband_rate_hz": min(source_rate, exact_subband_rate_hz),
                     "exact_acquisition_method": exact_acquisition_method,
                     "learned_beacon": learned_template_source,
                     "detection_gates": detection_gates(exact_acquisition_method),
                     "exact_start_s": exact_start_s, "exact_stop_s": exact_stop_s},
        "windows": windows,
        "exact_checks": exact_checks,
        "doppler_track": doppler_track,
        "summary": {"window_count": len(windows), "qualified_window_count": len(qualified),
                    "qualified_fraction": len(qualified) / len(windows) if windows else 0.0,
                    "exact_check_count": len(exact_checks),
                    "exact_sampled_time_s": exact_sampled_time_s,
                    "exact_temporal_coverage_fraction": (
                        exact_sampled_time_s / capture_duration_s
                        if capture_duration_s > 0 else 0.0),
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
