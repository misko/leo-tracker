"""Phase-invariant pilot detectors, written to be obviously correct.

The deployed survey detector correlates a handful of anchor symbols, throws the
phase away with ``|z|**2``, and folds the powers.  Everything below keeps the
complex value instead, and the reason it is allowed to is algebra rather than
measurement.

For frame ``m`` and pilot symbol ``i`` the matched correlation against the known
4QAM edge-pilot symbol is

    z[m,i] ~ A[m,i] * exp(j*(phi_m + 2*pi*df*t_i))

where ``phi_m`` is that frame's absolute carrier phase.  Qin models ``phi_m`` as
unmodelled and reports that attempts to generalise it failed, which is why raw
frame amplitudes may never be summed.  But

    d[m,i] = z[m,i+1] * conj(z[m,i]) = A[m,i+1]*A[m,i] * exp(j*2*pi*df*T_sym)

cancels ``phi_m`` *exactly*, and ``S(f) = |sum_i z_i exp(-j*2*pi*f*t_i)|**2`` is
invariant to a common phase on every term.  So evidence can be accumulated
across frames whose absolute phases are unrelated, provided the phase is
cancelled first, and residual CFO can be *estimated* from a few dozen complex
values instead of *searched* over ~800 hypotheses of raw IQ.

**The ambiguity is the price.**  Adjacent-symbol phase is sampled once per
``T_sym = 4.4 us``, so every estimate here is periodic at 227.3 kHz and unique
only within +/-113.6 kHz (:data:`AMBIGUITY_HALF_SPAN_HZ`).  A coarse stage in
front of these is mandatory, not optional.  Config E's 116.7 kHz hypothesis
spacing leaves a worst nearest-bin residual of 58.3 kHz, comfortably inside.

**These are reference implementations: correct first, fast never.**  Plain numpy
and plain loops, so that an optimised version can be gated against them.  That
gate is what stops optimisation quietly becoming redesign.

**Amplitude weighting is the primitive.**  Each ``z`` enters weighted by its own
matched-filter strength; ``phase_only`` normalises to unit magnitude first and is
offered only because it is the obvious thing to try and was reported worse.

Three places the algebra does not survive contact with the signal, each measured
here and each with a test:

* **The 8-anchor candidate is not cheap.**  Spread over 300 symbols, its
  ``S(f)`` is a comb with ~200 Hz teeth repeating every 5.32 kHz, so at a fixed
  frequency it needs a coarse CFO as good as the 300-symbol detector needs.
  Searching finds a tooth for any residual and cannot say which one.
* **The offset estimate reads ~0.5% high.**  Each symbol's matched filter has
  its phase centre wherever its 4QAM code puts it — 2.26 to 7.62 samples into
  an 11-sample window — and across a contiguous run that centre drifts, which
  is indistinguishable from extra time between symbols.
* **Wrapping is silent.**  A residual outside the window is reported 227 kHz
  wrong with the score barely dented; only the coarse stage prevents it.

Provenance: the separation ratios that motivated this family were measured
outside this repository, on a corpus labelled by the detector being replaced.
Nothing here is evidence for them.  The injection harness is what settles it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .pilots import OFDM_SYMBOL_DURATION_S, _edge_pilot_frame_cached
from .structure import STARLINK_FRAME_DURATION_S

#: What every report in this module calls itself.
SCHEMA = "leo-tracker.starlink-relative-phase/v1"

#: Adjacent-symbol phase is one sample of the carrier every ``T_sym``, so any
#: estimate built from it repeats at this rate.  Nothing in this module can
#: distinguish ``f`` from ``f + AMBIGUITY_SPAN_HZ``; that is the coarse stage's
#: job and there is a regression test pinning the fact.
AMBIGUITY_SPAN_HZ = 1.0 / OFDM_SYMBOL_DURATION_S
AMBIGUITY_HALF_SPAN_HZ = AMBIGUITY_SPAN_HZ / 2.0

#: Same wrong-code null ``pilots.py`` already uses.  Rolling the pilot sequence
#: in time keeps the modulation, the bandwidth and the frame structure and
#: destroys only the identity of the code, which is what separates "this IQ
#: matches Qin's published sequence" from "this IQ carries OFDM-like energy".
CONTROL_SYMBOL_ROLL = 17

#: The eight spread anchors ``fast_scan.SURVEY_BANK`` already correlates.  The
#: 8-anchor detector's only structural change from the deployed one is not
#: taking the magnitude.
SURVEY_ANCHOR_COUNT = 8

#: Frequency bins the GLRT lays across one ambiguity period.  External work
#: reports detection quality nearly flat from 256 to 2048 with finer sizes
#: buying only CFO precision; it is a parameter so that can be checked here.
DEFAULT_TRANSFORM_SIZE = 512

#: Symbols carrying published pilot codes, from Qin Appendix A.
FIRST_PILOT_SYMBOL = 2
LAST_PILOT_SYMBOL = 301


def survey_anchor_symbols(count: int = SURVEY_ANCHOR_COUNT) -> np.ndarray:
    """The anchor symbols the deployed survey bank uses, spelled the same way.

    Duplicated from :func:`fast_scan.build_bank` rather than imported because
    importing would couple this module to a file another change is editing, and
    the expression is one line whose agreement is worth a test instead.
    """
    if count <= 0:
        raise ValueError("anchor count must be positive")
    return np.unique(np.rint(
        np.linspace(FIRST_PILOT_SYMBOL, LAST_PILOT_SYMBOL, count)).astype(int))


def anchor_alias_spacing_hz(count: int = SURVEY_ANCHOR_COUNT) -> float:
    """How far apart ``S(f)`` repeats itself when only spread anchors are kept.

    Measured at 5,320 Hz for the eight survey anchors, whose mean spacing is
    42.7 symbols.  The spacings are 43,42,43,43,43,42,43 rather than uniform, so
    the repeats are not perfect — and they are close enough that it does not
    help: the first alias reaches 0.998 of the true peak.
    """
    anchors = survey_anchor_symbols(count)
    if anchors.size < 2:
        return AMBIGUITY_SPAN_HZ
    return float(1.0 / (np.mean(np.diff(anchors)) * OFDM_SYMBOL_DURATION_S))


def contiguous_symbols(count: int, first: int = FIRST_PILOT_SYMBOL) -> np.ndarray:
    """``count`` consecutive pilot symbols, which is what a differential needs.

    Adjacent products and the FFT-style search both assume a uniform time grid.
    A spread anchor set violates that: the eight survey anchors sit ~43 symbols
    apart, so a transform over them alias-folds every 5.3 kHz.
    """
    if count < 2:
        raise ValueError("at least two symbols are required")
    if first < FIRST_PILOT_SYMBOL or first + count - 1 > LAST_PILOT_SYMBOL:
        raise ValueError(
            f"symbols must lie in {FIRST_PILOT_SYMBOL}..{LAST_PILOT_SYMBOL}")
    return np.arange(first, first + count)


@dataclass(frozen=True)
class SymbolCorrelations:
    """Complex per-symbol matched-filter output, the shared primitive.

    ``values`` is ``z[m,i]`` for frame opportunity ``m`` and requested symbol
    ``i``, raw and unnormalised: every statistic downstream is a ratio, so the
    receiver gain divides out and nothing is gained by scaling here.

    ``times_s`` is the absolute time of each correlation window's centre, which
    is the phase reference the residual-CFO ramp is measured against.  A frame
    opportunity is kept only when every requested symbol fits entirely inside
    the probe, so the array is rectangular and no partial frame contributes a
    quietly shortened correlation.
    """

    values: np.ndarray
    times_s: np.ndarray
    symbols: np.ndarray
    frame_starts: np.ndarray
    sample_rate_hz: float
    epoch_sample: int
    frequency_offset_hz: float
    edge: str
    symbol_roll: int
    phase_only: bool

    @property
    def frames(self) -> int:
        return int(self.values.shape[0])

    @property
    def symbol_count(self) -> int:
        return int(self.values.shape[1])

    @property
    def lags_s(self) -> np.ndarray:
        """Time of each symbol relative to the first symbol of its own frame.

        Frames differ only by a common phase, which every statistic here is
        invariant to, so measuring within the frame keeps the exponent
        arguments small instead of letting them run to tens of thousands of
        radians across an 80 ms probe.
        """
        if self.values.size == 0:
            return self.times_s
        return self.times_s - self.times_s[:, :1]

    @property
    def symbol_step_s(self) -> float:
        """Measured spacing between adjacent requested symbols.

        Taken from the samples rather than assumed, because the window starts
        are rounded to integer samples and only a rate that makes the symbol an
        exact number of samples — 11, at 2.5 MS/s — guarantees the nominal
        4.4 us.
        """
        if self.values.size == 0 or self.values.shape[1] < 2:
            return float(OFDM_SYMBOL_DURATION_S)
        return float(np.median(np.diff(self.times_s, axis=1)))


def pilot_correlations(samples: np.ndarray, sample_rate_hz: float,
                       epoch_sample: int, frequency_offset_hz: float, *,
                       edge: str = "lower", symbols: np.ndarray | None = None,
                       symbol_roll: int = 0,
                       phase_only: bool = False) -> SymbolCorrelations:
    """Correlate chosen pilot symbols at a candidate epoch and coarse CFO.

    This is what the deployed detector computes and then destroys with
    ``|z|**2``.  Everything else in this module is a way of combining what comes
    back.

    The frame grid is the fractional one: slot ``m`` starts at
    ``epoch + round(m * 3333.333...)``, matching :func:`injection.frame_offsets`.
    Truncating it to 3333 drifts twenty samples across an 80 ms probe, which is
    nearly two whole symbols by the last slot.
    """
    values_in = np.asarray(samples, np.complex128)
    if values_in.ndim != 1:
        raise ValueError("samples must be one dimensional")
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    if epoch_sample < 0:
        raise ValueError("epoch sample must be nonnegative")
    chosen = (survey_anchor_symbols() if symbols is None
              else np.asarray(symbols, dtype=int).ravel())
    if chosen.size == 0:
        raise ValueError("at least one pilot symbol is required")
    if chosen.min() < FIRST_PILOT_SYMBOL or chosen.max() > LAST_PILOT_SYMBOL:
        raise ValueError(
            f"pilot symbols must lie in {FIRST_PILOT_SYMBOL}..{LAST_PILOT_SYMBOL}")
    if np.any(np.diff(chosen) <= 0):
        raise ValueError("pilot symbols must be strictly increasing")

    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge,
                                        int(symbol_roll))
    period = float(sample_rate_hz) * STARLINK_FRAME_DURATION_S
    symbol_period = float(sample_rate_hz) * OFDM_SYMBOL_DURATION_S

    rows, moments, starts = [], [], []
    frame = 0
    while True:
        frame_start = int(epoch_sample) + int(round(frame * period))
        if frame_start >= values_in.size:
            break
        row, moment, complete = [], [], True
        for symbol in chosen:
            local_start = int(round(symbol * symbol_period))
            local_stop = min(int(round((symbol + 1) * symbol_period)),
                             template.size)
            count = local_stop - local_start
            start = frame_start + local_start
            if count < 2 or start + count > values_in.size:
                complete = False
                break
            index = np.arange(start, start + count, dtype=float)
            local = np.asarray(template[local_start:local_stop], np.complex128)
            corrected = values_in[start:start + count] * np.exp(
                -2j * np.pi * float(frequency_offset_hz) * index / sample_rate_hz)
            row.append(complex(np.vdot(local, corrected)))
            moment.append((start + (count - 1) / 2) / sample_rate_hz)
        if not complete:
            break
        rows.append(row)
        moments.append(moment)
        starts.append(frame_start)
        frame += 1

    shape = (len(rows), chosen.size)
    values = (np.asarray(rows, np.complex128) if rows
              else np.zeros(shape, np.complex128))
    times = (np.asarray(moments, float) if moments else np.zeros(shape, float))
    if phase_only:
        magnitude = np.abs(values)
        values = np.divide(values, magnitude, out=np.zeros_like(values),
                           where=magnitude > 0)
    return SymbolCorrelations(
        values=values, times_s=times, symbols=chosen,
        frame_starts=np.asarray(starts, np.int64),
        sample_rate_hz=float(sample_rate_hz), epoch_sample=int(epoch_sample),
        frequency_offset_hz=float(frequency_offset_hz), edge=edge,
        symbol_roll=int(symbol_roll), phase_only=bool(phase_only))


def coherent_ceiling(correlations: SymbolCorrelations) -> float:
    """The value ``S(f)`` reaches when every symbol in every frame agrees.

    ``sum_m (sum_i |z[m,i]|)**2``.  Dividing by it turns every score in this
    module into a fraction of the perfectly-aligned case, so a noiseless
    replica reads 1.0 rather than a number that depends on gain, probe length
    and symbol count — which is the same convention the plan's testing section
    asks of the coherent detector.
    """
    if correlations.values.size == 0:
        return 0.0
    return float(np.sum(np.sum(np.abs(correlations.values), axis=1) ** 2))


def frame_spectrum(correlations: SymbolCorrelations,
                   frequencies_hz: np.ndarray) -> np.ndarray:
    """``S(f) = sum_m |sum_i z[m,i] exp(-j2pi f t[m,i])|**2``, one entry per ``f``.

    The magnitude is taken *inside* the frame sum.  That is the whole point: a
    frame's unknown ``phi_m`` is common to all of its symbols, so it survives
    the inner sum as a unit-magnitude factor and dies under ``|.|**2``.  Summing
    the inner sums instead would be the forbidden direct complex-amplitude
    integration across frames.

    A frequency at a time: 512 small numpy calls read better than one large
    einsum, and this exists to be checked by eye.
    """
    frequencies = np.asarray(frequencies_hz, float).ravel()
    total = np.zeros(frequencies.size, float)
    if correlations.values.size == 0:
        return total
    lags = correlations.lags_s
    for index, frequency in enumerate(frequencies):
        rotated = correlations.values * np.exp(-2j * np.pi * frequency * lags)
        total[index] = float(np.sum(np.abs(np.sum(rotated, axis=1)) ** 2))
    return total


def _shared(correlations: SymbolCorrelations) -> dict:
    """Fields every report in this family carries, so they compare directly."""
    magnitude = np.abs(correlations.values)
    return {"schema": SCHEMA, "edge": correlations.edge,
            "epoch_sample": correlations.epoch_sample,
            "coarse_frequency_offset_hz": correlations.frequency_offset_hz,
            "frames": correlations.frames,
            "symbols": correlations.symbol_count,
            "symbol_indexes": [int(item) for item in correlations.symbols],
            "symbol_step_s": correlations.symbol_step_s,
            "ambiguity_span_hz": AMBIGUITY_SPAN_HZ,
            "phase_only": correlations.phase_only,
            "mean_correlation_magnitude":
                float(magnitude.mean()) if magnitude.size else 0.0}


def _with_control(exact: dict, control: dict) -> dict:
    """Report the wrong-code difference, because the level alone means little.

    An exact score of 0.4 is evidence only if the rolled sequence does not also
    reach 0.4 on the same IQ at the same epoch and the same CFO.  The control
    shares the interference environment, the gain and the search, so the
    difference is the part that is about Qin's code specifically.
    """
    merged = dict(exact)
    merged["control_score"] = control["score"]
    merged["margin"] = exact["score"] - control["score"]
    merged["control_residual_frequency_offset_hz"] = control[
        "residual_frequency_offset_hz"]
    merged["control_symbol_roll"] = control["symbol_roll"]
    return merged


def _anchor_once(samples, sample_rate_hz, epoch_sample, frequency_offset_hz,
                 edge, anchor_count, residual_hz, search, transform_size,
                 symbol_roll, phase_only) -> dict:
    correlations = pilot_correlations(
        samples, sample_rate_hz, epoch_sample, frequency_offset_hz, edge=edge,
        symbols=survey_anchor_symbols(anchor_count), symbol_roll=symbol_roll,
        phase_only=phase_only)
    ceiling = coherent_ceiling(correlations)
    if search:
        # Anchors sit at integer symbol indexes, so S(f) still repeats at
        # 1/T_sym; the transform only has to be fine enough to resolve a
        # ~200 Hz tooth, which 4096 bins over the period does at 55 Hz.
        grid = np.fft.fftfreq(int(transform_size), d=OFDM_SYMBOL_DURATION_S)
        spectrum = frame_spectrum(correlations, grid)
        best = int(np.argmax(spectrum)) if spectrum.size else 0
        power = float(spectrum[best]) if spectrum.size else 0.0
        residual = float(grid[best]) if spectrum.size else 0.0
    else:
        power = float(frame_spectrum(correlations, np.array([residual_hz]))[0])
        residual = float(residual_hz)
    return {**_shared(correlations), "detector": "anchor-relative-phase",
            "symbol_roll": int(symbol_roll),
            "score": power / ceiling if ceiling > 0 else 0.0,
            "residual_frequency_offset_hz": residual,
            "frequency_offset_hz": float(frequency_offset_hz) + residual,
            "searched": bool(search), "refines_cfo": False,
            "cfo_alias_spacing_hz": anchor_alias_spacing_hz(anchor_count)}


def anchor_relative_phase(samples: np.ndarray, sample_rate_hz: float,
                          epoch_sample: int, frequency_offset_hz: float, *,
                          edge: str = "lower",
                          anchor_count: int = SURVEY_ANCHOR_COUNT,
                          residual_hz: float = 0.0, search: bool = False,
                          transform_size: int = 8 * DEFAULT_TRANSFORM_SIZE,
                          control_symbol_roll: int = CONTROL_SYMBOL_ROLL,
                          phase_only: bool = False) -> dict:
    """Score the survey's own eight anchors without discarding their phase.

    The cheapest candidate in the bake-off, because the correlations already
    exist in the deployed path: the only structural change is keeping ``z``
    instead of ``|z|**2``.

    **It never refines the CFO, and ``refines_cfo`` says so whichever way it is
    called.**  The anchors are spread over 300 symbols, so ``S(f)`` is a comb
    with teeth ~200 Hz wide repeating every ``anchor_alias_spacing_hz`` —
    5,320 Hz for the standard eight, whose first alias reaches 0.998 of the true
    peak.  Two consequences, both measured and both worth knowing before this
    candidate is called cheap:

    * left at ``residual_hz=0`` it is not a detector for an arbitrary residual.
      It scores 1.00 at 0 Hz, 0.74 at 200 Hz and 0.02 at 750 Hz, so it needs a
      coarse CFO already good to about ``+/-200 Hz`` — the same tolerance as
      the 300-symbol full-frame detector, and therefore the same search cost the
      relative-phase idea was supposed to avoid.
    * with ``search=True`` it recovers a score near 1.0 for any residual,
      because some tooth of the comb always fits.  The frequency that tooth sits
      at is then one of ~43 equally good answers and must not be used as a CFO.
    """
    exact = _anchor_once(samples, sample_rate_hz, epoch_sample,
                         frequency_offset_hz, edge, anchor_count, residual_hz,
                         search, transform_size, 0, phase_only)
    control = _anchor_once(samples, sample_rate_hz, epoch_sample,
                           frequency_offset_hz, edge, anchor_count, residual_hz,
                           search, transform_size, int(control_symbol_roll),
                           phase_only)
    return _with_control(exact, control)


def _differential_once(samples, sample_rate_hz, epoch_sample,
                       frequency_offset_hz, edge, symbol_count, first_symbol,
                       symbol_roll, phase_only) -> dict:
    correlations = pilot_correlations(
        samples, sample_rate_hz, epoch_sample, frequency_offset_hz, edge=edge,
        symbols=contiguous_symbols(symbol_count, first_symbol),
        symbol_roll=symbol_roll, phase_only=phase_only)
    values = correlations.values
    leading, trailing = values[:, 1:], values[:, :-1]
    products = leading * np.conj(trailing)
    total = complex(np.sum(products))
    weight = float(np.sum(np.abs(leading) * np.abs(trailing)))
    step = correlations.symbol_step_s
    residual = float(np.angle(total) / (2 * np.pi * step)) if total != 0 else 0.0
    return {**_shared(correlations), "detector": f"differential-{symbol_count}",
            "symbol_roll": int(symbol_roll),
            "score": abs(total) / weight if weight > 0 else 0.0,
            "pairs": int(products.size),
            "residual_frequency_offset_hz": residual,
            "frequency_offset_hz": float(frequency_offset_hz) + residual,
            "refines_cfo": True}


def adjacent_differential(samples: np.ndarray, sample_rate_hz: float,
                          epoch_sample: int, frequency_offset_hz: float, *,
                          edge: str = "lower", symbol_count: int = 32,
                          first_symbol: int = FIRST_PILOT_SYMBOL,
                          control_symbol_roll: int = CONTROL_SYMBOL_ROLL,
                          phase_only: bool = False) -> dict:
    """Sum adjacent-symbol products across every symbol of every frame.

    ``D = sum_m sum_i z[m,i+1] conj(z[m,i])``.  Each term carries
    ``exp(j 2pi df T_sym)`` and no ``phi_m`` at all, so terms from frames with
    unrelated absolute phase add directionally rather than cancelling — which
    is the property the plan wants tested against sparse occupancy, where the
    ``sum |z|**2`` fold can only ever add noise.

    ``score`` is ``|D|`` over ``sum |z[m,i+1]||z[m,i]|``, so it is 1.0 when every
    product points the same way and falls to ~1/sqrt(pairs) on noise.  The
    estimate ``angle(D) / (2 pi T_sym)`` is a single-step phase measurement and
    therefore ambiguous outside +/-113.6 kHz.

    The estimate also reads about **0.5% high**, and that is a property of the
    published code rather than of this implementation.  ``T_sym`` assumes each
    symbol's matched filter has its phase centre at the middle of its window;
    the real ones sit anywhere from 2.26 to 7.62 samples into an 11-sample
    window, and across symbols 2..33 the centre drifts +0.055 samples per
    symbol.  That drift is indistinguishable from extra time between symbols.
    It is deterministic, so it could be calibrated out of a production
    implementation — it is left in here so a reference and an optimised version
    are comparing the same arithmetic.
    """
    exact = _differential_once(samples, sample_rate_hz, epoch_sample,
                               frequency_offset_hz, edge, symbol_count,
                               first_symbol, 0, phase_only)
    control = _differential_once(samples, sample_rate_hz, epoch_sample,
                                 frequency_offset_hz, edge, symbol_count,
                                 first_symbol, int(control_symbol_roll),
                                 phase_only)
    return _with_control(exact, control)


def _glrt_once(samples, sample_rate_hz, epoch_sample, frequency_offset_hz, edge,
               symbol_count, first_symbol, transform_size, symbol_roll,
               phase_only) -> dict:
    correlations = pilot_correlations(
        samples, sample_rate_hz, epoch_sample, frequency_offset_hz, edge=edge,
        symbols=contiguous_symbols(symbol_count, first_symbol),
        symbol_roll=symbol_roll, phase_only=phase_only)
    step = correlations.symbol_step_s
    grid = np.fft.fftfreq(int(transform_size), d=step)
    spectrum = frame_spectrum(correlations, grid)
    ceiling = coherent_ceiling(correlations)
    normalised = spectrum / ceiling if ceiling > 0 else spectrum
    best = int(np.argmax(normalised)) if normalised.size else 0
    residual = float(grid[best]) if normalised.size else 0.0
    median = float(np.median(normalised)) if normalised.size else 0.0
    return {**_shared(correlations), "detector": f"glrt-{symbol_count}",
            "symbol_roll": int(symbol_roll),
            "score": float(normalised[best]) if normalised.size else 0.0,
            "residual_frequency_offset_hz": residual,
            "frequency_offset_hz": float(frequency_offset_hz) + residual,
            "refines_cfo": True,
            "transform_size": int(transform_size),
            "frequency_resolution_hz": float(1.0 / (transform_size * step)),
            "searched_span_hz": [float(grid.min()), float(grid.max())],
            "spectrum_median": median,
            "peak_to_median": (float(normalised[best]) / median
                               if median > 0 else 0.0)}


def symbol_glrt(samples: np.ndarray, sample_rate_hz: float, epoch_sample: int,
                frequency_offset_hz: float, *, edge: str = "lower",
                symbol_count: int = 32,
                first_symbol: int = FIRST_PILOT_SYMBOL,
                transform_size: int = DEFAULT_TRANSFORM_SIZE,
                control_symbol_roll: int = CONTROL_SYMBOL_ROLL,
                phase_only: bool = False) -> dict:
    """Maximise ``S(f)`` over one full ambiguity period of residual CFO.

    The generalised likelihood ratio for an unknown residual offset: pick the
    ``f`` the data likes best and report the peak as the statistic.  The grid is
    ``fftfreq(transform_size, T_sym)``, which covers exactly
    ``[-113.6, +113.6) kHz`` — one period, no more, because a second period
    would be the same hypothesis wearing a different label.

    Evaluated as an explicit sum rather than an FFT.  It is the same arithmetic
    on a uniform symbol grid and it stays correct when the grid is not uniform,
    which is what a reference implementation is for.

    Searching buys a free parameter, so the wrong-code control searches too:
    both peaks are maxima over the same 512 bins and only their difference is
    about the code.

    Carries the same ~0.4% high bias as :func:`adjacent_differential`, from the
    same per-symbol phase centres, weighted differently because this fits a ramp
    across the whole run rather than averaging adjacent steps.
    """
    exact = _glrt_once(samples, sample_rate_hz, epoch_sample,
                       frequency_offset_hz, edge, symbol_count, first_symbol,
                       transform_size, 0, phase_only)
    control = _glrt_once(samples, sample_rate_hz, epoch_sample,
                         frequency_offset_hz, edge, symbol_count, first_symbol,
                         transform_size, int(control_symbol_roll), phase_only)
    return _with_control(exact, control)


def score_family(samples: np.ndarray, sample_rate_hz: float, epoch_sample: int,
                 frequency_offset_hz: float, *, edge: str = "lower",
                 differential_symbol_counts: tuple[int, ...] = (16, 32),
                 glrt_symbol_counts: tuple[int, ...] = (32,),
                 transform_size: int = DEFAULT_TRANSFORM_SIZE,
                 anchor_count: int = SURVEY_ANCHOR_COUNT,
                 anchor_search: bool = False,
                 control_symbol_roll: int = CONTROL_SYMBOL_ROLL,
                 phase_only: bool = False) -> dict:
    """Run the whole family at one candidate, keyed by detector name.

    The bake-off scores every candidate on the same epoch and CFO from the same
    coarse stage, so running them together is how they are meant to be read.
    """
    reports = [anchor_relative_phase(
        samples, sample_rate_hz, epoch_sample, frequency_offset_hz, edge=edge,
        anchor_count=anchor_count, search=anchor_search,
        control_symbol_roll=control_symbol_roll, phase_only=phase_only)]
    for count in differential_symbol_counts:
        reports.append(adjacent_differential(
            samples, sample_rate_hz, epoch_sample, frequency_offset_hz,
            edge=edge, symbol_count=count,
            control_symbol_roll=control_symbol_roll, phase_only=phase_only))
    for count in glrt_symbol_counts:
        reports.append(symbol_glrt(
            samples, sample_rate_hz, epoch_sample, frequency_offset_hz,
            edge=edge, symbol_count=count, transform_size=transform_size,
            control_symbol_roll=control_symbol_roll, phase_only=phase_only))
    return {"schema": SCHEMA, "edge": edge, "epoch_sample": int(epoch_sample),
            "coarse_frequency_offset_hz": float(frequency_offset_hz),
            "detectors": {report["detector"]: report for report in reports}}
