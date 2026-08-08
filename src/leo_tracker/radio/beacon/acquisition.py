"""LNB-offset-tolerant acquisition of the exact Starlink edge pilots."""
from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

from .pilots import (acquire_pilot_epoch, conditioned_pilot_frequency_search,
                     conditioned_pilot_score, matched_pilot_control_scores,
                     track_edge_pilots)
from .structure import STARLINK_FRAME_DURATION_S
from .templates import acquire_pss_epoch


def _conditioned_cfo_refinement(samples: np.ndarray, sample_rate_hz: float,
                                epoch_sample: int, initial_cfo_hz: float, *,
                                edge: str) -> tuple[dict, dict]:
    """Refine CFO after timing is fixed, then evaluate control at that same point."""
    offsets = initial_cfo_hz + np.arange(-2_000.0, 2_000.1, 100.0)
    exact = conditioned_pilot_frequency_search(
        samples, sample_rate_hz, epoch_sample, offsets, edge=edge)
    exact["initial_frequency_offset_hz"] = float(initial_cfo_hz)
    control = conditioned_pilot_score(samples, sample_rate_hz, epoch_sample,
        exact["frequency_offset_hz"], edge=edge, symbol_roll=17)
    control["conditioned_on_exact_hypothesis"] = True
    return exact, control


def acquisition_centers(span_hz: float, step_hz: float) -> tuple[float, ...]:
    """Return a symmetric digital-tuning bank including zero and both limits."""
    if span_hz < 0 or step_hz <= 0:
        raise ValueError("acquisition span must be nonnegative and step must be positive")
    if span_hz == 0:
        return (0.0,)
    count = int(np.ceil(span_hz / step_hz))
    return tuple(float(value) for value in np.linspace(-span_hz, span_hz, 2 * count + 1))


def usable_acquisition_span_hz(source_rate_hz: float, subband_rate_hz: float) -> float:
    """Widest symmetric digital tuning that keeps every subband inside the sampled band.

    A subband centered `offset` away from DC occupies `offset ± output_rate/2`, so
    the sampled bandwidth bounds the LNB error a capture can physically resolve.
    A caller may request more search than the recording supports; that request is
    clamped rather than refused, and the effective span is recorded.
    """
    output_rate = min(float(source_rate_hz), float(subband_rate_hz))
    return max(0.0, (float(source_rate_hz) - output_rate) / 2)


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
                           symbolwise_prefilter_margin: float = .008,
                           method: str = "coherent_grid_v1") -> dict:
    """Search LNB frequency uncertainty, then evaluate exact pilot codes.

    Every digital subband receives joint frame-epoch/CFO matching against both
    the exact pilot waveform and a time-scrambled control. The best exact-minus-
    control bank seeds the more detailed symbolwise tracker. This avoids treating
    a weak or incorrect PSS peak as authoritative timing.
    """
    if symbolwise_prefilter_margin < 0:
        raise ValueError("symbolwise prefilter must be nonnegative")
    if method not in ("coherent_grid_v1", "pss_symbolwise_v2",
                      "pilot_symbolwise_v3"):
        raise ValueError("unknown exact acquisition method")
    output_rate = min(float(source_rate_hz), float(subband_rate_hz))
    requested_span = float(acquisition_span_hz)
    span = min(requested_span, usable_acquisition_span_hz(source_rate_hz, output_rate))
    centers = acquisition_centers(span, acquisition_step_hz)
    frequency_offsets = tuple(np.arange(-350_000, 350_001, 25_000, dtype=float))
    banks = []
    for center in centers:
        subband = extract_complex_subband(samples, source_rate_hz, center, output_rate)
        if method in ("pss_symbolwise_v2", "pilot_symbolwise_v3"):
            pss_search = acquire_pss_epoch(subband, output_rate, edge=edge,
                maximum_candidates=4,
                frequency_offsets_hz=(-300_000.0, -150_000.0, 0.0,
                                      150_000.0, 300_000.0))
            timing_search = (pss_search if method == "pss_symbolwise_v2" else
                             acquire_pilot_epoch(subband, output_rate, edge=edge,
                                                 maximum_candidates=4))
            hypotheses = []
            for rank, candidate in enumerate(timing_search["candidate_epochs"]):
                initial_cfo = candidate["frequency_offset_hz"]
                coarse_offsets = tuple(sorted({float(np.clip(initial_cfo + delta,
                    -350_000.0, 350_000.0)) for delta in (-100_000.0, 0.0, 100_000.0)}))
                pilot = track_edge_pilots(subband, output_rate,
                    candidate["epoch_sample"], edge=edge,
                    coarse_frequency_offsets_hz=coarse_offsets)
                exact_match, control_match = _conditioned_cfo_refinement(
                    subband, output_rate, candidate["epoch_sample"],
                    pilot["frequency_offset_hz"], edge=edge)
                pilot["symbolwise_frequency_offset_hz"] = pilot["frequency_offset_hz"]
                pilot["frequency_offset_hz"] = exact_match["frequency_offset_hz"]
                margin = exact_match["score"] - control_match["score"]
                selection = max(margin, 0.0) * max(pilot["score_margin"], 0.0)
                hypotheses.append({"candidate_rank": rank, "pss": candidate,
                    "pilot": pilot, "exact_match": exact_match,
                    "control_match": control_match, "match_score_margin": margin,
                    "selection_score": selection})
            selected = max(hypotheses, key=lambda item: (
                item["selection_score"], item["match_score_margin"],
                item["pilot"]["score_margin"]))
            selected_timing = {**timing_search, **selected["pss"],
                               "selected_candidate_rank": selected["candidate_rank"]}
            selected_pss = (selected_timing if method == "pss_symbolwise_v2"
                            else pss_search)
            banks.append({"center_offset_hz": center, "samples": subband,
                "pss": selected_pss, "timing": selected_timing,
                "pilot": selected["pilot"],
                "exact_match": selected["exact_match"],
                "control_match": selected["control_match"],
                "match_score_margin": selected["match_score_margin"],
                "selection_score": selected["selection_score"],
                "hypotheses": hypotheses})
            continue
        exact_match, control_match = matched_pilot_control_scores(
            subband, output_rate, edge=edge, frequency_offsets_hz=frequency_offsets)
        banks.append({"center_offset_hz": center, "samples": subband,
                      "exact_match": exact_match, "control_match": control_match,
                      "match_score_margin": exact_match["score"] - control_match["score"]})
    best = max(banks, key=lambda item: (item.get("selection_score", 0.0),
                                       item["match_score_margin"]))
    pss = (best["pss"] if method in ("pss_symbolwise_v2", "pilot_symbolwise_v3") else
           acquire_pss_epoch(best["samples"], output_rate, edge=edge))
    period = output_rate * STARLINK_FRAME_DURATION_S
    matched_epoch = (int(best["timing"]["epoch_sample"])
                     if method in ("pss_symbolwise_v2", "pilot_symbolwise_v3") else
                     int(round(best["exact_match"]["sample_index"] % period)))
    if method in ("pss_symbolwise_v2", "pilot_symbolwise_v3"):
        pilot = dict(best["pilot"])
        pilot["local_frequency_offset_hz"] = pilot["frequency_offset_hz"]
        pilot["frequency_offset_hz"] += best["center_offset_hz"]
        pilot["evaluated"] = True
    elif best["match_score_margin"] >= symbolwise_prefilter_margin:
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
    pilot["epoch_source"] = ({"pss_symbolwise_v2": "pss_candidate_symbolwise_code_match",
                              "pilot_symbolwise_v3": "pilot_symbol_epoch_search"}.get(
                                  method, "joint_exact_pilot_match"))
    result = {
        "pss": pss,
        "pilot": pilot,
        "acquisition": {
            "source_rate_hz": float(source_rate_hz),
            "method": method,
            "subband_rate_hz": output_rate,
            "span_hz": span,
            "requested_span_hz": requested_span,
            "span_clamped_to_sampled_bandwidth": span < requested_span,
            "step_hz": float(acquisition_step_hz),
            "selected_center_offset_hz": best["center_offset_hz"],
            "selected_epoch_sample": matched_epoch,
            "exact_match": best["exact_match"],
            "control_match": best["control_match"],
            "match_score_margin": best["match_score_margin"],
            "symbolwise_prefilter_margin": symbolwise_prefilter_margin,
            "searched_bank_count": len(banks),
            "pilot_evaluated_bank_count": (len(banks) if method in (
                "pss_symbolwise_v2", "pilot_symbolwise_v3") else
                int(best["match_score_margin"] >= symbolwise_prefilter_margin)),
            "banks": [{"center_offset_hz": item["center_offset_hz"],
                       "exact_match_score": item["exact_match"]["score"],
                       "control_match_score": item["control_match"]["score"],
                       "match_score_margin": item["match_score_margin"]}
                      for item in banks],
        },
    }
    if method == "pilot_symbolwise_v3":
        result["pilot_epoch"] = best["timing"]
    return result
