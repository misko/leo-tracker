"""Persistent absolute-RF spectrum models from tuning-dither captures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence
import warnings

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks

from .measurement import load_measurement_waterfall
from .blind_comb import _integrate


BASELINE_SCHEMA = "leo-tracker.rf-baseline/v1"
NOVELTY_SCHEMA = "leo-tracker.rf-novelty/v1"


def _rf_axis(artifact: dict) -> np.ndarray:
    lo = float(artifact.get("lnb_lo_hz", 0))
    return lo+float(artifact["center_frequency_hz"])+np.asarray(
        artifact["frequency_offsets_hz"], float)


def _registration_shift(reference_axis: np.ndarray, reference_profile: np.ndarray,
                        axis: np.ndarray, profile: np.ndarray, bin_hz: float,
                        maximum_shift_hz: float) -> tuple[float, float]:
    reference_texture = reference_profile-gaussian_filter1d(reference_profile, 20)
    texture = profile-gaussian_filter1d(profile, 20)
    best = (float("-inf"), 0.0)
    for shift_bins in range(-int(maximum_shift_hz/bin_hz),
                            int(maximum_shift_hz/bin_hz)+1):
        shift_hz = shift_bins*bin_hz
        corrected = axis+shift_hz
        supported = ((reference_axis >= corrected[0]) & (reference_axis <= corrected[-1]) &
                     (abs(reference_axis-np.median(reference_axis)) > 300_000))
        if supported.sum() < 64: continue
        moved = np.interp(reference_axis[supported], corrected, texture)
        correlation = float(np.corrcoef(reference_texture[supported], moved)[0, 1])
        if np.isfinite(correlation) and correlation > best[0]: best = (correlation, shift_hz)
    return float(best[1]), float(best[0])


def build_rf_baseline(paths: Sequence[Path], destination: Path, *,
                      maximum_registration_shift_hz: float = 750_000) -> dict:
    if len(paths) < 2:
        raise ValueError("RF baseline requires at least two captures")
    artifacts = [load_measurement_waterfall(Path(path)) for path in paths]
    receiver_count = np.asarray(artifacts[0]["psd_db_raw_per_hz"]).shape[0]
    if any(np.asarray(item["psd_db_raw_per_hz"]).shape[0] != receiver_count for item in artifacts):
        raise ValueError("baseline captures must have the same receiver count")
    axes = [_rf_axis(item) for item in artifacts]
    bin_hz = float(np.median(np.diff(axes[0])))
    if any(not np.isclose(np.median(np.diff(axis)), bin_hz) for axis in axes):
        raise ValueError("baseline captures must have the same frequency-bin width")
    profiles = [[None for _ in range(receiver_count)] for _ in artifacts]
    for source, artifact in enumerate(artifacts):
        spectra = np.asarray(artifact["psd_db_raw_per_hz"], float)
        for receiver in range(receiver_count):
            profile = np.median(spectra[receiver], axis=0); profile -= np.median(profile)
            profiles[source][receiver] = profile
    shifts = np.zeros((receiver_count, len(paths)), float)
    registration_correlations = np.ones_like(shifts)
    for source in range(1, len(paths)):
        for receiver in range(receiver_count):
            shifts[receiver, source], registration_correlations[receiver, source] = _registration_shift(
                axes[0], profiles[0][receiver], axes[source], profiles[source][receiver],
                bin_hz, maximum_registration_shift_hz)
    corrected_axes = [[axes[source]+shifts[receiver, source]
                       for source in range(len(paths))] for receiver in range(receiver_count)]
    start = min(float(axis[0]) for group in corrected_axes for axis in group)
    stop = max(float(axis[-1]) for group in corrected_axes for axis in group)
    grid = start+np.arange(int(round((stop-start)/bin_hz))+1)*bin_hz
    registered = np.full((receiver_count, len(paths), grid.size), np.nan, np.float32)
    for source, artifact in enumerate(artifacts):
        for receiver in range(receiver_count):
            profile = profiles[source][receiver]; axis = corrected_axes[receiver][source]
            supported = (grid >= axis[0]) & (grid <= axis[-1])
            registered[receiver, source, supported] = np.interp(grid[supported], axis, profile)
    coverage = np.sum(np.isfinite(registered), axis=1)
    # Union RF grids intentionally contain receiver-specific uncovered edges.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="All-NaN slice encountered",
                               category=RuntimeWarning)
        baseline = np.nanmedian(registered, axis=1)
        variability = 1.4826*np.nanmedian(abs(registered-baseline[:, None, :]), axis=1)
    destination = Path(destination); destination.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(destination, schema=np.array(BASELINE_SCHEMA), rf_hz=grid,
        baseline_db=baseline.astype(np.float32), coverage_count=coverage.astype(np.int16),
        variability_mad_db=variability.astype(np.float32),
        source_paths_json=np.array(json.dumps([str(Path(path)) for path in paths])),
        source_center_frequency_hz=np.asarray(
            [float(item["center_frequency_hz"]) for item in artifacts]),
        registration_shift_hz=shifts, registration_correlation=registration_correlations,
        bin_width_hz=np.array(bin_hz))
    return {"schema": BASELINE_SCHEMA, "path": str(destination),
        "sources": len(paths), "receiver_count": receiver_count,
        "frequency_bins": int(grid.size), "bin_width_hz": bin_hz,
        "rf_start_hz": float(grid[0]), "rf_stop_hz": float(grid[-1]),
        "dual_coverage_fraction": float(np.mean(coverage >= 2)),
        "median_capture_variability_db": np.nanmedian(variability, axis=1).tolist(),
        "registration_shift_hz": shifts.tolist(),
        "median_registration_correlation": np.median(registration_correlations, axis=1).tolist()}


def analyze_rf_novelty(path: Path, baseline_path: Path, *, threshold_db: float = .35,
                       trend_smoothing_bins: float = 100,
                       maximum_registration_shift_hz: float = 750_000,
                       integration_s: float = 1.0) -> dict:
    artifact = load_measurement_waterfall(path)
    with np.load(baseline_path, allow_pickle=False) as stored:
        if str(stored["schema"]) != BASELINE_SCHEMA:
            raise ValueError("unsupported RF baseline schema")
        grid = np.asarray(stored["rf_hz"], float)
        baseline = np.asarray(stored["baseline_db"], float)
        coverage = np.asarray(stored["coverage_count"], int)
        variability = np.asarray(stored.get("variability_mad_db",
            np.zeros_like(baseline)), float)
    raw_spectra = np.asarray(artifact["psd_db_raw_per_hz"], float)
    spectra, integrated_times = _integrate(raw_spectra,
        np.asarray(artifact["utc_ns"], np.int64)/1e9, integration_s)
    axis = _rf_axis(artifact)
    if spectra.shape[0] != baseline.shape[0]:
        raise ValueError("baseline receiver count does not match capture")
    supported = (axis >= grid[0]) & (axis <= grid[-1])
    residual = np.full_like(spectra, np.nan)
    reports = []; corrected_axes = []
    for receiver in range(spectra.shape[0]):
        observed_profile = np.median(spectra[receiver], axis=0)
        observed_profile -= np.median(observed_profile)
        reference_supported = np.isfinite(baseline[receiver])
        shift_hz, registration_correlation = _registration_shift(
            grid[reference_supported], baseline[receiver, reference_supported], axis,
            observed_profile, float(np.median(np.diff(axis))), maximum_registration_shift_hz)
        corrected_axis = axis+shift_hz; corrected_axes.append(corrected_axis)
        receiver_supported = (corrected_axis >= grid[0]) & (corrected_axis <= grid[-1])
        model = np.interp(corrected_axis[receiver_supported], grid, baseline[receiver])
        historical_variability = np.interp(corrected_axis[receiver_supported], grid,
                                           variability[receiver])
        local_threshold = np.maximum(threshold_db, 3*historical_variability)
        rows = spectra[receiver]-np.median(spectra[receiver], axis=1, keepdims=True)
        difference = rows[:, receiver_supported]-model[None, :]
        # Capture-to-capture LNB/filter curvature is broad in frequency. Remove
        # it without erasing narrow carriers or their Doppler trajectories.
        difference -= gaussian_filter1d(difference, trend_smoothing_bins, axis=1,
                                        mode="nearest")
        residual[receiver][:, receiver_supported] = difference
        values = residual[receiver][:, receiver_supported]
        valid_columns = np.any(np.isfinite(values), axis=0) & np.isfinite(local_threshold)
        excess_profile = np.full(values.shape[1], -np.inf)
        excess_profile[valid_columns] = np.nanmax(
            values[:, valid_columns]-local_threshold[None, valid_columns], axis=0)
        peaks, properties = find_peaks(excess_profile, prominence=.05, distance=2)
        order = np.argsort(properties["prominences"])[::-1][:20]
        peaks = peaks[order]; prominences = properties["prominences"][order]
        if peaks.size < 20:
            chosen = list(map(int, peaks)); extra_prominence = list(map(float, prominences))
            for index in np.argsort(np.nan_to_num(excess_profile, nan=-np.inf))[::-1]:
                if excess_profile[index] < 0 or any(abs(int(index)-item) < 2 for item in chosen):
                    continue
                chosen.append(int(index)); extra_prominence.append(float(excess_profile[index]))
                if len(chosen) >= 20: break
            peaks = np.asarray(chosen, int); prominences = np.asarray(extra_prominence, float)
        reports.append({"receiver": receiver,
            "median_absolute_residual_db": float(np.nanmedian(abs(values))),
            "p99_absolute_residual_db": float(np.nanpercentile(abs(values), 99)),
            "positive_novel_fraction": float(np.mean(values > local_threshold[None, :])),
            "negative_novel_fraction": float(np.mean(values < -local_threshold[None, :])),
            "median_historical_variability_db": float(np.nanmedian(historical_variability)),
            "registration_shift_hz": shift_hz,
            "registration_correlation": registration_correlation,
            "strongest_novel_rf_hz": corrected_axis[receiver_supported][peaks].tolist(),
            "strongest_novel_prominence_db": prominences.tolist()})
    finite_count = np.sum(np.isfinite(residual), axis=0)
    common = np.divide(np.nansum(residual, axis=0), finite_count,
                       out=np.full(residual.shape[1:], np.nan), where=finite_count > 0)
    return {"schema": NOVELTY_SCHEMA, "source": str(path), "baseline": str(baseline_path),
        "threshold_db": threshold_db, "trend_smoothing_bins": trend_smoothing_bins,
        "integration_s": integration_s,
        "rf_start_hz": float(axis[supported][0]),
        "rf_stop_hz": float(axis[supported][-1]),
        "baseline_minimum_coverage": int(np.min(coverage[:, np.searchsorted(grid, axis[supported])])),
        "receivers": reports,
        "common_positive_novel_fraction": float(np.nanmean(common > threshold_db)),
        "residual_db": residual, "rf_hz": axis,
        "corrected_rf_hz": np.asarray(corrected_axes),
        "utc_s": integrated_times,
        "elapsed_s": integrated_times-integrated_times[0]}


def write_rf_novelty(path: Path, baseline_path: Path, output: Path,
                     *, plot: Path | None = None, threshold_db: float = .35,
                     trend_smoothing_bins: float = 100, integration_s: float = 1.0) -> dict:
    report = analyze_rf_novelty(path, baseline_path, threshold_db=threshold_db,
                                trend_smoothing_bins=trend_smoothing_bins,
                                integration_s=integration_s)
    serializable = {key: value for key, value in report.items()
                    if key not in ("residual_db", "rf_hz", "corrected_rf_hz", "utc_s",
                                   "elapsed_s")}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(serializable, indent=2, sort_keys=True)+"\n")
    if plot is not None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        residual = np.asarray(report["residual_db"])
        corrected_rf = np.asarray(report["corrected_rf_hz"])/1e9
        elapsed = np.asarray(report["elapsed_s"])
        fig, axes = plt.subplots(2, 2, figsize=(14, 8), constrained_layout=True)
        for receiver in range(2):
            rf = corrected_rf[receiver]
            image = axes[0, receiver].imshow(residual[receiver], origin="lower", aspect="auto",
                extent=[rf[0], rf[-1], elapsed[0], elapsed[-1]], cmap="RdBu_r", vmin=-1, vmax=1)
            axes[0, receiver].set(title=f"RX{receiver} persistent-RF baseline residual",
                xlabel="Approx. Ku-band RF (GHz)", ylabel="Elapsed time (s)")
            fig.colorbar(image, ax=axes[0, receiver], label="Novelty (dB)")
            axes[1, receiver].plot(rf, np.nanpercentile(residual[receiver], 99, axis=0),
                                   label="99th percentile")
            axes[1, receiver].plot(rf, np.nanmedian(residual[receiver], axis=0),
                                   label="median", alpha=.8)
            axes[1, receiver].axhline(threshold_db, color="black", ls="--", lw=1)
            axes[1, receiver].set(xlabel="Approx. Ku-band RF (GHz)", ylabel="Novelty (dB)")
            axes[1, receiver].legend(); axes[1, receiver].grid(alpha=.2)
        fig.suptitle("Absolute-RF registered novelty after persistent-spectrum subtraction")
        plot = Path(plot); plot.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(plot, dpi=150); plt.close(fig)
    return serializable
