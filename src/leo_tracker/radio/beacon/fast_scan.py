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

Two bank sizes are offered.  ``SURVEY_BANK`` is the cheap first pass used to
decide which channel to dwell on; ``FULL_BANK`` matches the reference search
exactly.  A survey bank finds the same epoch as the full bank down to about
-10 dB and costs a seventh as much, so the scan is limited by how fast the
radio retunes rather than by arithmetic.
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

#: Cheap first pass: frequency offsets x anchor symbols used while surveying.
SURVEY_BANK = (3, 8)
#: Full search, identical in coverage to the reference acquisition.
FULL_BANK = (7, 24)
#: Doppler search span; Ku-band LEO stays inside roughly +/-250 kHz.
DEFAULT_OFFSET_SPAN_HZ = 300_000.0
#: Threads for the fused kernel.  Three leaves a core for the capture reader.
DEFAULT_THREADS = 3
#: Frames a probe must contain for the epoch fold to mean anything.
MINIMUM_PROBE_FRAMES = 4

#: Peak-to-median at the 99th percentile of pure noise, measured per bank over
#: 60 realisations.  A smaller bank folds fewer independent scores together, so
#: its noise floor sits higher and it needs a higher bar to hold the same false
#: alarm rate.  Detection is then reliable to about -8 dB for the survey bank
#: and -12 dB for the full one.
NOISE_CEILING = {SURVEY_BANK: 1.33, FULL_BANK: 1.20}
#: Used when a bank shape has not been characterised; deliberately strict.
FALLBACK_NOISE_CEILING = 1.40


def detection_threshold(shape: tuple[int, int]) -> float:
    """Peak-to-median a probe must beat for this bank shape."""
    return NOISE_CEILING.get(tuple(shape), FALLBACK_NOISE_CEILING)

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
    #: Measured on a Pi 5 over a persistent USB context, writing a *different*
    #: frequency each time.  Rewriting the same value costs 0.5 ms because the
    #: driver skips the reprogram, which flatters a benchmark that hops nowhere.
    tune_ms: float = 6.1
    #: A refill costs its nominal duration once the queue is deep enough to
    #: prefetch.  At depth one nothing is prefetched, so each refill also pays
    #: the transfer it can no longer overlap.
    shallow_refill_ms: float = 12.8
    compute_ms_per_probe_s: float = 400.0

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

    def cost_ms(self, sample_rate_hz: float = 2_500_000.0) -> dict:
        """Predicted per-tuning cost, split into what the radio charges for.

        Listening is quantised: a probe shorter than one buffer still costs a
        whole buffer, which is the entire reason block size dominates.
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
                "buffers_listened": listens,
                "signal_used_fraction": self.probe_s / (listens * buffer_s)}

    def sweep_ms(self, tunings: int = 8,
                 sample_rate_hz: float = 2_500_000.0) -> float:
        return tunings * self.cost_ms(sample_rate_hz)["total_ms"]


#: What the capture path uses.  Correct for a 120 s dwell, eight times too
#: coarse for a survey; kept so the comparison is explicit rather than implied.
DWELL_PROFILE = ScanProfile(block_size=262_144, settle_buffers=3, kernel_buffers=4)
#: A survey retunes between every read, so it has nothing to gain from the
#: driver's read-ahead and everything to lose: at depth 1 no buffer is
#: pre-filled, so there is no stale data and no settle to pay for. Sizing the
#: block to the probe then makes the single read exactly the signal wanted.
#: Measured 43.5 ms per tuning against the dwell profile's 314 ms.
SURVEY_PROFILE = ScanProfile()


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

    @property
    def size(self) -> int:
        return int(self.inverse_norm.size)


@lru_cache(maxsize=16)
def build_bank(edge: str = "lower", sample_rate_hz: float = 2_500_000.0,
               shape: tuple[int, int] = FULL_BANK,
               offset_span_hz: float = DEFAULT_OFFSET_SPAN_HZ,
               center_hz: float = 0.0) -> KernelBank:
    """Build (and cache) the kernel bank for one edge and search shape.

    ``center_hz`` places the search about a receiver's own local oscillator
    rather than about zero, which is what lets a port whose LNB disagrees with
    its twin be searched at all.
    """
    offset_count, anchor_count = shape
    if offset_count < 1 or anchor_count < 1:
        raise ValueError("bank shape entries must be positive")
    if sample_rate_hz <= 0 or offset_span_hz <= 0:
        raise ValueError("sample rate and offset span must be positive")
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge, 0)
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
        local_start=np.ascontiguousarray(starts, np.int32), taps=taps)


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
            "kernel_count": bank.size, "edge": bank.edge}


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
    bank = build_bank(edge, sample_rate_hz, profile.shape)
    frames = max(MINIMUM_PROBE_FRAMES,
                 math.ceil(profile.probe_s / STARLINK_FRAME_DURATION_S))
    count = int(frames * STARLINK_FRAME_DURATION_S * sample_rate_hz) + bank.taps
    probe(np.zeros(count, np.complex64), bank)
    return time.perf_counter() - started


def scan_tunings(read_probe, tunings, *, sample_rate_hz: float = 2_500_000.0,
                 shape: tuple[int, int] = SURVEY_BANK,
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
        bank = build_bank(edge, sample_rate_hz, shape)
        scored = probe(samples, bank, threads=threads)
        results.append({**scored, "channel": channel, "region": f"{edge}-edge",
                        "if_center_hz": centre,
                        "rf_center_hz": centre + lnb_lo_hz})
    results.sort(key=lambda item: item["peak_to_median"], reverse=True)
    return results


def scan_radio(context, tunings, *, profile: ScanProfile = SURVEY_PROFILE,
               sample_rate_hz: float = 2_500_000.0,
               lnb_lo_hz: float = 9_750_000_000.0,
               threads: int = DEFAULT_THREADS) -> dict:
    """Survey several tunings on one radio, reporting where the time went.

    The radio is configured from the profile rather than from whatever the
    capture path left behind: a survey wants a shallow queue and a block the
    size of its probe, which is the opposite of what a long dwell wants.

    Timing is returned split by what the radio charges for, because the parts
    are not comparable to each other: retuning and draining are radio time that
    no amount of arithmetic can recover, while scoring is host time that can be
    overlapped with the next tuning.
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
    wanted = int(profile.probe_s * sample_rate_hz)
    results, timing = [], {"tune": 0.0, "settle": 0.0, "listen": 0.0,
                           "compute": 0.0}
    try:
        for channel_number, edge in tunings:
            centre = starlink_edge_pilot_if_hz(channel_number, edge, lnb_lo_hz)
            bank = build_bank(edge, sample_rate_hz, profile.shape)

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
                piece = (raw[0::4].astype(np.float32)
                         + 1j * raw[1::4].astype(np.float32))
                pieces.append(piece)
                remaining -= piece.size
            samples = np.ascontiguousarray(
                np.concatenate(pieces)[:wanted].astype(np.complex64))
            timing["listen"] += time.perf_counter() - mark

            mark = time.perf_counter()
            scored = probe(samples, bank, threads=threads)
            timing["compute"] += time.perf_counter() - mark

            results.append({**scored, "channel": channel_number,
                            "region": f"{edge}-edge", "if_center_hz": centre,
                            "rf_center_hz": centre + lnb_lo_hz})
    finally:
        del buffer
    results.sort(key=lambda item: item["peak_to_median"], reverse=True)
    total = sum(timing.values())
    return {"results": results, "tunings": len(tunings),
            "timing_ms": {k: v * 1000 for k, v in timing.items()},
            "total_ms": total * 1000,
            "per_tuning_ms": total * 1000 / max(len(tunings), 1),
            "profile": {"block_size": profile.block_size,
                        "kernel_buffers": profile.kernel_buffers,
                        "settle_buffers": profile.settle_buffers,
                        "probe_s": profile.probe_s}}


def verify_presence(samples: np.ndarray, bank: KernelBank, *,
                    minimum_peak_to_median: float | None = None,
                    threads: int = DEFAULT_THREADS) -> dict:
    """Decide whether a dwell still sees its pilot.

    Used on a sampled subset of chunks during a dwell: the question is only
    whether the signal is still present, so the cheap survey bank is enough and
    the decision is one scalar against the bank's measured noise ceiling.
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
