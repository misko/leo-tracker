"""Coherent-IQ Doppler estimators used after PSD-triggered acquisition."""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np


def fll_frequency(iq, sample_rate_hz: float, *, samples_per_estimate: int) -> dict:
    """Estimate carrier frequency from the mean conjugate phase increment."""
    values = np.asarray(iq, np.complex64)
    if sample_rate_hz <= 0 or samples_per_estimate < 8:
        raise ValueError("sample rate and estimator length must be positive")
    count = values.size//samples_per_estimate
    if count < 2:
        raise ValueError("IQ is too short for two frequency estimates")
    blocks = values[:count*samples_per_estimate].reshape(count, samples_per_estimate)
    products = np.sum(np.conj(blocks[:, :-1])*blocks[:, 1:], axis=1)
    frequency = np.angle(products)*sample_rate_hz/(2*np.pi)
    times = (np.arange(count)+.5)*samples_per_estimate/sample_rate_hz
    slope, intercept = np.polyfit(times-np.mean(times), frequency, 1)
    coherence = np.abs(products)/(np.sum(np.abs(blocks[:, :-1])*np.abs(blocks[:, 1:]), axis=1)+
                                  np.finfo(float).tiny)
    return {"method": "conjugate-product-fll/v1", "time_s": times.tolist(),
        "frequency_hz": frequency.tolist(), "drift_hz_s": float(slope),
        "frequency_at_midpoint_hz": float(intercept),
        "median_coherence": float(np.median(coherence))}


def polynomial_phase_track(iq, sample_rate_hz: float, *, maximum_points: int = 200_000,
                           output_points: int = 512) -> dict:
    """Fit carrier phase, frequency and frequency rate coherently."""
    values = np.asarray(iq, np.complex64)
    if values.size < 16 or sample_rate_hz <= 0:
        raise ValueError("coherent phase tracking requires at least 16 IQ samples")
    products = np.conj(values[:-1])*values[1:]
    stride = max(1, int(np.ceil(products.size/maximum_points)))
    usable = products.size//stride*stride
    # Circularly average phase increments before converting to frequency.
    averaged = np.sum(products[:usable].reshape(-1, stride), axis=1)
    time = (np.arange(averaged.size)*stride+(stride+1)/2)/sample_rate_hz
    frequency = np.angle(averaged)*sample_rate_hz/(2*np.pi*stride)
    centered = time-np.mean(time)
    slope, intercept = np.polyfit(centered, frequency, 1)
    fitted_frequency = slope*centered+intercept
    phase_error = np.angle(averaged*np.exp(
        -2j*np.pi*fitted_frequency*stride/sample_rate_hz))
    if output_points < 2:
        raise ValueError("polynomial-phase output requires at least two points")
    retained = np.unique(np.rint(np.linspace(
        0, time.size-1, min(output_points, time.size))).astype(int))
    return {"method": "polynomial-phase-pll/v1", "time_s": time[retained].tolist(),
        "frequency_hz": frequency[retained].tolist(),
        "frequency_at_midpoint_hz": float(intercept),
        "drift_hz_s": float(slope),
        "phase_residual_rms_rad": float(np.sqrt(np.mean(phase_error**2))),
        "decimation": stride, "fit_point_count": int(time.size),
        "retained_trace_point_count": int(retained.size)}


def repetition_search(iq, *, minimum_lag: int, maximum_lag: int) -> dict:
    """Search blind complex autocorrelation for repeated waveform structure."""
    values = np.asarray(iq, np.complex64)
    if not 1 <= minimum_lag <= maximum_lag < values.size:
        raise ValueError("repetition lag interval is outside IQ")
    scores = []
    for lag in range(minimum_lag, maximum_lag+1):
        first, second = values[:-lag], values[lag:]
        denominator = np.linalg.norm(first)*np.linalg.norm(second)
        scores.append(0.0 if denominator == 0 else abs(np.vdot(first, second))/denominator)
    best = int(np.argmax(scores))
    return {"method": "blind-repetition-correlation/v1",
        "best_lag_samples": minimum_lag+best,
        "best_correlation": float(scores[best]),
        "lags_samples": list(range(minimum_lag, maximum_lag+1)),
        "correlation": [float(item) for item in scores]}


def cross_ambiguity(iq, template, sample_rate_hz: float, *, delays,
                    doppler_hz) -> dict:
    """Evaluate a discrete known-template delay/Doppler ambiguity surface."""
    values, reference = np.asarray(iq, np.complex64), np.asarray(template, np.complex64)
    delays = np.asarray(tuple(delays), int); dopplers = np.asarray(tuple(doppler_hz), float)
    if not delays.size or not dopplers.size or sample_rate_hz <= 0:
        raise ValueError("ambiguity search axes and sample rate must be non-empty")
    scores = np.zeros((delays.size, dopplers.size), float)
    for row, delay in enumerate(delays):
        if delay < 0:
            incoming, wanted = values[:delay], reference[-delay:]
        elif delay > 0:
            incoming, wanted = values[delay:], reference[:-delay]
        else:
            incoming, wanted = values, reference
        size = min(incoming.size, wanted.size)
        incoming, wanted = incoming[:size], wanted[:size]
        time = np.arange(size)/sample_rate_hz
        denominator = np.linalg.norm(incoming)*np.linalg.norm(wanted)
        for column, doppler in enumerate(dopplers):
            shifted = wanted*np.exp(2j*np.pi*doppler*time)
            scores[row, column] = (0.0 if denominator == 0 else
                                   abs(np.vdot(shifted, incoming))/denominator)
    best = np.unravel_index(np.argmax(scores), scores.shape)
    return {"method": "cross-ambiguity/v1", "delays_samples": delays.tolist(),
        "doppler_hz": dopplers.tolist(), "score": scores.tolist(),
        "best_delay_samples": int(delays[best[0]]),
        "best_doppler_hz": float(dopplers[best[1]]),
        "best_score": float(scores[best])}


def analyze_coherent_iq_artifact(path: Path, *, estimates_per_block: int = 16,
                                 repetition_minimum_lag: int = 32,
                                 repetition_maximum_lag: int = 4096) -> dict:
    with np.load(path, allow_pickle=False) as stored:
        iq = np.asarray(stored["iq"], np.complex64)
        sample_rate = float(stored["sample_rate_hz"])
        utc = np.asarray(stored.get("utc_ns", np.arange(iq.shape[0])), np.int64)
    if iq.ndim != 3:
        raise ValueError("coherent IQ artifact must be block x receiver x sample")
    reports = []
    for block in range(iq.shape[0]):
        receivers = []
        for receiver in range(iq.shape[1]):
            values = iq[block, receiver]
            length = max(8, values.size//estimates_per_block)
            maximum_lag = min(repetition_maximum_lag, values.size-1)
            repetition = (None if maximum_lag < repetition_minimum_lag else
                repetition_search(values, minimum_lag=repetition_minimum_lag,
                                  maximum_lag=maximum_lag))
            receivers.append({"receiver": receiver,
                "fll": fll_frequency(values, sample_rate,
                                     samples_per_estimate=length),
                "polynomial_phase": polynomial_phase_track(values, sample_rate),
                "repetition": repetition})
        reports.append({"block": block, "utc_ns": int(utc[block]),
                        "receivers": receivers})
    receiver_tracks = []
    for receiver in range(iq.shape[1]):
        block_times = np.asarray([item["utc_ns"] for item in reports], np.int64)/1e9
        frequencies = np.asarray([item["receivers"][receiver]["fll"][
            "frequency_at_midpoint_hz"] for item in reports], float)
        coherences = np.asarray([item["receivers"][receiver]["fll"][
            "median_coherence"] for item in reports], float)
        if block_times.size < 2 or np.ptp(block_times) <= 0:
            receiver_tracks.append({"receiver": receiver, "qualified": False,
                "warnings": ["fewer than two distinct triggered IQ block times"],
                "block_count": int(block_times.size)})
            continue
        centered = block_times-np.mean(block_times)
        slope, intercept = np.polyfit(centered, frequencies, 1)
        fitted = slope*centered+intercept
        residual_rms = float(np.sqrt(np.mean((frequencies-fitted)**2)))
        movement = float(np.ptp(fitted))
        qualified = bool(block_times.size >= 3 and np.median(coherences) >= .2 and
                         movement >= max(50, 2*residual_rms))
        warnings = []
        if block_times.size < 3: warnings.append("fewer than three triggered IQ blocks")
        if np.median(coherences) < .2: warnings.append("low conjugate-product coherence")
        if movement < max(50, 2*residual_rms):
            warnings.append("inter-block frequency motion is unresolved")
        receiver_tracks.append({"receiver": receiver,
            "utc_ns": [int(item["utc_ns"]) for item in reports],
            "frequency_hz": frequencies.tolist(), "fitted_frequency_hz": fitted.tolist(),
            "drift_hz_s": float(slope), "frequency_at_mean_time_hz": float(intercept),
            "residual_rms_hz": residual_rms, "fitted_span_hz": movement,
            "median_coherence": float(np.median(coherences)),
            "block_count": int(block_times.size), "qualified": qualified,
            "warnings": warnings})
    joint_track = None
    if len(receiver_tracks) == 2 and all("frequency_hz" in item for item in receiver_tracks):
        first, second = receiver_tracks
        first_frequency = np.asarray(first["frequency_hz"])
        second_frequency = np.asarray(second["frequency_hz"])
        correlation = float(np.corrcoef(first_frequency-np.mean(first_frequency),
                                        second_frequency-np.mean(second_frequency))[0, 1])
        if not np.isfinite(correlation): correlation = 0.0
        drift_difference = abs(first["drift_hz_s"]-second["drift_hz_s"])
        qualified = bool(first["qualified"] and second["qualified"] and
                         correlation >= .6 and drift_difference <= 1_500)
        warnings = []
        if correlation < .6: warnings.append("inter-block receiver paths disagree")
        if drift_difference > 1_500: warnings.append("inter-block receiver slopes disagree")
        joint_track = {"receiver_path_correlation": correlation,
            "receiver_frequency_offset_hz": float(np.median(
                second_frequency-first_frequency)),
            "drift_difference_hz_s": float(drift_difference),
            "mean_drift_hz_s": float((first["drift_hz_s"]+second["drift_hz_s"])/2),
            "qualified": qualified, "warnings": warnings}
    return {"schema": "leo-tracker.coherent-doppler-ensemble/v1",
        "source": str(path), "sample_rate_hz": sample_rate,
        "blocks": reports, "receiver_tracks": receiver_tracks,
        "joint_track": joint_track,
        "references": {"fll_pll": "https://github.com/gnss-sdr/gnss-sdr",
            "ambiguity": "https://doi.org/10.1155/2015/746919"}}


def write_coherent_iq_analysis(path: Path, output: Path, **kwargs) -> dict:
    report = analyze_coherent_iq_artifact(path, **kwargs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    return report


def _load_iq_vector(path: Path, *, block: int, receiver: int) -> tuple[np.ndarray, float | None]:
    loaded = np.load(path, allow_pickle=False)
    if isinstance(loaded, np.ndarray):
        values, sample_rate = loaded, None
    else:
        try:
            values = np.asarray(loaded["iq"])
            sample_rate = None if "sample_rate_hz" not in loaded else float(loaded["sample_rate_hz"])
        finally:
            loaded.close()
    if values.ndim == 3:
        if not (0 <= block < values.shape[0] and 0 <= receiver < values.shape[1]):
            raise ValueError("IQ block or receiver index is outside artifact")
        values = values[block, receiver]
    elif values.ndim != 1:
        raise ValueError("template IQ must be one-dimensional or an IQ evidence artifact")
    return np.asarray(values, np.complex64), sample_rate


def write_cross_ambiguity_analysis(iq_path: Path, template_path: Path, output: Path, *,
                                   block: int = 0, receiver: int = 0,
                                   maximum_delay_samples: int = 64,
                                   minimum_doppler_hz: float = -5_000,
                                   maximum_doppler_hz: float = 5_000,
                                   doppler_step_hz: float = 250) -> dict:
    values, sample_rate = _load_iq_vector(iq_path, block=block, receiver=receiver)
    template, template_rate = _load_iq_vector(template_path, block=0, receiver=0)
    if sample_rate is None:
        raise ValueError("input IQ artifact must contain sample_rate_hz")
    if template_rate is not None and not np.isclose(sample_rate, template_rate):
        raise ValueError("IQ and template sample rates differ")
    if maximum_delay_samples < 0 or doppler_step_hz <= 0 or maximum_doppler_hz < minimum_doppler_hz:
        raise ValueError("ambiguity search bounds are invalid")
    report = cross_ambiguity(values, template, sample_rate,
        delays=range(maximum_delay_samples+1),
        doppler_hz=np.arange(minimum_doppler_hz,
                             maximum_doppler_hz+doppler_step_hz/2, doppler_step_hz))
    payload = {"schema": "leo-tracker.cross-ambiguity/v1", "source": str(iq_path),
        "template": str(template_path), "block": block, "receiver": receiver,
        "sample_rate_hz": sample_rate, **report}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True)+"\n")
    return payload
