"""Fast multi-channel edge-pilot scanning and dwell verification.

The reference acquisition in :mod:`.pilots` searches every frame epoch against
every anchor symbol at every frequency hypothesis.  That is the right search to
run once a capture is worth analysing, but it costs roughly forty times real
time, so a survey of the eight low-band edge tunings would spend longer
computing than the satellite stays on a channel.

This module keeps the same search and the same statistic, and makes it fast
enough to run in the capture loop:

  - the frequency hypothesis is carried by the eleven-tap kernel rather than by
    rotating the signal, because ``|corr(x . e^-jwt, k)| == |corr(x, k .
    e^+jwm)|`` and the residual per-lag phase disappears under the magnitude;
  - the energy normaliser is computed once per probe, since multiplying by a
    unit-magnitude rotator leaves ``|x|`` unchanged;
  - correlation, magnitude and folding are fused in a C kernel so the
    full-length correlation is never stored.

Two bank sizes are offered.  ``SURVEY_BANK`` is the first pass used to decide
which channel to dwell on; ``FULL_BANK`` matches the reference search exactly.

The survey bank was three hypotheses over +/-300 kHz until measurement caught
up with it.  Three hypotheses over that span leaves 300 kHz between
neighbours, and an injection sweep put the largest spacing that still holds
Pd >= 0.9 at -12 dB at 150 kHz, so the bank was half as fine as the weakest
signal it claimed to find needed.  It now spreads thirteen over +/-700 kHz.
That is 3.76 times the arithmetic and none of the radio time; the survey's
wall clock goes from a field-measured 1.7 s to about 3.1 s against the 120 s
dwell it precedes.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np

import math

from .channels import starlink_edge_pilot_if_hz
from .pilots import OFDM_SYMBOL_DURATION_S, _edge_pilot_frame_cached
from .structure import STARLINK_FRAME_DURATION_S

#: First pass used while surveying: frequency hypotheses x anchor symbols.
#: Thirteen hypotheses across :data:`SURVEY_OFFSET_SPAN_HZ` put neighbouring
#: hypotheses 116.7 kHz apart.  Spacing, not span, is what this buys: an
#: injection sweep of 14,608 trials found that holding Pd >= 0.9 everywhere
#: needs <=150 kHz spacing at -12 dB, and -12 dB is where the detector stops
#: working at all.  ``Pd < 0.5 at worst CFO`` moves from -5.4 dB at three
#: hypotheses to -13.3 dB at thirteen.
#:
#: On real sky, 1,696 windows from 106 corpus captures, each bank judged at its
#: own measured 1% false-alarm rate: three hypotheses fire on 9.7% / 7.6% of
#: lnb-a / lnb-b observations against 24.3% / 20.4% for thirteen.  Those two
#: ports carry no LNB bias, so their pilots sat inside the old span already and
#: **span cannot explain the gain** -- spacing is what it is.  The biased port
#: gains most, as a span argument predicts and a spacing argument does not:
#: lnb-c goes 3.1% to 11.8%.
SURVEY_BANK = (13, 8)
#: Full search, identical in coverage to the reference acquisition.
FULL_BANK = (7, 24)
#: Doppler search span the *survey* uses, stated here rather than taken from
#: :data:`DEFAULT_OFFSET_SPAN_HZ` because those two numbers answer different
#: questions and moving one must not move the other.
#:
#: A 3-day sweep of the whole catalogue against this site -- 63,035,467
#: satellite-instants -- puts all-sky Doppler p99.9 at 279,059 Hz on the top
#: tuning, LNB bias uncertainty at 19,346 Hz and the margin to closed form at
#: 21,595 Hz, so +/-320 kHz is the requirement.  The survey searches +/-700 kHz
#: because it is buying *spacing*: thirteen hypotheses is what -12 dB needs,
#: and once thirteen are paid for, widening the span to cover a biased port as
#: well is free.
SURVEY_OFFSET_SPAN_HZ = 700_000.0
#: :func:`build_bank`'s default span.  **Do not move this to follow the
#: survey.**  :data:`FULL_BANK` spreads seven hypotheses across it, which
#: reproduces the reference acquisition's own
#: ``arange(-300_000, 300_001, 100_000)`` grid exactly; widening the default
#: would silently coarsen that agreement to 233 kHz spacing and the fast path
#: would stop being an optimisation of the reference.
DEFAULT_OFFSET_SPAN_HZ = 300_000.0
#: Threads for the fused kernel.  Three leaves a core for the capture reader.
DEFAULT_THREADS = 3
#: Frames a probe must contain for the epoch fold to mean anything.
MINIMUM_PROBE_FRAMES = 4

#: The survey bank's 1% point, measured on the sky.  See :data:`NOISE_CEILING`
#: for the construction and :data:`SURVEY_NULL_REALISATIONS` for what it rests
#: on.  Quoted to three decimals because the fourth is not resolved: the
#: bootstrap 90% interval on the 99th percentile is [1.2444, 1.2570].
#:
#: Restricting to exactly the direction an earlier and independent measurement
#: used -- a *lower*-edge bank on an *upper*-edge tuning, 848 of the 1,696 --
#: gives 1.2547, reproducing that measurement's 1.255 to three figures.  The
#: same run reproduces its 1.289 for the retired bank as 1.280, and its real-
#: sky fire rates for that bank on lnb-a and lnb-b, 9.0% and 7.0%, as 8.6% and
#: 6.9%.  Both directions are used here because the deployed bank runs on both
#: edges and the larger population is the better estimate.
SURVEY_NOISE_CEILING = 1.252
#: Clean null windows the ceiling above was measured over, drawn from 106
#: corpus captures whose manifests record ``sample_order`` -- 8 tunings x 2
#: receivers each, scored in both cross-edge directions.  The empirical
#: quantile of N samples cannot speak below roughly 3/N, so this population
#: supports a 1% rate comfortably, a 0.2% rate marginally, and nothing finer.
#: A *production* false-alarm figure for the whole search still needs its own
#: harness; this is a comparison threshold measured honestly, not that.
SURVEY_NULL_REALISATIONS = 1_696
SURVEY_NULL_CAPTURES = 106
#: The probe length the ceiling was measured at.  **It is not transferable to
#: another length.**  The fold is incoherent across frames, so the statistic
#: tightens as more of them are averaged: on synthetic Gaussian noise the
#: 99th percentile of this bank runs 1.310 at 20 ms, 1.189 at 40 ms and 1.137
#: at 80 ms.  One fixed bar therefore realises quite different false-alarm
#: rates at different lengths.  :func:`verify_presence` applies this ceiling
#: to whatever chunk it is handed and has no way to know the difference; that
#: is a real gap and it is recorded here rather than papered over.
SURVEY_CEILING_PROBE_S = 0.080
#: The sample rate the ceiling was measured at, and the second axis the same
#: argument runs along.  Rate changes the taps in every kernel (11 at 2.5 MS/s,
#: 22 at 5 MS/s), the number of epochs the fold maximises over (3,333 against
#: 6,667) and how much of the spectrum is noise rather than signal, so a bar
#: measured at one rate is no more transferable across rates than across
#: lengths.  The 1,696 clean windows behind :data:`SURVEY_NOISE_CEILING` were
#: all taken here.
SURVEY_CEILING_SAMPLE_RATE_HZ = 2_500_000.0

#: The eight published edge-pilot subcarriers, 234.375 kHz apart, span this
#: much.  It is a property of the signal, not of the receiver, which is why the
#: guard either side of it is a function of the sample rate alone.
PILOT_BANDWIDTH_HZ = 1_875_000.0


def pilot_guard_hz(sample_rate_hz: float) -> float:
    """Frequency room either side of the pilot band before subcarriers are lost.

    312.5 kHz at 2.5 MS/s and 1,562.5 kHz at 5 MS/s.  This is the whole reason
    5 MS/s is worth measuring: an LNB whose absolute error exceeds the guard
    slides pilot subcarriers past Nyquist and loses them, 0.58 dB for the first
    of eight and gracefully worse after that, and no amount of searching
    recovers a band that was never sampled.  Every port on this site sits
    inside 312.5 kHz *except* lnb-c at +434 kHz, which is exactly the port the
    span argument has always been about.
    """
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    return (float(sample_rate_hz) - PILOT_BANDWIDTH_HZ) / 2.0

#: Peak-to-median a probe must beat, per bank shape, for a 1% false alarm rate.
#:
#: :data:`SURVEY_BANK` is calibrated on the sky, not on synthetic noise, using
#: windows that are **target-pilot-free by construction**: a lower-edge bank
#: scored on an upper-edge tuning, whose pilot codes sit 230.6 MHz away.  No
#: window is screened on the statistic being calibrated, so no real detection
#: can be charged to the false-alarm budget.
#:
#: The construction matters more than the number.  Synthetic Gaussian noise is
#: not this distribution: at 80 ms its 99th percentile for this bank is 1.137
#: where the sky's is 1.252, because the sky carries interference and Gaussian
#: noise does not.  A same-edge population is not it either, in the other
#: direction, because 8-26% of those windows hold real pilots and charging a
#: detection to the false-alarm budget inflates the bar.
#:
#: The retired 1.33 was inflated that way, and by more than was thought: on
#: this clean null it realises **0.06%** false alarms rather than the 1% it
#: claimed, so it was roughly seventeen times too strict rather than too lax.
#: Adopting 1.252 is therefore a sensitivity change in its own right, on top
#: of the bank -- the retired bank at 1.33 fires on 7.8% of real-sky windows,
#: this bank at 1.252 on 21.0%.
#:
#: :data:`FULL_BANK`'s 1.20 predates all of this and is the 99th percentile of
#: synthetic Gaussian noise over 60 realisations.  It has not been re-measured
#: against the sky and should not be read as comparably grounded.
NOISE_CEILING = {SURVEY_BANK: SURVEY_NOISE_CEILING, FULL_BANK: 1.20}
#: Used when a bank shape has not been characterised; deliberately strict.
FALLBACK_NOISE_CEILING = 1.40


def detection_threshold(shape: tuple[int, int]) -> float:
    """Peak-to-median a probe must beat for this bank shape.

    Keyed by shape alone, so it is only the right answer for a bank searching
    the span its ceiling was measured at; :data:`SURVEY_PROFILE` is what pairs
    :data:`SURVEY_BANK` with :data:`SURVEY_OFFSET_SPAN_HZ`.

    Shape is not the whole configuration either: see :func:`survey_threshold`,
    which is what a *survey* should call, because the same shape scored over a
    different probe length or at a different sample rate does not share this
    bar.
    """
    return NOISE_CEILING.get(tuple(shape), FALLBACK_NOISE_CEILING)


def survey_threshold(shape: tuple[int, int], *, probe_s: float,
                     sample_rate_hz: float) -> dict:
    """The bar, plus whether it describes the configuration it is applied to.

    Returns the number **and** a ``calibrated`` flag, because those are two
    different facts and only one of them can be inferred from the other.  A
    threshold always exists; whether it means 1% false alarms in *this*
    configuration is a claim that rests on where the null population was drawn,
    and that population was drawn at exactly one probe length and one rate.

    The reason this is a function rather than a table is that the table cannot
    be written yet.  Characterising the other three configurations needs a null
    population taken in them, and producing that population is what the
    randomised survey exists to do.  So the honest order is randomise,
    accumulate, then calibrate -- and until the third step, a caller that would
    have stored ``active: false`` must store nothing instead.  A boolean that
    means "below a bar calibrated somewhere else" is not the same fact as
    ``active: false`` and must not share a column with it.
    """
    value = detection_threshold(shape)
    matches = (abs(float(probe_s) - SURVEY_CEILING_PROBE_S) < 1e-9
               and abs(float(sample_rate_hz) - SURVEY_CEILING_SAMPLE_RATE_HZ) < 1e-6)
    if matches:
        basis = (
            f"{value:.3f}: the 1% false-alarm point of the peak-to-median "
            f"statistic for a {shape[0]}x{shape[1]} bank, measured on real sky "
            f"over {SURVEY_NULL_REALISATIONS} target-pilot-free windows drawn "
            f"from {SURVEY_NULL_CAPTURES} captures -- a lower-edge bank scored "
            "on an upper-edge tuning, whose pilot codes sit 230.6 MHz away. No "
            "window was screened on the statistic being calibrated. Measured "
            f"at {SURVEY_CEILING_PROBE_S * 1000:.0f} ms and "
            f"{SURVEY_CEILING_SAMPLE_RATE_HZ / 1e6:.1f} MS/s, which is the "
            "configuration this probe was scored in")
    else:
        basis = (
            f"UNCALIBRATED at this configuration. The score was taken over "
            f"{float(probe_s) * 1000:.0f} ms at "
            f"{float(sample_rate_hz) / 1e6:.1f} MS/s; the only measured 1% "
            f"point for this bank, {value:.3f}, was taken over "
            f"{SURVEY_CEILING_PROBE_S * 1000:.0f} ms at "
            f"{SURVEY_CEILING_SAMPLE_RATE_HZ / 1e6:.1f} MS/s over "
            f"{SURVEY_NULL_REALISATIONS} clean windows. The statistic is known "
            "to move with probe length -- p99 1.310 / 1.189 / 1.137 at 20 / 40 "
            "/ 80 ms on synthetic noise -- and moves with rate too, because "
            "rate sets the kernel taps and the epoch count. No verdict is "
            "emitted here: the score is kept and the bar is left to a null "
            "population taken in this configuration, which is what randomising "
            "the configuration is accumulating")
    return {"threshold": value, "calibrated": matches, "basis": basis,
            "probe_s": float(probe_s), "sample_rate_hz": float(sample_rate_hz),
            "calibrated_probe_s": SURVEY_CEILING_PROBE_S,
            "calibrated_sample_rate_hz": SURVEY_CEILING_SAMPLE_RATE_HZ,
            "null_realisations": SURVEY_NULL_REALISATIONS}


#: Median peak-to-median of the **retired** three-hypothesis survey bank
#: against a beacon carrying a fixed frequency offset, over six realisations at
#: each point.  Kept because it is the measurement that made the case for
#: widening: a receiver whose LNB sat about 436 kHz from its twin scored 1.38
#: at -6 dB against that bank's 1.33 threshold, where the same beacon
#: on-centre scored 2.58.
#:
#: The reason it is history rather than a live caveat is that its two named
#: fixes have both been taken.  The bank now carries thirteen hypotheses over
#: +/-700 kHz, so 436 kHz sits 31 kHz from a hypothesis instead of 136 kHz from
#: one, and the same measurement repeated on that bank reads 2.46 / 2.41 / 2.37
#: at 0 / 200 / 436 kHz -- flat, where this fell by half.  Both open items --
#: "needs its own noise-ceiling characterisation and a re-measured compute
#: constant" -- are what :data:`SURVEY_NOISE_CEILING` and
#: ``ScanProfile.compute_ms_per_probe_s`` now are.  Do not read these numbers
#: as describing what the survey does today.
RETIRED_3X8_OFFSET_SENSITIVITY_DB6 = {0: 2.58, 200_000: 1.78, 436_000: 1.38}

_SOURCE = Path(__file__).with_name("_scan_kernel.c")
_CFLAGS = ["-O3", "-march=native", "-ffast-math", "-funroll-loops",
           "-fopenmp", "-fPIC", "-shared"]


class ScanKernelUnavailable(RuntimeError):
    """Raised when the fused kernel cannot be built for this machine."""


def _library_path() -> Path:
    cache = Path(os.environ.get("LEO_SCAN_CACHE",
                                Path(tempfile.gettempdir()) / "leo-scan-kernel"))
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"libleoscan-{sys.platform}-{os.uname().machine}.so"


@lru_cache(maxsize=1)
def _load_kernel():
    """Compile the fused kernel on first use and return the bound library.

    Building on demand keeps the package installable without a compiler; the
    caller can fall back to the reference path when this raises.
    """
    target = _library_path()
    if not target.is_file() or target.stat().st_mtime < _SOURCE.stat().st_mtime:
        compiler = os.environ.get("CC", "cc")
        try:
            subprocess.run([compiler, *_CFLAGS, str(_SOURCE), "-o", str(target)],
                           check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", b"")
            raise ScanKernelUnavailable(
                f"cannot build {_SOURCE.name}: "
                f"{detail.decode(errors='replace')[:400] or exc}") from exc
    library = ctypes.CDLL(str(target))
    f32 = np.ctypeslib.ndpointer(np.float32, flags="C_CONTIGUOUS")
    i32 = np.ctypeslib.ndpointer(np.int32, flags="C_CONTIGUOUS")
    f64 = np.ctypeslib.ndpointer(np.float64, flags="C_CONTIGUOUS")
    library.leo_fold_correlate.restype = None
    library.leo_fold_correlate.argtypes = [
        f32, f32, ctypes.c_int, f32, f32, ctypes.c_int, ctypes.c_int,
        f32, f32, i32, i32, ctypes.c_int, ctypes.c_int, f64, ctypes.c_int]
    return library


@dataclass(frozen=True)
class ScanProfile:
    """How a survey trades radio time for sensitivity.

    A survey is not a short dwell.  A dwell sizes its buffers for throughput
    over two minutes, where a 105 ms block costs nothing; a survey pays that
    block three times per tuning to look at 20 ms, so the same setting that is
    right for one is eight times too coarse for the other.  Keeping the choice
    here, with its cost model, stops a survey from silently inheriting the
    capture path's block size again.

    Settle is expressed in buffers because that is what the radio charges for:
    discarding two 105 ms blocks costs 210 ms whatever the part actually needs.
    """

    block_size: int = 50_000
    settle_buffers: int = 0
    kernel_buffers: int = 1
    probe_s: float = 0.020
    shape: tuple[int, int] = SURVEY_BANK
    #: The span the ``shape`` hypotheses are spread across.  A profile that
    #: named only the shape would leave the spacing -- the thing the shape is
    #: bought for -- undetermined.
    offset_span_hz: float = SURVEY_OFFSET_SPAN_HZ
    #: Measured on a Pi 5 over a persistent USB context, writing a *different*
    #: frequency each time.  Rewriting the same value costs 0.5 ms because the
    #: driver skips the reprogram, which flatters a benchmark that hops nowhere.
    tune_ms: float = 6.1
    #: A refill costs its nominal duration once the queue is deep enough to
    #: prefetch.  At depth one nothing is prefetched, so each refill also pays
    #: the transfer it can no longer overlap.
    shallow_refill_ms: float = 12.8
    #: Scoring costs a fixed part plus a part that grows with the bank, both
    #: per second of probe.  The fixed part is the energy normaliser and the
    #: running-power cumsum, which every probe pays whatever it is searching.
    #:
    #: Anchored on the field for its level and on this host for its shape.
    #: Field: 234 deployed surveys of the 24-kernel bank report a median
    #: 512.5 ms of scoring for 16 probes of 80 ms -- 400.4 ms per probe-second,
    #: which these two constants reproduce exactly at 24 kernels.  Host: 80 ms
    #: probes on a Pi 5 at three threads, best of nine, measured 28.2 ms at 24
    #: kernels and 106.2 ms at 104, a ratio of **3.76x** rather than the 4.33x
    #: a pure per-kernel model would predict.  Ignoring the fixed part would
    #: have overstated the new bank's cost by 15%.
    compute_ms_per_probe_s_fixed: float = 68.0
    compute_ms_per_probe_s_per_kernel: float = 13.85

    def __post_init__(self) -> None:
        if self.block_size < 1 or self.settle_buffers < 0 or self.probe_s <= 0:
            raise ValueError("block size, settle count and probe must be positive")
        if self.kernel_buffers < 1:
            raise ValueError("kernel buffer depth must be positive")
        # The driver keeps `kernel_buffers` in flight, so when a tuning changes
        # the first `kernel_buffers - 1` were already filled at the old setting.
        # Measured directly with a 44 dB gain step: depth 4 leaves 3 stale, 2
        # leaves 1, and 1 leaves none. Discarding fewer than that reads samples
        # from the previous tuning and reports them as this one's.
        if self.settle_buffers < self.kernel_buffers - 1:
            raise ValueError(
                f"settle_buffers={self.settle_buffers} cannot drain a "
                f"kernel_buffers={self.kernel_buffers} queue; "
                f"need at least {self.kernel_buffers - 1}")

    def buffer_s(self, sample_rate_hz: float) -> float:
        return self.block_size / float(sample_rate_hz)

    @property
    def kernel_count(self) -> int:
        """Correlations one probe runs: hypotheses times anchor symbols."""
        return int(self.shape[0]) * int(self.shape[1])

    @property
    def compute_ms_per_probe_s(self) -> float:
        """Scoring cost per second of probe, for *this* profile's bank.

        Derived rather than declared.  It read 400.0 for years because that is
        what the deployed 24-kernel bank costs, and a bank of another size
        would have inherited the number without inheriting the measurement.
        """
        return (self.compute_ms_per_probe_s_fixed
                + self.compute_ms_per_probe_s_per_kernel * self.kernel_count)

    def cost_ms(self, sample_rate_hz: float = 2_500_000.0) -> dict:
        """Predicted per-tuning cost, split into what the radio charges for.

        Listening is quantised: a probe shorter than one buffer still costs a
        whole buffer, which is the entire reason block size dominates.

        Scoring is charged for **one** probe.  The deployed survey scores both
        receivers off the same capture, so its host time is twice this while
        its radio time is exactly this; the two field measurements this model
        is pinned to were taken against one probe and the convention is kept so
        they still refer to something.
        """
        buffer_s = self.buffer_s(sample_rate_hz)
        listens = max(1, math.ceil(self.probe_s / buffer_s))
        settle = self.settle_buffers * buffer_s * 1000.0
        listen = listens * buffer_s * 1000.0
        if self.kernel_buffers < 2:
            listen += listens * self.shallow_refill_ms
        compute = self.probe_s * self.compute_ms_per_probe_s
        return {"tune_ms": self.tune_ms, "settle_ms": settle,
                "listen_ms": listen, "compute_ms": compute,
                "total_ms": self.tune_ms + settle + listen + compute,
                "buffers_listened": listens, "kernel_count": self.kernel_count,
                "signal_used_fraction": self.probe_s / (listens * buffer_s)}

    def sweep_ms(self, tunings: int = 8,
                 sample_rate_hz: float = 2_500_000.0) -> float:
        return tunings * self.cost_ms(sample_rate_hz)["total_ms"]


#: The bank the two field cost measurements below were taken against: three
#: hypotheses over +/-300 kHz by eight anchors, 24 kernels.  It is no longer
#: what the survey searches, and it is named anyway, because a measurement
#: belongs to the configuration it was made on.  Pointing those profiles at
#: whatever the survey currently uses would silently restate a field number as
#: a claim about hardware that was never run in that configuration.
MEASURED_COST_BANK = (3, 8)
MEASURED_COST_SPAN_HZ = 300_000.0

#: What the capture path uses.  Correct for a 120 s dwell, eight times too
#: coarse for a survey; kept so the comparison is explicit rather than implied.
DWELL_PROFILE = ScanProfile(block_size=262_144, settle_buffers=3,
                            kernel_buffers=4, shape=MEASURED_COST_BANK,
                            offset_span_hz=MEASURED_COST_SPAN_HZ)
#: A survey retunes between every read, so it has nothing to gain from the
#: driver's read-ahead and everything to lose: at depth 1 no buffer is
#: pre-filled, so there is no stale data and no settle to pay for. Sizing the
#: block to the probe then makes the single read exactly the signal wanted.
#:
#: The probe is 80 ms rather than the 20 ms this was first measured at. The
#: fold is incoherent across frames, so sensitivity improves as roughly the
#: square root of their number: 15 frames become 60, which is about a factor
#: of two, for about three times the sweep. That trade was taken because a
#: 20 ms probe measured no separation at all between tunings whose dwell
#: qualified something and tunings whose dwell did not.
#:
#: The bank is 104 kernels rather than 24, so scoring costs 3.76 times what it
#: did.  That is the whole price of the change and it is paid in host time, not
#: radio time: the sweep goes from a field-measured 1.70 s to about 3.1 s,
#: against the 120 s dwell it runs before.
SURVEY_PROFILE = ScanProfile(probe_s=0.080, block_size=200_000,
                             shape=SURVEY_BANK,
                             offset_span_hz=SURVEY_OFFSET_SPAN_HZ)


def survey_profile(probe_s: float = SURVEY_CEILING_PROBE_S,
                   sample_rate_hz: float = SURVEY_CEILING_SAMPLE_RATE_HZ
                   ) -> ScanProfile:
    """The survey profile for one probe length and one sample rate.

    ``block_size`` is the whole reason this is a function.  The survey reads
    exactly one block per tuning and sizes it to the probe, so a block left at
    another configuration's sample count either reads short -- a second refill,
    another 12.8 ms of radio time and a probe stitched from two -- or reads long
    and throws the surplus away.  :data:`SURVEY_PROFILE` is 200,000 because
    that is 80 ms at 2.5 MS/s, and it stops being right the moment either
    number moves.

    Everything else in the profile is a property of how the radio is driven
    rather than of the configuration, so it is inherited unchanged.
    """
    if probe_s <= 0 or sample_rate_hz <= 0:
        raise ValueError("probe length and sample rate must be positive")
    return ScanProfile(probe_s=float(probe_s),
                       block_size=int(round(probe_s * sample_rate_hz)),
                       shape=SURVEY_BANK,
                       offset_span_hz=SURVEY_OFFSET_SPAN_HZ)
#: The profile the model was validated against on hardware, kept because the
#: measurement belongs to a specific configuration rather than to whichever
#: one is currently deployed: 43.5 ms per tuning across eight tunings, on
#: :data:`MEASURED_COST_BANK`.
MEASURED_PROFILE_20MS = ScanProfile(probe_s=0.020, block_size=50_000,
                                    shape=MEASURED_COST_BANK,
                                    offset_span_hz=MEASURED_COST_SPAN_HZ)


@dataclass(frozen=True)
class KernelBank:
    """Precomputed correlation kernels for one edge, one sample rate.

    The bank depends only on published geometry, so it is built once and reused
    for every probe; it is the frequency hypothesis, not the signal, that the
    rotation is applied to.
    """

    edge: str
    sample_rate_hz: float
    offsets_hz: np.ndarray
    anchors: np.ndarray
    real: np.ndarray
    imag: np.ndarray
    inverse_norm: np.ndarray
    local_start: np.ndarray
    taps: int
    #: Nonzero on a wrong-code control bank.  Carried so a control cannot be
    #: mistaken for a detector further downstream: its scores share a shape
    #: with the real bank and would otherwise be judged against the real
    #: bank's threshold.
    symbol_roll: int = 0

    @property
    def size(self) -> int:
        return int(self.inverse_norm.size)


@lru_cache(maxsize=16)
def build_bank(edge: str = "lower", sample_rate_hz: float = 2_500_000.0,
               shape: tuple[int, int] = FULL_BANK,
               offset_span_hz: float = DEFAULT_OFFSET_SPAN_HZ,
               center_hz: float = 0.0, symbol_roll: int = 0) -> KernelBank:
    """Build (and cache) the kernel bank for one edge and search shape.

    ``center_hz`` places the search about a receiver's own local oscillator
    rather than about zero, which is what lets a port whose LNB disagrees with
    its twin be searched at all.

    ``symbol_roll`` rotates the pilot code sequence before the kernels are cut.
    It is the control the reference path uses (``symbol_roll=17``) and it is a
    valid one **only at a fixed epoch**.

    Every symbol is one symbol period long, so rolling the code sequence by
    ``r`` symbols produces the same waveform shifted by ``r`` symbol periods:
    measured coherence 0.909 between the 17-roll and the plain template
    circularly shifted 187 samples, which is exactly 17 x 11.  :func:`probe`
    searches every epoch, so it simply re-finds the true pilot 187 samples
    away -- 2.80 against the real bank's 3.44 on a -3 dB beacon, which is not a
    null by any reading.  ``conditioned_pilot_score`` in :mod:`.pilots`, which
    holds the epoch, separates the same signal 0.585 to 0.019.

    Use it for a conditioned control.  Do not use it as the null population for
    an epoch-searching threshold; :data:`NOISE_CEILING` uses the cross-edge
    construction instead, where the codes are unrelated rather than shifted.
    """
    offset_count, anchor_count = shape
    if offset_count < 1 or anchor_count < 1:
        raise ValueError("bank shape entries must be positive")
    if sample_rate_hz <= 0 or offset_span_hz <= 0:
        raise ValueError("sample rate and offset span must be positive")
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge,
                                        int(symbol_roll))
    symbol_period = sample_rate_hz * OFDM_SYMBOL_DURATION_S
    taps = max(2, round(symbol_period))
    offsets = np.linspace(-offset_span_hz, offset_span_hz, offset_count) + center_hz
    anchors = np.unique(np.rint(np.linspace(2, 301, anchor_count)).astype(int))
    lag = np.arange(taps) / sample_rate_hz
    kernels, starts = [], []
    for offset in offsets:
        for symbol in anchors:
            begin = round(symbol * symbol_period)
            piece = template[begin:begin + taps]
            if piece.size < taps:                      # pad a truncated tail
                piece = np.concatenate(
                    [piece, np.zeros(taps - piece.size, np.complex64)])
            kernels.append((piece * np.exp(2j * np.pi * offset * lag)
                            ).astype(np.complex64))
            starts.append(begin)
    stacked = np.stack(kernels)
    norms = np.sum(np.abs(stacked) ** 2, axis=1)
    return KernelBank(
        edge=edge, sample_rate_hz=float(sample_rate_hz), offsets_hz=offsets,
        anchors=anchors,
        real=np.ascontiguousarray(stacked.real, np.float32).ravel(),
        imag=np.ascontiguousarray(stacked.imag, np.float32).ravel(),
        inverse_norm=np.ascontiguousarray(
            1.0 / np.maximum(norms, 1e-30), np.float32),
        local_start=np.ascontiguousarray(starts, np.int32), taps=taps,
        symbol_roll=int(symbol_roll))


def survey_bank(edge: str = "lower", sample_rate_hz: float = 2_500_000.0, *,
                symbol_roll: int = 0) -> KernelBank:
    """The bank the survey actually runs: its shape and its span together.

    :func:`build_bank` takes the span as a separate argument defaulting to
    :data:`DEFAULT_OFFSET_SPAN_HZ`, so asking it for :data:`SURVEY_BANK` and
    nothing else builds thirteen hypotheses across +/-300 kHz -- a 50 kHz grid
    no deployment uses and no threshold describes.  The shape is not the
    configuration; this pairing is.
    """
    return build_bank(edge, sample_rate_hz, SURVEY_BANK, SURVEY_OFFSET_SPAN_HZ,
                      0.0, symbol_roll)


@lru_cache(maxsize=64)
def _fold_support(bank_key: tuple, lags: int, frames: int) -> np.ndarray:
    """Frames contributing to each epoch, summed over anchors.

    Depends only on geometry and probe length, never on the samples, so it is
    cached rather than recomputed for every probe.
    """
    anchors, symbol_period, period, epochs = bank_key
    epoch = np.arange(epochs)[:, None]
    offsets = np.rint(np.arange(frames) * period).astype(int)
    total = np.zeros(epochs)
    for symbol in anchors:
        index = epoch + (round(symbol * symbol_period) + offsets)[None, :]
        total += (index < lags).sum(axis=1)
    return np.maximum(total, 1.0)


def probe(samples: np.ndarray, bank: KernelBank, *,
          threads: int = DEFAULT_THREADS) -> dict:
    """Score one probe window against a kernel bank.

    Returns the folded peak, its epoch and frequency hypothesis, and the
    peak-to-median ratio that separates a pilot from noise.
    """
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("probe samples must be one dimensional")
    period = bank.sample_rate_hz * STARLINK_FRAME_DURATION_S
    epochs = round(period)
    if values.size < round(4 * period):
        raise ValueError("at least four frames are required")
    library = _load_kernel()
    taps, count = bank.taps, values.size
    lags = count - taps + 1

    real = np.ascontiguousarray(values.real, np.float32)
    imag = np.ascontiguousarray(values.imag, np.float32)
    power = real.astype(np.float64) ** 2 + imag.astype(np.float64) ** 2
    running = np.concatenate(([0.0], np.cumsum(power)))
    energy = running[taps:] - running[:-taps]
    inverse = np.ascontiguousarray(
        np.where(energy > 0, 1.0 / np.maximum(energy, 1e-30), 0.0), np.float32)

    frames = int(np.ceil(count / period)) + 1
    frame_offset = np.ascontiguousarray(
        np.rint(np.arange(frames) * period), np.int32)
    aggregate = np.zeros(bank.size * epochs)
    library.leo_fold_correlate(
        real, imag, count, bank.real, bank.imag, bank.size, taps,
        bank.inverse_norm, inverse, bank.local_start, frame_offset,
        frames, epochs, aggregate, int(threads))

    symbol_period = bank.sample_rate_hz * OFDM_SYMBOL_DURATION_S
    support = _fold_support((tuple(bank.anchors.tolist()), symbol_period,
                             period, epochs), lags, frames)
    scored = aggregate.reshape(bank.offsets_hz.size, bank.anchors.size, epochs)
    per_offset = scored.sum(axis=1) / support[None, :]
    folded = per_offset.max(axis=0)
    chosen = per_offset.argmax(axis=0)
    best = int(folded.argmax())
    median = float(np.median(folded))
    return {"epoch_sample": best, "epoch_s": best / bank.sample_rate_hz,
            "frequency_offset_hz": float(bank.offsets_hz[chosen[best]]),
            "folded_score": float(folded[best]), "folded_median": median,
            "peak_to_median": float(folded[best] / max(median, 1e-20)),
            "kernel_count": bank.size, "edge": bank.edge,
            **_corroboration(folded, per_offset, scored, chosen, best, power)}


#: Samples either side of the winning epoch treated as the same detection.
#: A pilot smears across a few lags, so a neighbour is the same evidence rather
#: than a second, independent one.
EPOCH_GUARD_SAMPLES = 8


def _corroboration(folded, per_offset, scored, chosen, best, power) -> dict:
    """Evidence about the peak beyond how far it stands above the median.

    All of it comes from arrays the correlation already produced, so it costs
    about six percent on top of a probe and no radio time at all.

    Peak-to-median asks one question — is the winner large — and answers it
    from the same fold that produced the winner. These ask different ones:

    ``anchor_agreement`` counts how many of the anchor symbols independently
    pick the winning epoch. A pilot repeats at every anchor; a fluctuation does
    not, and on noise this is zero rather than merely small. It is
    corroboration rather than another view of the same number.

    ``offset_contrast`` is how much the winning frequency hypothesis beats the
    worst. A narrowband pilot is localised in frequency and a broadband
    disturbance lifts every hypothesis together, which peak-to-median cannot
    distinguish because it divides that lift out of both terms.

    ``peak_to_p99`` and ``peak_to_second`` re-normalise against the tail rather
    than the middle.  The median is insensitive to a heavy tail, so
    peak-to-median flatters a distribution that has one.

    ``mean_power`` is the only absolute quantity here, and the only one that
    can see a port that has gone dead or is saturating — a ratio divides that
    away by construction.
    """
    guard = EPOCH_GUARD_SAMPLES
    peak = float(folded[best])
    ordered = np.sort(folded)
    p99 = float(ordered[int(0.99 * (ordered.size - 1))])

    without_peak = folded.copy()
    without_peak[max(0, best - guard):best + guard + 1] = -np.inf
    second = float(without_peak.max())

    profile = per_offset[:, best]
    weakest = float(profile.min())

    per_anchor = scored[int(chosen[best])]           # (anchors, epochs)
    agreement = int((np.abs(per_anchor.argmax(axis=1) - best) <= guard).sum())

    return {"folded_p99": p99,
            "peak_to_p99": peak / max(p99, 1e-20),
            "second_score": second,
            "peak_to_second": peak / max(second, 1e-20),
            "offset_profile": [float(value) for value in profile],
            "offset_contrast": float(profile.max() / max(weakest, 1e-20)),
            "anchor_agreement": agreement,
            "anchor_count": int(per_anchor.shape[0]),
            "mean_power": float(power.mean()),
            "peak_amplitude": float(np.sqrt(power.max()))}


def warm_kernel(profile: ScanProfile = SURVEY_PROFILE,
                sample_rate_hz: float = 2_500_000.0, edge: str = "lower") -> float:
    """Compile the kernel and build the bank before anything is timed.

    The fused kernel is built on first use, so the first probe in a process
    pays the compile.  Measured at 833 ms against 9 ms for every probe after
    it, which is enough to swamp a whole survey if it lands inside one.
    Returns the seconds spent, so a caller can log it rather than attribute it
    to the radio.
    """
    started = time.perf_counter()
    bank = build_bank(edge, sample_rate_hz, profile.shape, profile.offset_span_hz)
    frames = max(MINIMUM_PROBE_FRAMES,
                 math.ceil(profile.probe_s / STARLINK_FRAME_DURATION_S))
    count = int(frames * STARLINK_FRAME_DURATION_S * sample_rate_hz) + bank.taps
    probe(np.zeros(count, np.complex64), bank)
    return time.perf_counter() - started


def scan_tunings(read_probe, tunings, *, sample_rate_hz: float = 2_500_000.0,
                 shape: tuple[int, int] = SURVEY_BANK,
                 offset_span_hz: float = SURVEY_OFFSET_SPAN_HZ,
                 threads: int = DEFAULT_THREADS,
                 lnb_lo_hz: float = 9_750_000_000.0) -> list[dict]:
    """Survey several channel/edge tunings and rank them by pilot evidence.

    ``read_probe`` is called with the intermediate frequency for each tuning and
    returns that tuning's probe samples; retuning and settling belong to the
    caller because they dominate the wall time and can be overlapped with the
    scoring of the previous tuning.
    """
    results = []
    for channel, edge in tunings:
        centre = starlink_edge_pilot_if_hz(channel, edge, lnb_lo_hz)
        samples = read_probe(centre)
        if samples is None:
            continue
        bank = build_bank(edge, sample_rate_hz, shape, offset_span_hz)
        scored = probe(samples, bank, threads=threads)
        results.append({**scored, "channel": channel, "region": f"{edge}-edge",
                        "if_center_hz": centre,
                        "rf_center_hz": centre + lnb_lo_hz})
    results.sort(key=lambda item: item["peak_to_median"], reverse=True)
    return results


def collect_radio(context, tunings, *, profile: ScanProfile = SURVEY_PROFILE,
                  sample_rate_hz: float = 2_500_000.0,
                  lnb_lo_hz: float = 9_750_000_000.0) -> dict:
    """Tune, listen, and hand the IQ back.  Nothing here scores anything.

    This is everything that needs the radio and nothing else.  The split is the
    point: the capture host is a four-core Pi already running two live capture
    services, and radio time is the part no amount of arithmetic elsewhere can
    recover, while scoring is host time that any machine can spend.  Keeping
    them in one loop charged the Pi for both and made the survey's cost scale
    with the bank; separated, the Pi pays only for the seconds the antenna is
    pointed somewhere.

    **Radio time depends on probe duration, not on sample rate.**  A block is
    sized to the probe by :func:`survey_profile`, so 80 ms costs one 80 ms read
    whether that is 200,000 samples or 400,000 of them.  What the rate changes
    is bytes -- 8 tunings x samples x 2 receivers x 2 components x 2 bytes --
    and arithmetic downstream.

    The raw ci16 comes back shaped ``(tuning, sample, receiver, component)``:
    the capture path's own layout with a tuning axis in front, kept as the
    driver delivered it rather than as the complex64 a detector works in, so a
    later analysis re-decides from the signal rather than from this one's
    conversion of it.

    The radio is configured from the profile rather than from whatever the
    capture path left behind: a survey wants a shallow queue and a block the
    size of its probe, which is the opposite of what a long dwell wants.
    """
    import iio                                    # kept local: host-only import

    phy = context.find_device("ad9361-phy")
    lo = next(c for c in phy.channels
              if c.id == "altvoltage0" and c.output)
    receivers = [next(c for c in phy.channels
                      if c.id == f"voltage{index}" and not c.output)
                 for index in (0, 1)]
    for channel in receivers:
        channel.attrs["sampling_frequency"].value = str(int(sample_rate_hz))
        channel.attrs["rf_bandwidth"].value = str(int(sample_rate_hz))
        break
    stream = context.find_device("cf-ad9361-lpc")
    for channel in stream.channels:
        channel.enabled = True
    stream.set_kernel_buffers_count(profile.kernel_buffers)
    buffer = iio.Buffer(stream, profile.block_size, False)
    wanted = int(round(profile.probe_s * sample_rate_hz))
    plan, retained = [], []
    timing = {"tune": 0.0, "settle": 0.0, "listen": 0.0}
    try:
        for channel_number, edge in tunings:
            centre = starlink_edge_pilot_if_hz(channel_number, edge, lnb_lo_hz)

            mark = time.perf_counter()
            lo.attrs["frequency"].value = str(int(centre))
            timing["tune"] += time.perf_counter() - mark

            mark = time.perf_counter()
            for _ in range(profile.settle_buffers):
                buffer.refill()
            timing["settle"] += time.perf_counter() - mark

            mark = time.perf_counter()
            pieces, remaining = [], wanted
            while remaining > 0:
                buffer.refill()
                raw = np.frombuffer(buffer.read(), dtype=np.int16)
                pieces.append(raw)
                remaining -= raw.size // 4
            # i0 q0 i1 q1 per sample: one read carries both receivers.  They
            # arrive interleaved whether or not anyone looks at the second one,
            # so keeping both costs no radio time at all -- and the capture this
            # precedes keeps both, so a survey of one port could not be set
            # beside what that capture found.
            raw = np.concatenate(pieces).reshape(-1, 2, 2)[:wanted]
            # Copied rather than kept as a view, so a read that overshot the
            # probe releases the surplus instead of pinning it for the whole
            # survey.
            retained.append(raw.copy())
            timing["listen"] += time.perf_counter() - mark

            plan.append({"channel": channel_number, "region": f"{edge}-edge",
                         "edge": edge, "if_center_hz": centre,
                         "rf_center_hz": centre + lnb_lo_hz})
    finally:
        # The binding has no explicit close: the buffer is freed when its last
        # reference goes, and a leaked one holds the context so that whatever
        # opens the radio next fails with EBUSY.
        del buffer
    samples = np.stack(retained) if retained else None
    radio_s = sum(timing.values())
    return {
        "samples": samples, "plan": plan,
        # The retained IQ stays in the order it was collected.  Every consumer
        # indexes it by this, never by the score-sorted result list, so the two
        # cannot be mismatched.
        "sample_order": [(item["channel"], item["region"]) for item in plan],
        "tunings": len(plan),
        "timing_ms": {key: value * 1000 for key, value in timing.items()},
        "radio_ms": radio_s * 1000,
        "radio_ms_per_tuning": radio_s * 1000 / max(len(plan), 1),
        "sample_rate_hz": float(sample_rate_hz),
        "probe_s": float(profile.probe_s),
        "samples_per_tuning": int(samples.shape[1]) if samples is not None else 0,
        "iq_bytes": int(samples.nbytes) if samples is not None else 0,
        "pilot_guard_hz": pilot_guard_hz(sample_rate_hz),
        "profile": {"block_size": profile.block_size,
                    "kernel_buffers": profile.kernel_buffers,
                    "settle_buffers": profile.settle_buffers,
                    "probe_s": profile.probe_s,
                    "shape": list(profile.shape),
                    "offset_span_hz": float(profile.offset_span_hz)}}


def score_collection(collected: dict, *,
                     shape: tuple[int, int] = SURVEY_BANK,
                     offset_span_hz: float = SURVEY_OFFSET_SPAN_HZ,
                     threads: int = DEFAULT_THREADS,
                     sample_limit: int | None = None) -> dict:
    """Score collected IQ.  Callable anywhere, because it never touches a radio.

    Takes what :func:`collect_radio` returned and produces the verdict material
    the survey record has always carried.  Nothing here reads a device, so the
    analysis host runs it over the preserved corpus with sixteen workers and
    the capture host need not run it at all.

    ``sample_limit`` caps the window scored.  It is how a Pi-side score stays
    affordable while the *collected* configuration varies: correlation cost is
    linear in samples, so scoring a fixed prefix costs the same in all four
    configurations.  The record must then say which window was scored, because
    a score over 200,000 samples at 5 MS/s is a 40 ms probe and shares neither
    its threshold nor its sensitivity with an 80 ms one.

    Both receivers are scored against the *same* bank, centred on zero.
    Re-centring a port's search on its own local oscillator is what the
    analysis path had to do, and it does not transfer here: a bank spread
    thinly over its span raises its median as much as its peak when it is moved
    onto the signal, and the statistic is a ratio, so the score falls.
    Measured on a 436 kHz offset at -3 dB against the retired three-hypothesis
    bank, 1.41 centred against 1.61 as it stood.  The answer is a finer bank
    rather than a moved one, and :data:`SURVEY_BANK` is now that.
    """
    samples = collected.get("samples")
    plan = collected.get("plan") or []
    rate = float(collected["sample_rate_hz"])
    if samples is None:
        raise ValueError("nothing was collected to score")
    if samples.shape[0] != len(plan):
        raise ValueError(
            f"collected {samples.shape[0]} tunings against {len(plan)} planned")
    count = int(samples.shape[1])
    scored_count = count if sample_limit is None else min(count, int(sample_limit))
    if scored_count < 1:
        raise ValueError("sample limit leaves nothing to score")
    results = []
    mark = time.perf_counter()
    for index, item in enumerate(plan):
        bank = build_bank(item["edge"], rate, shape, offset_span_hz)
        block = samples[index, :scored_count]
        per_receiver = []
        for receiver in range(block.shape[1]):
            piece = block[:, receiver, :]
            values = np.empty(piece.shape[0], np.complex64)
            values.real, values.imag = piece[:, 0], piece[:, 1]
            per_receiver.append({"receiver": receiver,
                                 **probe(values, bank, threads=threads)})
        results.append({"channel": item["channel"], "region": item["region"],
                        "if_center_hz": item["if_center_hz"],
                        "rf_center_hz": item["rf_center_hz"],
                        "edge": item["edge"], "receivers": per_receiver,
                        # Either port seeing a pilot makes the channel worth
                        # dwelling on, so a tuning ranks by its best.
                        "peak_to_median": max(entry["peak_to_median"]
                                              for entry in per_receiver)})
    compute_s = time.perf_counter() - mark
    results.sort(key=lambda item: item["peak_to_median"], reverse=True)
    return {"results": results,
            "compute_ms": compute_s * 1000,
            "compute_ms_per_tuning": compute_s * 1000 / max(len(plan), 1),
            "scored_samples": scored_count,
            "scored_probe_s": scored_count / rate,
            "scored_fraction": scored_count / max(count, 1),
            "sample_rate_hz": rate,
            "shape": list(shape),
            "offset_span_hz": float(offset_span_hz)}


def scan_radio(context, tunings, *, profile: ScanProfile = SURVEY_PROFILE,
               sample_rate_hz: float = 2_500_000.0,
               lnb_lo_hz: float = 9_750_000_000.0,
               threads: int = DEFAULT_THREADS,
               keep_samples: bool = False,
               score: bool = True,
               score_sample_limit: int | None = None) -> dict:
    """Collect and score in one call, reporting where the time went.

    The composition of :func:`collect_radio` and :func:`score_collection`, kept
    because a caller that wants both in one place should not have to assemble
    them, and because every existing reader of this return shape still works.

    ``score=False`` collects and returns no verdict at all, which is what
    "the capture host records, the analysis host decides" looks like when it is
    taken literally.  ``score_sample_limit`` bounds what a Pi-side verdict
    costs when the configuration it was collected in is larger than the one the
    threshold was measured in.

    Timing is returned split by what the radio charges for, because the parts
    are not comparable to each other: retuning and draining are radio time that
    no amount of arithmetic can recover, while scoring is host time that can be
    moved to another machine entirely.
    """
    collected = collect_radio(context, tunings, profile=profile,
                              sample_rate_hz=sample_rate_hz, lnb_lo_hz=lnb_lo_hz)
    timing = dict(collected["timing_ms"])
    if score:
        scored = score_collection(collected, shape=profile.shape,
                                  offset_span_hz=profile.offset_span_hz,
                                  threads=threads,
                                  sample_limit=score_sample_limit)
    else:
        # Still one entry per tuning, carrying where the radio was pointed and
        # no scores at all.  Dropping the entries would lose the centres, and
        # every downstream reader pairs ``sample_order`` against this list to
        # learn which sky each block of IQ is of.
        scored = {"results": [{**item, "receivers": []}
                              for item in collected["plan"]],
                  "compute_ms": 0.0, "scored_samples": 0,
                  "scored_probe_s": 0.0, "scored_fraction": 0.0,
                  "sample_rate_hz": float(sample_rate_hz),
                  "shape": list(profile.shape),
                  "offset_span_hz": float(profile.offset_span_hz)}
    # Milliseconds throughout: ``collect_radio`` already converted, so anything
    # added here has to arrive in the same unit.
    timing["compute"] = scored["compute_ms"]
    total_ms = sum(timing.values())
    return {"results": scored["results"], "tunings": collected["tunings"],
            "samples": collected["samples"] if keep_samples else None,
            "sample_order": collected["sample_order"],
            "timing_ms": timing,
            "total_ms": total_ms,
            "per_tuning_ms": total_ms / max(collected["tunings"], 1),
            "radio_ms": collected["radio_ms"],
            "compute_ms": scored["compute_ms"],
            "scored_samples": scored["scored_samples"],
            "scored_probe_s": scored["scored_probe_s"],
            "scored": bool(score),
            "sample_rate_hz": float(sample_rate_hz),
            "probe_s": collected["probe_s"],
            "samples_per_tuning": collected["samples_per_tuning"],
            "iq_bytes": collected["iq_bytes"],
            "pilot_guard_hz": collected["pilot_guard_hz"],
            # The profile's span, not the module default: those two are
            # deliberately different numbers and a record that reported the
            # wrong one would put the wrong spacing in the corpus.
            "offset_span_hz": float(profile.offset_span_hz),
            "profile": collected["profile"]}


def verify_presence(samples: np.ndarray, bank: KernelBank, *,
                    minimum_peak_to_median: float | None = None,
                    threads: int = DEFAULT_THREADS) -> dict:
    """Decide whether a dwell still sees its pilot.

    Used on a sampled subset of chunks during a dwell: the question is only
    whether the signal is still present, so the survey bank is enough and the
    decision is one scalar against the bank's measured noise ceiling.

    That ceiling was measured at :data:`SURVEY_CEILING_PROBE_S`, and the
    statistic depends on how many frames were folded, so a caller handing this
    a much shorter chunk is using a bar that describes a different experiment.
    Pass ``minimum_peak_to_median`` when the chunk length is not the survey's.
    """
    shape = (int(bank.offsets_hz.size), int(bank.anchors.size))
    threshold = (detection_threshold(shape) if minimum_peak_to_median is None
                 else float(minimum_peak_to_median))
    scored = probe(samples, bank, threads=threads)
    scored["present"] = scored["peak_to_median"] >= threshold
    scored["minimum_peak_to_median"] = threshold
    return scored


def dwell_verifier(bank: KernelBank, *, analyse_every: int = 4,
                   patience: int = 3, threads: int = DEFAULT_THREADS):
    """Track pilot presence across a dwell, scoring one chunk in ``analyse_every``.

    Chunks arrive faster than they need to be checked, so most are admitted
    without analysis and only a sampled subset is scored.  Loss is declared
    after ``patience`` consecutive scored chunks come back empty rather than on
    the first, because dual-valid epochs are sparse enough that a single quiet
    chunk is normal.

    Returns a callable taking one chunk and reporting whether the dwell should
    continue.
    """
    if analyse_every < 1 or patience < 1:
        raise ValueError("analyse_every and patience must be positive")
    state = {"index": 0, "misses": 0, "scored": 0, "last": None}

    def observe(samples: np.ndarray | None) -> dict:
        index = state["index"]
        state["index"] += 1
        if samples is None or index % analyse_every:
            return {"chunk": index, "analysed": False, "continue": True,
                    "consecutive_misses": state["misses"],
                    "peak_to_median": state["last"]}
        scored = verify_presence(samples, bank, threads=threads)
        state["scored"] += 1
        state["last"] = scored["peak_to_median"]
        state["misses"] = 0 if scored["present"] else state["misses"] + 1
        return {"chunk": index, "analysed": True,
                "continue": state["misses"] < patience,
                "consecutive_misses": state["misses"],
                "scored_chunks": state["scored"],
                "peak_to_median": scored["peak_to_median"],
                "present": scored["present"],
                "frequency_offset_hz": scored["frequency_offset_hz"]}

    return observe
