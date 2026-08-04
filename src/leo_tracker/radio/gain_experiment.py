"""Controlled manual/AGC comparison using measurement-preserving artifacts."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time

import numpy as np

from .measurement import capture_measurement_waterfall, load_measurement_waterfall
from .pluto import PairedPlutoSource
from .source import RadioConfig


def gain_profiles(manual_gains_db=(20.0, 30.0, 40.0, 50.0)) -> list[tuple[str, float | None]]:
    return [("manual", float(gain)) for gain in manual_gains_db] + [
        ("slow_attack", None), ("fast_attack", None)]


def _fake_blocks(snapshots: int, block_size: int, sample_rate_hz: float, *,
                 amplitude: float, seed: int):
    rng = np.random.default_rng(seed); start = 1_700_000_000_000_000_000
    for index in range(snapshots):
        burst = 8 if snapshots//3 <= index < snapshots//2 else 1
        values = []
        for receiver in range(2):
            noise = (rng.normal(size=block_size) + 1j*rng.normal(size=block_size)) * amplitude
            tone = burst*amplitude*np.exp(2j*np.pi*(.08 + receiver*.01)*np.arange(block_size))
            values.append((noise + tone).astype(np.complex64))
        yield start + round(index*block_size*1e9/sample_rate_hz), values


def run_gain_experiment(output_dir: Path, *, center_frequency_hz: float,
                        sample_rate_hz: float, bandwidth_hz: float,
                        manual_gains_db=(20.0, 30.0, 40.0, 50.0),
                        snapshots: int = 64, block_size: int = 262_144,
                        fft_size: int = 16_384, output_bins: int = 4096,
                        lnb_lo_hz: float | None = None,
                        uri: str = "pluto://ip:192.168.2.1", settle_seconds: float = 2,
                        discard_buffers: int = 1, adc_full_scale: float | None = None,
                        fake: bool = False, seed: int = 0) -> dict:
    root = Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"gain experiment output is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    records = []
    for profile_index, (mode, gain) in enumerate(gain_profiles(manual_gains_db)):
        stem = f"{profile_index:02d}-{mode}" + ("" if gain is None else f"-{gain:g}db")
        artifact_path = root / f"{stem}.npz"
        if fake:
            amplitude = 10 ** ((gain or 35) / 40) / 20
            blocks = _fake_blocks(snapshots, block_size, sample_rate_hz,
                                  amplitude=amplitude, seed=seed+profile_index)
            fixed_gain = (gain, gain) if gain is not None else (35.0, 36.0)
            gain_reader = lambda fixed_gain=fixed_gain: fixed_gain
            identity = {"kind": "fake-gain-experiment"}
            source = None
        else:
            config = RadioConfig(center_frequency_hz, sample_rate_hz, bandwidth_hz,
                                 gain, 0, mode)
            source = PairedPlutoSource(config, uri=uri, block_size=block_size)
            if settle_seconds > 0:
                time.sleep(settle_seconds)
            iterator = iter(source.blocks())
            for _ in range(discard_buffers): next(iterator)
            blocks = ((block.utc_ns, [block.rx0, block.rx1]) for block in iterator)
            gain_reader = source.gain_snapshot
            identity = dict(source.identity)
        try:
            capture = capture_measurement_waterfall(
                blocks, artifact_path, sample_rate_hz=sample_rate_hz,
                center_frequency_hz=center_frequency_hz, bandwidth_hz=bandwidth_hz,
                snapshots=snapshots, fft_size=fft_size, output_bins=output_bins,
                samples_per_snapshot=block_size, lnb_lo_hz=lnb_lo_hz,
                gain_mode=mode, configured_gain_db=gain, gain_reader=gain_reader,
                adc_full_scale=adc_full_scale, identity=identity)
        finally:
            if source is not None: source.close()
        artifact = load_measurement_waterfall(artifact_path)
        clips = np.asarray(artifact["clip_fraction"], float)
        record = {"profile": stem, "gain_mode": mode, "configured_gain_db": gain,
                  "artifact": str(artifact_path), "capture": capture,
                  "median_rms_raw": np.nanmedian(artifact["rms_raw"], axis=1).tolist(),
                  "median_peak_raw": np.nanmedian(artifact["peak_raw"], axis=1).tolist(),
                  "median_hardware_gain_db": np.nanmedian(artifact["hardware_gain_db"], axis=1).tolist(),
                  "maximum_clip_fraction": None if np.all(np.isnan(clips)) else float(np.nanmax(clips))}
        records.append(record)
        (root / f"{stem}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    report = {"schema": "leo-tracker.gain-experiment/v1", "profiles": records,
              "invariants": {"center_frequency_hz": center_frequency_hz,
                  "sample_rate_hz": sample_rate_hz, "bandwidth_hz": bandwidth_hz,
                  "block_size": block_size, "snapshots": snapshots, "lnb_lo_hz": lnb_lo_hz}}
    (root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report
