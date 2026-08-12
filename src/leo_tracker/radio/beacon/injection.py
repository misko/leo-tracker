"""Put a known pilot into real noise, so detection becomes a measurable thing.

Every other way of labelling this corpus is a prior. A catalogued satellite
overhead is a prior; the current detector firing is a prior contaminated by the
detector we are trying to replace. Neither can give a detection probability,
because neither knows whether a pilot was actually transmitted during the probe.

Injection can. The replica goes into a real captured probe at a known epoch, a
known frequency offset, a known strength and a known set of transmitted frames,
and the detector either finds it or does not.

Three choices here are load-bearing:

**The background is real noise, not Gaussian.** The threshold this corpus exists
to re-derive was originally characterised on synthetic noise, and the field
distribution sits close enough to it that its intended false-alarm rate is
doubtful. Injecting into synthetic noise would reproduce exactly that mistake.
The host probe is recorded, so a background that turns out to hold a real signal
can be traced rather than silently counted as noise.

**SNR is defined inside transmitted frames.** Averaged over the whole probe it
would fall as occupancy falls, and occupancy and strength would stop being
independent axes. Sweeping one while holding the other is the entire point.

**Frame phase is random by default.** Qin models the per-frame phase as
unmodelled and reports that attempts to generalise it failed, so a coherent
carrier across frames would be an easier signal than the sky provides — and a
detector tuned on it could learn to exploit cross-frame coherence that does not
exist.
"""
from __future__ import annotations

import numpy as np

from .pilots import _edge_pilot_frame_cached
from .structure import STARLINK_FRAME_DURATION_S

#: What a truth record calls itself.
INJECTION_SCHEMA = "leo-tracker.pilot-injection/v1"


def frame_offsets(sample_rate_hz: float, count: int) -> np.ndarray:
    """Sample offsets of successive frame slots.

    The frame period is 3333.333 samples at 2.5 MS/s, not 3333. Rounding it down
    drifts twenty samples across the sixty slots of an 80 ms probe, which is
    enough to destroy the fold while still producing plausible-looking numbers.
    This is the one line most worth its own regression test.
    """
    period = float(sample_rate_hz) * STARLINK_FRAME_DURATION_S
    return np.rint(np.arange(count) * period).astype(np.int64)


def occupancy_mask(count: int, *, fraction: float,
                   rng: np.random.Generator) -> np.ndarray:
    """Which frame slots carry a transmission.

    Independent Bernoulli per slot, which is a model rather than a measurement:
    Kozhaya documents transmission-mode changes on a fifteen-second cadence, so
    real occupancy is clustered. It is here to sweep as a parameter, not to
    assert that the sky behaves this way.
    """
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("occupancy fraction must lie in [0, 1]")
    return rng.random(count) < fraction


def inject(host: np.ndarray, *, sample_rate_hz: float = 2_500_000.0,
           edge: str = "lower", epoch_sample: int = 0, cfo_hz: float = 0.0,
           snr_db: float = -6.0, occupancy: float | np.ndarray = 1.0,
           frame_phase: str = "random", seed: int | None = None,
           host_name: str = "") -> dict:
    """Inject the edge-pilot replica into ``host`` and report exactly what was done.

    Returns the combined samples and a truth record naming the epoch, offset,
    strength and transmitted slots, which is what a detector's answer is scored
    against.
    """
    values = np.asarray(host, np.complex64)
    if values.ndim != 1:
        raise ValueError("host samples must be one dimensional")
    if frame_phase not in ("random", "coherent"):
        raise ValueError("frame_phase must be random or coherent")
    template = _edge_pilot_frame_cached(float(sample_rate_hz), edge, 0)
    if values.size < template.size:
        raise ValueError("host must hold at least one frame")

    rng = np.random.default_rng(seed)
    slots = int((values.size - epoch_sample) // (
        float(sample_rate_hz) * STARLINK_FRAME_DURATION_S))
    if slots < 1:
        raise ValueError("no frame slot fits after the requested epoch")
    offsets = frame_offsets(sample_rate_hz, slots) + int(epoch_sample)
    mask = (occupancy_mask(slots, fraction=float(occupancy), rng=rng)
            if np.isscalar(occupancy) else np.asarray(occupancy, bool))
    if mask.size != slots:
        raise ValueError(f"occupancy mask must cover {slots} slots")

    # Build the signal at unit amplitude first, so the scaling can be computed
    # against the host noise measured over exactly the samples it will occupy.
    signal = np.zeros(values.size, np.complex64)
    time_s = np.arange(values.size) / float(sample_rate_hz)
    carrier = np.exp(2j * np.pi * float(cfo_hz) * time_s).astype(np.complex64)
    occupied = np.zeros(values.size, bool)
    for index, start in enumerate(offsets):
        if not mask[index]:
            continue
        stop = min(start + template.size, values.size)
        if stop <= start:
            continue
        piece = template[:stop - start]
        phase = (np.exp(2j * np.pi * rng.random()) if frame_phase == "random"
                 else 1.0 + 0j)
        signal[start:stop] += (piece * phase).astype(np.complex64)
        occupied[start:stop] = True

    if not occupied.any():
        raise ValueError("occupancy selected no frames; nothing would be injected")
    signal *= carrier

    # Strength is set against the host measured only where the signal lands, so
    # that occupancy and SNR move independently.
    noise_power = float(np.mean(np.abs(values[occupied]) ** 2))
    signal_power = float(np.mean(np.abs(signal[occupied]) ** 2))
    if signal_power <= 0:
        raise ValueError("replica carries no power")
    scale = float(np.sqrt(10 ** (snr_db / 10) * noise_power / signal_power))
    combined = (values + scale * signal).astype(np.complex64)

    achieved = 10 * np.log10(
        float(np.mean(np.abs(scale * signal[occupied]) ** 2)) /
        max(noise_power, 1e-30))
    return {
        "samples": combined,
        "truth": {
            "schema": INJECTION_SCHEMA, "edge": edge,
            "epoch_sample": int(epoch_sample),
            "epoch_s": float(epoch_sample) / float(sample_rate_hz),
            "cfo_hz": float(cfo_hz),
            "requested_snr_db": float(snr_db),
            "achieved_snr_db": float(achieved),
            "snr_basis": ("signal power over host power, measured only across "
                          "samples the signal occupies, so strength and "
                          "occupancy are independent axes"),
            "frame_slots": int(slots),
            "transmitted_slots": [int(i) for i in np.flatnonzero(mask)],
            "occupancy": float(mask.mean()),
            "frame_phase": frame_phase,
            "sample_rate_hz": float(sample_rate_hz),
            "scale": scale,
            "host": host_name,
            "host_caveat": ("the background is a real recorded probe and may "
                            "itself contain signal; it is named so that a "
                            "contaminated background can be traced"),
        },
    }
