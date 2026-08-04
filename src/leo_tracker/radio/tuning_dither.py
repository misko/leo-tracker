"""Classify spectral structure as sky-fixed or receiver-baseband-fixed."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter1d

from .measurement import load_measurement_waterfall


SCHEMA = "leo-tracker.tuning-dither-comparison/v1"


def center_transitions_in_interval(times_s: np.ndarray, centers_hz: np.ndarray,
                                   start_s: float, stop_s: float) -> int:
    times = np.asarray(times_s, float); centers = np.asarray(centers_hz, float)
    if times.shape != centers.shape:
        raise ValueError("times and tuning centers must have equal shape")
    selected = centers[(times >= start_s) & (times <= stop_s)]
    return 0 if selected.size < 2 else int(np.count_nonzero(np.diff(selected)))


def retune_transient_confounded(start_s: float, transition_times_s: np.ndarray,
                                guard_s: float = 0.75) -> tuple[bool, float | None]:
    if guard_s <= 0:
        raise ValueError("retune transient guard must be positive")
    transitions = np.asarray(transition_times_s, float)
    nearest = None if not transitions.size else float(np.min(abs(transitions-start_s)))
    return bool(start_s < guard_s or (nearest is not None and nearest < guard_s)), nearest


def dither_phase_locked(distances_s: list[float], *, minimum_events: int = 5,
                        tolerance_s: float = 0.20,
                        minimum_fraction: float = 0.80) -> tuple[bool, float | None]:
    """Detect a candidate population synchronized to one dither phase."""
    values = np.asarray([value for value in distances_s if np.isfinite(value)], float)
    if values.size < minimum_events:
        return False, None
    center = float(np.median(values))
    fraction = float(np.mean(abs(values-center) <= tolerance_s))
    return fraction >= minimum_fraction, center


def reconstruct_interleaved_spectra(artifact: dict) -> tuple[np.ndarray, dict | None]:
    """Map interleaved baseband spectra onto the artifact's nominal RF axis."""
    spectra = np.asarray(artifact["psd_db_raw_per_hz"], float)
    centers = artifact.get("center_frequency_hz_by_snapshot")
    if centers is None:
        return spectra, None
    centers = np.asarray(centers, float)
    if centers.shape != (spectra.shape[1],):
        raise ValueError("snapshot center metadata does not match the waterfall")
    frequencies = np.asarray(artifact["frequency_offsets_hz"], float)
    nominal = float(artifact["center_frequency_hz"])
    reconstructed = np.empty_like(spectra)
    for index, center in enumerate(centers):
        delta = center-nominal
        for receiver in range(spectra.shape[0]):
            row = spectra[receiver, index]
            fill = float(np.median(row))
            reconstructed[receiver, index] = np.interp(
                frequencies-delta, frequencies, row, left=fill, right=fill)
    unique, counts = np.unique(centers, return_counts=True)
    report = {"schema": "leo-tracker.interleaved-dither/v1",
        "nominal_center_frequency_hz": nominal,
        "center_frequencies_hz": unique.tolist(),
        "exposure_fraction": (counts/counts.sum()).tolist(),
        "reconstructed_to_nominal_rf_axis": True}
    if unique.size == 2:
        dither_hz = float(unique[1]-unique[0]); mask0 = centers == unique[0]
        profiles = []
        sky_axis = frequencies-dither_hz
        valid = ((abs(frequencies) >= 300_000) &
                 (sky_axis >= frequencies[0]) & (sky_axis <= frequencies[-1]))
        edge = max(1, round(.1*frequencies.size)); valid[:edge] = False; valid[-edge:] = False
        for receiver in range(spectra.shape[0]):
            first = np.median(spectra[receiver, mask0], axis=0)
            second = np.median(spectra[receiver, ~mask0], axis=0)
            first -= gaussian_filter1d(first, 20)
            second -= gaussian_filter1d(second, 20)
            baseband = _correlation(first[valid], second[valid])
            sky = _correlation(first[valid], np.interp(sky_axis, frequencies, second)[valid])
            advantage = sky-baseband
            profiles.append({"receiver": receiver,
                "baseband_fixed_correlation": baseband, "sky_fixed_correlation": sky,
                "sky_correlation_advantage": advantage,
                "classification": "sky-fixed" if advantage >= .05 else
                    "baseband-fixed" if advantage <= -.05 else "ambiguous"})
        report["receivers"] = profiles
        report["classification"] = (profiles[0]["classification"]
            if len({item["classification"] for item in profiles}) == 1
            else "receivers-disagree")
    return reconstructed, report


def _correlation(first: np.ndarray, second: np.ndarray) -> float:
    value = float(np.corrcoef(first, second)[0, 1])
    return value if np.isfinite(value) else 0.0


def compare_tuning_dither(first_path: Path, second_path: Path, *,
                          smoothing_bins: float = 20,
                          edge_fraction: float = .1,
                          exclude_dc_hz: float = 300_000) -> dict:
    first = load_measurement_waterfall(first_path); second = load_measurement_waterfall(second_path)
    frequencies = np.asarray(first["frequency_offsets_hz"], float)
    other_frequencies = np.asarray(second["frequency_offsets_hz"], float)
    if frequencies.shape != other_frequencies.shape or not np.allclose(frequencies, other_frequencies):
        raise ValueError("dither artifacts require identical baseband frequency axes")
    first_spectra = np.asarray(first["psd_db_raw_per_hz"], float)
    second_spectra = np.asarray(second["psd_db_raw_per_hz"], float)
    if first_spectra.shape[0] != second_spectra.shape[0]:
        raise ValueError("dither artifacts require the same receiver count")
    first_center = float(first["center_frequency_hz"])
    second_center = float(second["center_frequency_hz"])
    dither_hz = second_center-first_center
    if abs(dither_hz) < abs(float(np.median(np.diff(frequencies)))):
        raise ValueError("capture centers differ by less than one frequency bin")
    profiles = []
    edge = max(1, int(round(edge_fraction*frequencies.size)))
    mask = np.ones(frequencies.size, bool); mask[:edge] = False; mask[-edge:] = False
    mask &= abs(frequencies) >= exclude_dc_hz
    sky_axis = frequencies-dither_hz
    mask &= (sky_axis >= frequencies[0]) & (sky_axis <= frequencies[-1])
    for receiver in range(first_spectra.shape[0]):
        profile_a = np.median(first_spectra[receiver], axis=0)
        profile_b = np.median(second_spectra[receiver], axis=0)
        texture_a = profile_a-gaussian_filter1d(profile_a, smoothing_bins)
        texture_b = profile_b-gaussian_filter1d(profile_b, smoothing_bins)
        baseband = _correlation(texture_a[mask], texture_b[mask])
        shifted_b = np.interp(sky_axis, frequencies, texture_b)
        sky = _correlation(texture_a[mask], shifted_b[mask])
        advantage = sky-baseband
        classification = ("sky-fixed" if advantage >= .05 else
                          "baseband-fixed" if advantage <= -.05 else "ambiguous")
        profiles.append({"receiver": receiver, "baseband_fixed_correlation": baseband,
            "sky_fixed_correlation": sky, "sky_correlation_advantage": advantage,
            "classification": classification})
    agreement = len({item["classification"] for item in profiles}) == 1
    classification = profiles[0]["classification"] if agreement else "receivers-disagree"
    return {"schema": SCHEMA, "first": str(first_path), "second": str(second_path),
        "first_center_frequency_hz": first_center,
        "second_center_frequency_hz": second_center, "tuning_dither_hz": dither_hz,
        "configuration": {"smoothing_bins": smoothing_bins,
            "edge_fraction": edge_fraction, "exclude_dc_hz": exclude_dc_hz},
        "receivers": profiles, "receivers_agree": agreement,
        "classification": classification}
