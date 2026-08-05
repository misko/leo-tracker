"""Published 2026 Starlink edge-pilot codes and matched acquisition.

Codes and equations are from Qin et al., *Pilots and Other Predictable
Elements of the Starlink Ku-Band Downlink*, arXiv:2602.02627, Appendix A.
"""
from __future__ import annotations

from functools import lru_cache
import numpy as np
from scipy.signal import fftconvolve

from .channels import (STARLINK_EDGE_PILOT_SUBCARRIERS,
                       STARLINK_SUBCARRIER_SPACING_HZ,
                       starlink_edge_pilot_offset_hz, subcarrier_offset_hz)
from .structure import STARLINK_FRAME_DURATION_S

OFDM_SYMBOL_DURATION_S = 4.4e-6
CYCLIC_PREFIX_DURATION_S = 2 / 15 * 1e-6

# One 600-bit integer encodes 300 base-4 coefficients for symbols i=2..301.
EDGE_PILOT_HEX = {
    488: "7634046DA45F89042D0117E163167D4AE832D857515F3CAD90337697FB8F1CD048EFEF559ECD79688BCBBF44D2FA9BDFAE639DB5D7B1DD2DDCE4EC9733C0D4DCCF3172A0EC34CC226C530E",
    489: "CD9AFAA654147A5FE2B407B51FFE15215B3A71624139619628A9C33E8E3A32E5146C09BD3EE9026CA52032D7FD38960FFC52599E9B8A7F6942334BD4C6D99D4331DEF5674570B245FBB25F",
    490: "02481A2B278B88F096C8D174D369D0CF6781B70EBD402D6A6F4C985DA6265866A8374DC0B3E4917146FE3274CA5D61C3F9A31CB8125F291155CBD4F4F84E93C0D854BBC54EE14443EC2DF8",
    491: "D8DC99C2654265B8C32450114C37E2B725A822F1054B46F272877122E47109F113D59E37DFF418FEA3627C7A5CC0A93ABA0F9408E958DF4179C4DE40CEF842D333632B3E77BEB34B2E6045",
    492: "3CC5CA83B0D33089B14C3B6AC3D1946359726B4966B2E966BE61124A5D53E22A73EDBEB383A92F06CA6CAA8A5B1ECE695465145E286EEE1804CD79A00C84FC80C87DE9DF572F9B54AE798B",
    493: "C77BD59D15C2C917EEC97FB479B9F0B2BF5D2ECCD80248D2AC68C84CEA11BAD18D9F6F31B6AFD783347943562E2C6832EA76828FCDAB31EFF6A9A88EA48E3AFA625B2FCDA7B99B0295E926",
    494: "6152EF153B85110FB0B7E24D8334B1C4196DE872B598767BC3CB4A4827A09D924AA7F57EB946F1981D036E3001934B10C9E22ABB6AF1F047B3A874CA95E68CBA67063F605FD05D532AAD3C",
    495: "CD8CACF9DEFACD2CB9811439D8B7E16F9E09BED47370207150A86DFE24EA1298CCB0907F5BAB67D4660462C6B10F74B8D9FA7B6F9EC1399B30B43AF622A894B2220B6B509A84AABB58D023",
    528: "CCBF3A16929836160CEC6EB7417AE6C37DC1E828CEFB60CE0E6C3B546A76B0AE1E7BC0E9577528B0F78F82A4104EA2C316B945D385200C7E5A1C5B48F5F9F9AF5C4BA920ACA3A599DB9974",
    529: "9CF72F5F5B95CE7342C925CF1AAF457F182C32810E2F7486705D5FA2D9C8923B0173FB206B46045C6F162BB9FFD051DB5E5900EFD2DE24D4BB3FE87DD776F00B5613A7D22B2821E139A599",
    530: "296319D723210189953BB730DC6046E4EC5FB48F9718D5B600A01578CAC3159B58EE8A306663921FBE78EE7C1E8E049B4230A14EB4954933AB64F67B396DD6DB12BCBB3CCA60EA79E0614B",
    531: "1017FBBD3D03981EE9F4424D473B8A73E136C777956EAEBD4CA51E9B70D9F5D10657F268595A5C3687D2DD06C98630F817CABEF3EE660822350A70F10A29A8740212A9CF7E7D814D60A69C",
    532: "712EA482B28E96676E65D09994965587314F2B562D0E750FE566E89205A8D4DFED2C4FAFFC5ED1EA6FB63EC13513444006B78ADFB4BDB6CB05470601C9F8F4901423069C9FBD68D292C16F",
    533: "584E9F48ACA08784E696644C78ED9684FC484F32AA1B4DA8E95457358DF89FE8B9D84D47F30D3CA2F2DDF0E76E57F14A44675326EDCF15052CB62B7DF0EBE623057605CF2406E25BD56B3B",
    534: "4AF2ECF32983A9E781852F6E90DC6CCE901863F527E038DA22C0CE02E44FA0563718D93E7454293962B43594CC2EE427FAE6F15C1238D9C85ABC4E303F3AEC3404A52310CAC0378665E19A",
    535: "084AA73DF9F60535829A716EC94D95AA6901B41E81AEF28B03F08CDE7D45425B1164009D56459C4286E269F4B8EBDBA8BF6FC79847B08A69F79AF6E6A7AF05DA504455BA72727DD7BE7744",
}


def edge_pilot_symbols(edge: str = "lower") -> np.ndarray:
    """Return the published 300-by-8 unit-magnitude 4QAM pilot matrix."""
    indexes = STARLINK_EDGE_PILOT_SUBCARRIERS.get(edge)
    if indexes is None:
        raise ValueError("edge must be lower or upper")
    missing = [index for index in indexes if index not in EDGE_PILOT_HEX]
    if missing:
        raise ValueError(f"pilot codes are not installed for {edge} edge")
    output = np.empty((300, 8), np.complex64)
    for column, index in enumerate(indexes):
        encoded = int(EDGE_PILOT_HEX[index], 16)
        for row, symbol_index in enumerate(range(2, 302)):
            state = (encoded >> (2 * (301 - symbol_index))) & 3
            output[row, column] = np.exp(1j * np.pi / 2 * (state + .5))
    return output


@lru_cache(maxsize=32)
def _edge_pilot_frame_cached(sample_rate_hz: float, edge: str,
                             symbol_roll: int) -> np.ndarray:
    """Cached immutable-by-convention pilot waveform used by hot acquisition loops."""
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    count = round(sample_rate_hz * STARLINK_FRAME_DURATION_S)
    time_s = np.arange(count) / sample_rate_hz
    symbol_index = np.floor(time_s / OFDM_SYMBOL_DURATION_S).astype(int)
    output = np.zeros(count, np.complex64)
    codes = np.roll(edge_pilot_symbols(edge), symbol_roll, axis=0)
    indexes = STARLINK_EDGE_PILOT_SUBCARRIERS[edge]
    tuning_offset = starlink_edge_pilot_offset_hz(edge)
    for index in range(2, 302):
        selected = np.flatnonzero(symbol_index == index)
        if not selected.size:
            continue
        local_time = time_s[selected] - index * OFDM_SYMBOL_DURATION_S
        values = np.zeros(selected.size, np.complex128)
        for column, subcarrier in enumerate(indexes):
            frequency = subcarrier_offset_hz(subcarrier) - tuning_offset
            values += codes[index - 2, column] * np.exp(
                2j * np.pi * frequency * (local_time - CYCLIC_PREFIX_DURATION_S))
        output[selected] = values / np.sqrt(len(indexes))
    output.flags.writeable = False
    return output


def edge_pilot_frame(sample_rate_hz: float, edge: str = "lower", *, symbol_roll: int = 0) -> np.ndarray:
    """Synthesize one pilot-only frame at an SDR tuned to the pilot-band center."""
    return _edge_pilot_frame_cached(float(sample_rate_hz), edge, int(symbol_roll)).copy()


def acquire_pilot_epoch(samples: np.ndarray, sample_rate_hz: float, *,
                        edge: str = "lower", maximum_candidates: int = 4,
                        anchor_symbol_count: int = 24,
                        minimum_separation_samples: int = 20,
                        frequency_offsets_hz: tuple[float, ...] = tuple(
                            np.arange(-300_000, 300_001, 100_000, dtype=float))) -> dict:
    """Search every frame epoch using exact pilot symbols noncoherently.

    A narrow 2.5 MS/s capture contains only about eleven PSS samples, but it
    contains 300 known pilot symbols per frame. Correlating a spread of exact
    symbols and adding their powers preserves timing information despite a
    coarse residual CFO, without coherently cancelling across a whole frame.
    """
    values = np.asarray(samples, np.complex64)
    offsets = np.asarray(frequency_offsets_hz, dtype=float)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if maximum_candidates <= 0 or anchor_symbol_count <= 0:
        raise ValueError("candidate and anchor counts must be positive")
    if minimum_separation_samples <= 0:
        raise ValueError("pilot candidate separation must be positive")
    if offsets.ndim != 1 or offsets.size == 0:
        raise ValueError("at least one pilot frequency offset is required")
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    epoch_count = round(period)
    if values.size < round(4 * period):
        raise ValueError("at least four frames are required")
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge, 0)
    symbol_period = sample_rate_hz * OFDM_SYMBOL_DURATION_S
    anchors = np.unique(np.rint(np.linspace(2, 301, anchor_symbol_count)).astype(int))
    epochs = np.arange(epoch_count)[:, None]
    maximum_frames = int(np.ceil(values.size / period)) + 1
    time_s = np.arange(values.size, dtype=float) / sample_rate_hz
    bank = np.zeros((offsets.size, epoch_count), dtype=float)
    supports = np.zeros((offsets.size, epoch_count), dtype=int)
    for offset_index, offset in enumerate(offsets):
        corrected = values * np.exp(-2j * np.pi * offset * time_s)
        aggregate = np.zeros(epoch_count, dtype=float)
        support = np.zeros(epoch_count, dtype=int)
        for symbol in anchors:
            local_start = round(symbol * symbol_period)
            local_stop = round((symbol + 1) * symbol_period)
            local = template[local_start:local_stop]
            if local.size < 2:
                continue
            correlation = np.correlate(corrected, local, mode="valid")
            energy = np.convolve(np.abs(corrected) ** 2,
                                 np.ones(local.size), mode="valid")
            denominator = energy * float(np.vdot(local, local).real)
            score = np.zeros(correlation.size, dtype=float)
            usable = denominator > max(float(np.max(denominator)), 0.0) * 1e-12
            np.divide(np.abs(correlation) ** 2, denominator, out=score, where=usable)
            starts = local_start + np.rint(
                np.arange(maximum_frames) * period).astype(int)
            indexes = epochs + starts[None, :]
            valid = indexes < score.size
            safe = np.minimum(indexes, score.size - 1)
            aggregate += np.sum(score[safe] * valid, axis=1)
            support += np.sum(valid, axis=1)
        bank[offset_index] = aggregate / np.maximum(support, 1)
        supports[offset_index] = support
    selected_offsets = np.argmax(bank, axis=0)
    folded = bank[selected_offsets, np.arange(epoch_count)]
    median = float(np.median(folded))
    candidates = []
    for epoch in np.argsort(folded)[::-1]:
        epoch = int(epoch)
        if any(min(abs(epoch - item["epoch_sample"]),
                   epoch_count - abs(epoch - item["epoch_sample"])) <
               minimum_separation_samples for item in candidates):
            continue
        offset_index = int(selected_offsets[epoch])
        candidates.append({"epoch_sample": epoch,
            "epoch_s": epoch / sample_rate_hz,
            "frequency_offset_hz": float(offsets[offset_index]),
            "folded_score": float(folded[epoch]),
            "peak_to_median": float(folded[epoch] / max(median, 1e-20)),
            "symbol_frame_support": int(supports[offset_index, epoch])})
        if len(candidates) >= maximum_candidates:
            break
    best = candidates[0]
    return {"schema": "leo-tracker.starlink-pilot-epoch/v1", "edge": edge,
            "frame_period_samples": period, "folded_median": median,
            "anchor_symbols": anchors.tolist(),
            "searched_frequency_offsets_hz": offsets.tolist(),
            "candidate_minimum_separation_samples": minimum_separation_samples,
            "candidate_epochs": candidates, **best}


def _symbol_correlations(values: np.ndarray, sample_rate_hz: float, epoch_sample: int,
                         frequency_offset_hz: float, edge: str, symbol_roll: int = 0
                         ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge, int(symbol_roll))
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    symbol_period = sample_rate_hz * OFDM_SYMBOL_DURATION_S
    starts, local_starts, counts, times = [], [], [], []
    frame = 0
    while epoch_sample + round(frame * period) < values.size:
        frame_start = epoch_sample + round(frame * period)
        for symbol in range(2, 302):
            start = frame_start + round(symbol * symbol_period)
            stop = frame_start + round((symbol + 1) * symbol_period)
            local_start = round(symbol * symbol_period)
            local_stop = round((symbol + 1) * symbol_period)
            count = min(stop - start, local_stop - local_start)
            if start < 0 or start + count > values.size or count < 2:
                continue
            starts.append(start); local_starts.append(local_start); counts.append(count)
            times.append((start + (count - 1) / 2) / sample_rate_hz)
        frame += 1
    if not starts:
        return (np.empty(0, np.complex64), np.empty(0, float), np.empty(0, float))
    width = max(counts)
    columns = np.arange(width)[None, :]
    sample_indexes = np.asarray(starts)[:, None] + columns
    local_indexes = np.asarray(local_starts)[:, None] + columns
    mask = columns < np.asarray(counts)[:, None]
    local = template[np.minimum(local_indexes, template.size - 1)] * mask
    selected = values[np.minimum(sample_indexes, values.size - 1)] * mask
    corrected = selected * np.exp(
        -2j * np.pi * frequency_offset_hz * sample_indexes / sample_rate_hz)
    correlations = np.sum(np.conj(local) * corrected, axis=1)
    denominator = (np.sum(np.abs(local) ** 2, axis=1) *
                   np.sum(np.abs(corrected) ** 2, axis=1))
    powers = np.abs(correlations) ** 2 / np.maximum(denominator, 1e-20)
    return (np.asarray(correlations, np.complex64), np.asarray(powers, float),
            np.asarray(times, float))


def track_edge_pilots(samples: np.ndarray, sample_rate_hz: float, epoch_sample: int, *,
                      edge: str = "lower",
                      coarse_frequency_offsets_hz: tuple[float, ...] = tuple(
                          np.arange(-350_000, 350_001, 25_000, dtype=float))) -> dict:
    """Acquire and refine CFO from exact pilots at an acquired frame epoch.

    A time-scrambled (17-symbol roll) pilot template is evaluated as a mandatory
    negative control at the same epoch and frequency.
    """
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    candidates = []
    for offset in coarse_frequency_offsets_hz:
        correlations, powers, times = _symbol_correlations(
            values, sample_rate_hz, epoch_sample, float(offset), edge)
        candidates.append((float(np.mean(powers)) if powers.size else 0.0,
                           float(offset), correlations, times))
    coarse_score, coarse_offset, correlations, times = max(candidates, key=lambda item: item[0])
    usable = (np.abs(correlations) > np.median(np.abs(correlations)) * .25
              if correlations.size else np.zeros(0, dtype=bool))
    if np.count_nonzero(usable) >= 3:
        phase = np.unwrap(np.angle(correlations[usable]))
        residual_hz = float(np.polyfit(times[usable], phase, 1)[0] / (2 * np.pi))
        coarse_step = (min(np.diff(sorted(set(coarse_frequency_offsets_hz))))
                       if len(set(coarse_frequency_offsets_hz)) > 1 else np.inf)
        residual_hz = float(np.clip(residual_hz, -coarse_step / 2, coarse_step / 2))
    else:
        residual_hz = 0.0
    refined_offset = coarse_offset + residual_hz
    correlations, powers, _ = _symbol_correlations(
        values, sample_rate_hz, epoch_sample, refined_offset, edge)
    control_correlations, control_powers, _ = _symbol_correlations(
        values, sample_rate_hz, epoch_sample, refined_offset, edge, symbol_roll=17)
    score = float(np.mean(powers)) if powers.size else 0.0
    control_score = float(np.mean(control_powers)) if control_powers.size else 0.0
    coherence = (float(abs(np.sum(correlations)) / max(np.sum(np.abs(correlations)), 1e-20))
                 if correlations.size else 0.0)
    control_coherence = (float(abs(np.sum(control_correlations)) /
                               max(np.sum(np.abs(control_correlations)), 1e-20))
                         if control_correlations.size else 0.0)
    return {"schema": "leo-tracker.starlink-edge-pilot-track/v1", "edge": edge,
            "epoch_sample": int(epoch_sample), "coarse_frequency_offset_hz": coarse_offset,
            "residual_frequency_offset_hz": residual_hz,
            "frequency_offset_hz": refined_offset, "symbol_matches": int(powers.size),
            "noncoherent_score": score, "control_noncoherent_score": control_score,
            "score_margin": score - control_score, "coherence": coherence,
            "control_coherence": control_coherence}


def conditioned_pilot_score(samples: np.ndarray, sample_rate_hz: float, epoch_sample: int,
                            frequency_offset_hz: float, *, edge: str = "lower",
                            symbol_roll: int = 0) -> dict:
    """Score complete pilot frames at an already acquired epoch and CFO."""
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if epoch_sample < 0:
        raise ValueError("epoch sample must be nonnegative")
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge, int(symbol_roll))
    template_energy = float(np.vdot(template, template).real)
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    scores = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + template.size > values.size:
            break
        indexes = np.arange(start, start + template.size)
        corrected = values[start:start + template.size] * np.exp(
            -2j * np.pi * frequency_offset_hz * indexes / sample_rate_hz)
        denominator = template_energy * float(np.vdot(corrected, corrected).real)
        scores.append(0.0 if denominator <= 0 else
                      float(abs(np.vdot(template, corrected)) / np.sqrt(denominator)))
        frame += 1
    return {"schema": "leo-tracker.starlink-edge-pilot-conditioned/v1", "edge": edge,
            "symbol_roll": int(symbol_roll), "epoch_sample": int(epoch_sample),
            "frequency_offset_hz": float(frequency_offset_hz),
            "score": float(np.mean(scores)) if scores else 0.0,
            "maximum_score": float(max(scores)) if scores else 0.0,
            "frame_support": len(scores)}


def conditioned_pilot_frequency_search(samples: np.ndarray, sample_rate_hz: float,
                                       epoch_sample: int,
                                       frequency_offsets_hz: tuple[float, ...] | np.ndarray,
                                       *, edge: str = "lower", symbol_roll: int = 0) -> dict:
    """Batch a conditioned CFO search at one already acquired frame epoch."""
    values = np.asarray(samples, np.complex64)
    offsets = np.asarray(frequency_offsets_hz, dtype=float)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if epoch_sample < 0:
        raise ValueError("epoch sample must be nonnegative")
    if offsets.ndim != 1 or offsets.size == 0:
        raise ValueError("at least one frequency offset is required")
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge, int(symbol_roll))
    template_energy = float(np.vdot(template, template).real)
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    frame_scores = []
    frame = 0
    while True:
        start = epoch_sample + round(frame * period)
        if start + template.size > values.size:
            break
        indexes = np.arange(start, start + template.size, dtype=float)
        segment = values[start:start + template.size]
        corrected = segment[None, :] * np.exp(
            -2j * np.pi * offsets[:, None] * indexes[None, :] / sample_rate_hz)
        correlations = corrected @ np.conj(template)
        denominator = template_energy * float(np.vdot(segment, segment).real)
        frame_scores.append(np.abs(correlations) / np.sqrt(max(denominator, 1e-20)))
        frame += 1
    if frame_scores:
        scores = np.stack(frame_scores)
        means = np.mean(scores, axis=0)
        maxima = np.max(scores, axis=0)
        best = int(np.argmax(means))
    else:
        means = maxima = np.zeros(offsets.size)
        best = 0
    return {"schema": "leo-tracker.starlink-edge-pilot-conditioned/v1", "edge": edge,
            "symbol_roll": int(symbol_roll), "epoch_sample": int(epoch_sample),
            "frequency_offset_hz": float(offsets[best]),
            "score": float(means[best]), "maximum_score": float(maxima[best]),
            "frame_support": len(frame_scores),
            "searched_frequency_offsets_hz": offsets.tolist()}


def matched_pilot_score(samples: np.ndarray, sample_rate_hz: float, *, edge: str = "lower",
                        frequency_offsets_hz: tuple[float, ...] = (0.0,),
                        symbol_roll: int = 0) -> dict:
    """Search frame epoch and CFO using a normalized exact-pilot matched filter."""
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge, int(symbol_roll))
    if values.size < template.size:
        raise ValueError("at least one frame is required")
    template_energy = float(np.vdot(template, template).real)
    rolling_energy = fftconvolve(np.abs(values) ** 2, np.ones(template.size), mode="valid")
    best = {"score": 0.0, "frequency_offset_hz": None, "sample_index": None}
    time_s = np.arange(values.size) / sample_rate_hz
    for offset_hz in frequency_offsets_hz:
        corrected = values * np.exp(-2j * np.pi * offset_hz * time_s)
        correlation = fftconvolve(corrected, np.conj(template[::-1]), mode="valid")
        normalized = np.abs(correlation) / np.sqrt(np.maximum(rolling_energy * template_energy, 1e-20))
        index = int(np.argmax(normalized))
        score = float(normalized[index])
        if score > best["score"]:
            best = {"score": score, "frequency_offset_hz": float(offset_hz),
                    "sample_index": index, "epoch_s": index / sample_rate_hz}
    return {"schema": "leo-tracker.starlink-edge-pilot-match/v1", "edge": edge,
            "symbol_roll": int(symbol_roll),
            "searched_frequency_offsets_hz": list(frequency_offsets_hz), **best}


def matched_pilot_control_scores(samples: np.ndarray, sample_rate_hz: float, *,
                                 edge: str = "lower",
                                 frequency_offsets_hz: tuple[float, ...] = (0.0,),
                                 control_symbol_roll: int = 17) -> tuple[dict, dict]:
    """Batch the exact and time-scrambled delay/CFO searches.

    This is mathematically identical to two :func:`matched_pilot_score` calls,
    but shares the CFO correction bank, rolling energy, and batched FFT plan.
    The speedup permits denser temporal replay without weakening acquisition or
    using a signal-dependent prefilter.
    """
    values = np.asarray(samples, np.complex64)
    offsets = np.asarray(frequency_offsets_hz, dtype=float)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if offsets.ndim != 1 or offsets.size == 0:
        raise ValueError("at least one frequency offset is required")
    templates = np.stack([
        _edge_pilot_frame_cached(float(sample_rate_hz), edge, symbol_roll)
        for symbol_roll in (0, int(control_symbol_roll))])
    if values.size < templates.shape[1]:
        raise ValueError("at least one frame is required")
    time_s = np.arange(values.size, dtype=float) / sample_rate_hz
    corrected = values[None, :] * np.exp(
        -2j * np.pi * offsets[:, None] * time_s[None, :])
    correlations = fftconvolve(corrected[None, :, :],
        np.conj(templates[:, None, ::-1]), mode="valid", axes=-1)
    rolling_energy = fftconvolve(
        np.abs(values) ** 2, np.ones(templates.shape[1]), mode="valid")
    template_energy = np.sum(np.abs(templates) ** 2, axis=1)
    normalized = np.abs(correlations) / np.sqrt(np.maximum(
        rolling_energy[None, None, :] * template_energy[:, None, None], 1e-20))
    reports = []
    shape = normalized.shape[1:]
    for template_index, symbol_roll in enumerate((0, int(control_symbol_roll))):
        offset_index, sample_index = np.unravel_index(
            int(np.argmax(normalized[template_index])), shape)
        reports.append({
            "schema": "leo-tracker.starlink-edge-pilot-match/v1", "edge": edge,
            "symbol_roll": symbol_roll,
            "searched_frequency_offsets_hz": offsets.tolist(),
            "score": float(normalized[template_index, offset_index, sample_index]),
            "frequency_offset_hz": float(offsets[offset_index]),
            "sample_index": int(sample_index),
            "epoch_s": int(sample_index) / sample_rate_hz})
    return reports[0], reports[1]
