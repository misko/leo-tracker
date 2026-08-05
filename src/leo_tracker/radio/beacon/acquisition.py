"""LNB-offset-tolerant acquisition of the exact Starlink edge pilots."""
from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

from .pilots import matched_pilot_control_scores, track_edge_pilots
from .structure import STARLINK_FRAME_DURATION_S
from .templates import acquire_pss_epoch


def acquisition_centers(span_hz: float, step_hz: float) -> tuple[float, ...]:
    """Return a symmetric digital-tuning bank including zero and both limits."""
    if span_hz < 0 or step_hz <= 0:
        raise ValueError("acquisition span must be nonnegative and step must be positive")
    if span_hz == 0:
        return (0.0,)
    count = int(np.ceil(span_hz / step_hz))
    return tuple(float(value) for value in np.linspace(-span_hz, span_hz, 2 * count + 1))


def extract_complex_subband(samples: np.ndarray, source_rate_hz: float,
                            center_offset_hz: float, output_rate_hz: float) -> np.ndarray:
    """Digitally tune and anti-alias one complex subband."""
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if source_rate_hz <= 0 or output_rate_hz <= 0 or output_rate_hz > source_rate_hz:
        raise ValueError("rates must satisfy 0 < output rate <= source rate")
    if abs(center_offset_hz) + output_rate_hz / 2 > source_rate_hz / 2 + 1e-6:
        raise ValueError("requested subband extends beyond the sampled bandwidth")
    indexes = np.arange(values.size, dtype=np.float64)
    mixed = values * np.exp(-2j * np.pi * center_offset_hz * indexes / source_rate_hz)
    if output_rate_hz == source_rate_hz:
        return np.asarray(mixed, np.complex64)
    ratio = Fraction(output_rate_hz / source_rate_hz).limit_denominator(100_000)
    return np.asarray(resample_poly(mixed, ratio.numerator, ratio.denominator), np.complex64)


def acquire_exact_receiver(samples: np.ndarray, source_rate_hz: float, *, edge: str,
                           acquisition_span_hz: float = 0,
                           acquisition_step_hz: float = 500_000,
                           subband_rate_hz: float = 2_500_000,
                           symbolwise_prefilter_margin: float = .008) -> dict:
    """Search LNB frequency uncertainty, then evaluate exact pilot codes.

    Every digital subband receives joint frame-epoch/CFO matching against both
    the exact pilot waveform and a time-scrambled control. The best exact-minus-
    control bank seeds the more detailed symbolwise tracker. This avoids treating
    a weak or incorrect PSS peak as authoritative timing.
    """
    if symbolwise_prefilter_margin < 0:
        raise ValueError("symbolwise prefilter must be nonnegative")
    output_rate = min(float(source_rate_hz), float(subband_rate_hz))
    centers = acquisition_centers(acquisition_span_hz, acquisition_step_hz)
    frequency_offsets = tuple(np.arange(-350_000, 350_001, 25_000, dtype=float))
    banks = []
    for center in centers:
        subband = extract_complex_subband(samples, source_rate_hz, center, output_rate)
        exact_match, control_match = matched_pilot_control_scores(
            subband, output_rate, edge=edge, frequency_offsets_hz=frequency_offsets)
        banks.append({"center_offset_hz": center, "samples": subband,
                      "exact_match": exact_match, "control_match": control_match,
                      "match_score_margin": exact_match["score"] - control_match["score"]})
    best = max(banks, key=lambda item: item["match_score_margin"])
    pss = acquire_pss_epoch(best["samples"], output_rate, edge=edge)
    period = output_rate * STARLINK_FRAME_DURATION_S
    matched_epoch = int(round(best["exact_match"]["sample_index"] % period))
    if best["match_score_margin"] >= symbolwise_prefilter_margin:
        pilot = track_edge_pilots(best["samples"], output_rate, matched_epoch, edge=edge)
        pilot["local_frequency_offset_hz"] = pilot["frequency_offset_hz"]
        pilot["frequency_offset_hz"] += best["center_offset_hz"]
        pilot["evaluated"] = True
    else:
        local_cfo = float(best["exact_match"]["frequency_offset_hz"])
        pilot = {"schema": "leo-tracker.starlink-edge-pilot-track/v1", "edge": edge,
                 "epoch_sample": matched_epoch, "local_frequency_offset_hz": local_cfo,
                 "frequency_offset_hz": local_cfo + best["center_offset_hz"],
                 "score_margin": 0.0, "coherence": 0.0, "control_coherence": 0.0,
                 "symbol_matches": 0, "evaluated": False,
                 "skip_reason": "joint exact-minus-control margin below symbolwise prefilter"}
    pilot["epoch_source"] = "joint_exact_pilot_match"
    return {
        "pss": pss,
        "pilot": pilot,
        "acquisition": {
            "source_rate_hz": float(source_rate_hz),
            "subband_rate_hz": output_rate,
            "span_hz": float(acquisition_span_hz),
            "step_hz": float(acquisition_step_hz),
            "selected_center_offset_hz": best["center_offset_hz"],
            "selected_epoch_sample": matched_epoch,
            "exact_match": best["exact_match"],
            "control_match": best["control_match"],
            "match_score_margin": best["match_score_margin"],
            "symbolwise_prefilter_margin": symbolwise_prefilter_margin,
            "searched_bank_count": len(banks),
            "pilot_evaluated_bank_count": int(
                best["match_score_margin"] >= symbolwise_prefilter_margin),
            "banks": [{"center_offset_hz": item["center_offset_hz"],
                       "exact_match_score": item["exact_match"]["score"],
                       "control_match_score": item["control_match"]["score"],
                       "match_score_margin": item["match_score_margin"]}
                      for item in banks],
        },
    }
