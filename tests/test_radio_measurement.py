import json

import numpy as np
import pytest

from leo_tracker.radio.measurement import (capture_measurement_waterfall,
                                            load_measurement_waterfall,
                                            measure_block)
from leo_tracker.radio.source import RadioConfig
from leo_tracker.radio import cli


def test_absolute_psd_tracks_gain_while_frequency_and_crest_are_stable():
    rate, count, tone = 1_000_000, 65_536, 123_000
    time = np.arange(count) / rate
    samples = np.exp(2j * np.pi * tone * time).astype(np.complex64)
    first = measure_block(samples, rate, fft_size=4096, output_bins=1024)
    second = measure_block(2 * samples, rate, fft_size=4096, output_bins=1024)

    assert np.max(second.psd_db_raw_per_hz) - np.max(first.psd_db_raw_per_hz) == pytest.approx(6.0206, abs=.02)
    assert np.argmax(second.psd_db_raw_per_hz) == np.argmax(first.psd_db_raw_per_hz)
    assert second.crest_factor_db == pytest.approx(first.crest_factor_db, abs=.01)


def test_clipping_metric_and_gain_mode_validation():
    samples = np.array([0, 1 + 2j, 9 + 0j, -10j] * 256, np.complex64)
    measured = measure_block(samples, 1_000_000, fft_size=1024, output_bins=64,
                             adc_full_scale=8)
    assert measured.clip_fraction == .5
    RadioConfig(1e9, 1e6, .9e6, None, 0, "fast_attack")
    with pytest.raises(ValueError):
        RadioConfig(1e9, 1e6, .9e6, 20, 0, "fast_attack")


def test_v2_artifact_preserves_power_timing_gain_and_duty(tmp_path):
    rate, block_size, snapshots = 1_000_000, 4096, 8
    rng = np.random.default_rng(4)
    start = 1_700_000_000_000_000_000
    def blocks():
        for index in range(snapshots):
            values = [(rng.normal(size=block_size) + 1j*rng.normal(size=block_size)).astype(np.complex64)
                      for _ in range(2)]
            yield start + index * 10_000_000, values, 2_000_000 + index
    gains = iter([(31.0, 32.0)] * snapshots)
    path = tmp_path / "measurement.npz"

    report = capture_measurement_waterfall(
        blocks(), path, sample_rate_hz=rate, center_frequency_hz=1.8e9,
        bandwidth_hz=.9e6, snapshots=snapshots, fft_size=1024, output_bins=256,
        gain_mode="slow_attack", gain_reader=lambda: next(gains), lnb_lo_hz=9.75e9,
        identity={"kind": "fake"})
    artifact = load_measurement_waterfall(path)

    assert artifact["psd_db_raw_per_hz"].shape == (2, snapshots, 256)
    assert artifact["hardware_gain_db"].tolist() == [[31] * snapshots, [32] * snapshots]
    assert artifact["read_duration_ns"].tolist() == [2_000_000 + index
                                                     for index in range(snapshots)]
    assert report["retained_sample_time_s"] == pytest.approx(snapshots * block_size / rate)
    assert report["duty_fraction"] < .5
    assert json.loads(str(artifact["identity_json"])) == {"kind": "fake"}


def test_measurement_preserves_compact_snapshot_observer_scores(tmp_path):
    snapshots, block_size = 4, 2048
    def blocks():
        for index in range(snapshots):
            yield 1_700_000_000_000_000_000+index, [
                np.ones(block_size, np.complex64), np.ones(block_size, np.complex64)]
    path = tmp_path/"scores.npz"
    capture_measurement_waterfall(blocks(), path, sample_rate_hz=1e6,
        center_frequency_hz=1.8e9, bandwidth_hz=.8e6, snapshots=snapshots,
        fft_size=1024, output_bins=256,
        snapshot_observer=lambda index, *_: None if index == 0 else index/10)
    artifact = load_measurement_waterfall(path)
    assert np.isnan(artifact["snapshot_observer_score_db"][0])
    assert artifact["snapshot_observer_score_db"][1:].tolist() == pytest.approx([.1, .2, .3])


def test_measurement_preserves_per_snapshot_tuning_centers(tmp_path):
    centers = [1.8e9, 1.8e9, 1.8005e9, 1.8005e9]
    blocks = [(index, [np.ones(2048, np.complex64)]*2, 10, center)
              for index, center in enumerate(centers)]
    path = tmp_path/"dither.npz"
    report = capture_measurement_waterfall(iter(blocks), path, sample_rate_hz=1e6,
        center_frequency_hz=1.8e9, bandwidth_hz=.8e6, snapshots=4,
        fft_size=1024, output_bins=256)
    artifact = load_measurement_waterfall(path)
    assert artifact["center_frequency_hz_by_snapshot"].tolist() == centers
    assert report["center_exposure_fraction"] == {"1800000000.0": .5,
                                                   "1800500000.0": .5}


def test_measurement_cli_fake_interleaved_dither_is_balanced(tmp_path, capsys):
    path = tmp_path/"interleaved.npz"
    assert cli.main(["starlink-measurement-capture", str(path), "--fake",
        "--snapshots", "12", "--block-size", "2048", "--fft-size", "1024",
        "--output-bins", "256", "--sample-rate-hz", "1000000",
        "--bandwidth-hz", "800000", "--interleaved-dither-hz", "500000",
        "--dither-segment-s", ".004096", "--dither-discard-buffers", "1"]) == 0
    capsys.readouterr(); artifact = load_measurement_waterfall(path)
    centers = artifact["center_frequency_hz_by_snapshot"]
    assert set(centers) == {1_830_117_187.5, 1_830_617_187.5}
    assert np.count_nonzero(centers == centers.min()) == 6


def test_quantized_psd_round_trip_halves_storage_without_losing_db_precision(tmp_path):
    rng = np.random.default_rng(44)
    snapshots, block_size = 12, 4096
    captured = [(1_700_000_000_000_000_000+index, [
        (rng.normal(size=block_size)+1j*rng.normal(size=block_size)).astype(np.complex64)
        for _ in range(2)]) for index in range(snapshots)]
    floating = tmp_path/"floating.npz"; quantized = tmp_path/"quantized.npz"
    common = dict(sample_rate_hz=1_000_000, center_frequency_hz=1.8e9,
                  bandwidth_hz=800_000, snapshots=snapshots,
                  fft_size=1024, output_bins=1024)
    capture_measurement_waterfall(iter(captured), floating, **common)
    report = capture_measurement_waterfall(iter(captured), quantized,
        psd_quantization_db=.01, **common)

    original = load_measurement_waterfall(floating)["psd_db_raw_per_hz"]
    restored = load_measurement_waterfall(quantized)["psd_db_raw_per_hz"]
    with np.load(quantized, allow_pickle=False) as stored:
        assert stored["psd_db_raw_per_hz"].dtype == np.int16
        assert float(stored["psd_db_quantization_db"]) == .01
    assert np.max(np.abs(restored-original)) <= .0051
    assert quantized.stat().st_size < .65*floating.stat().st_size
    assert report["frequency_bin_width_hz"] == pytest.approx(976.5625)
    assert report["psd_quantization_db"] == .01


def test_psd_quantization_rejects_invalid_resolution(tmp_path):
    blocks = [(1, [np.ones(2048, np.complex64)]*2),
              (2, [np.ones(2048, np.complex64)]*2)]
    with pytest.raises(ValueError, match="quantization"):
        capture_measurement_waterfall(iter(blocks), tmp_path/"bad.npz",
            sample_rate_hz=1e6, center_frequency_hz=1.8e9,
            bandwidth_hz=.8e6, snapshots=2, fft_size=1024,
            output_bins=256, psd_quantization_db=.2)


def test_measurement_cli_discards_settling_buffers_and_records_temperature(tmp_path, capsys):
    path = tmp_path/"measurement.npz"
    assert cli.main(["starlink-measurement-capture", str(path), "--fake",
        "--snapshots", "4", "--block-size", "2048", "--fft-size", "1024",
        "--output-bins", "256", "--sample-rate-hz", "1000000",
        "--bandwidth-hz", "800000", "--discard-buffers", "3",
        "--host-temperature-c", "52.5", "--radio-temperature-c", "55.25"]) == 0
    capsys.readouterr()
    artifact = load_measurement_waterfall(path)
    identity = json.loads(str(artifact["identity_json"]))
    assert identity["discarded_settling_buffers"] == 3
    assert identity["host_temperature_c"] == 52.5
    assert identity["radio_temperature_c"] == 55.25
    assert int(artifact["utc_ns"][0]) == 1_700_000_000_300_000_000
