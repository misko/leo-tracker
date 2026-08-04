import json

import numpy as np
import pytest

from leo_tracker.radio import cli
from leo_tracker.radio.measurement import MEASUREMENT_SCHEMA
from leo_tracker.radio.tuning_dither import (
    center_transitions_in_interval, compare_tuning_dither,
    dither_phase_locked, reconstruct_interleaved_spectra,
    retune_transient_confounded,
)


def _artifact(path, profile, center):
    count, bins = 20, profile.size
    shape = (2, count)
    spectra = np.asarray([[profile for _ in range(count)] for _ in range(2)], np.float32)
    np.savez_compressed(path, schema=np.array(MEASUREMENT_SCHEMA),
        psd_db_raw_per_hz=spectra, utc_ns=np.arange(count, dtype=np.int64)*1_000_000_000,
        frequency_offsets_hz=(np.arange(bins)-bins//2)*10_000.0,
        sample_rate_hz=2_560_000.0, bandwidth_hz=2_000_000.0,
        center_frequency_hz=center, fft_size=bins, samples_per_snapshot=4096,
        rms_raw=np.ones(shape), peak_raw=np.ones(shape), crest_factor_db=np.zeros(shape),
        clip_fraction=np.zeros(shape), hardware_gain_db=np.full(shape, 50.0),
        gain_mode=np.array("manual"), configured_gain_db=np.array(50.0),
        identity_json=np.array("{}"), lnb_lo_hz=np.array(9_750_000_000.0))


def test_dither_distinguishes_sky_fixed_from_baseband_fixed(tmp_path):
    rng = np.random.default_rng(9); bins = 256; profile = rng.normal(0, 1, bins)
    first, sky, baseband = tmp_path/"first.npz", tmp_path/"sky.npz", tmp_path/"bb.npz"
    _artifact(first, profile, 1_800_000_000.0)
    # Tuning upward 200 kHz moves a fixed RF spectrum left by 20 bins.
    _artifact(sky, np.roll(profile, -20), 1_800_200_000.0)
    _artifact(baseband, profile, 1_800_200_000.0)
    assert compare_tuning_dither(first, sky)["classification"] == "sky-fixed"
    assert compare_tuning_dither(first, baseband)["classification"] == "baseband-fixed"


def test_dither_compare_cli_writes_machine_readable_report(tmp_path, capsys):
    rng = np.random.default_rng(10); profile = rng.normal(0, 1, 256)
    first, second, output = tmp_path/"a.npz", tmp_path/"b.npz", tmp_path/"result.json"
    _artifact(first, profile, 1_800_000_000.0)
    _artifact(second, np.roll(profile, -20), 1_800_200_000.0)
    assert cli.main(["starlink-dither-compare", str(first), str(second), str(output)]) == 0
    terminal = json.loads(capsys.readouterr().out); report = json.loads(output.read_text())
    assert terminal["classification"] == "sky-fixed"
    assert report["tuning_dither_hz"] == 200_000


def test_interleaved_sky_tone_reconstructs_to_one_frequency():
    bins = 256; frequencies = np.linspace(-500_000, 500_000, bins, endpoint=False)
    centers = np.array([1.8e9, 1.8002e9]*4)
    spectra = np.zeros((2, len(centers), bins))
    for time_index, center in enumerate(centers):
        offset = 300_000-(center-1.8e9)
        spectra[:, time_index, np.argmin(abs(frequencies-offset))] = 10
    rebuilt, report = reconstruct_interleaved_spectra({
        "psd_db_raw_per_hz": spectra, "frequency_offsets_hz": frequencies,
        "center_frequency_hz": 1.8e9,
        "center_frequency_hz_by_snapshot": centers})
    assert len(set(np.argmax(rebuilt[0], axis=1))) == 1
    assert report["exposure_fraction"] == [.5, .5]
    assert report["reconstructed_to_nominal_rf_axis"]


def test_counts_only_tuning_transitions_crossed_by_an_event():
    times = np.arange(10, dtype=float)
    centers = np.array([0, 0, 0, 1, 1, 1, 0, 0, 0, 1], dtype=float)
    assert center_transitions_in_interval(times, centers, 1, 8) == 2
    assert center_transitions_in_interval(times, centers, 3, 5) == 0


def test_retune_transient_guard_rejects_boundary_locked_events():
    transitions = np.array([3.0, 6.0, 9.0])
    assert retune_transient_confounded(6.33, transitions) == (True, pytest.approx(.33))
    assert retune_transient_confounded(7.25, transitions) == (False, pytest.approx(1.25))
    assert retune_transient_confounded(.2, transitions)[0]


def test_population_phase_lock_requires_repeated_common_dither_phase():
    locked, center = dither_phase_locked([2.60, 2.61, 2.62, 2.59, 2.64, 1.1])
    assert locked and center == pytest.approx(2.605, abs=.02)
    assert not dither_phase_locked([.2, 1.1, 2.0, 3.0, 4.0, 4.8])[0]
    assert not dither_phase_locked([2.6]*4)[0]
