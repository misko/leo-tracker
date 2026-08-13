"""Check a detection claim's work.  Deliberately expensive, and it never searches.

Cheap detectors *search*.  What they emit is a **certificate**: a falsifiable,
localised claim — this tuning, this receiver, this epoch sample, this offset,
this method, this score.  This module is the other half of that split.  It is
handed a certificate and evaluates the most careful statistic available
**conditioned at that exact point**, and the separation is not a cost
compromise, it is statistically superior.

**Why conditioning wins.**  For an exponential-tailed power statistic the 1%
threshold sits at ``-ln(0.01) = 4.61`` times the noise mean when one cell is
tested, and at ``-ln(1 - 0.99**(1/N))`` when ``N`` are.  An exhaustive sweep of
3,333 epochs by 3,734 CFO bins is ``N = 12,444,422`` cells and wants 20.94 —
**6.6 dB higher for the same false-alarm rate** (:func:`search_penalty_db`).
Searching harder makes each peak weaker evidence, not stronger.  So a signal no
method can *acquire* may still be *confirmable* once one method proposes where
to look, and providing that is the whole reason this component exists.

**But 6.6 dB is not what this statistic pays, and the difference is measured.**
The exponential tail belongs to a *single-frame* power statistic.  The score
here averages the magnitude over ~59 frames, so its null is nearly Gaussian —
coefficient of variation 0.523/sqrt(frames), measured 0.070 — and the maximum of
a Gaussian field grows with the square root of the log of the cell count rather
than with the log.  Measured against a bounded 3,333 x 11 search on 200 null
probes, the conditioned advantage is **2.2 dB**, against 5.16 dB from the
exponential model and 1.91 dB from a Gaussian one.  The direction of the
argument holds and the architecture follows from it; the size does not, and
quoting 6.6 dB for the frame-averaged score would overstate the case by about
3 dB.  The frame-by-frame scores are in every verdict precisely so that a reader
who wants the exponential-tailed combiner — a single frame's maximum, which is
the right one under sparse occupancy — can build it and get the larger penalty
back honestly.

**Why it is affordable.**  A full-frame epoch scan over an 80 ms probe costs
~0.1 s per CFO offset here; an exhaustive +/-700 kHz sweep at 375 Hz spacing is
3,734 offsets, ~6 minutes per tuning/receiver, ~100 minutes per capture.  A
whole conditioned verdict — three symbol sets, both codes, a fifteen-cell
refinement and an epoch neighbourhood — costs about **0.25 s**.  That factor of
~24,000 per capture is why the roles must stay apart, and
:func:`exhaustive_sensitivity_bound` — which does run the sweep, for a
sensitivity upper bound and nothing else — makes you acknowledge the seconds
before it will start.

**Withheld symbols are the part that cannot be bought with more compute.**  The
300 published pilot symbols are split into disjoint ACQUIRE and VERIFY sets
(:func:`symbol_split`).  Whatever chose the candidate saw ACQUIRE; the verdict
is computed on VERIFY.  No amount of extra searching produces this, because it
is the only construction in which the confirming statistic played no part in
selection.  When a certificate does not say which symbols its proposer used, the
verdict says ``withheld: "unknown"`` rather than claiming a guarantee it cannot
check.

**The wrong-code control is only a null while the epoch is held.**  Rolling the
pilot codes by ``r`` symbols shifts the waveform by ``r`` symbol periods — 17
symbols is 187 samples at 2.5 MS/s (:func:`control_epoch_shift_samples`).  A
control that is allowed to search epoch therefore re-finds the *same real
signal* at ``epoch - 187`` and reads high; measured on the corpus its p99 is
1.851 against a cross-edge null's 1.252, correlating 0.967 with the matched
score.  Held at the claimed epoch it collapses as it should, 0.585 exact against
0.019 control.  **Every control in this module is evaluated at the claimed
epoch, and the narrow CFO refinement refines frequency only.**  Letting the
epoch move would re-open exactly that hole.

**Everything is returned, never a verdict bit.**  Per-frame scores for both
codes, both symbol sets, the refinement window and its cell count, and the
extreme-value penalty that window costs.  A downstream reader can re-threshold,
recombine frames for sparse occupancy, or discount the refinement, without
running any of this again.

Three limits on all of the above, none of which the arithmetic shows:

* **Conditioning is per test, not per world.**  Adjudicating 10,000
  certificates at a 1% conditioned threshold produces ~100 confirmations from
  noise alone.  The 6.6 dB is bought by not searching *inside* one probe; it
  says nothing about how many probes were handed over.  Whoever counts
  confirmations still owes a correction for how many certificates were issued.
* **Withheld symbols only withhold what the proposer agreed not to look at.**
  A detector that scores all 300 symbols and then hands over the epoch has
  already used VERIFY, and no field in a certificate can undo that — hence
  ``withheld: "unknown"`` unless the proposer says otherwise, and hence
  :func:`acquire_certificate`, which is the proposer that does.
* **The claim has to be right to about one sample.**  The statistic falls from
  0.45 to 0.13 one sample off the true epoch, so a certificate whose timing is
  loose is refuted rather than merely weakened.  The epoch neighbourhood is
  reported for exactly this reason, and a claim that is beaten by its own
  neighbour raises a caveat instead of being quietly re-centred.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Mapping, Sequence

import numpy as np
from scipy.signal import fftconvolve

from .injection import frame_offsets
from .pilots import OFDM_SYMBOL_DURATION_S, _edge_pilot_frame_cached
from .structure import STARLINK_FRAME_DURATION_S

#: What every report in this module calls itself.
SCHEMA = "leo-tracker.starlink-adjudication/v1"

#: Symbols carrying published pilot codes, from Qin Appendix A.
FIRST_PILOT_SYMBOL = 2
LAST_PILOT_SYMBOL = 301
PILOT_SYMBOL_COUNT = LAST_PILOT_SYMBOL - FIRST_PILOT_SYMBOL + 1

#: The same wrong-code null ``pilots.py`` and ``relative_phase.py`` use.  Valid
#: here only because the epoch is pinned; see the module docstring.
CONTROL_SYMBOL_ROLL = 17

#: Frames are combined incoherently — magnitude per frame, then averaged — so
#: the statistic decorrelates over one frame's worth of frequency, 750 Hz, no
#: matter how many frames the probe holds.  This is what makes a refinement
#: window of a few hundred hertz cost a fraction of a cell rather than many.
FRAME_FREQUENCY_RESOLUTION_HZ = 1.0 / STARLINK_FRAME_DURATION_S

#: Default refinement: one frame resolution cell wide, sampled finely enough
#: that the peak is not missed between cells.  +/-375 Hz also happens to be the
#: full-frame detector's stated confirmation tolerance and just covers the
#: +355 Hz worst-case bias ``relative_phase`` documents for its CFO estimate.
DEFAULT_REFINE_SPAN_HZ = 375.0
DEFAULT_REFINE_CELLS = 15

#: Measured on this host, an 80 ms probe: one full-probe epoch scan at one CFO
#: offset, and one whole conditioned verdict.  The scan ranges 60-240 ms
#: depending on what else the four cores are doing — two live capture services
#: share them — so the working figure is rounded up rather than taken from a
#: quiet run.  The plan's independent measurement of the same scan is 89 ms.
#: These exist to price :func:`exhaustive_sensitivity_bound` *before* it runs,
#: which is the only point at which the price still matters.
MEASURED_SCAN_SECONDS_PER_OFFSET = 0.1
MEASURED_VERDICT_SECONDS = 0.25

#: Tunings times receivers in one survey probe.  A parameter everywhere it is
#: used, because the bank shape is being changed by other work; this is only the
#: default for pricing a whole capture.
DEFAULT_CAPTURE_STREAMS = 16


# --------------------------------------------------------------------------
# The extreme-value arithmetic, which is the argument for the whole design
# --------------------------------------------------------------------------

def extreme_value_threshold(cells: float, false_alarm: float = 0.01) -> float:
    """Threshold, in multiples of the noise mean, for a maximum over ``cells``.

    Exponential tail, ``P(X > x) = exp(-x/mu)``, which is what a squared
    normalised correlation against complex Gaussian noise has.  The maximum of
    ``N`` independent cells exceeds ``x`` with probability ``1 - (1-e**-x)**N``,
    so holding that at ``false_alarm`` gives
    ``x = -ln(1 - (1-false_alarm)**(1/N))``.

    One cell at 1% is 4.61.  A 3,333-epoch by 3,734-offset sweep is 20.94.
    """
    if cells < 1:
        raise ValueError("a search covers at least one cell")
    if not 0.0 < false_alarm < 1.0:
        raise ValueError("false alarm rate must lie strictly in (0, 1)")
    survival = -math.expm1(math.log1p(-false_alarm) / float(cells))
    return -math.log(survival)


def search_penalty_db(cells: float, false_alarm: float = 0.01) -> float:
    """How much higher a searched peak must sit than a conditioned one.

    The whole architectural claim in one number: 6.57 dB for the exhaustive
    sweep the plan prices at 88 minutes a capture, and under 1 dB for the narrow
    refinement this module allows itself.

    **Read it as an upper bound on the penalty, not a measurement of it.**  The
    exponential tail is right for a single-frame power statistic; the statistic
    reported here averages the magnitude over ~59 frames, whose null is much
    lighter-tailed, so its real search penalty is smaller — measured at 2.2 dB
    where this function says 5.16 for the same space.  Overstating the penalty is
    the safe direction, since it never makes a searched claim look better than it
    is, but it must not be quoted as if it were measured.
    """
    return 10.0 * math.log10(extreme_value_threshold(cells, false_alarm) /
                             extreme_value_threshold(1, false_alarm))


def control_epoch_shift_samples(symbol_roll: int = CONTROL_SYMBOL_ROLL,
                                sample_rate_hz: float = 2_500_000.0) -> int:
    """Where a wrong-code control re-finds the true signal if it may search.

    Rolling the code matrix by ``r`` symbols shifts the transmitted waveform by
    ``r`` symbol periods, so a rolled bank free to choose its own epoch peaks at
    ``true_epoch - r * 11`` at 2.5 MS/s and reports a wrong-code score that is
    really the right code seen late.  Every control in this module is pinned to
    the claimed epoch instead; this function exists so the trap can be named in
    a test rather than described in a comment.
    """
    return int(round(int(symbol_roll) * float(sample_rate_hz) *
                     OFDM_SYMBOL_DURATION_S))


# --------------------------------------------------------------------------
# The withheld-symbol split
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SymbolSplit:
    """Which pilot symbols selected the candidate and which will judge it.

    Disjointness is the entire content: the verdict statistic must not have been
    available to whatever proposed the point.  ``mode`` is exposed because the
    two natural splits are not equivalent —

    ``interleaved``
        VERIFY is every other symbol, so it spans the same 1.33 ms as ACQUIRE.
        The two sets are exchangeable, which makes the null clean and makes both
        sets equally sensitive to a residual CFO (one frame, 750 Hz).
    ``contiguous``
        VERIFY is a trailing block, so it spans half the frame and tolerates
        about twice the CFO error — at the cost of no longer being exchangeable
        with ACQUIRE: a timing or frequency error hits the two sets differently
        and the null is only as good as that asymmetry is small.
    ``random``
        Interleaved without the regular structure, in case the code itself has a
        period that an every-other-symbol rule could align with.
    """

    acquire: np.ndarray
    verify: np.ndarray
    mode: str
    verify_fraction: float
    seed: int | None

    def __post_init__(self) -> None:
        for name in ("acquire", "verify"):
            values = np.asarray(getattr(self, name), dtype=int).ravel()
            if values.size == 0:
                raise ValueError(f"{name} symbol set is empty")
            if values.min() < FIRST_PILOT_SYMBOL or values.max() > LAST_PILOT_SYMBOL:
                raise ValueError(
                    f"pilot symbols must lie in "
                    f"{FIRST_PILOT_SYMBOL}..{LAST_PILOT_SYMBOL}")
            object.__setattr__(self, name, np.unique(values))
        if np.intersect1d(self.acquire, self.verify).size:
            raise ValueError(
                "acquire and verify symbol sets must be disjoint; overlapping "
                "sets are what the withheld construction exists to prevent")

    @property
    def all_symbols(self) -> np.ndarray:
        """Both sets together — the ordinary 300-symbol detector's view."""
        return np.union1d(self.acquire, self.verify)

    def as_dict(self) -> dict:
        return {"mode": self.mode, "verify_fraction": self.verify_fraction,
                "seed": self.seed,
                "acquire_symbols": [int(item) for item in self.acquire],
                "verify_symbols": [int(item) for item in self.verify],
                "acquire_count": int(self.acquire.size),
                "verify_count": int(self.verify.size)}


def symbol_split(mode: str = "interleaved", verify_fraction: float = 0.5, *,
                 seed: int | None = None, first: int = FIRST_PILOT_SYMBOL,
                 last: int = LAST_PILOT_SYMBOL) -> SymbolSplit:
    """Split the published pilot symbols into disjoint ACQUIRE and VERIFY sets."""
    if not 0.0 < verify_fraction < 1.0:
        raise ValueError("verify fraction must lie strictly in (0, 1)")
    if first < FIRST_PILOT_SYMBOL or last > LAST_PILOT_SYMBOL or first >= last:
        raise ValueError(
            f"pilot symbols must lie in {FIRST_PILOT_SYMBOL}..{LAST_PILOT_SYMBOL}")
    symbols = np.arange(first, last + 1)
    count = symbols.size
    wanted = int(round(count * verify_fraction))
    if not 0 < wanted < count:
        raise ValueError("the split must leave both sets non-empty")
    if mode == "interleaved":
        marks = np.floor(np.arange(1, count + 1) * verify_fraction).astype(int)
        chosen = np.flatnonzero(np.diff(np.concatenate([[0], marks])) > 0)
    elif mode == "contiguous":
        chosen = np.arange(count - wanted, count)
    elif mode == "random":
        chosen = np.random.default_rng(seed).permutation(count)[:wanted]
    else:
        raise ValueError("mode must be interleaved, contiguous or random")
    verify = symbols[np.sort(chosen)]
    acquire = np.setdiff1d(symbols, verify)
    return SymbolSplit(acquire=acquire, verify=verify, mode=mode,
                       verify_fraction=float(verify_fraction), seed=seed)


# --------------------------------------------------------------------------
# The claim being judged
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Certificate:
    """A cheap detector's falsifiable, localised claim.

    Only ``epoch_sample`` and ``cfo_hz`` are load-bearing — they are the point
    the adjudicator conditions on.  Everything else is provenance and is echoed
    into the verdict so a stored verdict answers "who said this, and what did
    they already score it at" without a join.

    ``acquire_symbols`` is the field that decides whether the withheld
    construction actually holds.  A proposer that names the symbols it used lets
    :func:`adjudicate` prove its verdict set was withheld; one that does not
    gets ``withheld: "unknown"``, which is honest rather than reassuring.
    """

    epoch_sample: int
    cfo_hz: float
    tuning: object = None
    receiver: object = None
    utc: str = ""
    method: str = ""
    score: float | None = None
    control: float | None = None
    margin: float | None = None
    edge: str = "lower"
    acquire_symbols: tuple[int, ...] | None = None

    @classmethod
    def from_mapping(cls, mapping: "Certificate | Mapping") -> "Certificate":
        """Accept a certificate object or the JSON dict one was stored as."""
        if isinstance(mapping, Certificate):
            return mapping
        if not isinstance(mapping, Mapping):
            raise TypeError("certificate must be a Certificate or a mapping")
        missing = [key for key in ("epoch_sample", "cfo_hz") if key not in mapping]
        if missing:
            raise ValueError(
                f"certificate must localise the claim; missing {missing}")
        symbols = mapping.get("acquire_symbols")
        return cls(epoch_sample=int(mapping["epoch_sample"]),
                   cfo_hz=float(mapping["cfo_hz"]),
                   tuning=mapping.get("tuning"), receiver=mapping.get("receiver"),
                   utc=str(mapping.get("utc", "")),
                   method=str(mapping.get("method", "")),
                   score=mapping.get("score"), control=mapping.get("control"),
                   margin=mapping.get("margin"),
                   edge=str(mapping.get("edge", "lower")),
                   acquire_symbols=(None if symbols is None
                                    else tuple(int(item) for item in symbols)))

    def as_dict(self) -> dict:
        return {"epoch_sample": int(self.epoch_sample),
                "cfo_hz": float(self.cfo_hz), "tuning": self.tuning,
                "receiver": self.receiver, "utc": self.utc,
                "method": self.method, "score": self.score,
                "control": self.control, "margin": self.margin,
                "edge": self.edge,
                "acquire_symbols": (None if self.acquire_symbols is None
                                    else list(self.acquire_symbols))}


# --------------------------------------------------------------------------
# The conditioned statistic
# --------------------------------------------------------------------------

def pilot_sample_indexes(sample_rate_hz: float,
                         symbols: Sequence[int] | np.ndarray) -> np.ndarray:
    """Sample offsets inside one frame covered by the chosen pilot symbols.

    Windows are ``[round(i*T), round((i+1)*T))`` — the same expression
    ``pilots._symbol_correlations`` uses, so a subset of symbols here is exactly
    the corresponding subset there rather than a nearby one.
    """
    chosen = np.asarray(symbols, dtype=int).ravel()
    if chosen.size == 0:
        raise ValueError("at least one pilot symbol is required")
    if chosen.min() < FIRST_PILOT_SYMBOL or chosen.max() > LAST_PILOT_SYMBOL:
        raise ValueError(
            f"pilot symbols must lie in {FIRST_PILOT_SYMBOL}..{LAST_PILOT_SYMBOL}")
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    symbol_period = float(sample_rate_hz) * OFDM_SYMBOL_DURATION_S
    frame_samples = round(float(sample_rate_hz) * STARLINK_FRAME_DURATION_S)
    pieces = []
    for symbol in np.unique(chosen):
        start = int(round(symbol * symbol_period))
        stop = min(int(round((symbol + 1) * symbol_period)), frame_samples)
        if stop - start < 2:
            raise ValueError(f"symbol {symbol} does not fit inside a frame")
        pieces.append(np.arange(start, stop))
    return np.concatenate(pieces)


@dataclass(frozen=True)
class ConditionedScores:
    """Normalised coherent scores at one epoch, over a grid of CFO cells.

    ``frame_scores`` is ``(offsets, frames)``:
    ``|sum_n conj(t[n]) x[m,n] e**(-j2pi f n / fs)| / sqrt(sum|t|^2 sum|x|^2)``
    with ``n`` running over the chosen symbols' samples only.  It is 1.0 for a
    noiseless replica whatever the symbol subset, which is the repository's
    ``|r^H s| / sqrt(|r|^2 |s|^2)`` convention and the reason a VERIFY score and
    an ACQUIRE score are directly comparable.

    The magnitude is taken **per frame** before averaging, so the unknown
    per-frame carrier phase Qin leaves unmodelled cancels instead of being
    summed — the same rule ``relative_phase`` follows.  A consequence worth
    knowing: the frame-start phase ``exp(-j2pi f s / fs)`` is a unit factor
    common to a frame's samples and dies under that magnitude, which is why the
    exponent below carries only the within-frame offset and why this agrees
    exactly with the FFT scan in :func:`epoch_score_scan`.
    """

    frame_scores: np.ndarray
    frequency_offsets_hz: np.ndarray
    frame_starts: np.ndarray
    epoch_sample: int
    symbols: np.ndarray
    samples_per_frame: int
    edge: str
    symbol_roll: int

    @property
    def frames(self) -> int:
        return int(self.frame_scores.shape[1])

    @property
    def mean_scores(self) -> np.ndarray:
        """Mean over frames: the statistic a verdict is read from."""
        if self.frame_scores.size == 0:
            return np.zeros(self.frequency_offsets_hz.size)
        return self.frame_scores.mean(axis=1)

    @property
    def maximum_scores(self) -> np.ndarray:
        """Best single frame.  Sparse occupancy is why this is carried."""
        if self.frame_scores.size == 0:
            return np.zeros(self.frequency_offsets_hz.size)
        return self.frame_scores.max(axis=1)


def conditioned_scores(samples: np.ndarray, sample_rate_hz: float,
                       epoch_sample: int,
                       frequency_offsets_hz: float | Sequence[float] | np.ndarray,
                       *, edge: str = "lower",
                       symbols: Sequence[int] | np.ndarray | None = None,
                       symbol_roll: int = 0) -> ConditionedScores:
    """Score the chosen pilot symbols at one fixed epoch, over given CFO cells.

    **The epoch never moves.**  Frame ``m`` starts at
    ``epoch_sample + round(m * 3333.333...)``, the fractional grid
    :func:`injection.frame_offsets` defines; truncating it to 3333 drifts twenty
    samples across an 80 ms probe, nearly two whole symbols by the last slot.
    Only frames that fit entirely are used, so no frame contributes a quietly
    shortened correlation.

    Written as an explicit gather and one matrix product: obviously correct
    first, and the reference the FFT scan is gated against.
    """
    values = np.asarray(samples)
    if not np.iscomplexobj(values):
        values = np.asarray(values, np.complex128)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    if epoch_sample < 0:
        raise ValueError("epoch sample must be nonnegative")
    offsets = np.atleast_1d(np.asarray(frequency_offsets_hz, dtype=float))
    if offsets.ndim != 1 or offsets.size == 0:
        raise ValueError("at least one frequency offset is required")
    chosen = (np.arange(FIRST_PILOT_SYMBOL, LAST_PILOT_SYMBOL + 1)
              if symbols is None else np.unique(np.asarray(symbols, dtype=int)))
    indexes = pilot_sample_indexes(sample_rate_hz, chosen)
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge,
                                        int(symbol_roll))
    selected_template = np.asarray(template[indexes], np.complex128)
    template_energy = float(np.vdot(selected_template,
                                    selected_template).real)

    span = int(indexes[-1]) + 1
    period = float(sample_rate_hz) * STARLINK_FRAME_DURATION_S
    room = values.size - int(epoch_sample) - span
    slots = int(room // period) + 1 if room >= 0 else 0
    starts = (frame_offsets(sample_rate_hz, slots) + int(epoch_sample)
              if slots > 0 else np.zeros(0, np.int64))
    starts = starts[starts + span <= values.size]

    if starts.size == 0 or template_energy <= 0:
        return ConditionedScores(
            frame_scores=np.zeros((offsets.size, 0)),
            frequency_offsets_hz=offsets, frame_starts=starts,
            epoch_sample=int(epoch_sample), symbols=chosen,
            samples_per_frame=int(indexes.size), edge=edge,
            symbol_roll=int(symbol_roll))

    gathered = np.asarray(values[starts[:, None] + indexes[None, :]],
                          np.complex128)
    energy = np.sum(np.abs(gathered) ** 2, axis=1)
    products = np.conj(selected_template)[None, :] * gathered
    phases = np.exp(-2j * np.pi * offsets[:, None] *
                    indexes[None, :] / float(sample_rate_hz))
    # einsum rather than ``@``: this host's numpy has no tuned BLAS and its
    # generic complex gemm runs ~40x slower than einsum's own loop here.
    correlations = np.einsum("mn,cn->cm", products, phases)  # (offsets, frames)
    denominator = np.sqrt(np.maximum(template_energy * energy, 1e-30))
    scores = np.abs(correlations) / denominator[None, :]
    return ConditionedScores(
        frame_scores=scores, frequency_offsets_hz=offsets, frame_starts=starts,
        epoch_sample=int(epoch_sample), symbols=chosen,
        samples_per_frame=int(indexes.size), edge=edge,
        symbol_roll=int(symbol_roll))


def _paired(samples, sample_rate_hz, epoch_sample, offsets, edge, symbols,
            control_symbol_roll):
    """One symbol set scored twice: the published code and the rolled control.

    Both at the same epoch, the same offsets and the same normalisation, so the
    margin between them is about the identity of the code and nothing else.
    """
    exact = conditioned_scores(samples, sample_rate_hz, epoch_sample, offsets,
                               edge=edge, symbols=symbols, symbol_roll=0)
    control = conditioned_scores(samples, sample_rate_hz, epoch_sample, offsets,
                                 edge=edge, symbols=symbols,
                                 symbol_roll=int(control_symbol_roll))
    return exact, control


def _block(exact: ConditionedScores, control: ConditionedScores,
           cell: int) -> dict:
    """Everything one (symbol set, CFO cell) has to say, in a JSON-able dict.

    ``frame_scores`` is carried in full because occupancy is sparse: a mean over
    59 frames dilutes a signal that lives in two of them, and a reader who wants
    a different frame combiner must not have to run any of this again.
    """
    empty = np.zeros(0)
    return {"symbols": int(exact.symbols.size),
            "samples_per_frame": exact.samples_per_frame,
            "frames": exact.frames,
            "frequency_offset_hz": float(exact.frequency_offsets_hz[cell]),
            "score": float(exact.mean_scores[cell]),
            "maximum_frame_score": float(exact.maximum_scores[cell]),
            "control_score": float(control.mean_scores[cell]),
            "control_maximum_frame_score": float(control.maximum_scores[cell]),
            "control_symbol_roll": int(control.symbol_roll),
            "margin": float(exact.mean_scores[cell] - control.mean_scores[cell]),
            "frame_scores": [float(item) for item in
                             (exact.frame_scores[cell] if exact.frames else empty)],
            "control_frame_scores": [
                float(item) for item in
                (control.frame_scores[cell] if control.frames else empty)]}


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------

def adjudicate(samples: np.ndarray, certificate: "Certificate | Mapping", *,
               sample_rate_hz: float = 2_500_000.0, edge: str | None = None,
               split: SymbolSplit | None = None,
               refine_span_hz: float = DEFAULT_REFINE_SPAN_HZ,
               refine_cells: int = DEFAULT_REFINE_CELLS,
               control_symbol_roll: int = CONTROL_SYMBOL_ROLL,
               false_alarm: float = 0.01,
               neighbourhood_samples: Sequence[int] = (-1, 1)) -> dict:
    """Judge one certificate at the point it names.  No epoch search, ever.

    Computes, all at the claimed epoch:

    * the 300-symbol full-frame conditioned score, and the ACQUIRE and VERIFY
      halves of it separately;
    * the rolled wrong-code control for each of those, at the same point;
    * a **narrow** CFO refinement of the VERIFY set around the claim.  That is a
      small search, and it is priced rather than hidden: ``refinement`` reports
      the window, the number of grid cells, how many of them are statistically
      independent given a 750 Hz frame resolution, and the decibels of
      extreme-value penalty that costs.  Frequency only — the epoch stays where
      the certificate put it, because a control free to move in time re-finds
      the real signal 187 samples early and stops being a null;
    * an epoch neighbourhood, as a **diagnostic only**.  It says whether the
      claim sits on the peak or beside it.  Taking its maximum would convert
      this module into a searcher and invalidate every threshold in it.

    The verdict is the VERIFY block.  ``withheld`` says whether that block was
    genuinely unavailable to the proposer: ``"yes"`` when the certificate names
    an ACQUIRE set disjoint from VERIFY, ``"no"`` when they overlap, and
    ``"unknown"`` when the certificate does not say — in which case the VERIFY
    score is an ordinary conditioned score and carries no protection from
    selection bias.

    Returns a plain dict so it survives a JSON round trip into whatever stores
    it.  Nothing here thresholds anything.
    """
    started = time.perf_counter()
    claim = Certificate.from_mapping(certificate)
    chosen_edge = claim.edge if edge is None else edge
    parts = split if split is not None else symbol_split()
    if refine_cells < 1:
        raise ValueError("the refinement covers at least one cell")
    if refine_span_hz < 0:
        raise ValueError("the refinement span must be nonnegative")

    caveats = []
    if claim.acquire_symbols is None:
        withheld, basis = "unknown", (
            "the certificate does not name the symbols its proposer used, so "
            "the verify set cannot be shown to have been withheld")
        caveats.append(basis)
    else:
        proposer = np.asarray(claim.acquire_symbols, dtype=int)
        overlap = np.intersect1d(proposer, parts.verify)
        if overlap.size:
            withheld, basis = "no", (
                f"the proposer used {overlap.size} of the verify symbols; the "
                "verify score took part in selection and is inflated by it")
            caveats.append(basis)
        else:
            withheld, basis = "yes", (
                "the proposer's symbols are disjoint from the verify set")

    refine_offsets = (claim.cfo_hz + np.linspace(-refine_span_hz, refine_span_hz,
                                                 int(refine_cells))
                      if refine_cells > 1 else np.array([claim.cfo_hz]))
    point = np.array([claim.cfo_hz])
    # The claim is cell 0 and the refinement grid follows it, so one pass over
    # the verify symbols yields both the conditioned verdict and its refinement
    # and the two are guaranteed to be the same frames and the same normaliser.
    verify_pair = _paired(samples, sample_rate_hz, claim.epoch_sample,
                          np.concatenate([point, refine_offsets]), chosen_edge,
                          parts.verify, control_symbol_roll)
    blocks = {
        "full": _block(*_paired(samples, sample_rate_hz, claim.epoch_sample,
                                point, chosen_edge, parts.all_symbols,
                                control_symbol_roll), 0),
        "acquire": _block(*_paired(samples, sample_rate_hz, claim.epoch_sample,
                                   point, chosen_edge, parts.acquire,
                                   control_symbol_roll), 0),
        "verify": _block(*verify_pair, 0),
    }
    best = 1 + int(np.argmax(verify_pair[0].mean_scores[1:]))
    refined = _block(*verify_pair, best)
    total_span = float(refine_offsets[-1] - refine_offsets[0])
    independent = 1.0 + total_span / FRAME_FREQUENCY_RESOLUTION_HZ
    refined.update({
        "window_hz": [float(refine_offsets[0]), float(refine_offsets[-1])],
        "cells": int(refine_offsets.size),
        "cell_spacing_hz": (float(total_span / (refine_offsets.size - 1))
                            if refine_offsets.size > 1 else 0.0),
        "frame_frequency_resolution_hz": FRAME_FREQUENCY_RESOLUTION_HZ,
        "independent_cells": independent,
        "grid_penalty_db": search_penalty_db(refine_offsets.size, false_alarm),
        "search_penalty_db": search_penalty_db(independent, false_alarm),
        "residual_frequency_offset_hz": (refined["frequency_offset_hz"] -
                                         claim.cfo_hz),
        "cell_scores": [float(item) for item in
                        verify_pair[0].mean_scores[1:]],
        "control_cell_scores": [float(item) for item in
                                verify_pair[1].mean_scores[1:]],
        "searched": "frequency only; the epoch is fixed at the claim"})

    neighbourhood = []
    for shift in neighbourhood_samples:
        epoch = int(claim.epoch_sample) + int(shift)
        if epoch < 0:
            continue
        block = _block(*_paired(samples, sample_rate_hz, epoch, point,
                                chosen_edge, parts.verify,
                                control_symbol_roll), 0)
        neighbourhood.append({"epoch_offset_samples": int(shift),
                              "score": block["score"],
                              "control_score": block["control_score"]})

    if blocks["verify"]["frames"] == 0:
        caveats.append("no complete frame fits after the claimed epoch; every "
                       "score is zero because there is no evidence, not "
                       "because the claim was refuted")
    beaten = [item for item in neighbourhood
              if item["score"] > blocks["verify"]["score"]]
    if beaten:
        caveats.append(
            f"a neighbouring epoch scores higher than the claim "
            f"({max(item['score'] for item in beaten):.4f} at "
            f"{min(item['epoch_offset_samples'] for item in beaten):+d} "
            "samples); the claim's timing is probably off, and this verdict "
            "refutes the claim as given rather than the signal behind it. "
            "Re-centring it here would be an epoch search")
    if refined["score"] > blocks["verify"]["score"]:
        caveats.append(
            f"the refinement lifted the verify score by "
            f"{refined['score'] - blocks['verify']['score']:.4f}; that lift "
            f"bought {refined['search_penalty_db']:.2f} dB of threshold and "
            "must not be read as a conditioned result")
    return {"schema": SCHEMA, "edge": chosen_edge,
            "certificate": claim.as_dict(),
            "epoch_sample": int(claim.epoch_sample),
            "claimed_frequency_offset_hz": float(claim.cfo_hz),
            "sample_rate_hz": float(sample_rate_hz),
            "frames": blocks["full"]["frames"],
            "split": parts.as_dict(), "withheld": withheld,
            "withheld_basis": basis,
            "verdict_basis": ("the verify block, conditioned at the claimed "
                              "epoch and offset; every other block is context"),
            "conditioned": blocks, "refinement": refined,
            "epoch_neighbourhood": neighbourhood,
            "neighbourhood_basis": ("diagnostic only: taking its maximum would "
                                    "make this a search and void the "
                                    "conditioned thresholds"),
            "conditioned_penalty_db": 0.0,
            "penalty_false_alarm_rate": float(false_alarm),
            "caveats": caveats,
            "cost_ms": (time.perf_counter() - started) * 1000.0}


# --------------------------------------------------------------------------
# The searching half, quarantined
# --------------------------------------------------------------------------

def epoch_score_scan(samples: np.ndarray, sample_rate_hz: float,
                     frequency_offset_hz: float, *, edge: str = "lower",
                     symbols: Sequence[int] | np.ndarray | None = None,
                     symbol_roll: int = 0,
                     epoch_count: int | None = None) -> dict:
    """**This searches.**  Every epoch in one frame period, at one CFO offset.

    It is here for three jobs and no others: to let the withheld construction
    select a candidate on ACQUIRE alone (:func:`acquire_certificate`), to price
    the exhaustive sensitivity bound, and to be the searched arm of a fair
    conditioned-versus-searched comparison.  Its output is a *proposal*.  It is
    never a verdict, and a threshold calibrated for a conditioned test does not
    apply to it — that is the whole 6.6 dB argument.

    Never run it with a rolled control template on real IQ and call the answer a
    null: a rolled bank free to choose its epoch re-finds the true signal at
    ``epoch - 187`` samples.

    Mathematically identical to calling :func:`conditioned_scores` at every
    epoch — the CFO rotation is folded into the template so one FFT correlation
    serves all epochs — and there is a test that pins the agreement.  It runs in
    single precision, as the rest of this repository's search paths do, so the
    agreement is to about 1e-6 relative rather than to machine epsilon; the
    double-precision reference is what a verdict is read from.
    """
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("samples must be one dimensional")
    chosen = (np.arange(FIRST_PILOT_SYMBOL, LAST_PILOT_SYMBOL + 1)
              if symbols is None else np.unique(np.asarray(symbols, dtype=int)))
    indexes = pilot_sample_indexes(sample_rate_hz, chosen)
    span = int(indexes[-1]) + 1
    period = float(sample_rate_hz) * STARLINK_FRAME_DURATION_S
    epochs = int(round(period)) if epoch_count is None else int(epoch_count)
    if epochs < 1:
        raise ValueError("at least one epoch must be scanned")

    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge,
                                        int(symbol_roll))
    masked = np.zeros(span, np.complex64)
    masked[indexes] = template[indexes]
    weights = np.zeros(span, np.float32)
    weights[indexes] = 1.0
    # Rotating the template rather than the probe is what makes one correlation
    # cover every epoch: the leftover exp(-j2pi f s / fs) is constant across a
    # frame and dies under the per-frame magnitude.
    masked = (masked * np.exp(2j * np.pi * float(frequency_offset_hz) *
                              np.arange(span) / float(sample_rate_hz))
              ).astype(np.complex64)
    template_energy = float(np.vdot(masked, masked).real)

    if values.size < span + epochs - 1:
        raise ValueError("probe is too short to scan a whole frame period")
    correlation = fftconvolve(values, np.conj(masked[::-1]), mode="valid")
    energy = fftconvolve(np.abs(values) ** 2, weights[::-1], mode="valid")

    usable = correlation.size - epochs        # last epoch's last frame must fit
    slots = int(usable // period) + 1
    starts = frame_offsets(sample_rate_hz, max(slots, 1))
    starts = starts[starts + epochs - 1 < correlation.size]
    if starts.size == 0:
        raise ValueError("probe holds no complete frame for every epoch")
    positions = np.arange(epochs)[:, None] + starts[None, :]
    scores = (np.abs(correlation[positions]) /
              np.sqrt(np.maximum(template_energy * energy[positions], 1e-30)))
    folded = scores.mean(axis=1)
    best = int(np.argmax(folded))
    return {"schema": SCHEMA, "searched": "epoch", "edge": edge,
            "symbol_roll": int(symbol_roll),
            "frequency_offset_hz": float(frequency_offset_hz),
            "epochs": int(epochs), "frames": int(starts.size),
            "symbols": int(chosen.size), "scores": folded,
            "epoch_sample": best, "score": float(folded[best]),
            "median_score": float(np.median(folded)),
            "peak_to_median": float(folded[best] / max(np.median(folded), 1e-30))}


def acquire_certificate(samples: np.ndarray, sample_rate_hz: float, *,
                        split: SymbolSplit | None = None,
                        frequency_offsets_hz: Sequence[float] = (0.0,),
                        edge: str = "lower", tuning: object = None,
                        receiver: object = None, utc: str = "") -> Certificate:
    """Propose an epoch using the ACQUIRE symbols only.  **This searches.**

    The other half of the withheld construction, and the only way to get a
    certificate whose ``withheld`` field :func:`adjudicate` can answer ``"yes"``
    to.  It looks at ACQUIRE and nothing else, so the VERIFY symbols remain
    genuinely unused until the verdict is computed — which is the one property
    no amount of additional search can manufacture.
    """
    parts = split if split is not None else symbol_split()
    best = None
    for offset in frequency_offsets_hz:
        found = epoch_score_scan(samples, sample_rate_hz, float(offset),
                                 edge=edge, symbols=parts.acquire)
        if best is None or found["score"] > best["score"]:
            best = found
    return Certificate(epoch_sample=int(best["epoch_sample"]),
                       cfo_hz=float(best["frequency_offset_hz"]),
                       tuning=tuning, receiver=receiver, utc=utc,
                       method="adjudicate.acquire-epoch-scan",
                       score=float(best["score"]), edge=edge,
                       acquire_symbols=tuple(int(i) for i in parts.acquire))


# --------------------------------------------------------------------------
# The exhaustive upper bound, priced before it runs
# --------------------------------------------------------------------------

def exhaustive_cost_estimate(sample_count: int,
                             frequency_offsets_hz: Sequence[float] | np.ndarray,
                             *, sample_rate_hz: float = 2_500_000.0,
                             seconds_per_offset: float =
                             MEASURED_SCAN_SECONDS_PER_OFFSET,
                             verdict_seconds: float = MEASURED_VERDICT_SECONDS,
                             streams: int = DEFAULT_CAPTURE_STREAMS) -> dict:
    """What an exhaustive sweep would cost, in seconds and in threshold.

    Cheap, and the thing to call first.  A +/-700 kHz sweep at 375 Hz spacing is
    3,734 offsets: minutes for one tuning and receiver, and there are sixteen of
    those in a capture — an hour and a half or so.  The same evidence
    conditioned at one certificate costs a fifth of a second, and the sweep's
    peak has to clear a threshold 6.6 dB higher to mean the same thing.  Those
    two facts together are the whole argument for splitting search from
    adjudication.
    """
    offsets = np.asarray(frequency_offsets_hz, dtype=float).ravel()
    if offsets.size == 0:
        raise ValueError("at least one frequency offset is required")
    if verdict_seconds <= 0 or seconds_per_offset <= 0 or streams < 1:
        raise ValueError("costs must be positive and a capture holds a stream")
    epochs = int(round(float(sample_rate_hz) * STARLINK_FRAME_DURATION_S))
    cells = float(epochs) * float(offsets.size)
    scale = max(float(sample_count) / (0.08 * float(sample_rate_hz)), 0.0)
    seconds = float(seconds_per_offset) * offsets.size * scale
    return {"schema": SCHEMA, "offsets": int(offsets.size), "epochs": epochs,
            "cells": cells, "seconds": seconds,
            "capture_seconds": seconds * int(streams), "streams": int(streams),
            "seconds_per_offset": float(seconds_per_offset),
            "verdict_seconds": float(verdict_seconds),
            "cost_ratio": seconds * int(streams) / float(verdict_seconds),
            "search_penalty_db": search_penalty_db(cells),
            "basis": ("measured full-probe scan cost, scaled linearly in probe "
                      "length from the 80 ms probe it was measured on")}


def exhaustive_sensitivity_bound(samples: np.ndarray, sample_rate_hz: float, *,
                                 frequency_offsets_hz: Sequence[float] | np.ndarray,
                                 acknowledge_cost_seconds: float,
                                 edge: str = "lower",
                                 symbols: Sequence[int] | np.ndarray | None = None,
                                 control_symbol_roll: int = CONTROL_SYMBOL_ROLL,
                                 false_alarm: float = 0.01) -> dict:
    """**Minutes to hours per probe.**  A sensitivity ceiling, never a detector.

    Answers one question — *what is the most that could possibly be found in
    this probe?* — by scanning every epoch at every offset given.  At the plan's
    +/-700 kHz by 375 Hz that is 3,734 offsets, several minutes per
    tuning/receiver and **an hour and a half per capture**, against a fifth of a
    second for the same evidence conditioned at a certificate.  It is for a
    handful of probes chosen by hand.  **Never call it in a loop, and never over
    a corpus.**

    Because the cost is the entire reason this function is dangerous, it refuses
    to run until you pass ``acknowledge_cost_seconds`` at least as large as
    :func:`exhaustive_cost_estimate` predicts.  Call that first; it is free.

    What comes back is not comparable with a conditioned verdict at face value.
    The peak is a maximum over ``epochs * offsets`` cells and must clear a
    threshold ``search_penalty_db`` higher for the same false-alarm rate — 6.6 dB
    at full span.  The control is computed **conditioned at the winning epoch**,
    never searched, because a rolled template allowed to pick its own epoch
    re-finds the true signal 187 samples early and reports it as a false alarm.
    """
    offsets = np.asarray(frequency_offsets_hz, dtype=float).ravel()
    values = np.asarray(samples)
    estimate = exhaustive_cost_estimate(values.size, offsets,
                                        sample_rate_hz=sample_rate_hz)
    if float(acknowledge_cost_seconds) < estimate["seconds"]:
        raise ValueError(
            f"this sweep is estimated at {estimate['seconds']:.0f} s for one "
            f"tuning and receiver ({estimate['offsets']} offsets), "
            f"{estimate['capture_seconds'] / 60:.0f} minutes for a whole "
            f"capture; pass acknowledge_cost_seconds >= "
            f"{estimate['seconds']:.0f} if that is genuinely intended. "
            "Conditioned adjudication of a certificate costs "
            f"{estimate['verdict_seconds'] * 1000:.0f} ms.")

    started = time.perf_counter()
    best, curve = None, []
    for offset in offsets:
        found = epoch_score_scan(values, sample_rate_hz, float(offset),
                                 edge=edge, symbols=symbols)
        curve.append(float(found["score"]))
        if best is None or found["score"] > best["score"]:
            best = found
    control = conditioned_scores(values, sample_rate_hz, best["epoch_sample"],
                                 np.array([best["frequency_offset_hz"]]),
                                 edge=edge, symbols=symbols,
                                 symbol_roll=int(control_symbol_roll))
    cells = float(best["epochs"]) * float(offsets.size)
    return {"schema": SCHEMA, "edge": edge, "searched": "epoch and frequency",
            "epoch_sample": int(best["epoch_sample"]),
            "frequency_offset_hz": float(best["frequency_offset_hz"]),
            "score": float(best["score"]),
            "control_score": float(control.mean_scores[0]),
            "control_basis": ("conditioned at the winning epoch; a searched "
                              "control re-finds the true signal 187 samples "
                              "early and is not a null"),
            "margin": float(best["score"] - control.mean_scores[0]),
            "frames": int(best["frames"]), "cells": cells,
            "offsets": int(offsets.size), "epochs": int(best["epochs"]),
            "per_offset_peak_scores": curve,
            "search_penalty_db": search_penalty_db(cells, false_alarm),
            "conditioned_equivalent_seconds": estimate["verdict_seconds"],
            "estimated_seconds": estimate["seconds"],
            "elapsed_seconds": time.perf_counter() - started}
