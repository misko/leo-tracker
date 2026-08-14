"""Capture-and-score driver shared by E2, E3, E4 and E5.

Probes are captured in small chunks and scored by a worker pool, then dropped.
Nothing large is ever held: /tmp is a shared 2 GB tmpfs that has filled twice
tonight, so the footprint stays at one chunk of IQ.  Results are appended to a
JSONL file as each chunk finishes, so a libiio teardown segfault -- which
happens after the work, not during it -- can never cost a measurement.
"""
from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT = Path(__file__).resolve().parent
PROBE = 100_000            # 20 ms at 5 MS/s = 15 Starlink frames
CHUNK = 10                 # probes captured before a scoring pass
WORKERS = 3


def _init() -> None:
    import pipeline
    pipeline.warm()


def _score(job):
    """(tag, receiver, index, int16 interleaved bytes) -> scored observation."""
    import pipeline
    tag, receiver, index, raw = job
    flat = np.frombuffer(raw, np.int16).astype(np.float32)
    samples = (flat[0::2] + 1j * flat[1::2]).astype(np.complex64)
    scored = pipeline.score_probe(samples)
    scored.update(tag=tag, receiver=receiver, index=index)
    return scored


def pack(samples: np.ndarray) -> bytes:
    flat = np.empty(2 * samples.size, np.int16)
    flat[0::2] = np.rint(samples.real)
    flat[1::2] = np.rint(samples.imag)
    return flat.tobytes()


# --------------------------------------------------------------------------
# empirical SNR, from the known transmitted stream
# --------------------------------------------------------------------------

def snr_terms(samples: np.ndarray, tx_fft_conj: np.ndarray,
              tx_energy: float) -> dict:
    """Coherent projection of the probe onto the known cyclic TX stream.

    The whole-probe rms difference the plan asks for saturates below about
    -70 dB of TX gain -- 1.40 counts against a 1.398 count floor -- because the
    injected power is then far under the noise.  Projecting onto the stream that
    was actually transmitted buys ~50 dB of coherent gain and measures the same
    quantity 15 dB further down; where both work they agree to 0.3 dB, which is
    checked and reported rather than assumed.

    ``signal_power`` is debiased by the mean over lags, so a probe whose argmax
    landed on noise contributes its own noise level rather than an
    extreme-value spike.
    """
    values = np.asarray(samples, np.complex128)
    corr = np.fft.ifft(np.fft.fft(values) * tx_fft_conj)
    power = np.abs(corr) ** 2
    peak = float(power.max())
    floor = float(power.mean())
    scale = tx_energy * values.size
    return {"signal_power": max(0.0, (peak - floor)) / scale,
            "signal_power_raw": peak / scale,
            "total_power": float(np.mean(np.abs(values) ** 2)),
            "lag": int(np.argmax(power))}


def tx_reference(wave: np.ndarray, probe: int = PROBE):
    stream = np.tile(np.asarray(wave, np.complex128), probe // wave.size)
    return np.conj(np.fft.fft(stream)), float(np.vdot(stream, stream).real)


# --------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------

def run(name: str, conditions, *, probes: int, rig, wave_for, out_path: Path,
        settle_s: float = 0.35, meta: dict | None = None) -> Path:
    """``conditions`` is a list of dicts; ``wave_for(condition)`` gives the TX.

    Each condition names its own TX gain and waveform, so one driver serves the
    gain ladder, the offset sweep and the occupancy draw.
    """
    from rig import TX_OFF_DB

    out_path.write_text("")
    header = {"record": "header", "experiment": name, "probe_samples": PROBE,
              "sample_rate_hz": 5_000_000.0,
              "rig": "CABLED LOOPBACK: TX2 -> SMA tee -> 2x 30 dB -> RX1, RX2",
              "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "conditions": conditions, "probes_per_condition": probes,
              **(meta or {})}
    with out_path.open("a") as handle:
        handle.write(json.dumps(header) + "\n")

    pool = mp.get_context("spawn").Pool(WORKERS, initializer=_init)
    try:
        current = None
        pushed = None
        cache: dict = {}
        for condition in conditions:
            wave = wave_for(condition)
            if wave is not pushed:
                rig.start_tx(wave)
                pushed = wave
                current = None
            key = id(wave)
            if key not in cache:
                cache.clear()
                cache[key] = tx_reference(wave)
            tx_fft_conj, tx_energy = cache[key]
            gain = rig.set_tx2_gain(condition["tx2_gain_db"])
            if gain != current:
                time.sleep(settle_s)
                current = gain
            rig.flush_rx(2)
            done = 0
            while done < probes:
                take = min(CHUNK, probes - done)
                jobs, levels_rows = [], []
                for _ in range(take):
                    rx1, rx2 = rig.capture()
                    for receiver, samples in ((1, rx1), (2, rx2)):
                        jobs.append((condition["tag"], receiver, done, pack(samples)))
                        levels_rows.append(snr_terms(samples, tx_fft_conj, tx_energy))
                    done += 1
                scored = pool.map(_score, jobs, chunksize=1)
                with out_path.open("a") as handle:
                    for row, extra in zip(scored, levels_rows):
                        row.update(extra)
                        row.update(tx2_gain_db=gain,
                                   offset_hz=condition.get("offset_hz", 0.0),
                                   transmitting=condition.get("transmitting", True))
                        handle.write(json.dumps(row) + "\n")
                del jobs
            print(f"[{name}] {condition['tag']}: {probes} probes "
                  f"({time.strftime('%H:%M:%S')})", flush=True)
    finally:
        rig.set_tx2_gain(TX_OFF_DB)
        pool.close()
        pool.join()
    return out_path
