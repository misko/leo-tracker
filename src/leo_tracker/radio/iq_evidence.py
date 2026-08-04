"""Bounded raw-IQ evidence selection and waveform-specific diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy.ndimage import gaussian_filter1d


IQ_SCHEMA = "leo-tracker.triggered-iq/v1"
WAVEFORM_SCHEMA = "leo-tracker.starlink-waveform-evidence/v1"


def _utc_ns(value: str) -> int:
    return int(round(datetime.fromisoformat(value.replace("Z", "+00:00"))
                     .astimezone(timezone.utc).timestamp()*1e9))


@dataclass
class _SelectedBlock:
    score_db: float
    index: int
    utc_ns: int
    samples: np.ndarray
    gain_db: np.ndarray
    trigger_bin: np.ndarray


class IQEvidenceSelector:
    """Keep only the strongest dual-RX narrow positive novelty snapshots."""

    def __init__(self, *, maximum_blocks: int = 4, warmup_blocks: int = 16,
                 threshold_db: float = .5, minimum_separation_blocks: int = 8,
                 receiver_alignment_bins: int = 8, stratum_blocks: int | None = None):
        if maximum_blocks < 1 or warmup_blocks < 2 or threshold_db <= 0:
            raise ValueError("invalid IQ evidence selector configuration")
        self.maximum_blocks = maximum_blocks
        self.warmup_blocks = warmup_blocks
        self.threshold_db = threshold_db
        self.minimum_separation_blocks = minimum_separation_blocks
        self.receiver_alignment_bins = receiver_alignment_bins
        if stratum_blocks is not None and stratum_blocks < 1:
            raise ValueError("IQ evidence stratum must be positive")
        self.stratum_blocks = stratum_blocks
        self._history: list[np.ndarray] = []
        self._reference: np.ndarray | None = None
        self._selected: list[_SelectedBlock] = []
        self._scores: list[float] = []

    def observe(self, index: int, utc_ns: int, samples: Sequence[np.ndarray],
                spectra_db: Sequence[np.ndarray], gain_db: Sequence[float]) -> float | None:
        spectra = np.asarray(spectra_db, float)
        if spectra.ndim != 2 or spectra.shape[0] != 2:
            raise ValueError("IQ evidence selection requires two receiver spectra")
        score = None
        if self._reference is None and len(self._history) >= self.warmup_blocks:
            # Freeze a robust start-of-capture reference. Recomputing a large
            # rolling median at 30.72 MS/s costs more CPU than the radio read
            # cadence permits, while a frozen reference is exactly what an
            # onset trigger needs.
            self._reference = np.median(np.asarray(self._history), axis=0)
            self._history.clear()
        if self._reference is not None:
            residual = spectra-self._reference
            residual -= gaussian_filter1d(residual, 20, axis=1, mode="nearest")
            # Both independent receiver paths must show a positive, spectrally
            # narrow excess. Gaussian smoothing makes a one-bin impulse less
            # likely to win while preserving a two-to-eight-bin carrier.
            narrow = gaussian_filter1d(residual, 1, axis=1, mode="nearest")
            best_score, best_bins = float("-inf"), (0, 0)
            for shift in range(-self.receiver_alignment_bins,
                               self.receiver_alignment_bins+1):
                if shift < 0:
                    first, second = narrow[0, -shift:], narrow[1, :shift]
                    offset0, offset1 = -shift, 0
                elif shift > 0:
                    first, second = narrow[0, :-shift], narrow[1, shift:]
                    offset0, offset1 = 0, shift
                else:
                    first, second = narrow[0], narrow[1]
                    offset0 = offset1 = 0
                common = np.minimum(first, second)
                peak = int(np.argmax(common)); value = float(common[peak])
                if value > best_score:
                    best_score, best_bins = value, (peak+offset0, peak+offset1)
            score = best_score
            if score >= self.threshold_db:
                candidate = _SelectedBlock(score, index, int(utc_ns),
                    np.asarray(samples, np.complex64).copy(), np.asarray(gain_db, float),
                    np.asarray(best_bins, np.int32))
                nearby = ([item for item in self._selected
                           if item.index//self.stratum_blocks == index//self.stratum_blocks]
                          if self.stratum_blocks is not None else
                          [item for item in self._selected
                           if abs(item.index-index) < self.minimum_separation_blocks])
                if nearby:
                    best = max(nearby, key=lambda item: item.score_db)
                    if score > best.score_db:
                        self._selected.remove(best); self._selected.append(candidate)
                else:
                    self._selected.append(candidate)
                self._selected.sort(key=lambda item: item.score_db, reverse=True)
                del self._selected[self.maximum_blocks:]
        if self._reference is None:
            self._history.append(spectra.astype(np.float32))
        self._scores.append(np.nan if score is None else score)
        return score

    @property
    def selected_count(self) -> int:
        return len(self._selected)

    def write(self, destination: Path, *, sample_rate_hz: float,
              center_frequency_hz: float, lnb_lo_hz: float | None,
              identity: dict | None = None) -> dict | None:
        if not self._selected:
            return None
        chosen = sorted(self._selected, key=lambda item: item.index)
        all_scores = np.asarray(self._scores, float)
        all_scores = all_scores[np.isfinite(all_scores)]
        tail_probabilities = np.asarray([
            (1+np.sum(all_scores >= item.score_db))/(all_scores.size+1) for item in chosen],
            np.float32)
        destination = Path(destination); destination.parent.mkdir(parents=True, exist_ok=True)
        fields = {"schema": np.array(IQ_SCHEMA),
            "iq": np.asarray([item.samples for item in chosen], np.complex64),
            "snapshot_index": np.asarray([item.index for item in chosen], np.int32),
            "utc_ns": np.asarray([item.utc_ns for item in chosen], np.int64),
            "trigger_score_db": np.asarray([item.score_db for item in chosen], np.float32),
            "trigger_empirical_tail_probability": tail_probabilities,
            "hardware_gain_db": np.asarray([item.gain_db for item in chosen], np.float32),
            "trigger_bin": np.asarray([item.trigger_bin for item in chosen], np.int32),
            "spectrum_bins": np.array(self._reference.shape[1], np.int32),
            "sample_rate_hz": np.array(sample_rate_hz),
            "center_frequency_hz": np.array(center_frequency_hz),
            "identity_json": np.array(json.dumps(identity or {}))}
        if lnb_lo_hz is not None:
            fields["lnb_lo_hz"] = np.array(lnb_lo_hz)
        # Uncompressed storage avoids wasting CPU attempting to compress noise.
        np.savez(destination, **fields)
        return {"schema": IQ_SCHEMA, "path": str(destination), "blocks": len(chosen),
                "bytes": destination.stat().st_size,
                "maximum_trigger_score_db": max(item.score_db for item in chosen)}


def gate_iq_evidence(iq_path: Path, wide_report_path: Path, output: Path,
                     *, margin_s: float = 1.0, frequency_margin_hz: float = 15_000) -> dict:
    """Retain raw blocks only when both time and dual-RX frequency overlap."""
    wide = json.loads(Path(wide_report_path).read_text())
    qualified = [item for item in wide.get("candidates", [])
                 if item.get("moving_rf_qualified", item.get("leo_like_qualified", False))]
    intervals = [(_utc_ns(item["start_utc"])-round(margin_s*1e9),
                  _utc_ns(item["stop_utc"])+round(margin_s*1e9)) for item in qualified]
    with np.load(iq_path, allow_pickle=False) as stored:
        if str(stored["schema"]) != IQ_SCHEMA:
            raise ValueError("unsupported triggered-IQ schema")
        fields = {name: stored[name] for name in stored.files}
    timestamps = np.asarray(fields["utc_ns"], np.int64)
    trigger_bins = fields.get("trigger_bin")
    spectrum_bins = int(fields.get("spectrum_bins", 0))
    rate = float(fields["sample_rate_hz"])
    center = float(fields["center_frequency_hz"])+float(fields.get("lnb_lo_hz", 0))
    registration = np.asarray(wide.get("rf_registration_shift_hz", [0, 0]), float)
    trigger_rf = None
    if trigger_bins is not None and spectrum_bins > 0:
        trigger_rf = center+(np.asarray(trigger_bins, float)-spectrum_bins/2)*rate/spectrum_bins
        trigger_rf += registration[None, :]
    time_mask, frequency_mask = [], []
    for block, timestamp in enumerate(timestamps):
        matching = [item for item, (start, stop) in zip(qualified, intervals, strict=True)
                    if start <= timestamp <= stop]
        time_mask.append(bool(matching))
        frequency_match = trigger_rf is None
        for item in ([] if trigger_rf is None else matching):
            relative_s = (timestamp-_utc_ns(item["start_utc"]))/1e9+float(item["time_s"][0])
            paths = [np.interp(relative_s, item["time_s"], receiver["path_rf_hz"])
                     for receiver in item["receivers"]]
            tolerance = max(frequency_margin_hz,
                            max(float(receiver["median_width_hz"])/2
                                for receiver in item["receivers"]))
            if trigger_rf is not None and np.all(abs(trigger_rf[block]-paths) <= tolerance):
                frequency_match = True; break
        frequency_mask.append(frequency_match)
    time_mask = np.asarray(time_mask, bool); frequency_mask = np.asarray(frequency_mask, bool)
    mask = time_mask & frequency_mask
    block_fields = {"iq", "snapshot_index", "utc_ns", "trigger_score_db",
                    "trigger_empirical_tail_probability",
                    "hardware_gain_db", "trigger_bin"}
    for name in block_fields:
        if name in fields:
            fields[name] = fields[name][mask]
    result = {"schema": IQ_SCHEMA, "source": str(iq_path),
              "wide_report": str(wide_report_path), "input_blocks": int(mask.size),
              "qualified_intervals": len(intervals), "retained_blocks": int(mask.sum()),
              "rejected_time_blocks": int((~time_mask).sum()),
              "rejected_frequency_blocks": int(np.sum(time_mask & ~frequency_mask)),
              "rejected_nonoverlap_blocks": int((~mask).sum()), "margin_s": margin_s,
              "frequency_margin_hz": frequency_margin_hz,
              "path": None}
    if mask.any():
        fields["qualification_json"] = np.array(json.dumps(result, sort_keys=True))
        output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
        np.savez(output, **fields); result["path"] = str(output)
    return result


def _normalized_lag_correlation(samples: np.ndarray, lag: int) -> float:
    if lag <= 0 or lag >= samples.size:
        return 0.0
    first, second = samples[:-lag], samples[lag:]
    denominator = float(np.linalg.norm(first)*np.linalg.norm(second))
    return 0.0 if denominator == 0 else float(abs(np.vdot(first, second))/denominator)


def _complex_lag_correlations(samples: np.ndarray, lags: np.ndarray) -> np.ndarray:
    """Magnitude-normalized complex autocorrelation at selected positive lags."""
    values = np.asarray(samples, np.complex128)
    lags = np.asarray(lags, int)
    if values.ndim != 1 or np.any(lags <= 0) or np.any(lags >= values.size):
        raise ValueError("complex autocorrelation lags must lie inside a 1-D signal")
    fft_size = 1 << (2*values.size-1).bit_length()
    transformed = np.fft.fft(values, fft_size)
    numerators = np.fft.ifft(np.conj(transformed)*transformed, fft_size)[lags]
    squared = abs(values)**2
    prefix = np.concatenate(([0.0], np.cumsum(squared)))
    first_energy = prefix[values.size-lags]-prefix[0]
    second_energy = prefix[values.size]-prefix[lags]
    denominator = np.sqrt(first_energy*second_energy)
    return np.divide(abs(numerators), denominator, out=np.zeros(lags.size, float),
                     where=denominator > 0)


def _texture_lag_correlations(texture: np.ndarray, shifts: np.ndarray) -> np.ndarray:
    """Linear normalized autocorrelation at selected lags using one FFT."""
    values = np.asarray(texture, float)
    shifts = np.asarray(shifts, int)
    if values.ndim != 1 or np.any(shifts <= 0) or np.any(shifts >= values.size):
        raise ValueError("texture autocorrelation shifts must lie inside a 1-D texture")
    fft_size = 1 << (2*values.size-1).bit_length()
    transformed = np.fft.rfft(values, fft_size)
    numerators = np.fft.irfft(abs(transformed)**2, fft_size)[shifts]
    squared = values*values
    prefix = np.concatenate(([0.0], np.cumsum(squared)))
    first_energy = prefix[values.size-shifts]-prefix[0]
    second_energy = prefix[values.size]-prefix[shifts]
    denominator = np.sqrt(first_energy*second_energy)
    return np.divide(numerators, denominator, out=np.zeros_like(numerators),
                     where=denominator > 0)


def analyze_starlink_waveform_iq(path: Path, *, beacon_rate_hz: float = 750.0,
                                 tone_spacing_hz: float = 43_949.5,
                                 period_search_fraction: float = .02,
                                 tone_search_hz: float = 2_000) -> dict:
    """Test triggered IQ for Starlink-like repetition and spectral spacing.

    These are feature tests, not a decoder and not a protocol-identification
    decision. Controls beside each expected value make the evidence auditable.
    """
    with np.load(path, allow_pickle=False) as stored:
        if str(stored["schema"]) != IQ_SCHEMA:
            raise ValueError("unsupported triggered-IQ schema")
        iq = np.asarray(stored["iq"], np.complex64)
        rate = float(stored["sample_rate_hz"])
        utc_ns = np.asarray(stored["utc_ns"], np.int64)
        scores = np.asarray(stored["trigger_score_db"], float)
        trigger_tail = np.asarray(stored.get("trigger_empirical_tail_probability",
                                             np.full(scores.shape, np.nan)), float)
    if iq.ndim != 3 or iq.shape[1] != 2:
        raise ValueError("triggered IQ must be block x receiver x sample")
    nominal_lag = rate/beacon_rate_hz
    lag_radius = max(2, int(round(nominal_lag*period_search_fraction)))
    lags = np.arange(max(1, int(round(nominal_lag))-lag_radius),
                     int(round(nominal_lag))+lag_radius+1)
    results = []
    for block in range(iq.shape[0]):
        receiver_results = []
        for receiver in range(2):
            samples = iq[block, receiver].astype(np.complex128)
            samples -= np.mean(samples)
            correlations = _complex_lag_correlations(samples, lags)
            best_index = int(np.argmax(correlations)); best_lag = int(lags[best_index])
            # Adjacent ranges displaced by 10--20% provide a local null that
            # has the same amount of overlapping data.
            control_lags = np.concatenate((
                np.arange(int(.8*nominal_lag), int(.9*nominal_lag)+1,
                          max(1, lag_radius//8)),
                np.arange(int(1.1*nominal_lag), int(1.2*nominal_lag)+1,
                          max(1, lag_radius//8))))
            controls = _complex_lag_correlations(samples, control_lags)
            period_p = float((1+np.sum(controls >= correlations[best_index])) /
                             (controls.size+1))
            window = np.hanning(samples.size)
            spectrum = 20*np.log10(abs(np.fft.fft(samples*window))+1e-20)
            texture = spectrum-gaussian_filter1d(spectrum, 200, mode="wrap")
            # A sparse tone comb is better measured from products of only the
            # strongest spectral excursions. Full-spectrum correlation is
            # dominated by the unknown OFDM-like payload texture.
            sparse_texture = np.maximum(texture-np.percentile(texture, 99), 0)
            bin_hz = rate/samples.size
            spacing_bins = np.arange(max(1, int((tone_spacing_hz-tone_search_hz)/bin_hz)),
                                     int((tone_spacing_hz+tone_search_hz)/bin_hz)+1)
            spacing_corr = _texture_lag_correlations(sparse_texture, spacing_bins)
            spacing_corr = np.nan_to_num(spacing_corr, nan=0.0)
            tone_index = int(np.argmax(spacing_corr))
            control_spacing_bins = np.arange(max(2, int(20_000/bin_hz)),
                                             int(80_000/bin_hz)+1,
                                             max(1, int(500/bin_hz)))
            excluded = abs(control_spacing_bins*bin_hz-tone_spacing_hz) <= 5_000
            control_scores = _texture_lag_correlations(
                sparse_texture, control_spacing_bins[~excluded])
            tone_control = float(np.median(control_scores))
            tone_p = float((1+np.sum(control_scores >= spacing_corr[tone_index])) /
                           (len(control_scores)+1))
            receiver_results.append({"receiver": receiver,
                "best_period_samples": best_lag,
                "best_period_s": best_lag/rate,
                "best_beacon_rate_hz": rate/best_lag,
                "period_correlation": float(correlations[best_index]),
                "period_control_median_correlation": float(np.median(controls)),
                "period_excess_ratio": float(correlations[best_index]/
                    max(np.median(controls), 1e-12)),
                "period_empirical_false_alarm_probability": period_p,
                "best_tone_spacing_hz": float(spacing_bins[tone_index]*bin_hz),
                "tone_spacing_correlation": float(spacing_corr[tone_index]),
                "tone_spacing_control_median_correlation": tone_control,
                "tone_spacing_excess_ratio": float(spacing_corr[tone_index]/
                    max(tone_control, 1e-12)),
                "tone_spacing_empirical_false_alarm_probability": tone_p,
                "fft_bin_hz": float(bin_hz)})
        period_difference = abs(receiver_results[0]["best_period_s"]-
                                receiver_results[1]["best_period_s"])
        spacing_difference = abs(receiver_results[0]["best_tone_spacing_hz"]-
                                 receiver_results[1]["best_tone_spacing_hz"])
        results.append({"block": block, "utc_ns": int(utc_ns[block]),
                        "trigger_score_db": float(scores[block]),
                        "trigger_empirical_tail_probability": (None if not np.isfinite(
                            trigger_tail[block]) else float(trigger_tail[block])),
                        "dual_receiver_period_difference_s": period_difference,
                        "dual_receiver_tone_spacing_difference_hz": spacing_difference,
                        "dual_receiver_feature_consistent": bool(
                            period_difference <= 2/rate and spacing_difference <= 2*rate/iq.shape[2]),
                        "receivers": receiver_results})
    return {"schema": WAVEFORM_SCHEMA, "source": str(path), "blocks": iq.shape[0],
        "samples_per_block": iq.shape[2], "sample_rate_hz": rate,
        "expected_beacon_rate_hz": beacon_rate_hz,
        "expected_period_s": 1/beacon_rate_hz,
        "expected_tone_spacing_hz": tone_spacing_hz,
        "interpretation": ("feature tests only; protocol identity requires repeatable dual-RX "
                           "excess over controls and independent RF-motion evidence"),
        "results": results}


def write_starlink_waveform_iq(path: Path, output: Path, **kwargs) -> dict:
    result = analyze_starlink_waveform_iq(path, **kwargs)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
    return result
