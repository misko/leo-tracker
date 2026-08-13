"""Score every preserved probe with every candidate detector, forever.

The bake-off the plan asks for cannot be run as a single sweep.  One sweep of
real sky is one draw from a distribution nobody has characterised: occupancy is
as low as 2%, nothing here is injected, and there is no ground truth to score
against.  So this accumulates instead.  Every probe the corpus preserves is
scored by every candidate, the answers are written beside the probe, and a
separate review turns a day of them into a comparison.

**Nothing here gates anything.**  No capture is selected, no dwell is started,
no deployed threshold moves.  This is shadow scoring; its only output is a file.

What is recorded, and why it is not just a score
------------------------------------------------

A score cannot be checked by anything else.  A *certificate* can:

    {tuning, receiver, epoch_sample, cfo_hz, utc, method, score, control, margin}

That is a falsifiable, localised claim — this method says a pilot sits at this
frame epoch and this carrier offset at this instant — and once a claim names a
place, every other method can be asked about that place, and so can the second
receiver, the other edge of the channel, the catalogue, and the next probe on
the same tuning.  :mod:`survey_comparison` is what asks.

Eight methods propose: the two coarse banks, the eight-anchor relative phase,
adjacent differentials over 16 and 32 symbols, GLRTs over 32 and 64, and the
300-symbol matched filter.  Six confirm from the shared correlation set, and
:mod:`adjudicate` supplies the seventh, eighth and ninth — the full pilot set
and its disjoint ACQUIRE and VERIFY halves — because it already holds the epoch,
prices its own refinement and refuses to search.

**Searching and confirming are different statistics with different nulls.**  A
searcher maximises over its cells and pays the extreme-value penalty for it: the
deployed 3x8 bank maximises over 3 offsets x 3,333 epochs, config E over 13 x
3,333, the full-frame detector over ~197,000 lags.  A confirmer evaluates at one
cell that somebody else proposed and pays nothing.  So a signal too weak for any
method to acquire can still be confirmable once one method proposes where to
look — which is the whole reason the conditioned columns exist.

**The size of that advantage is 2.2 dB, not the 5.2 dB the exponential model
gives.**  Measured over a bounded 3,333 x 11 search: 0.02575 searched against
0.02000 conditioned, a ratio of 1.288.  The exponential tail belongs to a
*single-frame* power statistic; a score averaged over ~59 frames has a nearly
Gaussian null (measured cv 0.070) and a Gaussian maximum grows with the root of
the log of the cell count rather than with the log — 5.16 dB predicted by the
exponential model, 1.91 dB by the Gaussian one, 2.20 dB measured.  The direction
and the architecture survive; only the magnitude was wrong, and the flattering
number is not quoted here.

**Which is why frames are not averaged away before storage.**  A max-over-frames
combiner keeps the exponential tail *and* is the better combiner under sparse
occupancy, so it would legitimately earn something nearer the larger figure.
Every conditioned score therefore carries ``frame_max`` beside its frame-summed
value, and the adjudicated verdict carries the whole per-frame array, so that
comparison stays open without re-running anything.

Every method carries ``search_cells``, and every score is labelled ``searched``
or ``conditioned``.  The two must never be pooled.

Two nulls, and they are not interchangeable
-------------------------------------------

* **Cross-edge — the primary calibration.**  The opposite edge's template on this
  tuning's own IQ.  Its pilot codes live 230 MHz away and cannot be in the band,
  and it is not a time-shifted copy of the target template, so it stays a null
  even when the epoch is searched.  Target-pilot-free *by construction* — no
  screening on the statistic being calibrated, which is the flaw that inflated
  every earlier threshold in this repository.  Run in both directions, and which
  one is recorded, because the plan's construction was lower-on-upper only.
* **Wrong code — valid only with the epoch pinned.**  Rolling the pilot sequence
  by ``r`` symbols shifts the waveform by ``r`` symbol periods, so the rolled
  template *is* the plain frame displaced ``r * 11`` samples: measured coherence
  0.909 at 17 symbols, and a rolled bank's winning epoch is exactly
  ``true_epoch - r*11``.  **Given a free epoch it therefore re-finds the real
  signal and is not a null at all** — on the corpus its p99 reaches 1.851 against
  the cross-edge 1.252, correlating 0.967 with the matched score, and a threshold
  calibrated on it would have had the survey fire on 1.8% of sky instead of 21%.
  Held at one epoch it is exactly what it claims: 0.585 exact against 0.019
  control.

Every control therefore carries ``control_epoch``, ``"pinned"`` or ``"searched"``,
and only pinned controls may calibrate anything.  The one detector that searches
its own epoch — the 300-symbol matched filter — records **both**, plus the shift
between the exact peak and the control peak, so the trap is documented in the
data rather than merely avoided in the code.  See
:data:`ROLLED_CONTROL_SHIFT_SYMBOLS`.

Conventions this file follows rather than invents
-------------------------------------------------

``sample_order`` from the manifest maps IQ tuning index to channel and edge, and
an entry without it is **skipped and counted**, never guessed: the eight-tuning
list is not a constant of nature and a mis-mapped probe would attribute a
detection to the wrong sky.  The cross-receiver comparison uses
:mod:`analysis`'s own wrapped ``epoch_difference_samples`` and
``cfo_difference_hz``, so the survey and dwell paths say the same thing with the
same words.  Copy, score, record why; never raise on one bad entry.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timezone
import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np

from .adjudicate import adjudicate
from .analysis import DUAL_EPOCH_DELTA_SAMPLES, LEARNED_MAXIMUM_CFO_DIFFERENCE_HZ
from . import fast_scan
from .pilots import (OFDM_SYMBOL_DURATION_S, edge_pilot_frame,
                     matched_pilot_control_scores)
from .relative_phase import (CONTROL_SYMBOL_ROLL, DEFAULT_TRANSFORM_SIZE,
                             adjacent_differential, anchor_relative_phase,
                             contiguous_symbols, pilot_correlations,
                             survey_anchor_symbols, symbol_glrt)
from .structure import STARLINK_FRAME_DURATION_S

#: What a probe's score sidecar calls itself.  A reader must be able to tell one
#: version from the next without inferring it from which keys are present.
#:
#: v2 states the bank the *capture* ran.  In v1 ``deployed_shape`` and
#: ``deployed_threshold`` were this host's constants at scoring time, so every
#: sidecar in a corpus spanning the bank widening claimed the same (13, 8) —
#: 283 of the 375 manifests say [3, 8] — and the reproduction check below re-ran
#: bank 46 of its observations could not represent.  The two keys keep their
#: names and change their meaning, which is exactly the case a version exists
#: for: a mixed corpus would otherwise hold two definitions under one spelling
#: and the comparison would average them without a word.  Re-scoring the corpus
#: is the price, and it has to be paid anyway because the v1 numbers are wrong.
#:
#: v2 also states the *window* each reproduction delta was computed over, and
#: computes it over the capture's window rather than the whole preserved probe.
#: That is a third key keeping its name and changing its meaning
#: (``deployed_reproduction_delta``) and would earn a bump of its own, except
#: that v2 has never been written to the share — the census reads 84 sidecars
#: at v1 and 0 at v2 — so there is no mixed corpus to protect and extending the
#: unshipped version is free.  Two bumps for one re-score would cost seven more
#: hours and buy nothing.
SCORES_SCHEMA = "leo-tracker.survey-detector-comparison/v2"

#: Name of the sidecar written beside the preserved IQ.
SCORES_FILENAME = "scores.json"

#: The two coarse front ends, spelled out rather than taken from
#: :mod:`fast_scan`'s module constants.  Those constants are being changed in
#: parallel with this work, and a comparison whose configuration silently
#: followed somebody else's edit would not be a comparison.  A is what is
#: deployed; E is what the plan decided.
COARSE_CONFIGS = {
    "A": {"shape": (3, 8), "offset_span_hz": 300_000.0},
    "E": {"shape": (13, 8), "offset_span_hz": 700_000.0},
}

#: Which coarse stage hands its epoch and offset to the candidates.  E, because
#: its 116.7 kHz spacing leaves a worst residual of 58.3 kHz — inside the
#: +/-113.6 kHz window every relative-phase statistic is unique in — while A's
#: 150 kHz worst residual is outside it, where estimates wrap silently.
CANDIDATE_COARSE = "E"

#: Adjacent-differential and GLRT symbol counts under comparison.
DIFFERENTIAL_SYMBOL_COUNTS = (16, 32)
GLRT_SYMBOL_COUNTS = (32, 64)

#: Spread anchors the deployed bank already correlates.
ANCHOR_COUNT = 8

#: Two claims closer than this are the same claim.  A pilot smears over a few
#: lags, and no statistic here resolves frequency better than the ~200 Hz comb
#: teeth of the anchor detector, so conditioning twice inside these bounds would
#: cost a correlation set and buy nothing.
POINT_EPOCH_SAMPLES = 8
POINT_CFO_HZ = 200.0

#: One in this many observations also runs the cross-edge null.  The null only
#: has to accumulate: at eight per probe a day of corpus growth passes the
#: ~1,456 realisations that support a 1% rate, and halving the arm halves the
#: most expensive part of the sweep.  The offset rotates with the capture name,
#: so every tuning and receiver is covered across entries rather than one half
#: being nulled forever.
DEFAULT_NULL_STRIDE = 2

#: How far the wrong-code control's waveform sits from the exact one, in
#: symbols.  Rolling the codes rolls the samples: at 2.5 MS/s a symbol is 11
#: samples, so the 17-roll template is the plain frame displaced 187 samples and
#: a control allowed to pick its own epoch lands there, on the real signal.
#: :func:`rolled_control_shift_samples` turns this into the number the
#: ``full-frame-300`` certificate is checked against.
ROLLED_CONTROL_SHIFT_SYMBOLS = CONTROL_SYMBOL_ROLL

#: Peak-to-median a *clean* 80 ms null reaches at its 99th percentile, and how
#: much shorter probes cost.  Recorded because nothing else in the codebase
#: tracks that the threshold depends on probe length, and because the deployed
#: 1.33 is about seventeen times stricter than the 80 ms figure — the deployed
#: detector has been **missing** detections, not manufacturing them.  The
#: 8.11/2.72/2.11% false-alarm figures in revision 5 of the plan are wrong and
#: self-contradictory: contamination inflates a threshold, and an inflated
#: threshold fires less.
CLEAN_NULL_P99_BY_PROBE_MS = {20: 1.310, 40: 1.189, 80: 1.137}

#: Physics bound on how fast a claimed offset may move between probes on one
#: tuning: 5.5 kHz/s at all-sky p99.9 and 12.7 kHz/s worst case, from the
#: three-day catalogue sweep in the plan.  Consumed by the review, defined here
#: so the producer and the consumer cannot drift apart.
TYPICAL_DOPPLER_RATE_HZ_S = 5_500.0
MAXIMUM_DOPPLER_RATE_HZ_S = 12_700.0

#: Cross-receiver agreement gates, imported rather than restated so the survey
#: and the dwell path mean the same thing by "the receivers agree".
CROSS_RECEIVER_EPOCH_SAMPLES = DUAL_EPOCH_DELTA_SAMPLES
CROSS_RECEIVER_CFO_HZ = LEARNED_MAXIMUM_CFO_DIFFERENCE_HZ

#: Doppler scales with carrier, and one channel's two edge bands sit 230.6 MHz
#: apart, so the same satellite is seen ~2% further out on the upper edge.  The
#: two probes are also taken a tuning slot apart, which at the worst Doppler rate
#: moves the offset by a further ~700 Hz.
CROSS_EDGE_CFO_TOLERANCE_HZ = 15_000.0


class ProbeUnusable(ValueError):
    """A preserved entry cannot be scored, with the reason in the message."""


# --------------------------------------------------------------------------
# reading a preserved probe
# --------------------------------------------------------------------------

def scores_path(entry: Path) -> Path:
    return Path(entry) / SCORES_FILENAME


def _survey_record(manifest: dict) -> dict:
    record = (manifest.get("metadata") or {}).get("pre_dwell_survey")
    if not isinstance(record, dict) or record.get("state") != "complete":
        raise ProbeUnusable("manifest carries no complete pre_dwell_survey")
    return record


def tuning_plan(manifest: dict) -> list[dict]:
    """Which IQ block belongs to which channel and edge, from ``sample_order``.

    **Never inferred.**  ``summarise`` sorts the tuning records by score while
    the IQ stays in collection order, so position agrees with the record list
    only by luck.  Roughly half the corpus predates the field; those entries are
    skipped and counted.  Guessing would silently attribute a detection to the
    wrong sky, and a wrong answer here is worse than no answer.
    """
    record = _survey_record(manifest)
    order = record.get("sample_order")
    if not order:
        raise ProbeUnusable("survey record carries no sample_order")
    by_key = {(item.get("channel"), item.get("region")): item
              for item in record.get("tunings") or []}
    plan = []
    for index, pair in enumerate(order):
        channel, region = int(pair[0]), str(pair[1])
        entry = by_key.get((channel, region))
        if entry is None:
            raise ProbeUnusable(
                f"sample_order names ({channel}, {region}) with no tuning record")
        plan.append({"iq_index": index, "channel": channel, "region": region,
                     "edge": region.split("-")[0], "tuning": entry})
    return plan


#: How far a declared sample count may sit from ``probe_s * sample_rate_hz``
#: before the two are treated as describing different files. One sample of
#: rounding is inherent; anything larger means the shape and the configuration
#: disagree, and there is no way to tell which of them is right.
SHAPE_CONSISTENCY_TOLERANCE_SAMPLES = 2


def survey_sample_rate_hz(manifest: dict) -> float:
    """The rate the *survey* ran at, which is not the rate the dwell ran at.

    Read from the survey's own declarations in preference order and never from
    ``manifest["sample_rate_hz"]``, which belongs to the recording the survey
    merely preceded and is a different number by construction whenever the
    survey draws 5 MS/s or the dwell is a wide or oversampled one.
    """
    record = _survey_record(manifest)
    block = manifest.get("survey_iq") or {}
    for value in ((record.get("capture_config") or {}).get("sample_rate_hz"),
                  record.get("sample_rate_hz"),
                  block.get("sample_rate_hz")):
        if value:
            return float(value)
    # Every probe taken before the configuration was written down was taken at
    # 2.5 MS/s, so this is the right answer for them and only for them.
    return 2_500_000.0


def capture_bank(record: dict) -> dict:
    """The bank the capture host actually searched, from its own record.

    Read the way :func:`survey_sample_rate_hz` reads the rate — out of the
    capture's declarations, never out of this module's constants — and for the
    same reason.  The corpus spans a widening: measured over all 375 manifests
    on the share, 283 record a (3, 8) bank over +/-300 kHz gating at 1.33 and 92
    record (13, 8) over +/-700 kHz gating at 1.252, and a sidecar that reported
    this host's current pairing for both described neither.

    The span cannot come from ``profile`` alone; only the 92 widened records
    carry it there and all 283 narrow ones carry it at record level, so both are
    consulted in that order — reading ``profile`` alone would call 283 of 375
    captures unknown.  ``threshold`` is the gate the capture host
    actually applied, which is not ``fast_scan.detection_threshold`` of the
    shape: (3, 8) is not in :data:`fast_scan.NOISE_CEILING`, so asking for it
    yields the 1.40 fallback while the capture gated at 1.33.

    Where the record is silent every field is None and ``known`` is False.  It
    is never filled in from anything else: a probe taken before the profile was
    written down is a probe whose bank nobody recorded, and saying so is the
    only answer that a reader can price.  A guess with the same spelling as a
    measurement is worse than a hole.
    """
    profile = record.get("profile") or {}
    shape = profile.get("shape")
    shape = ([int(value) for value in shape]
             if isinstance(shape, (list, tuple)) and len(shape) == 2 else None)
    span = profile.get("offset_span_hz") or record.get("offset_span_hz")
    threshold = record.get("threshold")
    return {"shape": shape,
            "offset_span_hz": float(span) if span else None,
            "threshold": float(threshold) if threshold is not None else None,
            "threshold_calibrated": record.get("threshold_calibrated"),
            "known": shape is not None and bool(span)}


def capture_scored_samples(record: dict, preserved: int) -> tuple[int, str]:
    """How much of the probe the capture host scored, and how that is known.

    The capture host scores a *bounded prefix* so its cost does not grow with
    the randomised draw — ``capture_config.scored_samples`` is 200,000 on every
    arm, the cheapest arm's whole probe — while the preserved probe is 200,000,
    400,000 or 800,000 samples depending on which arm was drawn.  Three of the
    four live arms preserve more than they scored, so an analysis host that
    recomputes over the whole probe scores a different sample set from the one
    it is checking against and disagrees by construction.

    Proven from the IQ on ``ch1-lower-edge-narrow-pluto-5d4d-20260813T062858Z``
    (arm 80ms-5.0MSps, 200,000 scored of 400,000 preserved): the mean over the
    first 200,000 samples is 87.4947, which is the manifest's deployed
    ``mean_power`` exactly, and the mean over all 400,000 is 84.8204, which is
    the recomputed one exactly.  Sixteen of sixteen target observations on that
    arm failed the reproduction check, worst delta 0.1555 and cheapest
    6.945e-04, and the population grows with every capture: 23 of the 40
    randomised entries on the share already preserve more than they scored, and
    each new one has a three-in-four chance of joining them.

    Where the record declares no window — 353 of the 393 manifests, every probe
    taken before the draw went live — the capture scored everything it kept and
    the whole preserved probe is the answer.  A declaration longer than the file
    is also the whole file, because nothing can be scored past its end.
    """
    declared = (record.get("capture_config") or {}).get("scored_samples")
    if not declared:
        return preserved, ("whole preserved probe; the record declares no "
                           "scored_samples")
    if int(declared) >= preserved:
        return preserved, (f"whole preserved probe; capture_config."
                           f"scored_samples is {int(declared)}, which is the "
                           "whole probe or more")
    return int(declared), "capture_config.scored_samples"


def read_probe(entry: Path, manifest: dict) -> np.ndarray:
    """The preserved IQ as ``(tuning, sample, receiver, component)`` int16.

    Shape comes from the manifest's own ``survey_iq`` block rather than from a
    constant here, because the capture path is what decided it; the file length
    is then checked against that shape, so a truncated copy is a refusal rather
    than a silently reshaped one.

    Since the survey draws its configuration, the sample count is one of
    200,000, 400,000 or 800,000 and there is no longer any value a reader could
    default to. So the declared shape is also checked against the declared
    probe length and rate: those three numbers describe the same file three
    ways, and two of them agreeing while the third does not is the shape of a
    silent mis-mapping. A wrong reshape either raises or misreads, and this
    repository has already had one silent-mapping bug.
    """
    block = manifest.get("survey_iq") or {}
    tunings = int(block.get("tunings") or 0)
    per_tuning = int(block.get("samples_per_tuning") or 0)
    if tunings < 1 or per_tuning < 1:
        raise ProbeUnusable("manifest declares no survey_iq shape")
    dtype = str(block.get("dtype") or "ci16_le")
    if dtype != "ci16_le":
        raise ProbeUnusable(f"unsupported survey IQ dtype {dtype!r}")
    record = _survey_record(manifest)
    config = record.get("capture_config") or {}
    probe_s = config.get("probe_s") or block.get("probe_s")
    rate = ((config.get("sample_rate_hz") or block.get("sample_rate_hz"))
            if probe_s else None)
    if probe_s and rate:
        implied = round(float(probe_s) * float(rate))
        if abs(implied - per_tuning) > SHAPE_CONSISTENCY_TOLERANCE_SAMPLES:
            raise ProbeUnusable(
                f"survey_iq declares {per_tuning} samples per tuning but the "
                f"record's {float(probe_s) * 1000:.0f} ms at "
                f"{float(rate) / 1e6:.1f} MS/s implies {implied}")
    raw = np.fromfile(Path(entry) / "survey.ci16", dtype="<i2")
    expected = tunings * per_tuning * 2 * 2
    if raw.size != expected:
        raise ProbeUnusable(
            f"survey.ci16 holds {raw.size} int16, expected {expected}")
    return raw.reshape(tunings, per_tuning, 2, 2)


def receiver_samples(block: np.ndarray, iq_index: int, receiver: int) -> np.ndarray:
    """One tuning's one receiver, as the complex64 every detector here takes."""
    piece = block[iq_index, :, receiver, :]
    values = np.empty(piece.shape[0], np.complex64)
    values.real, values.imag = piece[:, 0], piece[:, 1]
    return values


def probe_times(manifest: dict, plan: list[dict]) -> tuple[list[str | None], str]:
    """UTC of each tuning's probe, using the survey's own bracketing.

    Delegated to :mod:`survey_truth` rather than recomputed: the start of the
    scan is not recorded directly, it is bracketed between ``started+warm`` and
    ``created_utc_ns - total``, and a second implementation of that bracket
    would be a second chance to get it wrong.  A record too old to carry the
    timestamps yields ``None`` and a reason, because a claim with no instant
    attached can still be compared across methods — only across *time*.
    """
    from .survey_truth import probe_window, tuning_time
    try:
        window = probe_window(manifest)
    except (ValueError, KeyError, TypeError) as exc:
        return [None] * len(plan), f"unavailable: {type(exc).__name__}: {exc}"
    stamps = []
    for item in plan:
        moment = tuning_time(window, item["iq_index"]).astimezone(timezone.utc)
        stamps.append(moment.isoformat().replace("+00:00", "Z"))
    return stamps, window["basis"]


# --------------------------------------------------------------------------
# the statistics, searched and conditioned
# --------------------------------------------------------------------------

def _frame_period_samples(sample_rate_hz: float) -> float:
    return float(sample_rate_hz) * STARLINK_FRAME_DURATION_S


def search_cells(method: str, sample_rate_hz: float, sample_count: int) -> int:
    """How many hypotheses this method maximised over to make its claim.

    The single number that separates "this method is more sensitive" from "this
    method searched harder and paid for it in false alarms".  A conditioned
    evaluation is one cell by definition.
    """
    epochs = round(_frame_period_samples(sample_rate_hz))
    if method.startswith("coarse-"):
        return int(COARSE_CONFIGS[method.split("-", 1)[1]]["shape"][0] * epochs)
    if method.startswith("glrt-"):
        return int(DEFAULT_TRANSFORM_SIZE)
    if method == "full-frame-300":
        frame = round(_frame_period_samples(sample_rate_hz))
        return max(1, int(sample_count - frame + 1))
    return 1


def _differential_at(correlations) -> dict:
    """``|sum d| / sum |d|`` and the offset its phase implies, at one point.

    The arithmetic of :func:`relative_phase.adjacent_differential`, lifted so
    that one correlation set can serve every symbol count and both templates
    instead of being recomputed six times.  It is a duplication, so
    ``test_the_shared_conditioned_path_reproduces_the_reference_detectors``
    pins it against the public function on the same input: the repository's rule
    is that an optimisation must agree with the obvious implementation, and this
    is how that rule is kept rather than asserted.
    """
    values = correlations.values
    if values.size == 0 or values.shape[1] < 2:
        return {"score": 0.0, "residual_frequency_offset_hz": 0.0, "pairs": 0,
                "frame_max": 0.0, "frames": 0}
    leading, trailing = values[:, 1:], values[:, :-1]
    products = leading * np.conj(trailing)
    total = complex(np.sum(products))
    weight = float(np.sum(np.abs(leading) * np.abs(trailing)))
    step = correlations.symbol_step_s
    return {"score": abs(total) / weight if weight > 0 else 0.0,
            "residual_frequency_offset_hz":
                float(np.angle(total) / (2 * np.pi * step)) if total != 0 else 0.0,
            "pairs": int(products.size),
            **_per_frame(np.abs(np.sum(products, axis=1)),
                         np.sum(np.abs(leading) * np.abs(trailing), axis=1))}


def _per_frame(numerators: np.ndarray, denominators: np.ndarray) -> dict:
    """The best single frame, beside the frame count that produced it.

    Kept because averaging over ~59 frames is not obviously the right combiner:
    it dilutes a signal that lives in two of them, and Starlink occupancy runs
    as low as 2%.  A max-over-frames statistic also keeps the exponential tail
    the frame average loses, so it earns back more of the conditioned advantage
    than the 2.2 dB the averaged score measures.  One extra float per score
    keeps that comparison open; the full array is kept only where
    :func:`adjudicated` already produces it, because carrying it everywhere
    would be ~1.5 MB an entry for the review to parse.
    """
    usable = np.asarray(denominators, float) > 0
    if not usable.any():
        return {"frame_max": 0.0, "frames": int(np.size(denominators))}
    per_frame = np.divide(np.asarray(numerators, float),
                          np.asarray(denominators, float),
                          out=np.zeros(np.shape(denominators), float),
                          where=usable)
    return {"frame_max": float(per_frame.max()),
            "frames": int(per_frame.size)}


def _spectrum_at(correlations, residual_hz: float = 0.0) -> dict:
    """``S(f)`` normalised by the coherent ceiling, evaluated — not maximised.

    This is the GLRT and anchor statistic with the search taken out of it.  The
    searched version reports ``max_f S(f)``; conditioned on a claim it reports
    ``S(f_claimed)``, which is a different statistic with a much lower threshold
    for the same false-alarm rate and must be kept in its own column.

    Written out per frame rather than through :func:`frame_spectrum` and
    :func:`coherent_ceiling`, which sum the two halves before returning them:
    the ratio of the sums is exactly what those two produce — the gate test
    pins that against :func:`symbol_glrt` — and the per-frame terms are the
    thing a different combiner needs.
    """
    values = correlations.values
    if values.size == 0:
        return {"score": 0.0, "frame_max": 0.0, "frames": 0}
    rotated = values * np.exp(-2j * np.pi * float(residual_hz)
                              * correlations.lags_s)
    powers = np.abs(np.sum(rotated, axis=1)) ** 2
    ceilings = np.sum(np.abs(values), axis=1) ** 2
    total = float(ceilings.sum())
    return {"score": float(powers.sum() / total) if total > 0 else 0.0,
            **_per_frame(powers, ceilings)}


def _first_symbols(correlations, count: int):
    return replace(correlations,
                   values=correlations.values[:, :count],
                   times_s=correlations.times_s[:, :count],
                   symbols=correlations.symbols[:count])


def conditioned_suite(samples: np.ndarray, sample_rate_hz: float,
                      epoch_sample: int, frequency_offset_hz: float, *,
                      edge: str, symbol_roll: int = 0,
                      differential_counts: tuple[int, ...] = DIFFERENTIAL_SYMBOL_COUNTS,
                      glrt_counts: tuple[int, ...] = GLRT_SYMBOL_COUNTS,
                      anchor_count: int = ANCHOR_COUNT) -> dict:
    """Every confirmer's statistic at one claimed ``(epoch, offset)``.

    Two correlation sets serve all of them — the widest contiguous run, sliced
    down for the narrower counts, and the spread anchors — because the
    correlations dominate the cost and they depend only on the point, not on
    which statistic reads them.  Timing is split into the shared part and each
    statistic's own part so a production implementation's cost can be predicted
    from the right half.
    """
    widest = max(max(differential_counts), max(glrt_counts))
    mark = time.perf_counter()
    contiguous = pilot_correlations(
        samples, sample_rate_hz, epoch_sample, frequency_offset_hz, edge=edge,
        symbols=contiguous_symbols(widest), symbol_roll=symbol_roll)
    anchors = pilot_correlations(
        samples, sample_rate_hz, epoch_sample, frequency_offset_hz, edge=edge,
        symbols=survey_anchor_symbols(anchor_count), symbol_roll=symbol_roll)
    correlation_ms = 1000.0 * (time.perf_counter() - mark)

    scores = {}
    work = [(f"anchor-{anchor_count}", _spectrum_at, anchors)]
    work += [(f"differential-{count}", _differential_at,
              _first_symbols(contiguous, count)) for count in differential_counts]
    work += [(f"glrt-{count}", _spectrum_at, _first_symbols(contiguous, count))
             for count in glrt_counts]
    for name, statistic, correlations in work:
        mark = time.perf_counter()
        value = statistic(correlations)
        value["elapsed_ms"] = 1000.0 * (time.perf_counter() - mark)
        scores[name] = value
    return {"scores": scores, "correlation_ms": correlation_ms,
            "statistic_ms": sum(value["elapsed_ms"] for value in scores.values()),
            "frames": contiguous.frames}


def pinned_full_frame(
        samples: np.ndarray, sample_rate_hz: float, epoch_sample: int,
        frequency_offset_hz: float, *, edge: str,
        control_symbol_roll: int = CONTROL_SYMBOL_ROLL) -> dict | None:
    """The searched 300-symbol detector's own control, with its epoch held.

    This exists *beside* :func:`adjudicated` rather than instead of it, and the
    reason is that they are different statistics.  The searched certificate is
    ``matched_pilot_score``: one frame's normalised correlation, maximised over
    lags.  The adjudicator's blocks are a mean over ~59 frames of a per-frame
    normalised correlation restricted to pilot samples.  Subtracting the second
    from the first would not be a margin, it would be two different numbers with
    a minus sign between them — so the control for the searched score is the
    same detector handed a window exactly one frame long, which gives its lag
    search a single lag and therefore the value at the claimed epoch.

    The adjudicator remains the confirmer.  This is only the control that makes
    the searched candidate's own margin a like-for-like difference.
    """
    frame = edge_pilot_frame(sample_rate_hz, edge)
    start = int(epoch_sample)
    if start < 0 or start + frame.size > samples.size:
        return None
    exact, control = matched_pilot_control_scores(
        samples[start:start + frame.size], sample_rate_hz, edge=edge,
        frequency_offsets_hz=(float(frequency_offset_hz),),
        control_symbol_roll=int(control_symbol_roll))
    return {"score": exact["score"], "control_score": control["score"],
            "margin": exact["score"] - control["score"]}


def adjudicated(samples: np.ndarray, sample_rate_hz: float, epoch_sample: int,
                frequency_offset_hz: float, *, edge: str,
                method: str = "", utc: str = "") -> dict:
    """Hand one claim to :mod:`adjudicate` and keep what a re-analysis needs.

    Delegated rather than rebuilt.  That module already conditions the
    300-symbol statistic at exactly the claimed point, splits the published
    pilots into disjoint ACQUIRE and VERIFY halves so a verdict can be computed
    on symbols the proposer did not use, evaluates the wrong-code control at the
    same epoch — the only place that control is a null — and prices its own
    narrow frequency refinement in decibels.  A second implementation of any of
    that would be a second set of conventions.

    **Every certificate here will come back** ``withheld: "unknown"``, and that
    is correct rather than a defect: the deployed and candidate detectors all
    score the full pilot set, so nothing was genuinely held back from them.  The
    field is recorded so a later proposer that *does* declare its ACQUIRE set
    can be told apart from these, not because these are protected.

    Only the VERIFY block keeps its per-frame arrays.  Carrying all three blocks
    whole would be 16 kB a claim and ~1.5 MB an entry, which the review would
    then have to parse for every probe in the corpus; VERIFY is the block the
    verdict is read from and the one a different frame combiner would want, so
    it is the one that pays for the space.
    """
    verdict = adjudicate(samples, {"epoch_sample": int(epoch_sample),
                                   "cfo_hz": float(frequency_offset_hz),
                                   "edge": edge, "method": method, "utc": utc},
                         sample_rate_hz=sample_rate_hz, edge=edge)
    blocks = {}
    for name, block in verdict["conditioned"].items():
        kept = {key: block[key] for key in
                ("score", "control_score", "margin", "maximum_frame_score",
                 "control_maximum_frame_score", "symbols", "frames")}
        if name == "verify":
            kept["frame_scores"] = block["frame_scores"]
            kept["control_frame_scores"] = block["control_frame_scores"]
        blocks[name] = kept
    refinement = {key: verdict["refinement"][key] for key in
                  ("score", "control_score", "margin", "cells",
                   "independent_cells", "search_penalty_db",
                   "residual_frequency_offset_hz")}
    return {"blocks": blocks, "refinement": refinement,
            "withheld": verdict["withheld"], "caveats": verdict["caveats"],
            "epoch_neighbourhood": verdict["epoch_neighbourhood"],
            "cost_ms": verdict["cost_ms"]}


def rolled_control_shift_samples(sample_rate_hz: float,
                                 symbol_roll: int = CONTROL_SYMBOL_ROLL) -> int:
    """Where a free-epoch rolled control lands relative to the true epoch.

    ``true_epoch - roll * samples_per_symbol``: 187 samples at 2.5 MS/s.  This
    is the whole reason a searched rolled control is not a null, and it is worth
    a function rather than a literal because the number moves with the rate.
    """
    return int(round(symbol_roll * float(sample_rate_hz)
                     * OFDM_SYMBOL_DURATION_S))


def _certificate(method: str, report: dict, *, epoch_sample: int, cfo_hz: float,
                 elapsed_ms: float, sample_rate_hz: float, sample_count: int,
                 coarse_config: str | None = None,
                 control_epoch: str | None = None,
                 epoch_searched: bool = False, **extra) -> dict:
    """One method's claim, in the shape every check downstream reads.

    Two flags carry the whole of the wrong-code-null argument, and neither is
    decoration.

    ``control_epoch`` says how the control was obtained.  Free to choose its own
    epoch it lands on the real signal 187 samples away and is not a null at all.

    ``epoch_searched`` says whether the *exact* score searched the epoch, and it
    matters even when the control is pinned: a score maximised over 196,668 lags
    and a control evaluated at one are not the same statistic, so the difference
    between them is not a margin and the control's distribution is not that
    score's null.  Only a method whose exact score and control share their epoch
    can be calibrated on its wrong-code control, which leaves the cross-edge arm
    as the only null for anything that searched.
    """
    return {"method": method, "epoch_sample": int(epoch_sample),
            "epoch_s": int(epoch_sample) / float(sample_rate_hz),
            "cfo_hz": float(cfo_hz),
            "score": float(report["score"]),
            "control_score": (None if report.get("control_score") is None
                              else float(report["control_score"])),
            "control_epoch": control_epoch,
            "epoch_searched": bool(epoch_searched),
            "margin": (None if report.get("margin") is None
                       else float(report["margin"])),
            "residual_cfo_hz": (None if report.get("residual_frequency_offset_hz")
                                is None else
                                float(report["residual_frequency_offset_hz"])),
            "search_cells": search_cells(method, sample_rate_hz, sample_count),
            "coarse_config": coarse_config,
            "elapsed_ms": float(elapsed_ms), **extra}


def search_observation(
        samples: np.ndarray, sample_rate_hz: float, *, edge: str, banks: dict,
        differential_counts: tuple[int, ...] = DIFFERENTIAL_SYMBOL_COUNTS,
        glrt_counts: tuple[int, ...] = GLRT_SYMBOL_COUNTS,
        anchor_count: int = ANCHOR_COUNT,
        transform_size: int = DEFAULT_TRANSFORM_SIZE,
        control_symbol_roll: int = CONTROL_SYMBOL_ROLL) -> dict:
    """Run every searching method once, and return a certificate for each.

    Timed as each method would actually be called — every candidate recomputes
    its own correlations, because that is what the reference implementations do
    and a cost measured on a shared primitive nobody shares would flatter them
    all equally and rank them wrongly.  :func:`conditioned_suite` is where the
    sharing lives, and it reports its own split.
    """
    count = int(samples.size)
    certificates, coarse = [], {}
    for name, bank in banks.items():
        mark = time.perf_counter()
        scored = fast_scan.probe(samples, bank)
        elapsed = 1000.0 * (time.perf_counter() - mark)
        coarse[name] = {
            "epoch_sample": int(scored["epoch_sample"]),
            "frequency_offset_hz": float(scored["frequency_offset_hz"]),
            "peak_to_median": float(scored["peak_to_median"]),
            "folded_score": float(scored["folded_score"]),
            "folded_median": float(scored["folded_median"]),
            "peak_to_second": float(scored["peak_to_second"]),
            "offset_contrast": float(scored["offset_contrast"]),
            "anchor_agreement": int(scored["anchor_agreement"]),
            "mean_power": float(scored["mean_power"]),
            "elapsed_ms": elapsed}
        # The bank has no rolled-template path, so the coarse statistic has no
        # wrong-code control and the review must not pretend otherwise: its
        # false-alarm evidence rests on the cross-edge arm alone.
        certificates.append(_certificate(
            f"coarse-{name}", {"score": scored["peak_to_median"]},
            epoch_sample=scored["epoch_sample"],
            cfo_hz=scored["frequency_offset_hz"], elapsed_ms=elapsed,
            sample_rate_hz=sample_rate_hz, sample_count=count,
            coarse_config=name, epoch_searched=True))

    seed = coarse[CANDIDATE_COARSE]
    epoch, cfo = seed["epoch_sample"], seed["frequency_offset_hz"]

    mark = time.perf_counter()
    report = anchor_relative_phase(
        samples, sample_rate_hz, epoch, cfo, edge=edge,
        anchor_count=anchor_count, search=False,
        control_symbol_roll=control_symbol_roll)
    # Every candidate below is handed the coarse epoch and keeps it, control
    # included, so their rolled controls are the conditioned kind that stays a
    # null.  ``symbol_glrt`` searches frequency, not time, which the roll does
    # not move.
    certificates.append(_certificate(
        f"anchor-{anchor_count}", report, epoch_sample=epoch,
        cfo_hz=report["frequency_offset_hz"],
        elapsed_ms=1000.0 * (time.perf_counter() - mark),
        sample_rate_hz=sample_rate_hz, sample_count=count,
        coarse_config=CANDIDATE_COARSE, control_epoch="pinned"))

    for symbols in differential_counts:
        mark = time.perf_counter()
        report = adjacent_differential(
            samples, sample_rate_hz, epoch, cfo, edge=edge,
            symbol_count=symbols, control_symbol_roll=control_symbol_roll)
        certificates.append(_certificate(
            f"differential-{symbols}", report, epoch_sample=epoch,
            cfo_hz=report["frequency_offset_hz"],
            elapsed_ms=1000.0 * (time.perf_counter() - mark),
            sample_rate_hz=sample_rate_hz, sample_count=count,
            coarse_config=CANDIDATE_COARSE, control_epoch="pinned"))

    for symbols in glrt_counts:
        mark = time.perf_counter()
        report = symbol_glrt(
            samples, sample_rate_hz, epoch, cfo, edge=edge,
            symbol_count=symbols, transform_size=transform_size,
            control_symbol_roll=control_symbol_roll)
        certificates.append(_certificate(
            f"glrt-{symbols}", report, epoch_sample=epoch,
            cfo_hz=report["frequency_offset_hz"],
            elapsed_ms=1000.0 * (time.perf_counter() - mark),
            sample_rate_hz=sample_rate_hz, sample_count=count,
            coarse_config=CANDIDATE_COARSE, control_epoch="pinned"))

    mark = time.perf_counter()
    exact, control = matched_pilot_control_scores(
        samples, sample_rate_hz, edge=edge, frequency_offsets_hz=(cfo,),
        control_symbol_roll=control_symbol_roll)
    # Its epoch is its own: it searches every lag at the coarse offset rather
    # than accepting the coarse epoch, which is a wider search than any other
    # candidate ran and is why ``search_cells`` exists.  That freedom is also
    # what breaks the rolled control here and nowhere else in this family, so
    # the honest control is re-taken at the epoch the exact score chose, and the
    # searched one is kept beside it as the exhibit rather than discarded.
    pinned = pinned_full_frame(samples, sample_rate_hz, exact["sample_index"],
                               cfo, edge=edge,
                               control_symbol_roll=control_symbol_roll)
    elapsed = 1000.0 * (time.perf_counter() - mark)
    period = _frame_period_samples(sample_rate_hz)
    shift = (int(control["sample_index"]) - int(exact["sample_index"])) % period
    pinned_control = None if pinned is None else pinned["control_score"]
    certificates.append(_certificate(
        "full-frame-300",
        {"score": exact["score"], "control_score": pinned_control,
         "margin": (None if pinned_control is None
                    else exact["score"] - pinned_control)},
        epoch_sample=exact["sample_index"], cfo_hz=cfo, elapsed_ms=elapsed,
        sample_rate_hz=sample_rate_hz, sample_count=count,
        coarse_config=CANDIDATE_COARSE,
        control_epoch=None if pinned is None else "pinned",
        # Its score is a maximum over every lag while this control sits at one,
        # so the control is honest and still cannot be this score's null. The
        # cross-edge arm, which searches the same lags with a template that is
        # not a shifted copy, is the only null this candidate has.
        epoch_searched=True,
        searched_control_score=float(control["score"]),
        searched_control_epoch_sample=int(control["sample_index"]),
        # Negative shifts wrap to just under the frame period, so the expected
        # signature of a control that re-found the signal is
        # ``period - roll*11``: 3146 of 3333 at 2.5 MS/s.
        searched_control_epoch_shift_samples=float(shift),
        rolled_shift_samples=rolled_control_shift_samples(
            sample_rate_hz, control_symbol_roll)))
    return {"coarse": coarse, "certificates": certificates}


def distinct_points(certificates: list[dict], sample_rate_hz: float, *,
                    epoch_tolerance: int = POINT_EPOCH_SAMPLES,
                    cfo_tolerance_hz: float = POINT_CFO_HZ) -> list[dict]:
    """Collapse claims that name the same place, keeping who claimed it.

    Epochs are compared modulo the frame period, the way
    :func:`analysis.analyze_exact_window` compares two receivers': a claim one
    whole frame later is the same claim about the same signal.
    """
    period = _frame_period_samples(sample_rate_hz)
    points: list[dict] = []
    for certificate in sorted(certificates, key=lambda item: item["method"]):
        epoch, cfo = certificate["epoch_sample"], certificate["cfo_hz"]
        for point in points:
            gap = abs(point["epoch_sample"] - epoch) % period
            gap = min(gap, period - gap)
            if (gap <= epoch_tolerance
                    and abs(point["cfo_hz"] - cfo) <= cfo_tolerance_hz):
                point["claimed_by"].append(certificate["method"])
                break
        else:
            points.append({"point_id": len(points), "epoch_sample": int(epoch),
                           "cfo_hz": float(cfo),
                           "claimed_by": [certificate["method"]]})
    return points


def confirm_points(samples: np.ndarray, sample_rate_hz: float, points: list[dict],
                   *, edge: str, null_edge: str | None = None,
                   differential_counts: tuple[int, ...] = DIFFERENTIAL_SYMBOL_COUNTS,
                   glrt_counts: tuple[int, ...] = GLRT_SYMBOL_COUNTS,
                   anchor_count: int = ANCHOR_COUNT,
                   control_symbol_roll: int = CONTROL_SYMBOL_ROLL) -> list[dict]:
    """Ask every confirmer about every claimed place, under every template.

    Two families answer.  The relative-phase confirmers share one correlation
    set per template.  The 300-symbol statistic is handed to
    :func:`adjudicated`, which returns it three ways — the full pilot set and
    the disjoint ACQUIRE and VERIFY halves — so ``full-frame-verify`` is a score
    on symbols a differently-built proposer could have been kept away from, even
    though today's proposers all see everything and the verdict says so.

    Three templates per point: the exact code, the same code rolled 17 symbols,
    and — when ``null_edge`` is given — the opposite edge's code.

    **This is the one place the rolled control is sound**, because nothing here
    searches an epoch: the roll displaces the waveform 187 samples, which only
    matters to a detector free to follow it.  Held at the point somebody else
    claimed, the rolled template is a genuinely absent code on the same IQ at
    the same gain — 0.585 exact against 0.019 control, measured.

    Together they give the conditioned statistic a threshold of its own instead
    of borrowing the searched one, which is 2.2 dB too strict for it — smaller
    than an exponential tail predicts, and still the whole point of asking.
    """
    kwargs = {"differential_counts": differential_counts,
              "glrt_counts": glrt_counts, "anchor_count": anchor_count}
    confirmed = []
    for point in points:
        epoch, cfo = point["epoch_sample"], point["cfo_hz"]
        exact = conditioned_suite(samples, sample_rate_hz, epoch, cfo,
                                  edge=edge, symbol_roll=0, **kwargs)
        control = conditioned_suite(samples, sample_rate_hz, epoch, cfo,
                                    edge=edge, symbol_roll=control_symbol_roll,
                                    **kwargs)
        null = (None if null_edge is None else
                conditioned_suite(samples, sample_rate_hz, epoch, cfo,
                                  edge=null_edge, symbol_roll=0, **kwargs))
        methods = {}
        for name, value in exact["scores"].items():
            other = control["scores"].get(name, {})
            methods[name] = {
                "score": value["score"],
                "control_score": other.get("score"),
                "margin": (None if other.get("score") is None
                           else value["score"] - other["score"]),
                "cross_edge_score": (None if null is None
                                     else null["scores"][name]["score"]),
                "residual_cfo_hz": value.get("residual_frequency_offset_hz"),
                "frame_max": value.get("frame_max"),
                # Nothing here searches an epoch, so every control is the kind
                # of rolled control that stays a null.
                "control_epoch": "pinned",
                "elapsed_ms": value["elapsed_ms"]}
        verdict = adjudicated(samples, sample_rate_hz, epoch, cfo, edge=edge,
                              method="+".join(point["claimed_by"]))
        null_verdict = (None if null_edge is None else
                        adjudicated(samples, sample_rate_hz, epoch, cfo,
                                    edge=null_edge))
        for name, block in verdict["blocks"].items():
            methods[f"full-frame-{name}"] = {
                "score": block["score"], "control_score": block["control_score"],
                "margin": block["margin"],
                "frame_max": block["maximum_frame_score"],
                "cross_edge_score": (None if null_verdict is None else
                                     null_verdict["blocks"][name]["score"]),
                "residual_cfo_hz": 0.0, "control_epoch": "pinned",
                "elapsed_ms": None}
        confirmed.append({**point, "methods": methods,
                          "adjudication": verdict,
                          "correlation_ms": exact["correlation_ms"]
                                            + control["correlation_ms"],
                          "frames": exact["frames"]})
    return confirmed


# --------------------------------------------------------------------------
# the checks that do not need another correlation
# --------------------------------------------------------------------------

def cross_receiver_checks(observations: list[dict], sample_rate_hz: float, *,
                          receiver_centers: tuple[float, float] = (0.0, 0.0),
                          calibrated: bool = False) -> list[dict]:
    """Do the two ports claim the same thing about the same sky?

    The strongest structural check available without leaving the probe.  The
    antennas are co-located, so a real satellite lands on the *same* frame epoch
    at both ports, and their offsets differ by exactly the inter-receiver LNB
    bias — a quantity the calibration measures directly.  **It needs neither
    absolute offset**, only their difference, which is the one thing
    ``receiver_centers`` can actually establish.
    """
    period = _frame_period_samples(sample_rate_hz)
    bias = float(receiver_centers[0]) - float(receiver_centers[1])
    by_tuning: dict[tuple, dict] = {}
    for observation in observations:
        if observation["arm"] != "target":
            continue
        key = (observation["channel"], observation["region"])
        for certificate in observation["certificates"]:
            slot = by_tuning.setdefault((key, certificate["method"]), {})
            slot[observation["receiver"]] = certificate
    checks = []
    for (key, method), pair in sorted(by_tuning.items(), key=lambda item: str(item[0])):
        if 0 not in pair or 1 not in pair:
            continue
        gap = abs(pair[0]["epoch_sample"] - pair[1]["epoch_sample"]) % period
        gap = min(gap, period - gap)
        difference = pair[0]["cfo_hz"] - pair[1]["cfo_hz"]
        residual = difference - bias
        checks.append({
            "channel": key[0], "region": key[1], "method": method,
            "epoch_difference_samples": float(gap),
            "cfo_difference_hz": float(abs(difference)),
            "cfo_residual_after_bias_hz": float(abs(residual)),
            "bias_hz": bias, "calibration_applied": bool(calibrated),
            "agrees": bool(gap <= CROSS_RECEIVER_EPOCH_SAMPLES
                           and abs(residual) <= CROSS_RECEIVER_CFO_HZ)})
    return checks


def cross_edge_checks(observations: list[dict]) -> list[dict]:
    """Does the same claim appear on both edges of one channel?

    Every pilot-bearing frame carries both edge bands, so a transmitting
    satellite should be visible at both tunings of a channel, at offsets
    differing only by the ~2% carrier scaling of Doppler across the 230.6 MHz
    between them.  Weaker than the cross-receiver check because the two probes
    are taken a tuning slot apart, during which the worst-case Doppler rate
    moves the offset by a few hundred hertz — recorded, not corrected.
    """
    by_channel: dict[tuple, dict] = {}
    for observation in observations:
        if observation["arm"] != "target":
            continue
        for certificate in observation["certificates"]:
            slot = by_channel.setdefault(
                (observation["channel"], observation["receiver"],
                 certificate["method"]), {})
            slot[observation["edge"]] = {**certificate,
                                         "rf_center_hz": observation["rf_center_hz"]}
    checks = []
    for key, pair in sorted(by_channel.items(), key=lambda item: str(item[0])):
        if "lower" not in pair or "upper" not in pair:
            continue
        lower, upper = pair["lower"], pair["upper"]
        ratio = (float(upper["rf_center_hz"]) / float(lower["rf_center_hz"])
                 if lower["rf_center_hz"] else 1.0)
        expected = lower["cfo_hz"] * ratio
        checks.append({
            "channel": key[0], "receiver": key[1], "method": key[2],
            "lower_cfo_hz": lower["cfo_hz"], "upper_cfo_hz": upper["cfo_hz"],
            "carrier_ratio": ratio,
            "cfo_residual_hz": float(abs(upper["cfo_hz"] - expected)),
            "agrees": bool(abs(upper["cfo_hz"] - expected)
                           <= CROSS_EDGE_CFO_TOLERANCE_HZ)})
    return checks


def _prior_coverage(satellites: list[dict], receiver: int, span_hz: float) -> float:
    """Share of the searched offset span some catalogued slice already covers.

    Without this number a geometry match reads as evidence when it may be
    arithmetic: the plan measures the prior covering ~74% of the search space,
    at which point "a satellite was predicted there" is barely 1.35:1 and saying
    so plainly is the difference between enrichment and a label.
    """
    intervals = []
    for satellite in satellites:
        for entry in satellite.get("receivers") or []:
            if int(entry.get("receiver", -1)) != receiver:
                continue
            centre = float(entry.get("predicted_offset_hz", 0.0))
            half = float(satellite.get("tolerance_hz") or 0.0)
            low, high = max(centre - half, -span_hz), min(centre + half, span_hz)
            if high > low:
                intervals.append((low, high))
    if not intervals:
        return 0.0
    covered, reach = 0.0, -span_hz
    for low, high in sorted(intervals):
        low = max(low, reach)
        if high > low:
            covered += high - low
            reach = high
    return float(covered / (2 * span_hz)) if span_hz > 0 else 0.0


def geometry_checks(certificates: list[dict], satellites: list[dict],
                    receiver: int, *, span_hz: float) -> dict:
    """Was anything catalogued predicted where this claim says the signal is?

    A prior, never a label — a catalogued satellite can be in view and silent,
    and that is one of the things the corpus exists to measure.  Reported with
    its own coverage fraction so the reader can price it.
    """
    coverage = _prior_coverage(satellites, receiver, span_hz)
    matches = {}
    for certificate in certificates:
        best, found = None, 0
        for satellite in satellites:
            for entry in satellite.get("receivers") or []:
                if int(entry.get("receiver", -1)) != receiver:
                    continue
                separation = abs(float(entry.get("predicted_offset_hz", 0.0))
                                 - certificate["cfo_hz"])
                if separation <= float(satellite.get("tolerance_hz") or 0.0):
                    found += 1
                    if best is None or separation < best["separation_hz"]:
                        best = {"norad_id": satellite.get("norad_id"),
                                "name": str(satellite.get("name", "")).strip(),
                                "elevation_deg": satellite.get("elevation_deg"),
                                "doppler_rate_hz_s": satellite.get("doppler_rate_hz_s"),
                                "separation_hz": separation}
        matches[certificate["method"]] = {"matches": found, "best": best}
    return {"prior_coverage_fraction": coverage, "by_method": matches,
            "catalogued_in_view": len(satellites)}


# --------------------------------------------------------------------------
# scoring one entry
# --------------------------------------------------------------------------

def _null_selected(capture: str, iq_index: int, receiver: int, stride: int) -> bool:
    if stride <= 1:
        return True
    digest = hashlib.sha256(capture.encode()).digest()[0]
    return ((iq_index * 2 + receiver + digest) % stride) == 0


def _banks(edge: str, sample_rate_hz: float) -> dict:
    return {name: fast_scan.build_bank(
                edge, sample_rate_hz, tuple(config["shape"]),
                offset_span_hz=config["offset_span_hz"])
            for name, config in COARSE_CONFIGS.items()}


#: Rates the corpus can hold, because the survey draws between them.  Warming
#: is keyed by rate all the way down — the pilot frame, the kernel bank and its
#: tap count all change — so warming one rate leaves the other cold.
CORPUS_SAMPLE_RATES_HZ = (2_500_000.0, 5_000_000.0)


def warm(sample_rate_hz: float | tuple[float, ...] = CORPUS_SAMPLE_RATES_HZ
         ) -> float:
    """Pay every one-off cost before anything is timed.

    The fused correlation kernel is compiled on first use — 833 ms measured,
    against 50 ms for a probe — and the pilot frames are built per edge and roll.
    Charging any of that to the first candidate that happens to run would make
    the cost column, which is half of this comparison, a measurement of
    ordering.

    Every rate the corpus can hold is warmed, not just one.  The bank cache is
    keyed by rate, so warming 2.5 MS/s and then scoring a 5 MS/s probe charges
    that probe for a bank build it did not cause — which is the same defect in
    a different place.
    """
    started = time.perf_counter()
    rates = ((sample_rate_hz,) if isinstance(sample_rate_hz, (int, float))
             else tuple(sample_rate_hz))
    frames = max(fast_scan.MINIMUM_PROBE_FRAMES,
                 int(np.ceil(0.080 / STARLINK_FRAME_DURATION_S)))
    for rate in rates:
        quiet = np.zeros(int(frames * _frame_period_samples(rate)) + 64,
                         np.complex64)
        for edge in ("lower", "upper"):
            for roll in (0, CONTROL_SYMBOL_ROLL):
                edge_pilot_frame(rate, edge, symbol_roll=roll)
            for bank in _banks(edge, rate).values():
                fast_scan.probe(quiet, bank)
    return time.perf_counter() - started


def score_entry(entry: Path, *, calibration: dict | None = None,
                null_stride: int = DEFAULT_NULL_STRIDE,
                differential_counts: tuple[int, ...] = DIFFERENTIAL_SYMBOL_COUNTS,
                glrt_counts: tuple[int, ...] = GLRT_SYMBOL_COUNTS,
                anchor_count: int = ANCHOR_COUNT,
                transform_size: int = DEFAULT_TRANSFORM_SIZE) -> dict:
    """Every method on every tuning and receiver of one preserved probe.

    Raises :class:`ProbeUnusable` when the entry cannot be scored at all; the
    sweep catches that, counts it and moves on.  Anything it does produce is
    complete, because a half-scored probe would enter the comparison as a
    missing-not-at-random hole.
    """
    from .lnb_calibration import receiver_centers
    entry = Path(entry)
    manifest = json.loads((entry / "manifest.json").read_text())
    plan = tuning_plan(manifest)
    block = read_probe(entry, manifest)
    if block.shape[0] != len(plan):
        raise ProbeUnusable(
            f"sample_order names {len(plan)} tunings, IQ holds {block.shape[0]}")
    record = _survey_record(manifest)
    # The survey's own rate, never the dwell's. Everything downstream of this
    # line is scaled by it: the kernel taps, the frame grid, the epoch count
    # and every frequency reported.
    rate = survey_sample_rate_hz(manifest)
    # The bank the capture searched, settled once here from the capture's own
    # record and never re-derived from this host's constants further down.
    # Not ``bank``: the arm loop below binds that to a KernelBank.
    deployed_bank = capture_bank(record)
    # Which coarse config re-runs what the capture actually searched.  None
    # where nothing here does, which excludes the reproduction check rather
    # than answering it with a bank the capture never ran.
    reproduces = _reproduction_config(deployed_bank)
    # And over how much of the probe it ran.  The capture host scores a bounded
    # prefix and this host preserves the whole thing, so the same bank over the
    # two windows is two different measurements; the check has to use the
    # capture's window or it is measuring the draw rather than the mapping.
    scored_samples, scored_samples_source = capture_scored_samples(
        record, int(block.shape[1]))
    capture = entry.name
    identity = manifest.get("identity") or {}
    radio_id = identity.get("radio_id") or ""
    centers = tuple(receiver_centers(calibration or {}, radio_id))
    calibrated = centers != (0.0, 0.0)
    stamps, time_basis = probe_times(manifest, plan)
    truth = _load_truth(entry)

    started = time.perf_counter()
    observations = []
    for item in plan:
        edge, opposite = item["edge"], _opposite(item["edge"])
        banks = {"target": _banks(edge, rate), "null": _banks(opposite, rate)}
        deployed = {int(scored.get("receiver", -1)): scored
                    for scored in item["tuning"].get("receivers") or []}
        for receiver in range(block.shape[2]):
            samples = receiver_samples(block, item["iq_index"], receiver)
            nulled = _null_selected(capture, item["iq_index"], receiver,
                                    null_stride)
            arms = [("target", edge, banks["target"])]
            if nulled:
                arms.append(("cross-edge-null", opposite, banks["null"]))
            for arm, template_edge, bank in arms:
                searched = search_observation(
                    samples, rate, edge=template_edge, banks=bank,
                    differential_counts=differential_counts,
                    glrt_counts=glrt_counts, anchor_count=anchor_count,
                    transform_size=transform_size)
                # The one number in the sidecar that belongs to somebody else's
                # window rather than to this host's whole-probe scoring.
                reproduced = (_reproduction_coarse(samples, bank, reproduces,
                                                   scored_samples,
                                                   searched["coarse"])
                              if arm == "target" else {})
                points = distinct_points(searched["certificates"], rate)
                confirmed = confirm_points(
                    samples, rate, points, edge=template_edge,
                    # Only the target arm carries a conditioned cross-edge null:
                    # the null arm's own cross-edge template is the target edge,
                    # which would put the signal back in the null.
                    null_edge=(opposite if arm == "target" and nulled else None),
                    differential_counts=differential_counts,
                    glrt_counts=glrt_counts, anchor_count=anchor_count)
                observations.append({
                    "arm": arm, "template_edge": template_edge,
                    "null_direction": (None if arm == "target"
                                       else f"{template_edge}-on-{edge}"),
                    "iq_index": item["iq_index"], "channel": item["channel"],
                    "region": item["region"], "edge": edge,
                    "receiver": receiver,
                    "receiver_label": _label(identity, receiver),
                    "if_center_hz": item["tuning"].get("if_center_hz"),
                    "rf_center_hz": item["tuning"].get("rf_center_hz"),
                    "utc": stamps[item["iq_index"]],
                    "deployed": _deployed(deployed.get(receiver)),
                    "deployed_reproduction_delta":
                        _reproduction_delta(deployed.get(receiver), reproduced,
                                            arm, reproduces),
                    # Which bank the delta above is against, over how many
                    # samples, and — when there is no delta but there should
                    # have been — why not.  The window is recorded rather than
                    # implied because the last time it was implied it was
                    # wrong for three quarters of the arms.
                    "deployed_reproduction_bank": (
                        reproduces if arm == "target" else None),
                    "deployed_reproduction_samples": (
                        scored_samples if arm == "target" else None),
                    "deployed_reproduction_excluded":
                        _reproduction_excluded(deployed.get(receiver),
                                               reproduced, arm, deployed_bank,
                                               reproduces),
                    "coarse": searched["coarse"],
                    "certificates": searched["certificates"],
                    "points": confirmed,
                    "geometry": _geometry_for(truth, item, receiver,
                                              searched["certificates"])})
    elapsed = time.perf_counter() - started
    deltas = [item["deployed_reproduction_delta"] for item in observations
              if item["deployed_reproduction_delta"] is not None]
    excluded = [item["deployed_reproduction_excluded"] for item in observations
                if item["deployed_reproduction_excluded"]]
    return {
        "schema": SCORES_SCHEMA, "capture": capture, "radio_id": radio_id,
        "deployed_reproduction": {
            "checked": len(deltas),
            # Counted, never dropped: a check that covers half a corpus and
            # says so is evidence, and one that covers half and does not is a
            # claim about the other half it never looked at.
            "excluded": len(excluded),
            "excluded_reason": excluded[0] if excluded else None,
            "bank": reproduces,
            # Which window the deltas above were computed over, beside the one
            # the rest of the sidecar was computed over.  A check whose window
            # has to be inferred from the arm name cannot be audited, and this
            # one was wrong for three of the four arms while it was implied.
            "scored_samples": scored_samples,
            "scored_samples_source": scored_samples_source,
            "preserved_samples": int(block.shape[1]),
            "worst_delta": max(deltas) if deltas else None},
        "sample_rate_hz": rate, "samples_per_tuning": int(block.shape[1]),
        # Which arm of the randomised capture experiment this probe belongs to,
        # carried forward so the comparison can group by configuration rather
        # than pooling four of them into one column. The draw travels with it
        # so the split can be audited from the sidecars alone.
        "capture_config": record.get("capture_config"),
        "capture_experiment": record.get("experiment"),
        "pilot_guard_hz": fast_scan.pilot_guard_hz(rate),
        "coarse_configs": {name: {"shape": list(config["shape"]),
                                  "offset_span_hz": config["offset_span_hz"]}
                           for name, config in COARSE_CONFIGS.items()},
        "candidate_coarse": CANDIDATE_COARSE,
        "control_symbol_roll": CONTROL_SYMBOL_ROLL,
        "rolled_control_shift_samples": rolled_control_shift_samples(rate),
        "transform_size": int(transform_size),
        "null_stride": int(null_stride),
        # The null threshold depends on probe length — a clean null reaches p99
        # 1.310 / 1.189 / 1.137 at 20 / 40 / 80 ms — and nothing else in the
        # codebase tracks that, so every entry carries the length it was scored
        # at and the review refuses to pool two of them.
        "probe_ms": 1000.0 * block.shape[1] / rate,
        # The bank and the gate the capture itself ran, out of its own record.
        # None where the record does not say, never this host's current
        # pairing: a corpus spanning the widening holds both, and the two must
        # not arrive under one number.
        "capture_bank": deployed_bank,
        "deployed_shape": deployed_bank["shape"],
        "deployed_threshold": deployed_bank["threshold"],
        # What *this* host's fast_scan would gate on today, per coarse config,
        # named for the host it belongs to.  Worth printing beside the capture's
        # own gate and worth confusing with it never: detection_threshold has no
        # entry for (3, 8), so coarse-A reads back the 1.40 fallback that no
        # deployment has ever applied.
        "scorer_coarse_threshold": {
            name: fast_scan.detection_threshold(tuple(config["shape"]))
            for name, config in COARSE_CONFIGS.items()},
        "scorer_survey_bank": list(fast_scan.SURVEY_BANK),
        "receiver_centers_hz": [float(value) for value in centers],
        "calibration_applied": bool(calibrated),
        # Also this host's state rather than the capture's, and there is nothing
        # better in the manifest to use — so it is labelled instead of fixed.
        "calibration_source": "analysis host at scoring time; the capture "
                              "recorded no receiver centres",
        "probe_time_basis": time_basis,
        "truth_available": truth is not None,
        "observations": observations,
        "cross_receiver": cross_receiver_checks(
            observations, rate, receiver_centers=centers, calibrated=calibrated),
        "cross_edge": cross_edge_checks(observations),
        "elapsed_s": elapsed,
    }


def _opposite(edge: str) -> str:
    return "upper" if edge == "lower" else "lower"


def _label(identity: dict, receiver: int) -> str:
    labels = identity.get("receiver_labels") or []
    return str(labels[receiver]) if receiver < len(labels) else f"rx{receiver}"


def _deployed(scored: dict | None) -> dict | None:
    """The deployed detector's own numbers, travelling in the same row.

    Copied from the manifest rather than recomputed, so the baseline in the
    comparison is literally what ran on the capture host at capture time — not
    this host's reconstruction of it.
    """
    if not scored:
        return None
    return {key: scored.get(key) for key in
            ("peak_to_median", "anchor_agreement", "anchor_count", "epoch_s",
             "frequency_offset_hz", "folded_score", "folded_median", "active",
             "offset_contrast", "mean_power")}


def _reproduction_config(bank: dict) -> str | None:
    """Which coarse config, if any, re-runs the bank the capture searched.

    Shape and span both have to match, because neither alone is the
    configuration: thirteen hypotheses over +/-300 kHz is a 50 kHz grid no
    deployment has ever run.  A capture whose bank is unrecorded, or recorded
    and not among the configs this comparison runs, has no answer here — and
    None is that answer.  Falling back to a config the capture did not run
    would produce a number, which is the failure this whole change is about.
    """
    if not bank.get("known"):
        return None
    for name, config in COARSE_CONFIGS.items():
        if (list(config["shape"]) == bank["shape"]
                and float(config["offset_span_hz"]) == bank["offset_span_hz"]):
            return name
    return None


def _reproduction_coarse(samples: np.ndarray, banks: dict, config: str | None,
                         scored_samples: int, searched: dict) -> dict:
    """The capture's own bank, re-run over the window the capture host scored.

    Truncation happens here and nowhere else.  Everything else in the sidecar is
    measured over the whole preserved probe, which is the entire reason the
    probe is preserved; only the reproduction check has to match a window
    somebody else chose, and it has to match it exactly.  Two sides scoring
    different sample sets is not a check — it is a comparison of two different
    measurements, disagreeing by construction and then blaming the sample
    mapping for it, which is the permanent false alarm
    :data:`survey_comparison.REPRODUCTION_TOLERANCE` was measured to prevent.
    Measured on the 80ms-5.0MSps arm, 200,000 scored of 400,000 preserved: 16
    of 16 target observations failed, worst delta 0.1555.

    Where the capture scored the whole probe the full scan already *is* the
    right answer and is reused rather than recomputed — every entry taken
    before the randomised draw went live, 353 of 393, and the 80ms-2.5MSps arm
    besides — so this costs nothing on the corpus as it stands and one extra
    fold per target observation on the three arms that need it.
    """
    if config is None:
        return {}
    if scored_samples >= samples.size:
        return searched
    scored = fast_scan.probe(samples[:scored_samples], banks[config])
    return {config: {
        "epoch_sample": int(scored["epoch_sample"]),
        "frequency_offset_hz": float(scored["frequency_offset_hz"]),
        "peak_to_median": float(scored["peak_to_median"]),
        "folded_score": float(scored["folded_score"]),
        "folded_median": float(scored["folded_median"]),
        "mean_power": float(scored["mean_power"]),
        "samples": int(scored_samples)}}


def _reproduction_delta(scored: dict | None, coarse: dict, arm: str,
                        config: str | None) -> float | None:
    """How far re-running the capture's own bank lands from its own number.

    The deployed detector ran *some* bank on these exact samples at capture
    time and wrote the answer into the manifest.  Re-running **that** bank here
    must return the same number, and it is the only end-to-end check available
    on the whole chain in front of the comparison: the ``sample_order`` mapping,
    the reshape, the receiver index and the bank build all have to be right for
    it to hold, and every one of them fails silently otherwise.

    ``config`` is which coarse config reproduces the capture, from
    :func:`_reproduction_config`, and it is not optional.  Hard-wiring it to A
    made the check meaningless the moment the bank widened.  Measured over the
    800 target observations the corpus's sidecars already hold: against each
    capture's own bank 0 of 800 disagree by more than 1e-6, worst 5.165e-08;
    against A always, 160 of 800 do, worst 5.604e-01.  Many of those sit at
    offsets A has no hypothesis for — its three are 300 kHz apart — and the
    fold's median does the rest, since it is taken over a curve already
    maximised across the offset axis, so thirteen hypotheses lift it about 2%
    where three do not, which is why even a shared 0 Hz argmax disagrees.

    Only the target arm can be checked — the null arm deliberately scores the
    opposite edge's bank, which is a different number by design.  ``coarse`` is
    the capture's window, from :func:`_reproduction_coarse`, not this host's
    whole-probe scan.
    """
    if not _reproducible(scored, arm) or config is None or config not in coarse:
        return None
    return abs(float(coarse[config]["peak_to_median"])
               - float(scored["peak_to_median"]))


def _reproducible(scored: dict | None, arm: str) -> bool:
    """Whether this observation is one the reproduction check is about at all.

    Shared by the delta and the exclusion so the two cannot drift apart: every
    observation this returns True for lands in exactly one of the two counts,
    and every observation it returns False for lands in neither because it was
    never in scope.  A null arm deliberately scores the opposite edge's bank
    and a row with no deployed number has nothing to be checked against.
    """
    return (arm == "target" and bool(scored)
            and scored.get("peak_to_median") is not None)


def _reproduction_excluded(scored: dict | None, coarse: dict, arm: str,
                           bank: dict, config: str | None) -> str | None:
    """Why an observation that *should* be checkable was not checked.

    Excluded is not the same as failed and neither is the same as absent.  A
    null arm was never in scope and says nothing here; a target arm carrying a
    deployed number that cannot be reproduced is a hole in the corpus's
    verification, and the reader has to be told how big it is and why, or the
    reproduction statistic silently describes a subset it does not name.

    The reasons below are the exact negative of :func:`_reproduction_delta`'s
    guard, one branch for one branch.  The last of them was the hole: when the
    selected config was missing from the coarse dict the delta returned None
    and so did this, so the observation appeared in neither count and the
    denominator shrank in silence — the very failure ``excluded`` was added to
    prevent, one level further in.  It is unreachable while ``score_entry``
    computes every config for every observation, and it stops being unreachable
    the moment a config is added, removed or fails.
    """
    if not _reproducible(scored, arm):
        return None
    if not bank.get("known"):
        return "capture record does not say which bank it searched"
    if config is None:
        return (f"capture searched shape {bank['shape']} over "
                f"+/-{bank['offset_span_hz']:.0f} Hz, which this comparison "
                "does not re-run")
    if config not in coarse:
        return (f"capture searched bank {config} and this host produced no "
                f"coarse-{config} row for this observation, so there was "
                "nothing to reproduce against")
    return None


def _load_truth(entry: Path) -> dict | None:
    try:
        return json.loads((Path(entry) / "truth.json").read_text())
    except (OSError, ValueError):
        return None


def _geometry_for(truth: dict | None, item: dict, receiver: int,
                  certificates: list[dict]) -> dict | None:
    """Prior coverage priced over the span this comparison's claims come from.

    The denominator belongs to the search being priced, and every certificate
    in the row was drawn from the candidate coarse config's +/-700 kHz.  Taking
    it from the capture's own narrower span instead does not narrow what was
    searched, only what the fraction is divided by, while ``geometry_checks``
    goes on matching unclamped: 722 of 9,792 certificates on A-era captures
    carry ``|cfo_hz|`` above 300,000, the largest 694,750, so a claim outside
    the capture's span can still match a satellite that same span excluded from
    the denominator.  It does not even move the way swapping it was meant to —
    :func:`_prior_coverage` clamps its numerator too, so on a measured
    [3, 8]/300 kHz capture the fraction reads 0.3611 at 700 kHz and 0.1969 at
    300 kHz, falling 1.83x rather than rising.
    """
    if truth is None:
        return None
    for tuning in truth.get("tunings") or []:
        if tuning.get("iq_index") == item["iq_index"]:
            return geometry_checks(
                certificates, tuning.get("satellites") or [], receiver,
                span_hz=COARSE_CONFIGS[CANDIDATE_COARSE]["offset_span_hz"])
    return None


# --------------------------------------------------------------------------
# the sweep
# --------------------------------------------------------------------------

#: How the sidecar starts, so "is this already done" costs one short read.
_SCHEMA_PREFIX = f'{{"schema": "{SCORES_SCHEMA}"'


def _scored(entry: Path) -> bool:
    """Whether a finished run of *this* schema already exists.

    Presence is not enough: a sidecar from an earlier schema has to be replaced
    or the comparison silently mixes two definitions of the same column.

    Checked from the first few bytes rather than by parsing.  The sweep runs on
    every analysis job over a corpus of hundreds, and each sidecar is ~350 kB on
    a network share — parsing them all would cost more than scoring the one
    entry that actually needs it.  A file that does not open with the expected
    prefix falls back to a full parse, so a sidecar written by anything else is
    still read correctly rather than silently rescored.
    """
    path = scores_path(entry)
    try:
        with path.open("rb") as stream:
            head = stream.read(len(_SCHEMA_PREFIX))
    except OSError:
        return False
    if head == _SCHEMA_PREFIX.encode():
        return True
    try:
        return json.loads(path.read_text()).get("schema") == SCORES_SCHEMA
    except (OSError, ValueError):
        return False


def _write(entry: Path, payload: dict) -> None:
    """Publish the sidecar in one step, so a reader never sees half of one.

    Same discipline as the corpus copy it sits beside: write a ``.partial``,
    flush it to the platter, then rename.  A crash mid-write leaves the entry
    unscored, which the next sweep fixes, rather than leaving a truncated file
    that parses far enough to poison an aggregate.

    Keys are left in insertion order rather than sorted, so ``schema`` stays
    first and :func:`_scored` can recognise a finished file from its opening
    bytes; sorting would bury it behind the observation list.
    """
    temporary = scores_path(entry).with_suffix(".json.partial")
    with temporary.open("w") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, scores_path(entry))


def run(corpus_root: Path, *, calibration: dict | None = None,
        limit: int | None = None, rebuild: bool = False,
        null_stride: int = DEFAULT_NULL_STRIDE,
        maximum_seconds: float | None = None) -> dict:
    """Score every preserved probe that has not been scored yet.

    Never raises on one bad entry.  This runs beside analysis on every job, and
    a corpus that stopped growing statistics because one probe was truncated is
    a slower experiment; a job that failed because of one is a capture nobody
    analysed.  Reasons are counted rather than discarded.
    """
    root = Path(corpus_root)
    outcome = {"scored": 0, "already_scored": 0, "no_sample_order": 0,
               "unusable": 0, "failed": 0, "elapsed_s": 0.0, "warm_s": 0.0,
               "budget_reached": False, "errors": []}
    if not root.is_dir():
        return outcome
    started = time.perf_counter()
    warmed = False
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not (entry / "survey.ci16").is_file():
            continue
        if not rebuild and _scored(entry):
            outcome["already_scored"] += 1
            continue
        if limit is not None and outcome["scored"] >= limit:
            outcome["budget_reached"] = True
            break
        if (maximum_seconds is not None
                and time.perf_counter() - started >= maximum_seconds):
            outcome["budget_reached"] = True
            break
        if not warmed:
            outcome["warm_s"] = warm()
            warmed = True
        try:
            payload = score_entry(entry, calibration=calibration,
                                  null_stride=null_stride)
            _write(entry, payload)
        except ProbeUnusable as exc:
            key = ("no_sample_order" if "sample_order" in str(exc)
                   else "unusable")
            outcome[key] += 1
            _note(outcome, entry.name, exc)
            continue
        except Exception as exc:                       # noqa: BLE001 - fail open
            outcome["failed"] += 1
            _note(outcome, entry.name, exc)
            continue
        outcome["scored"] += 1
    outcome["elapsed_s"] = time.perf_counter() - started
    return outcome


def _note(outcome: dict, name: str, exc: BaseException) -> None:
    """Keep the first few reasons, because a count alone cannot be debugged."""
    if len(outcome["errors"]) < 8:
        outcome["errors"].append(
            {"entry": name, "error": f"{type(exc).__name__}: {exc}"})


def scoring_status(corpus_root: Path) -> dict:
    """What has been scored, against what the corpus holds."""
    root = Path(corpus_root)
    held = scored = eligible = 0
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or not (entry / "survey.ci16").is_file():
                continue
            held += 1
            if _scored(entry):
                scored += 1
                continue
            try:
                tuning_plan(json.loads((entry / "manifest.json").read_text()))
            except (OSError, ValueError, ProbeUnusable):
                continue
            eligible += 1
    return {"held": held, "scored": scored, "eligible_unscored": eligible,
            "schema": SCORES_SCHEMA}
