"""Compact wideband acquisition and stationary-suppressed Doppler tracking."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .starlink import averaged_psd


WATERFALL_SCHEMA = "leo-tracker.starlink-waterfall/v1"


def _offset_mhz_to_rf_ghz(offset_mhz, *, if_center_hz: float, lnb_lo_hz: float):
    return (lnb_lo_hz + if_center_hz + np.asarray(offset_mhz) * 1e6) / 1e9


def _rf_ghz_to_offset_mhz(rf_ghz, *, if_center_hz: float, lnb_lo_hz: float):
    return (np.asarray(rf_ghz) * 1e9 - lnb_lo_hz - if_center_hz) / 1e6
MOVING_SCHEMA = "leo-tracker.starlink-wide-doppler/v1"


@dataclass(frozen=True)
class MovingWideTrack:
    receiver: int
    points: int
    start_offset_hz: float
    stop_offset_hz: float
    frequency_span_hz: float
    fitted_drift_hz_s: float
    median_depression_db: float
    score_db: float


def _compact_psd(samples: np.ndarray, sample_rate_hz: float, *,
                 fft_size: int, output_bins: int) -> np.ndarray:
    _, psd_db = averaged_psd(samples, sample_rate_hz, fft_size=fft_size)
    if output_bins < 64 or output_bins > psd_db.size or psd_db.size % output_bins:
        raise ValueError("output bins must divide the FFT size and be at least 64")
    width = psd_db.size // output_bins
    power = 10 ** ((psd_db - float(np.max(psd_db))) / 10)
    compact = power.reshape(output_bins, width).mean(axis=1)
    value = 10 * np.log10(compact + np.finfo(float).tiny)
    return (value - np.median(value)).astype(np.float32)


def capture_compact_waterfall(blocks: Iterable[tuple[int, Sequence[np.ndarray]]],
                              destination: Path, *, sample_rate_hz: float,
                              center_frequency_hz: float, snapshots: int,
                              fft_size: int = 16_384, output_bins: int = 4096,
                              identity: dict | None = None,
                              lnb_lo_hz: float | None = None) -> dict:
    """Persist normalized PSD snapshots; never persist routine raw IQ."""
    if snapshots < 2:
        raise ValueError("at least two snapshots are required")
    rows: list[list[np.ndarray]] = []
    utc: list[int] = []
    receiver_count = None
    for index, (utc_ns, incoming) in enumerate(blocks):
        values = [np.asarray(item, np.complex64) for item in incoming]
        if receiver_count is None: receiver_count = len(values)
        if not values or len(values) != receiver_count:
            raise ValueError("receiver count changed during waterfall capture")
        rows.append([_compact_psd(item, sample_rate_hz, fft_size=fft_size,
                                  output_bins=output_bins) for item in values])
        utc.append(int(utc_ns))
        if index + 1 >= snapshots: break
    if len(rows) != snapshots:
        raise RuntimeError(f"source ended after {len(rows)} of {snapshots} snapshots")
    # Acquisition-major lists become receiver x time x frequency.
    spectra = np.asarray(rows, np.float32).transpose(1, 0, 2)
    offsets = np.linspace(-sample_rate_hz / 2, sample_rate_hz / 2,
                          output_bins, endpoint=False, dtype=np.float64)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = dict(spectra_db=spectra, utc_ns=np.asarray(utc, np.int64),
                  frequency_offsets_hz=offsets, sample_rate_hz=sample_rate_hz,
                  center_frequency_hz=center_frequency_hz, fft_size=fft_size,
                  identity_json=np.array(__import__("json").dumps(identity or {})))
    if lnb_lo_hz is not None:
        fields["lnb_lo_hz"] = float(lnb_lo_hz)
    np.savez_compressed(destination, **fields)
    return {"schema": WATERFALL_SCHEMA, "path": str(destination),
            "snapshots": snapshots, "receiver_count": receiver_count,
            "frequency_bins": output_bins,
            "first_utc_ns": utc[0], "last_utc_ns": utc[-1],
            "wall_duration_s": (utc[-1] - utc[0]) / 1e9,
            "sample_rate_hz": sample_rate_hz,
            "center_frequency_hz": center_frequency_hz,
            "identity": identity or {}}


def _integrate(spectra: np.ndarray, utc_ns: np.ndarray,
               integration_s: float) -> tuple[np.ndarray, np.ndarray]:
    elapsed = (utc_ns - utc_ns[0]) / 1e9
    groups = np.floor(elapsed / integration_s).astype(int)
    values, times = [], []
    for group in np.unique(groups):
        selected = groups == group
        if selected.sum() < 2: continue
        # Rows are normalized dB, so averaging in linear relative power is
        # appropriate and prevents one impulsive FFT from dominating.
        linear = 10 ** (spectra[selected] / 10)
        row = 10 * np.log10(np.mean(linear, axis=0) + np.finfo(float).tiny)
        values.append(row - np.median(row)); times.append(float(np.median(elapsed[selected])))
    if len(values) < 4:
        raise ValueError("waterfall is too short for requested integration")
    return np.asarray(values), np.asarray(times)


def _moving_depression_track(values: np.ndarray, times_s: np.ndarray,
                             offsets_hz: np.ndarray, *, receiver: int,
                             max_drift_hz_s: float) -> tuple[MovingWideTrack, np.ndarray]:
    baseline = np.median(values, axis=0)
    score = np.maximum(0.0, baseline[None, :] - values)
    bin_hz = float(offsets_hz[1] - offsets_hz[0])
    cadence = float(np.median(np.diff(times_s)))
    max_step = max(1, int(np.ceil(max_drift_hz_s * cadence / abs(bin_hz))))
    accumulated = score[0].astype(np.float64)
    back = np.zeros((values.shape[0], values.shape[1]), np.int16)
    for time_index in range(1, values.shape[0]):
        choices = []
        for shift in range(-max_step, max_step + 1):
            shifted = np.roll(accumulated, shift)
            if shift < 0: shifted[shift:] = -np.inf
            elif shift > 0: shifted[:shift] = -np.inf
            choices.append(shifted)
        candidates = np.asarray(choices)
        selected = np.argmax(candidates, axis=0)
        accumulated = score[time_index] + candidates[selected, np.arange(values.shape[1])]
        back[time_index] = (selected - max_step).astype(np.int16)
    path = np.empty(values.shape[0], np.int32); path[-1] = int(np.argmax(accumulated))
    for time_index in range(values.shape[0] - 1, 0, -1):
        path[time_index - 1] = path[time_index] - int(back[time_index, path[time_index]])
    frequencies = offsets_hz[path]
    depression = score[np.arange(score.shape[0]), path]
    slope = float(np.polyfit(times_s - np.mean(times_s), frequencies, 1)[0])
    result = MovingWideTrack(receiver, len(path), float(frequencies[0]),
        float(frequencies[-1]), float(np.ptp(frequencies)), slope,
        float(np.median(depression)), float(np.mean(depression)))
    return result, frequencies


def analyze_compact_waterfall(path: Path, *, integration_s: float = 1.0,
                              max_drift_hz_s: float = 10_000,
                              permutations: int = 0, seed: int = 0) -> dict:
    if permutations < 0:
        raise ValueError("permutations cannot be negative")
    value = np.load(path)
    spectra = np.asarray(value["spectra_db"], np.float32)
    utc = np.asarray(value["utc_ns"], np.int64)
    offsets = np.asarray(value["frequency_offsets_hz"], np.float64)
    tracks, paths, significance, common_times = [], [], [], None
    rng = np.random.default_rng(seed)
    for receiver in range(spectra.shape[0]):
        integrated, times = _integrate(spectra[receiver], utc, integration_s)
        track, frequencies = _moving_depression_track(integrated, times, offsets,
            receiver=receiver, max_drift_hz_s=max_drift_hz_s)
        tracks.append(track); paths.append(frequencies); common_times = times
        null_scores = []
        for _ in range(permutations):
            shuffled = integrated[rng.permutation(integrated.shape[0])]
            null, _ = _moving_depression_track(shuffled, times, offsets,
                receiver=receiver, max_drift_hz_s=max_drift_hz_s)
            null_scores.append(null.score_db)
        significance.append({"permutations": permutations,
            "false_alarm_probability": (None if not permutations else
                (1 + sum(item >= track.score_db for item in null_scores)) /
                (permutations + 1)),
            "null_score_p95_db": (None if not null_scores else
                float(np.percentile(null_scores, 95))),
            "observed_score_db": track.score_db})
    assert common_times is not None
    agreement = None
    if len(paths) == 2:
        agreement = {"median_absolute_difference_hz": float(np.median(np.abs(paths[0]-paths[1]))),
                     "correlation": float(np.corrcoef(paths[0], paths[1])[0, 1]),
                     "same_bin_fraction": float(np.mean(paths[0] == paths[1]))}
    return {"schema": MOVING_SCHEMA, "source": str(path),
            "integration_s": integration_s, "max_drift_hz_s": max_drift_hz_s,
            "time_s": common_times.tolist(), "tracks": [asdict(item) for item in tracks],
            "significance": significance,
            "frequency_offsets_hz": [item.tolist() for item in paths],
            "receiver_agreement": agreement}


def plot_compact_waterfall(path: Path, analysis: dict, output: Path, *,
                           lnb_lo_hz: float | None = None) -> None:
    """Render both receiver waterfalls with the blind moving tracks overlaid."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    value = np.load(path)
    spectra = np.asarray(value["spectra_db"], np.float32)
    utc = np.asarray(value["utc_ns"], np.int64)
    offsets = np.asarray(value["frequency_offsets_hz"], np.float64) / 1e6
    if lnb_lo_hz is None and "lnb_lo_hz" in value.files:
        lnb_lo_hz = float(value["lnb_lo_hz"])
    if_center_hz = float(value["center_frequency_hz"])
    elapsed = (utc - utc[0]) / 1e9
    fig, axes = plt.subplots(spectra.shape[0], 1, figsize=(11, 3.8*spectra.shape[0]),
                             sharex=True, constrained_layout=True)
    axes = np.atleast_1d(axes)
    for receiver, axis in enumerate(axes):
        baseline = np.median(spectra[receiver], axis=0)
        residual = spectra[receiver] - baseline[None, :]
        image = axis.imshow(residual, origin="lower", aspect="auto", cmap="RdBu_r",
            vmin=-1, vmax=1, extent=[offsets[0], offsets[-1], elapsed[0], elapsed[-1]])
        axis.plot(np.asarray(analysis["frequency_offsets_hz"][receiver])/1e6,
                  analysis["time_s"], color="black", lw=1.2, label="blind moving track")
        axis.set(ylabel="Elapsed time (s)", title=f"RX{receiver} stationary-subtracted waterfall")
        axis.legend(loc="upper right")
    axes[-1].set_xlabel("Baseband frequency offset (MHz)")
    if lnb_lo_hz is not None:
        to_rf_ghz = lambda offset_mhz: _offset_mhz_to_rf_ghz(
            offset_mhz, if_center_hz=if_center_hz, lnb_lo_hz=lnb_lo_hz)
        to_offset_mhz = lambda rf_ghz: _rf_ghz_to_offset_mhz(
            rf_ghz, if_center_hz=if_center_hz, lnb_lo_hz=lnb_lo_hz)
        rf_axis = axes[0].secondary_xaxis(
            "top", functions=(to_rf_ghz, to_offset_mhz))
        rf_axis.set_xlabel(
            f"Approx. Ku-band RF (GHz; assumed LNB LO {lnb_lo_hz/1e9:.3f} GHz)")
    fig.colorbar(image, ax=axes, label="Residual PSD (dB)")
    output.parent.mkdir(parents=True, exist_ok=True); fig.savefig(output, dpi=150); plt.close(fig)
