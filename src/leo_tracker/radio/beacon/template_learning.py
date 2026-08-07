"""Learn and validate a narrowband, full-duration Starlink beacon template.

The published edge pilots provide delay and carrier hypotheses.  Once those
nuisance parameters are removed, coherent structure that repeats throughout
the 4/3-ms frame can be estimated by robustly averaging many received frames.
The learned waveform is deliberately stored separately for each RF chain: an
LNBF and its analog path imprint a stable complex bandpass response on it.
"""
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


LEARNED_BEACON_SCHEMA = "leo-tracker.starlink-learned-bandpass-beacon/v1"


def _unit(values: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(values))
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("waveform has no finite energy")
    return np.asarray(values / norm, np.complex64)


def normalized_matches(frames: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Return phase-invariant normalized matched-filter amplitudes."""
    values = np.asarray(frames, np.complex64)
    reference = _unit(np.asarray(template, np.complex64))
    if values.ndim != 2 or values.shape[1] != reference.size:
        raise ValueError("frames and template dimensions do not match")
    energy = np.linalg.norm(values, axis=1)
    output = np.zeros(values.shape[0], float)
    usable = np.isfinite(energy) & (energy > 0)
    output[usable] = np.abs(values[usable] @ np.conj(reference)) / energy[usable]
    return output


def estimate_repeating_template(frames: np.ndarray, anchor: np.ndarray, *,
                                iterations: int = 5) -> tuple[np.ndarray, dict]:
    """Estimate the repeating frame after phase/amplitude alignment.

    The anchor need only provide enough known structure to establish the first
    per-frame complex phase.  Subsequent iterations use the learned waveform.
    Complex coordinate-wise medians prevent a few active user-data bursts from
    becoming part of the persistent beacon estimate.
    """
    values = np.asarray(frames, np.complex64)
    if values.ndim != 2 or values.shape[0] < 4:
        raise ValueError("at least four two-dimensional frames are required")
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    template = _unit(np.asarray(anchor, np.complex64))
    if template.size != values.shape[1]:
        raise ValueError("anchor length does not match frame length")
    retained = np.ones(values.shape[0], bool)
    history = []
    for _ in range(iterations):
        gains = values @ np.conj(template)
        frame_energy = np.linalg.norm(values, axis=1)
        coherence = np.abs(gains) / np.maximum(frame_energy, np.finfo(float).tiny)
        finite = np.isfinite(coherence) & (frame_energy > 0)
        if np.count_nonzero(finite) < 4:
            raise ValueError("too few finite frames for beacon learning")
        # Keep the coherent majority, but never train on fewer than four frames.
        threshold = float(np.quantile(coherence[finite], .25))
        retained = finite & (coherence >= threshold)
        if np.count_nonzero(retained) < 4:
            selected = np.flatnonzero(finite)[np.argsort(coherence[finite])[-4:]]
            retained = np.zeros(values.shape[0], bool); retained[selected] = True
        phase = gains[retained] / np.maximum(np.abs(gains[retained]),
                                             np.finfo(float).tiny)
        rms = frame_energy[retained] / np.sqrt(values.shape[1])
        aligned = values[retained] * np.conj(phase[:, None])
        aligned /= np.maximum(rms[:, None], np.finfo(float).tiny)
        estimate = (np.median(aligned.real, axis=0) +
                    1j * np.median(aligned.imag, axis=0))
        updated = _unit(estimate)
        change = float(1 - abs(np.vdot(template, updated)))
        template = updated
        history.append({"retained_frame_count": int(np.count_nonzero(retained)),
                        "coherence_q25": threshold,
                        "template_change": change})
    return template, {"input_frame_count": int(values.shape[0]),
                      "retained_frame_count": int(np.count_nonzero(retained)),
                      "iterations": history}


def validate_template(frames: np.ndarray, learned: np.ndarray,
                      pilot: np.ndarray) -> dict:
    """Evaluate a learned template on held-out frames and two true controls."""
    values = np.asarray(frames, np.complex64)
    learned_score = normalized_matches(values, learned)
    pilot_score = normalized_matches(values, pilot)
    # A large circular shift retains spectrum and energy while destroying frame
    # alignment.  Negating or phase-rotating a template would not be a control,
    # because the detector is intentionally invariant to complex phase.
    control = np.roll(learned, max(1, learned.size // 7))
    control_score = normalized_matches(values, control)
    eps = np.finfo(float).tiny
    ratio_db = 20 * np.log10(np.maximum(np.median(learned_score), eps) /
                              max(float(np.median(pilot_score)), eps))
    return {
        "held_out_frame_count": int(values.shape[0]),
        "learned_match_median": float(np.median(learned_score)),
        "learned_match_q10": float(np.quantile(learned_score, .1)),
        "pilot_match_median": float(np.median(pilot_score)),
        "rolled_control_match_median": float(np.median(control_score)),
        "learned_minus_control_median": float(np.median(learned_score-control_score)),
        "learned_to_pilot_amplitude_ratio_db": float(ratio_db),
        "learned_beats_control_fraction": float(np.mean(learned_score > control_score)),
        "learned_beats_pilot_fraction": float(np.mean(learned_score > pilot_score)),
    }


def _training_frames(capture: BeaconCapture, followup: dict, *, receiver: int,
                     window_s: float, maximum_frames: int) -> tuple[np.ndarray, np.ndarray]:
    rate = float(capture.manifest["sample_rate_hz"])
    period = rate * STARLINK_FRAME_DURATION_S
    count = round(period)
    groups: list[np.ndarray] = []
    group_ids: list[int] = []
    occupied: set[int] = set()
    for check_index, check in enumerate(followup.get("checks", [])):
        receivers = check.get("receivers", [])
        flags = check.get("receiver_candidates", [])
        if (len(receivers) != 2 or len(flags) != 2 or not all(flags)):
            continue
        source = receivers[receiver]
        acquisition = source["acquisition"]
        if (abs(float(acquisition["subband_rate_hz"]) - rate) > 1e-6 or
                abs(float(acquisition.get("selected_center_offset_hz", 0))) > 1e-6):
            continue
        start = int(check["start_sample"])
        available = min(round(window_s * rate),
                        int(capture.manifest["captured_samples_per_receiver"]) - start)
        if available < count:
            continue
        samples = capture.read_window(start, available)[:, receiver]
        epoch = int(acquisition["selected_epoch_sample"])
        cfo = float(source["pilot"]["frequency_offset_hz"])
        frame_index = 0
        while True:
            local_start = epoch + round(frame_index * period)
            if local_start + count > samples.size:
                break
            absolute_start = start + local_start
            frame_index += 1
            if absolute_start in occupied:
                continue
            occupied.add(absolute_start)
            absolute = absolute_start + np.arange(count)
            frame = samples[local_start:local_start+count] * np.exp(
                -2j * np.pi * cfo * absolute / rate)
            groups.append(np.asarray(frame, np.complex64)); group_ids.append(check_index)
            if len(groups) >= maximum_frames:
                break
        if len(groups) >= maximum_frames:
            break
    if len(groups) < 8:
        raise ValueError("fewer than eight independently aligned candidate frames")
    return np.stack(groups), np.asarray(group_ids, np.int32)


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def learn_bandpass_beacon(capture_path: Path, followup_path: Path, output: Path,
                          samples_output: Path, *, window_s: float = .1,
                          maximum_frames: int = 600,
                          iterations: int = 5) -> dict:
    """Learn per-RX beacon templates and prove them on held-out frame groups."""
    capture = BeaconCapture.open(capture_path, verify=True)
    followup = json.loads(Path(followup_path).read_text())
    if Path(followup["capture"]).resolve() != Path(capture_path).resolve():
        raise ValueError("follow-up and capture paths do not match")
    rate = float(capture.manifest["sample_rate_hz"])
    region = capture.manifest.get("metadata", {}).get("region")
    if region not in ("lower-edge", "upper-edge"):
        raise ValueError("beacon learning requires an edge-band capture")
    edge = region.removesuffix("-edge")
    pilot = edge_pilot_frame(rate, edge)
    arrays: dict[str, np.ndarray] = {"pilot_template": _unit(pilot)}
    validations = []
    learning = []
    for receiver in range(2):
        frames, groups = _training_frames(capture, followup, receiver=receiver,
                                          window_s=window_s,
                                          maximum_frames=maximum_frames)
        unique = np.unique(groups)
        if unique.size >= 2:
            held_groups = set(map(int, unique[1::2]))
            held = np.asarray([int(value) in held_groups for value in groups])
        else:
            held = np.arange(frames.shape[0]) % 5 == 0
        if np.count_nonzero(held) < 4 or np.count_nonzero(~held) < 4:
            held = np.arange(frames.shape[0]) % 2 == 0
        template, details = estimate_repeating_template(
            frames[~held], pilot, iterations=iterations)
        arrays[f"template_rx{receiver}"] = template
        arrays[f"training_group_rx{receiver}"] = groups[~held]
        learning.append({"receiver": receiver, **details,
                         "training_frame_count": int(np.count_nonzero(~held)),
                         "held_out_frame_count": int(np.count_nonzero(held))})
        validations.append({"receiver": receiver,
                            **validate_template(frames[held], template, pilot)})
    samples_output = Path(samples_output)
    samples_output.parent.mkdir(parents=True, exist_ok=True)
    temporary = samples_output.with_suffix(samples_output.suffix + ".next")
    with temporary.open("wb") as stream:
        np.savez_compressed(stream, **arrays)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, samples_output)
    digest = hashlib.sha256(samples_output.read_bytes()).hexdigest()
    minimum_held = min(item["held_out_frame_count"] for item in validations)
    minimum_margin = min(item["learned_minus_control_median"]
                         for item in validations)
    minimum_control_fraction = min(item["learned_beats_control_fraction"]
                                   for item in validations)
    minimum_gain = min(item["learned_to_pilot_amplitude_ratio_db"]
                       for item in validations)
    qualified = bool(minimum_held >= 20 and minimum_margin >= .02 and
                     minimum_control_fraction >= .8 and minimum_gain > 0)
    report = {
        "schema": LEARNED_BEACON_SCHEMA,
        "created_utc": utc_iso(datetime.now(timezone.utc)),
        "capture": str(Path(capture_path).resolve()),
        "followup": str(Path(followup_path).resolve()),
        "samples": str(samples_output.resolve()),
        "samples_sha256": digest,
        "sample_rate_hz": rate,
        "frame_duration_s": STARLINK_FRAME_DURATION_S,
        "frame_sample_count": int(pilot.size),
        "region": region,
        "configuration": {"window_s": window_s,
                          "maximum_frames": maximum_frames,
                          "iterations": iterations,
                          "split_basis": "followup_check_group_or_interleaved_fallback"},
        "learning": learning,
        "validation": validations,
        "summary": {
            "qualified": qualified,
            "minimum_held_out_frame_count": minimum_held,
            "minimum_learned_minus_control_median": minimum_margin,
            "minimum_learned_beats_control_fraction": minimum_control_fraction,
            "minimum_learned_to_pilot_amplitude_ratio_db": minimum_gain,
            "qualification_gates": {"minimum_held_out_frames": 20,
                "minimum_learned_minus_control_median": .02,
                "minimum_learned_beats_control_fraction": .8,
                "minimum_learned_to_pilot_amplitude_ratio_db_exclusive": 0},
        },
    }
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(output, report)
    return report


def load_learned_beacon(path: Path) -> tuple[dict, dict[str, np.ndarray]]:
    """Load a learned beacon only after schema, path, and checksum validation."""
    report = json.loads(Path(path).read_text())
    if report.get("schema") != LEARNED_BEACON_SCHEMA:
        raise ValueError("unsupported learned beacon schema")
    samples = Path(report["samples"])
    # Offloaded analysis bundles are mount-point independent: keep the sample
    # dependency beside the report and resolve its relative path there.
    if not samples.is_absolute():
        samples = Path(path).resolve().parent / samples
    if hashlib.sha256(samples.read_bytes()).hexdigest() != report["samples_sha256"]:
        raise ValueError("learned beacon sample checksum mismatch")
    with np.load(samples, allow_pickle=False) as source:
        arrays = {name: source[name] for name in source.files}
    count = int(report["frame_sample_count"])
    for receiver in range(2):
        if arrays.get(f"template_rx{receiver}", np.empty(0)).shape != (count,):
            raise ValueError("learned beacon template dimensions do not match artifact")
    return report, arrays
