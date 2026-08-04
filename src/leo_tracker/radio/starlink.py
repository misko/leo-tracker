"""Continuous, channel-aware Starlink Ku signal acquisition and scoring."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
from typing import Iterable, Iterator, Sequence

import numpy as np


STARLINK_CHANNEL_BANDWIDTH_HZ = 240_000_000.0
STARLINK_CHANNEL_SPACING_HZ = 250_000_000.0
STARLINK_SUBCARRIER_SPACING_HZ = 234_375.0
STARLINK_GUTTER_WIDTH_HZ = 4 * STARLINK_SUBCARRIER_SPACING_HZ
STARLINK_FRAME_RATE_HZ = 750.0
STARLINK_TONE_SPACING_HZ = 43_949.5


@dataclass(frozen=True)
class StarlinkChannel:
    number: int
    rf_center_hz: float
    lnb_band: str
    lnb_lo_hz: float
    if_center_hz: float
    reported_active: bool


@dataclass(frozen=True)
class StarlinkMetrics:
    gutter_depth_db: float
    gutter_offset_hz: float
    frame_periodicity: float
    comb_excess_db: float
    evidence_count: int
    promoted: bool


@dataclass(frozen=True)
class GutterCandidate:
    offset_hz: float
    depth_db: float


def channel_plan() -> tuple[StarlinkChannel, ...]:
    result = []
    for number in range(1, 9):
        rf = (10.7e9 + STARLINK_SUBCARRIER_SPACING_HZ / 2
              + STARLINK_CHANNEL_SPACING_HZ * (number - .5))
        band, lo = ("low", 9.75e9) if rf < 11.7e9 else ("high", 10.6e9)
        result.append(StarlinkChannel(number, rf, band, lo, rf - lo, number >= 3))
    return tuple(result)


def get_channel(number: int) -> StarlinkChannel:
    if not 1 <= number <= 8:
        raise ValueError("Starlink channel number must be in 1..8")
    return channel_plan()[number - 1]


def averaged_psd(samples: np.ndarray, sample_rate_hz: float, *,
                 fft_size: int = 16_384) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(samples, dtype=np.complex64)
    if values.ndim != 1 or values.size < 1024:
        raise ValueError("Starlink analysis needs at least 1024 complex samples")
    size = min(int(fft_size), values.size)
    size = 1 << (size.bit_length() - 1)
    count = values.size // size
    window = np.hanning(size).astype(np.float32)
    power = np.zeros(size, np.float64)
    for index in range(count):
        frame = values[index * size:(index + 1) * size]
        power += np.abs(np.fft.fftshift(np.fft.fft(frame * window))) ** 2
    power /= count
    frequencies = np.fft.fftshift(np.fft.fftfreq(size, 1 / sample_rate_hz))
    return frequencies, 10 * np.log10(power + np.finfo(float).tiny)


def _rank_gutter_spectrum(frequencies: np.ndarray, power: np.ndarray, *,
                          sample_rate_hz: float, search_hz: float, top_count: int,
                          minimum_separation_hz: float) -> tuple[GutterCandidate, ...]:
    if top_count < 1 or search_hz <= 0 or minimum_separation_hz <= 0:
        raise ValueError("wide gutter search parameters must be positive")
    power = np.minimum(power, np.percentile(power, 95))
    prefix = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
    step_hz = max(sample_rate_hz / power.size, 10_000.0)
    limit = min(search_hz, sample_rate_hz / 2 - 1_200_000)
    offsets = np.arange(-limit, limit + step_hz / 2, step_hz)

    def means(low: np.ndarray, high: np.ndarray) -> np.ndarray:
        left = np.searchsorted(frequencies, low, side="left")
        right = np.searchsorted(frequencies, high, side="right")
        return (prefix[right] - prefix[left]) / np.maximum(1, right - left)

    half = STARLINK_GUTTER_WIDTH_HZ / 2
    gutter = means(offsets - half, offsets + half)
    left = means(offsets - 1_150_000, offsets - half - 100_000)
    right = means(offsets + half + 100_000, offsets + 1_150_000)
    scores = 10 * np.log10((left + right) / 2 / np.maximum(gutter, 1e-30))
    ranked = np.argsort(scores)[::-1]
    selected: list[GutterCandidate] = []
    for index in ranked:
        offset = float(offsets[index])
        if all(abs(offset - item.offset_hz) >= minimum_separation_hz for item in selected):
            selected.append(GutterCandidate(offset, float(scores[index])))
            if len(selected) == top_count:
                break
    return tuple(selected)


def search_gutter_offsets(samples: np.ndarray, sample_rate_hz: float, *,
                          search_hz: float = 15_000_000,
                          fft_size: int = 16_384, top_count: int = 5,
                          minimum_separation_hz: float = 750_000
                          ) -> tuple[GutterCandidate, ...]:
    """Quickly rank wideband locations resembling the Starlink center gutter."""
    frequencies, psd_db = averaged_psd(samples, sample_rate_hz, fft_size=fft_size)
    power = 10 ** ((psd_db - float(np.max(psd_db))) / 10)
    return _rank_gutter_spectrum(frequencies, power, sample_rate_hz=sample_rate_hz,
        search_hz=search_hz, top_count=top_count,
        minimum_separation_hz=minimum_separation_hz)


def aggregate_gutter_search(blocks: Iterable[tuple[int, Sequence[np.ndarray]]], *,
                            sample_rate_hz: float, snapshots: int,
                            search_hz: float = 15_000_000,
                            fft_size: int = 16_384, top_count: int = 5,
                            bin_hz: float = 100_000) -> dict:
    """Rank wide-offset candidates by recurrence across snapshots/receivers."""
    if snapshots < 1 or bin_hz <= 0:
        raise ValueError("snapshots and bin size must be positive")
    rows: list[dict] = []
    bins: dict[tuple[int, int], list[float]] = {}
    first_utc_ns = last_utc_ns = None
    receiver_count = None
    integrated: list[np.ndarray] | None = None
    integrated_frequencies: np.ndarray | None = None
    for snapshot, (utc_ns, incoming) in enumerate(blocks):
        values = [np.asarray(item, np.complex64) for item in incoming]
        if receiver_count is None: receiver_count = len(values)
        if not values or len(values) != receiver_count:
            raise ValueError("receiver count changed during offset search")
        if integrated is None: integrated = [np.zeros(fft_size, np.float64) for _ in values]
        receiver_rows = []
        for receiver, samples in enumerate(values):
            frequencies, psd_db = averaged_psd(samples, sample_rate_hz, fft_size=fft_size)
            power = 10 ** ((psd_db - float(np.median(psd_db))) / 10)
            integrated[receiver] += power / float(np.mean(power))
            integrated_frequencies = frequencies
            candidates = _rank_gutter_spectrum(frequencies, power,
                sample_rate_hz=sample_rate_hz, search_hz=search_hz,
                top_count=top_count, minimum_separation_hz=750_000)
            receiver_rows.append([asdict(item) for item in candidates])
            for item in candidates:
                key = (receiver, round(item.offset_hz / bin_hz))
                bins.setdefault(key, []).append(item.depth_db)
        rows.append({"snapshot": snapshot, "utc_ns": int(utc_ns),
                     "receivers": receiver_rows})
        first_utc_ns = int(utc_ns) if first_utc_ns is None else first_utc_ns
        last_utc_ns = int(utc_ns)
        if snapshot + 1 >= snapshots: break
    if len(rows) < snapshots:
        raise RuntimeError(f"source ended after {len(rows)} of {snapshots} snapshots")
    ranked = []
    for (receiver, index), depths in bins.items():
        ranked.append({"receiver": receiver, "offset_hz": index * bin_hz,
                       "hits": len(depths), "hit_fraction": len(depths) / snapshots,
                       "median_depth_db": float(np.median(depths)),
                       "max_depth_db": float(np.max(depths))})
    ranked.sort(key=lambda item: (item["hits"], item["median_depth_db"]), reverse=True)
    assert integrated is not None and integrated_frequencies is not None
    integrated_candidates = [[asdict(item) for item in _rank_gutter_spectrum(
        integrated_frequencies, power / snapshots, sample_rate_hz=sample_rate_hz,
        search_hz=search_hz, top_count=top_count,
        minimum_separation_hz=750_000)] for power in integrated]
    return {"schema": "leo-tracker.starlink-offset-search/v1",
            "sample_rate_hz": sample_rate_hz, "search_hz": search_hz,
            "fft_size": fft_size, "snapshots": snapshots,
            "receiver_count": receiver_count, "bin_hz": bin_hz,
            "first_utc_ns": first_utc_ns, "last_utc_ns": last_utc_ns,
            "ranked_candidates": ranked,
            "integrated_candidates": integrated_candidates,
            "observations": rows}


def frame_periodicity(samples: np.ndarray, sample_rate_hz: float,
                      *, frame_rate_hz: float = STARLINK_FRAME_RATE_HZ) -> float:
    """Normalized complex correlation at the nominal 1/750-second frame lag."""
    values = np.asarray(samples, dtype=np.complex64)
    lag = round(sample_rate_hz / frame_rate_hz)
    if lag < 1 or values.size < 3 * lag:
        raise ValueError("block is too short to test Starlink frame periodicity")
    left, right = values[:-lag], values[lag:]
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return 0.0 if not denominator else float(abs(np.vdot(left, right)) / denominator)


def analyze_starlink_block(samples: np.ndarray, sample_rate_hz: float, *,
                           search_hz: float = 400_000,
                           gutter_threshold_db: float = 3.0,
                           periodicity_threshold: float = .05,
                           comb_threshold_db: float = 4.0,
                           fft_size: int = 16_384) -> StarlinkMetrics:
    """Score known channel-center structure without using a TLE prediction."""
    frequencies, psd = averaged_psd(samples, sample_rate_hz, fft_size=fft_size)
    bin_hz = sample_rate_hz / psd.size
    # A bin-by-bin gutter search is needlessly expensive and can make analysis
    # slower than acquisition. Ten-kilohertz coarse spacing is still tiny
    # against the 937.5 kHz gutter; fine tone bins remain at FFT resolution.
    offset_step_hz = max(bin_hz, 10_000.0)
    offsets = np.arange(-search_hz, search_hz + offset_step_hz / 2, offset_step_hz)
    half_gutter = STARLINK_GUTTER_WIDTH_HZ / 2
    best_depth, best_offset = -np.inf, 0.0
    for offset in offsets:
        distance = np.abs(frequencies - offset)
        gutter = distance < half_gutter
        shoulder = (distance > half_gutter + 100_000) & (distance < min(sample_rate_hz*.46, 1_150_000))
        if gutter.sum() < 8 or shoulder.sum() < 8:
            continue
        depth = float(np.median(psd[shoulder]) - np.median(psd[gutter]))
        if depth > best_depth:
            best_depth, best_offset = depth, float(offset)
    if not np.isfinite(best_depth):
        raise ValueError("sample rate is too narrow to score the Starlink gutter")

    gutter_bins = np.abs(frequencies - best_offset) < half_gutter
    baseline = float(np.median(psd[gutter_bins]))
    half = 4
    tone_values = []
    for tone in range(-half, half + 1):
        expected = best_offset + tone * STARLINK_TONE_SPACING_HZ
        index = int(np.argmin(np.abs(frequencies - expected)))
        lo, hi = max(0, index - 1), min(psd.size, index + 2)
        tone_values.append(float(np.max(psd[lo:hi])))
    comb_excess = float(np.median(tone_values) - baseline)
    periodicity = frame_periodicity(samples, sample_rate_hz)
    evidence = int(best_depth >= gutter_threshold_db) + int(periodicity >= periodicity_threshold) + int(comb_excess >= comb_threshold_db)
    return StarlinkMetrics(best_depth, best_offset, periodicity, comb_excess,
                           evidence, evidence >= 2)


def synthetic_starlink_block(sample_rate_hz: float, sample_count: int, *, seed: int = 0,
                             gutter_offset_hz: float = 120_000,
                             snr_scale: float = 2.5) -> np.ndarray:
    """Deterministic Starlink-like channel-center fixture, not a protocol simulator."""
    if sample_count < round(4 * sample_rate_hz / STARLINK_FRAME_RATE_HZ):
        raise ValueError("synthetic block must contain at least four frame periods")
    rng = np.random.default_rng(seed)
    spectrum = rng.standard_normal(sample_count) + 1j * rng.standard_normal(sample_count)
    frequencies = np.fft.fftfreq(sample_count, 1 / sample_rate_hz)
    gutter = np.abs(frequencies - gutter_offset_hz) < STARLINK_GUTTER_WIDTH_HZ / 2
    spectrum[gutter] *= .03
    shaped = np.fft.ifft(spectrum).astype(np.complex64)
    shaped *= snr_scale / max(float(np.sqrt(np.mean(np.abs(shaped)**2))), 1e-12)
    time_s = np.arange(sample_count) / sample_rate_hz
    for tone in range(-4, 5):
        frequency = gutter_offset_hz + tone * STARLINK_TONE_SPACING_HZ
        shaped += (.45 * np.exp(1j * (2*np.pi*frequency*time_s + tone))).astype(np.complex64)
    lag = round(sample_rate_hz / STARLINK_FRAME_RATE_HZ)
    beacon_size = max(32, lag // 5)
    beacon = (rng.standard_normal(beacon_size) + 1j*rng.standard_normal(beacon_size)).astype(np.complex64)
    beacon /= np.sqrt(np.mean(np.abs(beacon)**2))
    for start in range(0, sample_count - beacon_size + 1, lag):
        shaped[start:start + beacon_size] += beacon * (.8 * snr_scale)
    noise = (rng.standard_normal(sample_count) + 1j*rng.standard_normal(sample_count)).astype(np.complex64)
    return shaped + noise * .15


def _quantize(channels: Sequence[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    scales = np.array([max(float(np.percentile(np.abs(values), 99.9)), 1e-12) / 30_000
                       for values in channels], dtype=np.float64)
    output = np.empty((len(channels), channels[0].size, 2), np.int16)
    for index, (values, scale) in enumerate(zip(channels, scales, strict=True)):
        output[index, :, 0] = np.clip(np.real(values) / scale, -32768, 32767).astype(np.int16)
        output[index, :, 1] = np.clip(np.imag(values) / scale, -32768, 32767).astype(np.int16)
    return output, scales


def read_event_iq(path: Path) -> tuple[list[np.ndarray], float]:
    value = np.load(path)
    raw, scales = value["iq_int16"], value["scale_per_channel"]
    channels = [(raw[index, :, 0].astype(np.float32) + 1j*raw[index, :, 1].astype(np.float32)) * scales[index]
                for index in range(raw.shape[0])]
    return channels, float(value["sample_rate_hz"])


def observe_blocks(blocks: Iterable[tuple[int, Sequence[np.ndarray]]], output_dir: Path, *,
                   sample_rate_hz: float, channel: StarlinkChannel, duration_s: float,
                   ring_seconds: float = 2.0, thresholds: dict | None = None,
                   identity: dict | None = None) -> dict:
    """Analyze contiguous blocks and retain IQ only for the first promoted event."""
    if duration_s <= 0 or ring_seconds <= 0:
        raise ValueError("duration and ring length must be positive")
    options = dict(thresholds or {})
    observations, rings, total = [], None, 0
    event_path = None
    for utc_ns, incoming in blocks:
        values = [np.asarray(item, np.complex64) for item in incoming]
        if not values or any(item.ndim != 1 or item.size != values[0].size for item in values):
            raise ValueError("all receiver blocks must be equal one-dimensional arrays")
        if rings is None:
            ring_blocks = max(1, int(np.ceil(ring_seconds * sample_rate_hz / values[0].size)))
            rings = [deque(maxlen=ring_blocks) for _ in values]
        metrics = [analyze_starlink_block(item, sample_rate_hz, **options) for item in values]
        for ring, item in zip(rings, values, strict=True): ring.append(item.copy())
        observations.append({"utc_ns": int(utc_ns), "sample_index": total,
                             "receivers": [asdict(item) for item in metrics]})
        if event_path is None and any(item.promoted for item in metrics):
            retained = [np.concatenate(tuple(ring)) for ring in rings]
            count = min(item.size for item in retained); retained = [item[-count:] for item in retained]
            quantized, scales = _quantize(retained)
            output_dir.mkdir(parents=True, exist_ok=True); event_path = output_dir / "event_iq.npz"
            np.savez_compressed(event_path, iq_int16=quantized, scale_per_channel=scales,
                                sample_rate_hz=sample_rate_hz,
                                center_frequency_hz=channel.if_center_hz)
        total += values[0].size
        if total >= round(duration_s * sample_rate_hz): break
    if not observations:
        raise RuntimeError("source ended before producing a block")
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamps = np.asarray([item["utc_ns"] for item in observations], dtype=np.int64)
    expected_block_s = observations[1]["sample_index"] / sample_rate_hz if len(observations) > 1 else total / sample_rate_hz
    intervals = np.diff(timestamps) / 1e9
    max_timing_excess = (None if intervals.size == 0 else
                         float(np.max(np.maximum(0, intervals - expected_block_s))))
    identity_value = identity or {}
    continuity = ("synthetic_exact" if str(identity_value.get("kind", "")).startswith("fake")
                  else "not_sample-counter-verified")
    report = {"schema": "leo-tracker.starlink-observation/v1",
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "channel": asdict(channel), "sample_rate_hz": sample_rate_hz,
              "requested_duration_s": duration_s, "observed_samples": total,
              "observed_duration_s": total / sample_rate_hz,
              "receiver_count": len(observations[0]["receivers"]),
              "thresholds": options, "identity": identity_value,
              "continuity": continuity, "expected_block_duration_s": expected_block_s,
              "max_interblock_timing_excess_s": max_timing_excess,
              "block_count": len(observations),
              "promoted_blocks": sum(any(r["promoted"] for r in item["receivers"])
                                     for item in observations),
              "event_iq": None if event_path is None else event_path.name,
              "observations": observations}
    (output_dir / "observation.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def fake_blocks(*, sample_rate_hz: float, block_size: int, duration_s: float,
                receiver_count: int, signal: bool, seed: int = 0) -> Iterator[tuple[int, list[np.ndarray]]]:
    count = int(np.ceil(duration_s * sample_rate_hz / block_size))
    start = 1_700_000_000_000_000_000
    for block in range(count):
        channels = []
        for receiver in range(receiver_count):
            if signal:
                value = synthetic_starlink_block(sample_rate_hz, block_size,
                    seed=seed + block * 17 + receiver, gutter_offset_hz=120_000 + receiver*6_000)
            else:
                rng = np.random.default_rng(seed + block * 17 + receiver)
                value = (rng.standard_normal(block_size) + 1j*rng.standard_normal(block_size)).astype(np.complex64)
            channels.append(value)
        yield start + round(block * block_size * 1e9 / sample_rate_hz), channels


def threaded_source_blocks(source, *, paired: bool, queue_blocks: int = 8
                           ) -> Iterator[tuple[int, list[np.ndarray]]]:
    """Overlap hardware refills with analysis using a bounded lossless queue."""
    if queue_blocks < 1:
        raise ValueError("queue_blocks must be at least one")
    pending: queue.Queue = queue.Queue(maxsize=queue_blocks)
    stop = threading.Event(); sentinel = object(); failure: list[BaseException] = []

    def produce():
        try:
            for block in source.blocks():
                if stop.is_set(): break
                values = [block.rx0, block.rx1] if paired else [block.samples]
                item = (block.utc_ns or 0, values)
                while not stop.is_set():
                    try: pending.put(item, timeout=.1); break
                    except queue.Full: pass
        except BaseException as exc:  # forwarded on the consumer thread
            failure.append(exc)
        finally:
            while not stop.is_set():
                try: pending.put(sentinel, timeout=.1); break
                except queue.Full: pass

    worker = threading.Thread(target=produce, name="starlink-radio-reader", daemon=True)
    worker.start()
    try:
        while True:
            item = pending.get()
            if item is sentinel:
                if failure: raise failure[0]
                return
            yield item
    finally:
        stop.set(); worker.join(timeout=2)
