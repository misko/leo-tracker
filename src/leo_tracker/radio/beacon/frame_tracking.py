"""Conditioned 750 Hz Starlink full-frame carrier tracking."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import numpy as np

from leo_tracker.orbit.artifacts import utc_iso

from .artifact import BeaconCapture
from .pilots import edge_pilot_frame
from .structure import STARLINK_FRAME_DURATION_S
from .template_learning import load_learned_beacon


FRAME_TRACK_SCHEMA = "leo-tracker.starlink-conditioned-frame-track/v3"
LEGACY_FRAME_TRACK_SCHEMAS = {
    "leo-tracker.starlink-conditioned-frame-track/v1",
    "leo-tracker.starlink-conditioned-frame-track/v2",
}


def conditioned_frame_observations(samples: np.ndarray, sample_rate_hz: float, *,
                                   epoch_sample: int, coarse_cfo_hz: float,
                                   absolute_start_sample: int = 0,
                                   edge: str = "lower",
                                   minimum_margin: float = .005,
                                   template: np.ndarray | None = None,
                                   control: np.ndarray | None = None,
                                   coarse_cfo_rate_hz_s: float = 0.0,
                                   coarse_reference_sample: int | None = None) -> dict:
    """Measure phase-continuous CFO on every complete 4/3-ms frame."""
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if sample_rate_hz <= 0 or epoch_sample < 0 or minimum_margin < 0:
        raise ValueError("invalid conditioned frame tracking configuration")
    if template is None:
        template = edge_pilot_frame(sample_rate_hz, edge)
        control = edge_pilot_frame(sample_rate_hz, edge, symbol_roll=17)
    else:
        template = np.asarray(template, np.complex64)
        control = (np.roll(template, max(1, template.size // 7)) if control is None
                   else np.asarray(control, np.complex64))
        expected = round(sample_rate_hz * STARLINK_FRAME_DURATION_S)
        if template.shape != (expected,) or control.shape != template.shape:
            raise ValueError("learned template dimensions do not match sample rate")
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    starts = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + template.size > values.size:
            break
        starts.append(start)
        frame += 1
    if len(starts) < 3:
        raise ValueError("conditioned window must contain at least three complete frames")
    starts_array = np.asarray(starts, np.int64)
    local = np.arange(template.size, dtype=np.int64)
    indexes = starts_array[:, None] + local[None, :]
    frames = values[indexes]
    absolute = absolute_start_sample + indexes
    reference_sample = (absolute_start_sample if coarse_reference_sample is None
                        else int(coarse_reference_sample))
    relative_time = (absolute - reference_sample) / sample_rate_hz
    wiped = frames * np.exp(-2j * np.pi * (
        coarse_cfo_hz * relative_time +
        .5 * coarse_cfo_rate_hz_s * relative_time**2))
    # Estimate residual CFO inside each frame rather than differentiating phase
    # between adjacent frames. Starlink may transmit only a small fraction of
    # the 750 possible frames; phase from an inactive frame is noise and must
    # not contaminate either neighboring active frame. Eight coherent sub-frame
    # correlations retain the full-frame template gain while making every
    # accepted frame an independent frequency observation.
    block_count = 8
    edges = np.linspace(0, template.size, block_count + 1, dtype=np.int64)
    exact_blocks = np.column_stack([
        wiped[:, start:stop] @ np.conj(template[start:stop])
        for start, stop in zip(edges[:-1], edges[1:], strict=True)])
    control_blocks = np.column_stack([
        wiped[:, start:stop] @ np.conj(control[start:stop])
        for start, stop in zip(edges[:-1], edges[1:], strict=True)])
    exact = np.sum(exact_blocks, axis=1)
    rolled = np.sum(control_blocks, axis=1)
    energy = np.sum(np.abs(frames) ** 2, axis=1)
    exact_norm = np.sqrt(np.maximum(
        energy * float(np.vdot(template, template).real), 1e-30))
    control_norm = np.sqrt(np.maximum(
        energy * float(np.vdot(control, control).real), 1e-30))
    exact_score = np.abs(exact) / exact_norm
    control_score = np.abs(rolled) / control_norm
    times_s = ((absolute_start_sample + starts_array + template.size / 2) /
               sample_rate_hz)
    block_centers_s = np.asarray([
        (start + stop - 1) / (2 * sample_rate_hz)
        for start, stop in zip(edges[:-1], edges[1:], strict=True)])
    centered_block_time = block_centers_s - np.mean(block_centers_s)
    phase = np.unwrap(np.angle(exact_blocks), axis=1)
    centered_phase = phase - np.mean(phase, axis=1, keepdims=True)
    phase_slopes = (centered_phase @ centered_block_time /
                    np.sum(centered_block_time**2))
    residual_hz = phase_slopes / (2 * np.pi)
    prediction = coarse_cfo_hz + coarse_cfo_rate_hz_s * (
        times_s - reference_sample / sample_rate_hz)
    frequency_hz = prediction + residual_hz
    margin = exact_score - control_score
    valid = np.isfinite(frequency_hz) & (margin >= minimum_margin)
    # Phase uncertainty of a normalized coherent correlation, propagated over
    # one frame interval. This is formal measurement noise; dual-RX scatter is
    # added by the calibrated 10 Hz aggregation stage.
    snr_proxy = np.maximum(exact_score**2 / np.maximum(1 - exact_score**2, 1e-6),
                           1e-6)
    sigma_phase = np.sqrt(1 / (2 * snr_proxy))
    phase_fit = (np.mean(phase, axis=1, keepdims=True) +
                 phase_slopes[:, None] * centered_block_time[None, :])
    fit_residual = phase - phase_fit
    fit_sigma_hz = np.sqrt(np.sum(fit_residual**2, axis=1) /
        max(block_count - 2, 1) / np.sum(centered_block_time**2)) / (2 * np.pi)
    score_sigma_hz = sigma_phase / (2 * np.pi * STARLINK_FRAME_DURATION_S)
    formal_sigma_hz = np.clip(np.maximum(fit_sigma_hz, score_sigma_hz),
                              1.0, 500.0)
    return {"time_s": times_s, "frequency_offset_hz": frequency_hz,
        "formal_sigma_hz": formal_sigma_hz, "exact_score": exact_score,
        "control_score": control_score, "score_margin": margin, "valid": valid,
        "frame_start_sample": absolute_start_sample + starts_array}


def _candidate_clusters(selected: list[tuple[float, int, dict]], *,
                        maximum_gap_s: float = .21
                        ) -> list[list[tuple[float, int, dict]]]:
    clusters: list[list[tuple[float, int, dict]]] = []
    for item in selected:
        if (not clusters or item[2]["start_s"] - clusters[-1][-1][2]["start_s"] >
                maximum_gap_s):
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _seed_frequency(check: dict, receiver: int) -> float:
    full = check.get("full_frame_evidence", {})
    receivers = full.get("receivers", [])
    if full.get("candidate") and len(receivers) == 2:
        return float(receivers[receiver]["frequency_offset_hz"])
    return float(check["receivers"][receiver]["pilot"]["frequency_offset_hz"])


def _cluster_cfo_rates(cluster: list[tuple[float, int, dict]]) -> np.ndarray:
    times = np.asarray([item[2]["start_s"] for item in cluster], float)
    rates = []
    for receiver in range(2):
        frequencies = np.asarray([_seed_frequency(item[2], receiver)
                                  for item in cluster], float)
        rates.append(float(np.polyfit(times, frequencies, 1)[0])
                     if len(times) >= 2 and np.ptp(times) > 0 else 0.0)
    return np.clip(np.asarray(rates), -15_000.0, 15_000.0)


def _timing_margin(values: np.ndarray, rate: float, *, epoch_sample: int,
                   absolute_start_sample: int, cfo_hz: float, cfo_rate_hz_s: float,
                   reference_sample: int, template: np.ndarray,
                   control: np.ndarray, maximum_frames: int = 8) -> float:
    period = rate * STARLINK_FRAME_DURATION_S
    starts = []
    frame = 0
    while len(starts) < maximum_frames:
        start = epoch_sample + round(frame * period)
        if start + template.size > values.size:
            break
        starts.append(start); frame += 1
    if len(starts) < 3:
        return float("-inf")
    starts_array = np.asarray(starts, np.int64)
    indexes = starts_array[:, None] + np.arange(template.size)[None, :]
    frames = values[indexes]
    absolute = absolute_start_sample + indexes
    relative_time = (absolute - reference_sample) / rate
    wiped = frames * np.exp(-2j * np.pi * (
        cfo_hz * relative_time + .5 * cfo_rate_hz_s * relative_time**2))
    energy = np.sum(np.abs(frames)**2, axis=1)
    exact = np.abs(wiped @ np.conj(template)) / np.sqrt(np.maximum(
        energy * float(np.vdot(template, template).real), 1e-30))
    rolled = np.abs(wiped @ np.conj(control)) / np.sqrt(np.maximum(
        energy * float(np.vdot(control, control).real), 1e-30))
    return float(np.median(exact - rolled))


def _extend_cluster_forward(capture: BeaconCapture,
                            cluster: list[tuple[float, int, dict]], *,
                            stop_sample: int, window_s: float,
                            maximum_missed_windows: int, edge: str,
                            minimum_margin: float,
                            minimum_window_margin: float,
                            minimum_sparse_frame_margin: float,
                            maximum_relative_error_hz: float,
                            templates: list[np.ndarray],
                            controls: list[np.ndarray]) -> tuple[list[dict], dict]:
    """Propagate one acquired lock; return measurements only where RF is read."""
    rate = float(capture.manifest["sample_rate_hz"])
    period = rate * STARLINK_FRAME_DURATION_S
    seed = cluster[-1][2]
    reference_sample = int(seed["start_sample"])
    cfo = np.asarray([_seed_frequency(seed, receiver) for receiver in range(2)], float)
    cfo_rate = _cluster_cfo_rates(cluster)
    global_epochs = np.asarray([reference_sample + int(seed["receivers"][receiver]
        ["acquisition"]["selected_epoch_sample"]) for receiver in range(2)], np.int64)
    relative_seed = float(cfo[1] - cfo[0])
    cursor = reference_sample + round(window_s * rate)
    block_samples = round(window_s * rate)
    results = []
    misses = 0
    accepted = 0

    def measure(shift: int, paired: np.ndarray,
                predicted_epochs: list[int]) -> list[dict]:
        return [conditioned_frame_observations(
            paired[:, receiver], rate,
            epoch_sample=predicted_epochs[receiver] + shift,
            coarse_cfo_hz=float(cfo[receiver]), absolute_start_sample=cursor,
            edge=edge, minimum_margin=minimum_margin,
            template=templates[receiver], control=controls[receiver],
            coarse_cfo_rate_hz_s=float(cfo_rate[receiver]),
            coarse_reference_sample=reference_sample) for receiver in range(2)]

    def evidence(measured: list[dict], *, allow_single_sparse: bool
                 ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, str | None]:
        count = min(len(item["time_s"]) for item in measured)
        frequencies = np.column_stack([item["frequency_offset_hz"][:count]
                                       for item in measured])
        valid = np.column_stack([item["valid"][:count] for item in measured])
        relative_error = np.abs((frequencies[:, 1] - frequencies[:, 0]) -
                                relative_seed)
        valid &= (relative_error <= maximum_relative_error_hz)[:, None]
        dual = np.all(valid, axis=1)
        dual_margins = np.min(np.column_stack(
            [item["score_margin"][:count] for item in measured]), axis=1)
        dense = bool(np.count_nonzero(dual) >= 3 and
            np.median(dual_margins[dual]) >= minimum_window_margin)
        strong = dual & (dual_margins >= minimum_sparse_frame_margin)
        sparse_required = 1 if allow_single_sparse else 2
        sparse = np.count_nonzero(strong) >= sparse_required
        if sparse and not dense:
            # Weak chance-valid frames in the same window do not inherit the
            # credibility of an independently strong sparse frame.
            valid &= strong[:, None]
            dual = np.all(valid, axis=1)
        margin = (float(np.median(dual_margins[dual]))
                  if np.any(dual) else float("-inf"))
        basis = "dense_window" if dense else "sparse_predicted_epoch" if sparse else None
        return frequencies, valid, dual, margin, basis

    while cursor + block_samples <= stop_sample and misses < maximum_missed_windows:
        paired = capture.read_window(cursor, block_samples)
        predicted_epochs = []
        for receiver in range(2):
            frame = int(np.ceil((cursor - global_epochs[receiver]) / period))
            predicted_epochs.append(int(global_epochs[receiver] + round(frame * period) -
                                         cursor))
        # The propagated lattice is the strongest timing prior. Try it first
        # across every frame; this is what permits a single high-confidence
        # frame in a low-PRF window to update the loop. Only run the bounded
        # timing recovery bank after that prediction fails.
        shift = 0
        measured = measure(shift, paired, predicted_epochs)
        frequencies, valid, dual, window_margin, acceptance_basis = evidence(
            measured, allow_single_sparse=True)
        if acceptance_basis is None:
            shift_scores = []
            for candidate_shift in range(-12, 13):
                margins = [_timing_margin(paired[:, receiver], rate,
                    epoch_sample=predicted_epochs[receiver] + candidate_shift,
                    absolute_start_sample=cursor, cfo_hz=float(cfo[receiver]),
                    cfo_rate_hz_s=float(cfo_rate[receiver]),
                    reference_sample=reference_sample, template=templates[receiver],
                    control=controls[receiver]) for receiver in range(2)]
                shift_scores.append((min(margins), candidate_shift))
            _, shift = max(shift_scores)
            measured = measure(shift, paired, predicted_epochs)
            frequencies, valid, dual, window_margin, acceptance_basis = evidence(
                measured, allow_single_sparse=False)
        count = min(len(item["time_s"]) for item in measured)
        if acceptance_basis is not None:
            accepted += 1; misses = 0
            # A common timing correction updates the frame lattice but never
            # creates an observation at the unmeasured prediction itself.
            global_epochs += shift
            midpoint = float(np.median(np.mean([item["time_s"][:count]
                                                for item in measured], axis=0)[dual]))
            latest = np.asarray([np.median(frequencies[dual, receiver])
                                 for receiver in range(2)])
            elapsed = midpoint - reference_sample / rate
            if elapsed > 0:
                cfo_rate = np.clip((latest - cfo) / elapsed, -15_000, 15_000)
                cfo = latest
                reference_sample = round(midpoint * rate)
            results.append({"measured": measured, "valid": valid,
                            "count": count, "cursor": cursor,
                            "window_margin": window_margin,
                            "acceptance_basis": acceptance_basis})
        else:
            misses += 1
        cursor += block_samples
    return results, {"attempted_window_count": max(0, round(
        (cursor - (int(seed["start_sample"]) + block_samples)) / block_samples)),
        "accepted_window_count": accepted, "terminal_missed_window_count": misses,
        "stop_sample": cursor}


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def track_conditioned_frames(capture_path: Path, followup_path: Path, output: Path,
                             samples_output: Path, *, window_s: float = .1,
                             minimum_margin: float = .005,
                             maximum_relative_error_hz: float = 500.0,
                             beacon_template_path: Path | None = None,
                             maximum_extension_s: float = 30.0,
                             maximum_missed_windows: int = 3,
                             minimum_extension_window_margin: float = .02,
                             minimum_sparse_frame_margin: float = .05) -> dict:
    """Track all frames following independent dense dual-RX acquisitions."""
    if (min(window_s, maximum_relative_error_hz, maximum_extension_s,
            minimum_extension_window_margin, minimum_sparse_frame_margin) <= 0 or
            maximum_missed_windows < 1):
        raise ValueError("conditioned tracking bounds must be positive")
    capture = BeaconCapture.open(capture_path)
    manifest = capture.manifest
    followup = json.loads(Path(followup_path).read_text())
    if Path(followup["capture"]).resolve() != Path(capture_path).resolve():
        raise ValueError("follow-up and capture paths do not match")
    region = manifest.get("metadata", {}).get("region")
    if region not in ("lower-edge", "upper-edge"):
        raise ValueError("conditioned tracking requires an edge-band capture")
    rate = float(manifest["sample_rate_hz"])
    learned_report = None
    learned_arrays = None
    if beacon_template_path is not None:
        learned_report, learned_arrays = load_learned_beacon(beacon_template_path)
        if not learned_report.get("summary", {}).get("qualified", False):
            raise ValueError("learned beacon did not pass held-out qualification")
        if (abs(float(learned_report["sample_rate_hz"]) - rate) > 1e-6 or
                learned_report["region"] != region):
            raise ValueError("learned beacon rate or region does not match capture")
    total = int(manifest["captured_samples_per_receiver"])
    points = {}
    for index, check in enumerate(followup.get("checks", [])):
        if (len(check.get("receivers", [])) != 2 or
                not check.get("candidate", False) or
                check.get("epoch_difference_samples", np.inf) > 20):
            continue
        full = check.get("full_frame_evidence", {})
        if full.get("candidate") and len(full.get("receivers", [])) == 2:
            score = sum(float(receiver["score_margin"])
                        for receiver in full["receivers"])
        else:
            score = sum(float(receiver["acquisition"].get("match_score_margin", 0))
                        for receiver in check["receivers"])
        key = int(check["start_sample"])
        if key not in points or score > points[key][0]:
            points[key] = (score, index, check)
    selected = sorted(points.values(), key=lambda item: item[2]["start_sample"])
    edge = region.removesuffix("-edge")
    templates = [(learned_arrays[f"template_rx{receiver}"]
                  if learned_arrays is not None else edge_pilot_frame(rate, edge))
                 for receiver in range(2)]
    controls = [(np.roll(template, max(1, template.size // 7))
                 if learned_arrays is not None else
                 edge_pilot_frame(rate, edge, symbol_roll=17))
                for template in templates]
    arrays = {name: [] for name in ("time_s", "frequency_offset_hz",
        "formal_sigma_hz", "exact_score", "control_score", "score_margin",
        "valid", "frame_start_sample", "source_check_index")}

    def append_measurement(measured: list[dict], check_index: int,
                           valid: np.ndarray) -> None:
        count = min(len(item["time_s"]) for item in measured)
        if count < 3:
            return
        arrays["time_s"].append(np.mean(
            [item["time_s"][:count] for item in measured], axis=0))
        arrays["frequency_offset_hz"].append(np.column_stack(
            [item["frequency_offset_hz"][:count] for item in measured]))
        arrays["formal_sigma_hz"].append(np.column_stack(
            [item["formal_sigma_hz"][:count] for item in measured]))
        arrays["exact_score"].append(np.column_stack(
            [item["exact_score"][:count] for item in measured]))
        arrays["control_score"].append(np.column_stack(
            [item["control_score"][:count] for item in measured]))
        arrays["score_margin"].append(np.column_stack(
            [item["score_margin"][:count] for item in measured]))
        arrays["valid"].append(valid[:count])
        arrays["frame_start_sample"].append(np.column_stack(
            [item["frame_start_sample"][:count] for item in measured]))
        arrays["source_check_index"].append(np.full(count, check_index, np.int32))

    for position, (_, check_index, check) in enumerate(selected):
        start = int(check["start_sample"])
        nominal_stop = min(total, start + round(window_s * rate))
        if position + 1 < len(selected):
            nominal_stop = min(nominal_stop,
                               int(selected[position + 1][2]["start_sample"]))
        if nominal_stop - start < round(3 * STARLINK_FRAME_DURATION_S * rate):
            continue
        paired = capture.read_window(start, nominal_stop - start)
        measured = []
        for receiver in range(2):
            source = check["receivers"][receiver]
            measured.append(conditioned_frame_observations(
                paired[:, receiver], rate,
                epoch_sample=int(source["acquisition"]["selected_epoch_sample"]),
                coarse_cfo_hz=_seed_frequency(check, receiver),
                absolute_start_sample=start, edge=edge,
                minimum_margin=minimum_margin,
                template=templates[receiver], control=controls[receiver]))
        count = min(len(item["time_s"]) for item in measured)
        if count < 3:
            continue
        time_values = np.mean([item["time_s"][:count] for item in measured], axis=0)
        frequencies = np.column_stack([item["frequency_offset_hz"][:count]
                                       for item in measured])
        valid = np.column_stack([item["valid"][:count] for item in measured])
        seed_relative = (_seed_frequency(check, 1) - _seed_frequency(check, 0))
        relative_error = np.abs((frequencies[:, 1] - frequencies[:, 0]) - seed_relative)
        valid &= (relative_error <= maximum_relative_error_hz)[:, None]
        append_measurement(measured, check_index, valid)

    extension_reports = []
    clusters = _candidate_clusters(selected)
    for cluster_index, cluster in enumerate(clusters):
        last_check = cluster[-1][2]
        limit = min(total, int(last_check["start_sample"] +
                               maximum_extension_s * rate))
        if cluster_index + 1 < len(clusters):
            limit = min(limit, int(clusters[cluster_index + 1][0][2]["start_sample"]))
        if limit - int(last_check["start_sample"]) < round(2 * window_s * rate):
            continue
        extended, extension = _extend_cluster_forward(
            capture, cluster, stop_sample=limit, window_s=window_s,
            maximum_missed_windows=maximum_missed_windows, edge=edge,
            minimum_margin=minimum_margin,
            minimum_window_margin=minimum_extension_window_margin,
            minimum_sparse_frame_margin=minimum_sparse_frame_margin,
            maximum_relative_error_hz=maximum_relative_error_hz,
            templates=templates, controls=controls)
        extension["cluster_index"] = cluster_index
        extension["seed_check_index"] = cluster[-1][1]
        extension_reports.append(extension)
        for result in extended:
            append_measurement(result["measured"], -1, result["valid"])
    packed = {}
    for name, parts in arrays.items():
        if parts:
            packed[name] = np.concatenate(parts, axis=0)
        elif name in ("frequency_offset_hz", "formal_sigma_hz", "exact_score",
                      "control_score", "score_margin", "valid", "frame_start_sample"):
            packed[name] = np.empty((0, 2), bool if name == "valid" else float)
        else:
            packed[name] = np.empty(0, float)
    order = np.argsort(packed["time_s"])
    for name in packed:
        packed[name] = packed[name][order]
    samples_output = Path(samples_output)
    samples_output.parent.mkdir(parents=True, exist_ok=True)
    temporary_samples = samples_output.with_suffix(samples_output.suffix + ".next")
    with temporary_samples.open("wb") as stream:
        np.savez_compressed(stream, **packed)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary_samples, samples_output)
    digest = hashlib.sha256(samples_output.read_bytes()).hexdigest()
    valid_rows = np.all(packed["valid"], axis=1) if packed["valid"].size else np.empty(0, bool)
    report = {"schema": FRAME_TRACK_SCHEMA,
        "created_utc": utc_iso(datetime.now(timezone.utc)),
        "capture": str(Path(capture_path).resolve()),
        "followup": str(Path(followup_path).resolve()),
        "source_learned_beacon": (str(Path(beacon_template_path).resolve())
                                  if beacon_template_path is not None else None),
        "samples": {"path": str(samples_output.resolve()), "sha256": digest,
                    "arrays": {name: list(value.shape) for name, value in packed.items()}},
        "configuration": {"frame_rate_hz": 1 / STARLINK_FRAME_DURATION_S,
            "window_s": window_s, "minimum_margin": minimum_margin,
            "maximum_relative_error_hz": maximum_relative_error_hz,
            "maximum_extension_s": maximum_extension_s,
            "maximum_missed_windows": maximum_missed_windows,
            "minimum_extension_window_margin": minimum_extension_window_margin,
            "minimum_sparse_frame_margin": minimum_sparse_frame_margin,
            "template_source": ("learned_bandpass_beacon" if learned_arrays is not None
                                else "published_edge_pilots")},
        "summary": {"seed_count": len(selected),
            "seed_cluster_count": len(clusters),
            "extension_attempted_window_count": sum(item["attempted_window_count"]
                                                       for item in extension_reports),
            "extension_accepted_window_count": sum(item["accepted_window_count"]
                                                      for item in extension_reports),
            "frame_observation_count": int(len(packed["time_s"])),
            "dual_valid_frame_count": int(np.count_nonzero(valid_rows)),
            "dual_valid_fraction": (float(np.mean(valid_rows)) if valid_rows.size else 0.0),
            "measured_span_s": (float(np.ptp(packed["time_s"]))
                                if len(packed["time_s"]) > 1 else 0.0)}}
    report["extensions"] = extension_reports
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(output, report)
    return report
