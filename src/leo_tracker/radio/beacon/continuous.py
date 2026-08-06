"""Continuous full-frame Starlink edge-pilot tracking.

The acquisition detector deliberately works on sparse, short windows.  This
module starts from one of those acquired frame epochs and follows the complete
300-symbol edge beacon at a fixed navigation output cadence.  The resulting
frequency observations remain referenced to the receiver/LNB oscillators; the
orbit association stage estimates those unavoidable affine nuisance terms.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from leo_tracker.orbit.artifacts import utc_iso

from .artifact import BeaconCapture
from .frame_tracking import FRAME_TRACK_SCHEMA, LEGACY_FRAME_TRACK_SCHEMAS
from .pilots import edge_pilot_frame
from .structure import STARLINK_FRAME_DURATION_S


TRACK_SCHEMA = "leo-tracker.starlink-continuous-track/v1"
SPEED_OF_LIGHT_M_S = 299_792_458.0


@dataclass(frozen=True)
class TrackSeed:
    time_s: float
    epoch_samples: tuple[int, int]
    cfo_hz: tuple[float, float]
    candidate_receivers: tuple[bool, bool]
    drift_hz_s: tuple[float, float]
    score: float
    source_check_index: int


def _parabolic_peak(frequencies: np.ndarray, scores: np.ndarray,
                    index: int) -> tuple[float, float]:
    """Refine a uniformly sampled maximum without claiming false precision."""
    frequency = float(frequencies[index])
    step = float(np.median(np.diff(frequencies))) if frequencies.size > 1 else 0.0
    if index == 0 or index == scores.size - 1 or step <= 0:
        return frequency, max(step, 1.0)
    left, peak, right = map(float, scores[index - 1:index + 2])
    denominator = left - 2 * peak + right
    displacement = 0.0 if denominator >= 0 else .5 * (left - right) / denominator
    displacement = float(np.clip(displacement, -1.0, 1.0))
    curvature = max(peak - .5 * (left + right), 1e-12)
    background = max(float(np.median(scores)), 1e-12)
    # This is a formal correlator uncertainty.  Cross-receiver disagreement is
    # folded into the calibrated consensus uncertainty later in the pipeline.
    sigma = step * np.sqrt(background / curvature) / np.sqrt(12)
    return frequency + displacement * step, float(np.clip(sigma, step / 8, 500.0))


def measure_full_frame_window(samples: np.ndarray, sample_rate_hz: float, *,
                              epoch_sample: int, predicted_cfo_hz: float,
                              edge: str = "lower", search_span_hz: float = 2_000,
                              search_step_hz: float = 100) -> dict:
    """Measure CFO from every complete 4/3-ms frame in one output window.

    The expensive part of a conditioned delay/Doppler bank is expressed as one
    matrix multiplication for all frames.  The exact beacon and a 17-symbol
    rolled control are evaluated on the identical frequency grid.
    """
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if sample_rate_hz <= 0 or search_span_hz <= 0 or search_step_hz <= 0:
        raise ValueError("rate, search span, and search step must be positive")
    if epoch_sample < 0:
        raise ValueError("epoch sample must be nonnegative")
    template = edge_pilot_frame(sample_rate_hz, edge)
    control = edge_pilot_frame(sample_rate_hz, edge, symbol_roll=17)
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    starts: list[int] = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + template.size > values.size:
            break
        starts.append(start)
        frame += 1
    if len(starts) < 3:
        raise ValueError("window must contain at least three complete Starlink frames")

    local_indexes = np.arange(template.size, dtype=float)
    indexes = np.asarray(starts)[:, None] + local_indexes.astype(np.int64)[None, :]
    frames = values[indexes]
    energy = np.sum(np.abs(frames) ** 2, axis=1)
    exact_energy = float(np.vdot(template, template).real)
    control_energy = float(np.vdot(control, control).real)
    offsets = np.arange(-search_span_hz, search_span_hz + search_step_hz / 2,
                        search_step_hz, dtype=float)
    frequencies = predicted_cfo_hz + offsets
    oscillators = np.exp(-2j * np.pi * frequencies[:, None] *
                         local_indexes[None, :] / sample_rate_hz)
    exact_correlations = (frames * np.conj(template)[None, :]) @ oscillators.T
    control_correlations = (frames * np.conj(control)[None, :]) @ oscillators.T
    exact_scores = np.mean(np.abs(exact_correlations) /
        np.sqrt(np.maximum(exact_energy * energy[:, None], 1e-30)), axis=0)
    control_scores = np.mean(np.abs(control_correlations) /
        np.sqrt(np.maximum(control_energy * energy[:, None], 1e-30)), axis=0)
    best = int(np.argmax(exact_scores))
    frequency_hz, formal_sigma_hz = _parabolic_peak(frequencies, exact_scores, best)
    return {
        "frequency_offset_hz": frequency_hz,
        "formal_sigma_hz": formal_sigma_hz,
        "exact_score": float(exact_scores[best]),
        "control_score": float(control_scores[best]),
        "score_margin": float(exact_scores[best] - control_scores[best]),
        "frame_count": len(starts),
        "search_center_hz": float(predicted_cfo_hz),
        "search_span_hz": float(search_span_hz),
        "search_step_hz": float(search_step_hz),
        "peak_at_search_boundary": bool(best in (0, frequencies.size - 1)),
    }


def _candidate_seeds(followup: dict, sample_rate_hz: float, *,
                     maximum_gap_s: float = 5.0,
                     maximum_seed_collapse_gap_s: float = 10.0) -> list[TrackSeed]:
    candidates = []
    for index, check in enumerate(followup.get("checks", [])):
        receivers = check.get("receivers", [])
        if len(receivers) != 2 or not (check.get("candidate") or
                                      any(check.get("receiver_candidates", []))):
            continue
        epochs = tuple(int(item["acquisition"]["selected_epoch_sample"])
                       for item in receivers)
        cfo = tuple(float(item["pilot"]["frequency_offset_hz"])
                    for item in receivers)
        margins = [float(item["acquisition"].get("match_score_margin", 0)) +
                   float(item["pilot"].get("score_margin", 0)) for item in receivers]
        flags = tuple(bool(value) for value in check.get(
            "receiver_candidates", [check.get("candidate", False)] * 2))
        candidates.append(TrackSeed(float(check["start_s"]), epochs, cfo, flags,
                                    (0.0, 0.0), float(sum(margins)), index))
    if not candidates:
        raise ValueError("follow-up contains no usable Starlink acquisition seed")
    # A one-receiver trigger can rescue a capture where the second RF chain is
    # temporarily weak.  Once any simultaneous dual-RX acquisition exists,
    # however, single-RX CFOs are not allowed to fragment or bias the calibrated
    # trajectory used to seed both receivers.
    dual_candidates = [item for item in candidates
                       if all(item.candidate_receivers)]
    if dual_candidates:
        candidates = dual_candidates

    # Dense replay contributes several seeds for the same RF event.  Collapse
    # only locally continuous points; widely separated events in one capture
    # remain independent tracks.
    groups: list[list[TrackSeed]] = []
    for seed in sorted(candidates, key=lambda item: item.time_s):
        compatible = []
        for group_index, group in enumerate(groups):
            previous = group[-1]
            elapsed = seed.time_s - previous.time_s
            if len(group) >= 2:
                group_times = np.asarray([item.time_s for item in group])
                group_cfo = np.asarray([np.mean([
                    value for value, valid in zip(item.cfo_hz,
                                                  item.candidate_receivers, strict=True)
                    if valid]) for item in group])
                drift = float(np.clip(np.polyfit(group_times, group_cfo, 1)[0],
                                      -15_000, 15_000))
                previous_cfo = np.mean([
                    value for value, valid in zip(previous.cfo_hz,
                                                  previous.candidate_receivers, strict=True)
                    if valid])
                expected = previous_cfo + drift * elapsed
                allowed_error = 5_000 + 2_000 * elapsed
            else:
                expected = np.mean([
                    value for value, valid in zip(previous.cfo_hz,
                                                  previous.candidate_receivers, strict=True)
                    if valid])
                allowed_error = 5_000 + 15_000 * elapsed
            seed_cfo = np.mean([
                value for value, valid in zip(seed.cfo_hz,
                                              seed.candidate_receivers, strict=True)
                if valid])
            mean_delta = abs(seed_cfo - expected)
            # Pilot timing has cyclic aliases at low SNR.  CFO continuity is
            # therefore the stable cross-window grouping observable; exact
            # dual-RX epoch agreement was already required within each seed.
            # A tracker may coast through a longer RF outage, but acquisition
            # seeds on opposite sides of that outage must remain available:
            # their recovered frame epochs can legitimately reset between
            # Starlink transmissions.  Collapsing them would retain only the
            # highest-scoring (often later) seed and make the earlier burst
            # unreachable even though tracking itself is bidirectional.
            collapse_gap_s = min(maximum_gap_s, maximum_seed_collapse_gap_s)
            if 0 < elapsed <= collapse_gap_s and mean_delta <= allowed_error:
                compatible.append((mean_delta / elapsed, group_index))
        if compatible:
            groups[min(compatible)[1]].append(seed)
        else:
            groups.append([seed])
    seeds = []
    for group in groups:
        selected = max(group, key=lambda item: item.score)
        if len(group) >= 2:
            fitted = []
            for receiver in range(2):
                valid_group = [item for item in group
                               if item.candidate_receivers[receiver]]
                if len(valid_group) < 2:
                    fitted.append(0.0)
                    continue
                fitted.append(float(np.clip(np.polyfit(
                    [item.time_s for item in valid_group],
                    [item.cfo_hz[receiver] for item in valid_group], 1)[0],
                    -15_000, 15_000)))
            drift = tuple(fitted)
        else:
            drift = (0.0, 0.0)
        seeds.append(TrackSeed(selected.time_s, selected.epoch_samples,
                               selected.cfo_hz, selected.candidate_receivers,
                               drift, selected.score,
                               selected.source_check_index))
    return seeds


def _global_epoch(seed: TrackSeed, receiver: int, sample_rate_hz: float) -> int:
    start_sample = round(seed.time_s * sample_rate_hz)
    return start_sample + seed.epoch_samples[receiver]


def _local_epoch(global_epoch: int, window_start: int, sample_rate_hz: float) -> int:
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    frame = int(np.ceil((window_start - global_epoch) / period))
    return global_epoch + round(frame * period) - window_start


def _dense_followup_groups(followup: dict, *, maximum_gap_s: float,
                           maximum_drift_hz_s: float) -> list[list[tuple[int, dict]]]:
    """Group independently acquired 10 Hz full-frame measurements by CFO path.

    Starlink edge transmissions do not preserve one recoverable local frame
    epoch across every 100 ms window. Dense acquisition nevertheless measures
    the complete exact frame at each epoch. CFO and the stable RX1-RX0 offset,
    rather than a fictitious global epoch, therefore define track continuity.
    """
    points = [(index, check) for index, check in enumerate(followup.get("checks", []))
              if len(check.get("receivers", [])) == 2 and
              all(check.get("receiver_candidates", [False, False])) and
              check.get("epoch_difference_samples", np.inf) <= 20]
    groups: list[list[tuple[int, dict]]] = []
    for point in points:
        _, check = point
        compatible = []
        current_cfo = np.asarray([
            receiver["pilot"]["frequency_offset_hz"]
            for receiver in check["receivers"]], float)
        for group_index, group in enumerate(groups):
            history = group[-6:]
            history_times = np.asarray([item[1]["start_s"] for item in history])
            elapsed = float(check["start_s"] - history_times[-1])
            if not 0 < elapsed <= maximum_gap_s:
                continue
            history_cfo = np.asarray([[
                receiver["pilot"]["frequency_offset_hz"]
                for receiver in item[1]["receivers"]] for item in history], float)
            if len(history) >= 2:
                rates = np.asarray([np.polyfit(history_times,
                    history_cfo[:, receiver], 1)[0] for receiver in range(2)])
                rates = np.clip(rates, -maximum_drift_hz_s, maximum_drift_hz_s)
            else:
                rates = np.zeros(2)
            expected = history_cfo[-1] + rates * elapsed
            cfo_error = float(np.max(np.abs(current_cfo - expected)))
            relative_error = abs(float(np.diff(current_cfo)[0] -
                                       np.median(np.diff(history_cfo, axis=1))))
            allowed_cfo = 2_000 + 2_000 * elapsed
            if cfo_error <= allowed_cfo and relative_error <= 1_500:
                compatible.append((cfo_error + relative_error, group_index))
        if compatible:
            groups[min(compatible)[1]].append(point)
        else:
            groups.append([point])
    return groups


def _tracks_from_dense_followup(followup: dict, manifest: dict, *,
                                maximum_gap_s: float,
                                maximum_drift_hz_s: float) -> list[dict]:
    groups = _dense_followup_groups(followup, maximum_gap_s=maximum_gap_s,
                                    maximum_drift_hz_s=maximum_drift_hz_s)
    tracks = []
    for track_index, group in enumerate(groups):
        if len(group) < 2:
            continue
        observations = []
        for check_index, check in group:
            receivers = []
            for receiver_index, source in enumerate(check["receivers"]):
                exact = source["acquisition"].get("exact_match", {})
                searched = np.asarray(exact.get("searched_frequency_offsets_hz", []), float)
                step = (float(np.median(np.diff(searched))) if searched.size >= 2 else 200.0)
                receivers.append({"receiver": receiver_index, "valid": True,
                    "validity_basis": "independent_dense_full_frame_acquisition",
                    "frequency_offset_hz": float(source["pilot"]["frequency_offset_hz"]),
                    "formal_sigma_hz": max(abs(step) / np.sqrt(12), 50.0),
                    "exact_score": float(exact.get("score", 0)),
                    "control_score": float(source["acquisition"].get(
                        "control_match", {}).get("score", 0)),
                    "score_margin": float(source["acquisition"].get(
                        "match_score_margin", 0)),
                    "epoch_sample": int(source["acquisition"]["selected_epoch_sample"]),
                    "source_check_index": check_index})
            observations.append({"time_s": float(check["start_s"] +
                check.get("duration_s", 0) / 2),
                "start_sample": int(check["start_sample"]), "receivers": receivers,
                "dual_tracking_support": True,
                "relative_receiver_error_hz": float(
                    receivers[1]["frequency_offset_hz"] -
                    receivers[0]["frequency_offset_hz"]),
                "lock_valid": True})
        times = np.asarray([item["time_s"] for item in observations])
        frequencies = np.asarray([[receiver["frequency_offset_hz"]
            for receiver in item["receivers"]] for item in observations])
        rates = ([float(np.polyfit(times, frequencies[:, receiver], 1)[0])
                  for receiver in range(2)] if len(observations) >= 2 else [0.0, 0.0])
        calibration = _relative_calibration(observations)
        duration = float(times[-1] - times[0]) if len(times) > 1 else 0.0
        track = {"track_id": f"track-{track_index:03d}",
            "seed": {"time_s": float(times[0]),
                     "epoch_samples": [item["epoch_sample"] for item in
                                       observations[0]["receivers"]],
                     "receiver_cfo_hz": frequencies[0].tolist(),
                     "score": float(sum(item["score_margin"] for item in
                                        observations[0]["receivers"])),
                     "candidate_receivers": [True, True],
                     "initial_drift_hz_s": rates,
                     "source_check_index": group[0][0]},
            "observations": observations,
            "relative_receiver_calibration": calibration,
            "summary": {"observation_count": len(observations),
                        "valid_observation_count": len(observations),
                        "dual_valid_observation_count": len(observations),
                        "valid_duration_s": duration,
                        "dual_valid_duration_s": duration}}
        _apply_consensus(track, manifest)
        tracks.append(track)
    return tracks


def _tracks_from_conditioned_frames(frame_track_path: Path, capture_path: Path,
                                    followup_path: Path, manifest: dict, *,
                                    output_rate_hz: float, maximum_gap_s: float,
                                    maximum_drift_hz_s: float) -> list[dict]:
    """Aggregate checksummed 750 Hz frame phase into calibrated 10 Hz points."""
    frame_report = json.loads(Path(frame_track_path).read_text())
    if frame_report.get("schema") not in ({FRAME_TRACK_SCHEMA} |
                                           LEGACY_FRAME_TRACK_SCHEMAS):
        raise ValueError("frame track has an unknown schema")
    if (Path(frame_report["capture"]).resolve() != Path(capture_path).resolve() or
            Path(frame_report["followup"]).resolve() != Path(followup_path).resolve()):
        raise ValueError("frame track sources do not match the requested capture")
    samples_path = Path(frame_report["samples"]["path"])
    if hashlib.sha256(samples_path.read_bytes()).hexdigest() != frame_report[
            "samples"]["sha256"]:
        raise ValueError("conditioned frame sample checksum mismatch")
    with np.load(samples_path, allow_pickle=False) as source:
        times = np.asarray(source["time_s"], float)
        frequencies = np.asarray(source["frequency_offset_hz"], float)
        sigmas = np.asarray(source["formal_sigma_hz"], float)
        exact = np.asarray(source["exact_score"], float)
        control = np.asarray(source["control_score"], float)
        margins = np.asarray(source["score_margin"], float)
        valid = np.asarray(source["valid"], bool)
        frame_starts = np.asarray(source["frame_start_sample"])
    sparse_margin = float(frame_report.get("configuration", {}).get(
        "minimum_sparse_frame_margin", .05))
    if any(value.shape != frequencies.shape for value in
           (sigmas, exact, control, margins, valid, frame_starts)) or frequencies.shape != (
            len(times), 2):
        raise ValueError("conditioned frame arrays have inconsistent shapes")
    dual = np.all(valid, axis=1) & np.all(np.isfinite(frequencies), axis=1)
    bins = np.floor(times * output_rate_hz).astype(np.int64)
    observations = []
    for bin_index in np.unique(bins[dual]):
        selected = np.flatnonzero(dual & (bins == bin_index))
        strong = np.min(margins[selected], axis=1) >= sparse_margin
        sparse_selected = bool(np.any(strong))
        if sparse_selected:
            # A strong sparse frame must not be averaged with chance-valid
            # low-margin noise frames from the same navigation bin.
            selected = selected[strong]
        elif selected.size < 3:
            # At the documented 2% baseline PRF, a 100-ms navigation bin
            # contains only about 1.5 active frames on average. A singleton is
            # still a real full-frame CFO observation when both receivers
            # independently beat their controls by the sparse-frame gate.
            continue
        # Phase unwrap failures are rare but large.  A per-receiver MAD gate
        # prevents one frame from moving the durable 10 Hz observation.
        keep = np.ones(selected.size, bool)
        for receiver in range(2):
            values = frequencies[selected, receiver]
            median = np.median(values); mad = np.median(np.abs(values - median))
            keep &= np.abs(values - median) <= max(6 * mad, 100.0)
        selected = selected[keep]
        if not selected.size or (selected.size < 3 and not sparse_selected):
            continue
        receivers = []
        for receiver in range(2):
            weights = 1 / np.maximum(sigmas[selected, receiver], 1.0) ** 2
            estimate = float(np.average(frequencies[selected, receiver], weights=weights))
            scatter = (float(np.std(frequencies[selected, receiver], ddof=1) /
                             np.sqrt(selected.size))
                       if selected.size >= 2 else 0.0)
            formal = float(max(np.sqrt(1 / np.sum(weights)), scatter, 1.0))
            receivers.append({"receiver": receiver, "valid": True,
                "validity_basis": "conditioned_750hz_dual_frame_phase",
                "frequency_offset_hz": estimate, "formal_sigma_hz": formal,
                "exact_score": float(np.mean(exact[selected, receiver])),
                "control_score": float(np.mean(control[selected, receiver])),
                "score_margin": float(np.mean(margins[selected, receiver])),
                "conditioned_frame_count": int(selected.size),
                "first_frame_start_sample": int(frame_starts[selected[0], receiver]),
                "last_frame_start_sample": int(frame_starts[selected[-1], receiver])})
        observations.append({"time_s": float(np.mean(times[selected])),
            "start_sample": int(round(bin_index / output_rate_hz *
                                      manifest["sample_rate_hz"])),
            "receivers": receivers, "dual_tracking_support": True,
            "relative_receiver_error_hz": float(
                receivers[1]["frequency_offset_hz"] -
                receivers[0]["frequency_offset_hz"]),
            "lock_valid": True, "conditioned_frame_count": int(selected.size)})
    groups: list[list[dict]] = []
    for observation in observations:
        current = np.asarray([item["frequency_offset_hz"]
                              for item in observation["receivers"]])
        compatible = []
        for group_index, group in enumerate(groups):
            history = group[-6:]
            history_times = np.asarray([item["time_s"] for item in history])
            elapsed = observation["time_s"] - history_times[-1]
            if not 0 < elapsed <= maximum_gap_s:
                continue
            history_cfo = np.asarray([[receiver["frequency_offset_hz"]
                for receiver in item["receivers"]] for item in history])
            rates = (np.asarray([np.polyfit(history_times, history_cfo[:, receiver], 1)[0]
                       for receiver in range(2)]) if len(history) >= 2 else np.zeros(2))
            rates = np.clip(rates, -maximum_drift_hz_s, maximum_drift_hz_s)
            error = float(np.max(np.abs(current - (history_cfo[-1] + rates * elapsed))))
            relative_error = abs(float(np.diff(current)[0] -
                                       np.median(np.diff(history_cfo, axis=1))))
            if error <= 2_000 + 2_000 * elapsed and relative_error <= 1_500:
                compatible.append((error + relative_error, group_index))
        if compatible:
            groups[min(compatible)[1]].append(observation)
        else:
            groups.append([observation])
    tracks = []
    for track_index, group in enumerate(groups):
        if len(group) < 2:
            continue
        calibration = _relative_calibration(group)
        times_group = np.asarray([item["time_s"] for item in group])
        frequencies_group = np.asarray([[receiver["frequency_offset_hz"]
            for receiver in item["receivers"]] for item in group])
        rates = [float(np.polyfit(times_group, frequencies_group[:, receiver], 1)[0])
                 for receiver in range(2)]
        duration = float(times_group[-1] - times_group[0])
        track = {"track_id": f"track-{track_index:03d}",
            "seed": {"time_s": float(times_group[0]), "epoch_samples": [None, None],
                "receiver_cfo_hz": frequencies_group[0].tolist(), "score": float(
                    sum(item["score_margin"] for item in group[0]["receivers"])),
                "candidate_receivers": [True, True], "initial_drift_hz_s": rates,
                "source_check_index": None},
            "observations": group, "relative_receiver_calibration": calibration,
            "summary": {"observation_count": len(group),
                "valid_observation_count": len(group),
                "dual_valid_observation_count": len(group),
                "valid_duration_s": duration, "dual_valid_duration_s": duration,
                "conditioned_frame_count": sum(item["conditioned_frame_count"]
                                                for item in group)}}
        _apply_consensus(track, manifest); tracks.append(track)
    return tracks


def _reacquisition_span(search_span_hz: float, maximum_span_hz: float,
                        gap_s: float) -> float:
    """Grow bounded Doppler uncertainty while the beacon is absent."""
    return float(min(maximum_span_hz, search_span_hz + 1_000 * gap_s))


def _track_direction(capture: BeaconCapture, seed: TrackSeed, *, direction: int,
                     edge: str, output_interval_s: float, search_span_hz: float,
                     maximum_reacquisition_span_hz: float,
                     search_step_hz: float, minimum_margin: float,
                     tracking_margin: float, maximum_relative_error_hz: float,
                     maximum_gap_s: float, maximum_drift_hz_s: float) -> list[dict]:
    rate = float(capture.manifest["sample_rate_hz"])
    total = int(capture.manifest["captured_samples_per_receiver"])
    window_samples = round(output_interval_s * rate)
    seed_bin = int(np.floor(seed.time_s / output_interval_s))
    bin_index = seed_bin if direction > 0 else seed_bin - 1
    last_time = seed.time_s
    frequencies = list(seed.cfo_hz)
    rates = list(seed.drift_hz_s)
    global_epochs = [_global_epoch(seed, receiver, rate) for receiver in range(2)]
    gap_s = 0.0
    observations = []
    while True:
        start_sample = bin_index * window_samples
        if start_sample < 0 or start_sample + window_samples > total:
            break
        center_s = (start_sample + window_samples / 2) / rate
        elapsed = center_s - last_time
        predicted = [frequencies[receiver] + rates[receiver] * elapsed
                     for receiver in range(2)]
        paired = capture.read_window(start_sample, window_samples)
        receiver_results = []
        adaptive_span_hz = _reacquisition_span(
            search_span_hz, maximum_reacquisition_span_hz, gap_s)
        for receiver in range(2):
            local_epoch = _local_epoch(global_epochs[receiver], start_sample, rate)
            result = measure_full_frame_window(
                paired[:, receiver], rate, epoch_sample=local_epoch,
                predicted_cfo_hz=predicted[receiver], edge=edge,
                search_span_hz=adaptive_span_hz, search_step_hz=search_step_hz)
            result["receiver"] = receiver
            result["valid"] = bool(result["score_margin"] >= minimum_margin and
                                   not result["peak_at_search_boundary"])
            result["validity_basis"] = ("strong_exact_control_margin"
                                        if result["valid"] else None)
            receiver_results.append(result)
        relative_error = ((receiver_results[1]["frequency_offset_hz"] -
                           receiver_results[0]["frequency_offset_hz"]) -
                          (predicted[1] - predicted[0]))
        dual_tracking_support = bool(
            all(item["score_margin"] >= tracking_margin and
                not item["peak_at_search_boundary"] for item in receiver_results) and
            abs(relative_error) <= maximum_relative_error_hz)
        if dual_tracking_support:
            for result in receiver_results:
                if not result["valid"]:
                    result["valid"] = True
                    result["validity_basis"] = "dual_rx_tracking_prior"
        # Once prediction has coasted for a second, only simultaneous RX0/RX1
        # exact-code evidence may reacquire it. A lone receiver remains useful
        # during normal lock but cannot reset a long outage timer.
        any_valid = (all(item["valid"] for item in receiver_results)
                     if gap_s >= 1.0 else
                     any(item["valid"] for item in receiver_results))
        if any_valid:
            gap_s = 0.0
            for receiver, result in enumerate(receiver_results):
                if not result["valid"]:
                    continue
                measured = float(result["frequency_offset_hz"])
                if abs(elapsed) > 1e-9:
                    instantaneous = np.clip((measured - frequencies[receiver]) / elapsed,
                                            -maximum_drift_hz_s, maximum_drift_hz_s)
                    rates[receiver] = float(.7 * rates[receiver] + .3 * instantaneous)
                frequencies[receiver] = measured
            last_time = center_s
        else:
            gap_s += output_interval_s
        observations.append({"time_s": center_s, "start_sample": start_sample,
                             "receivers": receiver_results,
                             "dual_tracking_support": dual_tracking_support,
                             "relative_receiver_error_hz": float(relative_error),
                             "lock_valid": any_valid})
        if gap_s >= maximum_gap_s:
            break
        bin_index += direction
    return observations


def _relative_calibration(observations: list[dict]) -> dict:
    paired = [item for item in observations
              if all(receiver["valid"] for receiver in item["receivers"])]
    if len(paired) < 3:
        return {"available": False, "paired_observation_count": len(paired)}
    times = np.asarray([item["time_s"] for item in paired], float)
    reference = float(np.mean(times))
    differences = np.asarray([item["receivers"][1]["frequency_offset_hz"] -
                              item["receivers"][0]["frequency_offset_hz"]
                              for item in paired], float)
    design = np.column_stack((np.ones_like(times), times - reference))
    coefficients, *_ = np.linalg.lstsq(design, differences, rcond=None)
    residuals = differences - design @ coefficients
    return {"available": True, "paired_observation_count": len(paired),
            "reference_time_s": reference,
            "rx1_minus_rx0_offset_hz": float(coefficients[0]),
            "rx1_minus_rx0_drift_hz_s": float(coefficients[1]),
            "residual_rms_hz": float(np.sqrt(np.mean(residuals ** 2)))}


def _apply_consensus(track: dict, manifest: dict) -> None:
    calibration = track["relative_receiver_calibration"]
    tuned_rf_hz = float(manifest["rf_center_hz"])
    nominal_rf_hz = float(manifest.get("metadata", {}).get(
        "nominal_rf_hz", tuned_rf_hz))
    for observation in track["observations"]:
        valid = [item for item in observation["receivers"] if item["valid"]]
        if not valid:
            observation["consensus"] = {"valid": False}
            continue
        corrected = []
        variances = []
        for item in valid:
            value = float(item["frequency_offset_hz"])
            if item["receiver"] == 1 and calibration.get("available"):
                relative = (calibration["rx1_minus_rx0_offset_hz"] +
                    calibration["rx1_minus_rx0_drift_hz_s"] *
                    (observation["time_s"] - calibration["reference_time_s"]))
                value -= relative
            corrected.append(value)
            variances.append(max(float(item["formal_sigma_hz"]), 1.0) ** 2)
        weights = 1 / np.asarray(variances)
        consensus = float(np.average(corrected, weights=weights))
        sigma = float(np.sqrt(1 / np.sum(weights)))
        if calibration.get("available"):
            sigma = float(np.hypot(sigma, calibration["residual_rms_hz"] / np.sqrt(2)))
        apparent = tuned_rf_hz + consensus - nominal_rf_hz
        observation["consensus"] = {
            "valid": True,
            "reference_receiver": 0,
            "receiver_referenced_cfo_hz": consensus,
            "frequency_sigma_hz": sigma,
            "apparent_doppler_hz": apparent,
            "apparent_pseudorange_rate_m_s": -SPEED_OF_LIGHT_M_S * apparent / nominal_rf_hz,
            "warning": ("includes absolute LNB/receiver and satellite clock bias; "
                        "orbit association must fit nuisance offset and drift"),
        }


def _capture_utc_anchor(manifest: dict) -> tuple[int, str, float | None]:
    timing = manifest.get("stream_timing", {})
    if timing.get("first_read_start_utc_ns") is not None:
        return (int(timing["first_read_start_utc_ns"]), "host_first_read_start",
                timing.get("maximum_read_duration_s"))
    chunks = manifest.get("chunks", [])
    if chunks:
        return int(chunks[0]["first_utc_ns"]), "first_chunk_host_midpoint", None
    return int(manifest["created_utc_ns"]), "capture_manifest_created", None


def _sample_utc_ns(manifest: dict, sample_position: float
                   ) -> tuple[int, str, float | None]:
    """Map a stored sample position onto measured host UTC.

    An IIO refill is not hardware timestamped. It nevertheless supplies a
    host-time bracket for every block, and beacon artifacts retain the first
    and last block midpoints in each checksummed chunk. USB/network delivery
    can run slower than the configured RF sample clock, so extrapolating UTC
    from ``sample_position / sample_rate`` creates seconds of nonlinear timing
    error in a long capture. Piecewise interpolation preserves the measured
    elapsed time; the remaining half-refill ambiguity is published explicitly.
    """
    if sample_position < 0:
        raise ValueError("sample position must be nonnegative")
    timing = manifest.get("stream_timing", {})
    chunks = manifest.get("chunks", [])
    clock_samples = timing.get("clock_samples", [])
    if len(clock_samples) >= 2:
        positions = np.asarray([float(item["first_sample_index"]) +
            float(item["sample_count"]) / 2 for item in clock_samples])
        utc_values = np.asarray([float(item["utc_ns"]) for item in clock_samples])
        if np.all(np.diff(positions) > 0) and np.all(np.diff(utc_values) >= 0):
            utc_ns = round(float(np.interp(sample_position, positions, utc_values,
                left=utc_values[0] + (sample_position - positions[0]) * 1e9 /
                    float(manifest["sample_rate_hz"]),
                right=utc_values[-1] + (sample_position - positions[-1]) * 1e9 /
                    float(manifest["sample_rate_hz"]))))
            nearest = int(np.argmin(np.abs(positions - sample_position)))
            uncertainty = int(clock_samples[nearest].get("read_duration_ns", 0)) / 2e9
            return utc_ns, "iio_read_midpoint_interpolation", uncertainty
    measured = bool(timing.get("first_read_start_utc_ns") is not None and
                    int(timing.get("read_count", 0)) >= 2 and chunks)
    if measured:
        for chunk in chunks:
            first_sample = float(chunk["first_sample_index"])
            sample_count = float(chunk["sample_count"])
            if sample_count <= 0:
                continue
            stop_sample = first_sample + sample_count
            if sample_position <= stop_sample or chunk is chunks[-1]:
                fraction = float(np.clip(
                    (sample_position - first_sample) / sample_count, 0.0, 1.0))
                first_ns = int(chunk["first_utc_ns"])
                last_ns = int(chunk["last_utc_ns"])
                utc_ns = round(first_ns + fraction * (last_ns - first_ns))
                uncertainty = (float(timing.get("maximum_read_duration_s", 0)) / 2 +
                               float(timing.get("maximum_positive_host_gap_s", 0)))
                return utc_ns, "chunk_host_midpoint_interpolation", uncertainty
    anchor_ns, method, uncertainty = _capture_utc_anchor(manifest)
    rate = float(manifest["sample_rate_hz"])
    return (anchor_ns + round(sample_position * 1e9 / rate), method,
            uncertainty)


def track_capture(capture_path: Path, followup_path: Path, output: Path, *,
                  output_rate_hz: float = 10.0, search_span_hz: float = 2_000,
                  maximum_reacquisition_span_hz: float = 15_000,
                  search_step_hz: float = 200, minimum_margin: float = .015,
                  tracking_margin: float = .005,
                  maximum_relative_error_hz: float = 500,
                  maximum_gap_s: float = 5.0,
                  maximum_drift_hz_s: float = 15_000,
                  measurement_source: str = "auto",
                  frame_track_path: Path | None = None) -> dict:
    """Track every independent acquired event in a frozen narrow capture."""
    if min(output_rate_hz, search_span_hz, maximum_reacquisition_span_hz,
           search_step_hz, maximum_relative_error_hz,
           maximum_gap_s, maximum_drift_hz_s) <= 0 or min(
               minimum_margin, tracking_margin) < 0 or tracking_margin > minimum_margin:
        raise ValueError("tracking configuration is invalid")
    if maximum_reacquisition_span_hz < search_span_hz:
        raise ValueError("maximum reacquisition span cannot be below locked search span")
    if measurement_source not in ("auto", "conditioned_frames", "dense_followup",
                                  "periodic_epoch"):
        raise ValueError("unknown tracking measurement source")
    capture = BeaconCapture.open(capture_path)
    manifest = capture.manifest
    rate = float(manifest["sample_rate_hz"])
    followup = json.loads(Path(followup_path).read_text())
    if Path(followup["capture"]).resolve() != Path(capture_path).resolve():
        raise ValueError("follow-up and capture paths do not match")
    region = manifest.get("metadata", {}).get("region", "")
    if region not in ("lower-edge", "upper-edge"):
        raise ValueError("continuous tracking requires a fixed edge-band capture")
    exact_rate = float(followup.get("checks", [{}])[0].get("receivers", [{}])[0]
                       .get("acquisition", {}).get("subband_rate_hz", rate))
    if abs(exact_rate - rate) > 1e-6:
        raise ValueError("digitally offset/downsampled wide acquisitions are not trackable yet")
    frame_tracks = (_tracks_from_conditioned_frames(frame_track_path, capture_path,
        followup_path, manifest, output_rate_hz=output_rate_hz,
        maximum_gap_s=maximum_gap_s, maximum_drift_hz_s=maximum_drift_hz_s)
        if frame_track_path is not None and measurement_source not in (
            "dense_followup", "periodic_epoch") else [])
    if measurement_source == "conditioned_frames" and frame_track_path is None:
        raise ValueError("conditioned frame measurements require --frame-track")
    if measurement_source == "conditioned_frames" and not frame_tracks:
        raise ValueError("frame track contains no grouped dual-RX observations")
    dense_tracks = (_tracks_from_dense_followup(followup, manifest,
        maximum_gap_s=maximum_gap_s, maximum_drift_hz_s=maximum_drift_hz_s)
        if not frame_tracks and measurement_source != "periodic_epoch" else [])
    if measurement_source == "dense_followup" and not dense_tracks:
        raise ValueError("follow-up contains no group of dense dual-RX measurements")
    selected_source = ("conditioned_frames" if frame_tracks else
                       "dense_followup" if dense_tracks else "periodic_epoch")
    direct_tracks = frame_tracks or dense_tracks
    seeds = ([] if direct_tracks else
             _candidate_seeds(followup, rate, maximum_gap_s=maximum_gap_s))
    interval = 1 / output_rate_hz
    tracks = list(direct_tracks)
    for index, seed in enumerate(seeds):
        backward = _track_direction(capture, seed, direction=-1, edge=region[:-5],
            output_interval_s=interval, search_span_hz=search_span_hz,
            maximum_reacquisition_span_hz=maximum_reacquisition_span_hz,
            search_step_hz=search_step_hz, minimum_margin=minimum_margin,
            tracking_margin=tracking_margin,
            maximum_relative_error_hz=maximum_relative_error_hz,
            maximum_gap_s=maximum_gap_s, maximum_drift_hz_s=maximum_drift_hz_s)
        forward = _track_direction(capture, seed, direction=1, edge=region[:-5],
            output_interval_s=interval, search_span_hz=search_span_hz,
            maximum_reacquisition_span_hz=maximum_reacquisition_span_hz,
            search_step_hz=search_step_hz, minimum_margin=minimum_margin,
            tracking_margin=tracking_margin,
            maximum_relative_error_hz=maximum_relative_error_hz,
            maximum_gap_s=maximum_gap_s, maximum_drift_hz_s=maximum_drift_hz_s)
        observations = sorted(backward + forward, key=lambda item: item["time_s"])
        # Bin overlap at the seed boundary is possible only through rounding.
        observations = list({item["start_sample"]: item for item in observations}.values())
        observations.sort(key=lambda item: item["time_s"])
        valid = [item for item in observations if item["lock_valid"]]
        dual_valid = [item for item in observations
                      if all(receiver["valid"] for receiver in item["receivers"])]
        track = {"track_id": f"track-{index:03d}",
            "seed": {"time_s": seed.time_s, "epoch_samples": list(seed.epoch_samples),
                     "receiver_cfo_hz": list(seed.cfo_hz), "score": seed.score,
                     "candidate_receivers": list(seed.candidate_receivers),
                     "initial_drift_hz_s": list(seed.drift_hz_s),
                     "source_check_index": seed.source_check_index},
            "observations": observations,
            "relative_receiver_calibration": _relative_calibration(observations),
            "summary": {"observation_count": len(observations),
                        "valid_observation_count": len(valid),
                        "dual_valid_observation_count": len(dual_valid),
                        "valid_duration_s": (valid[-1]["time_s"] - valid[0]["time_s"]
                                             if len(valid) > 1 else 0.0),
                        "dual_valid_duration_s": (
                            dual_valid[-1]["time_s"] - dual_valid[0]["time_s"]
                            if len(dual_valid) > 1 else 0.0)}}
        _apply_consensus(track, manifest)
        tracks.append(track)

    anchor_ns, anchor_method, uncertainty_s = _sample_utc_ns(manifest, 0.0)
    mapping_methods = set()
    mapping_uncertainties = []
    for track in tracks:
        for observation in track["observations"]:
            utc_ns, method, uncertainty = _sample_utc_ns(
                manifest, observation["time_s"] * rate)
            mapping_methods.add(method)
            if uncertainty is not None:
                mapping_uncertainties.append(float(uncertainty))
            moment = datetime.fromtimestamp(utc_ns / 1e9, tz=timezone.utc)
            observation["utc"] = utc_iso(moment)
    if mapping_methods:
        anchor_method = (next(iter(mapping_methods)) if len(mapping_methods) == 1
                         else "mixed_host_timing_mapping")
    if mapping_uncertainties:
        uncertainty_s = max(mapping_uncertainties)
    report = {"schema": TRACK_SCHEMA, "created_utc": utc_iso(datetime.now(timezone.utc)),
        "capture": str(Path(capture_path).resolve()),
        "source_followup": str(Path(followup_path).resolve()),
        "source_frame_track": (str(Path(frame_track_path).resolve())
                               if frame_track_path is not None else None),
        "capture_manifest": manifest,
        "timing": {"capture_sample_zero_utc_ns": anchor_ns,
                   "anchor_method": anchor_method,
                   "host_timing_uncertainty_s": uncertainty_s,
                   "hardware_timestamped": False,
                   "sample_clock_is_utc_clock": False},
        "signal": {"edge": region[:-5], "sample_rate_hz": rate,
                   "tuned_rf_center_hz": manifest["rf_center_hz"],
                   "nominal_rf_hz": manifest.get("metadata", {}).get(
                       "nominal_rf_hz", manifest["rf_center_hz"])},
        "configuration": {"output_rate_hz": output_rate_hz,
            "measurement_source": selected_source,
            "search_span_hz": search_span_hz, "search_step_hz": search_step_hz,
            "maximum_reacquisition_span_hz": maximum_reacquisition_span_hz,
            "minimum_exact_control_margin": minimum_margin,
            "tracking_exact_control_margin": tracking_margin,
            "maximum_relative_receiver_error_hz": maximum_relative_error_hz,
            "maximum_gap_s": maximum_gap_s,
            "maximum_drift_hz_s": maximum_drift_hz_s},
        "tracks": tracks,
        "summary": {"seed_count": len(tracks) if direct_tracks else len(seeds),
                    "track_count": len(tracks),
                    "longest_valid_duration_s": max(
                        (item["summary"]["valid_duration_s"] for item in tracks), default=0.0),
                    "longest_dual_valid_duration_s": max(
                        (item["summary"]["dual_valid_duration_s"] for item in tracks),
                        default=0.0),
                    "valid_observation_count": sum(
                        item["summary"]["valid_observation_count"] for item in tracks),
                    "dual_valid_observation_count": sum(
                        item["summary"]["dual_valid_observation_count"] for item in tracks)}}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    return report
