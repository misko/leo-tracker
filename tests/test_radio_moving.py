import numpy as np

from leo_tracker.radio.moving import detect_moving_comb, detect_moving_ridge
from leo_tracker.radio.synthetic import linear_chirp, tone


def test_weak_chirp_wins_over_strong_stationary_spur(tmp_path):
    rate, duration = 20_000, 3.0
    stationary = tone(3100, rate, duration, amplitude=.9)
    moving = linear_chirp(-1800, 1700, rate, duration, amplitude=.16)
    rng = np.random.default_rng(14)
    noise = .09 / np.sqrt(2) * (rng.standard_normal(stationary.size) + 1j * rng.standard_normal(stationary.size))
    values = (stationary + moving + noise).astype("<c8")
    path = tmp_path / "iq.c64"; values.tofile(path)
    result = detect_moving_ridge(path, rate, fft_size=512, hop_size=256,
                                 search_hz=(-5000, 5000), candidates_per_frame=12,
                                 max_step_hz=200)
    expected = -1800 + 3500 * np.array([p.time_s for p in result.points]) / duration
    observed = np.array([p.frequency_hz for p in result.points])
    assert np.median(np.abs(observed - expected)) < 70
    assert result.frequency_span_hz > 2500
    assert result.median_excess_db > 4
    assert abs(result.fitted_drift_hz_s - 3500 / duration) < 80
    assert any(abs(f - 3100) < 50 for f in result.stationary_spurs_hz)


def test_detector_memory_contract_and_validation(tmp_path):
    bad = tmp_path / "bad.c64"; bad.write_bytes(b"123")
    import pytest
    with pytest.raises(ValueError, match="whole number"):
        detect_moving_ridge(bad, 10_000)


def test_blind_comb_tracker_suppresses_unrelated_fixed_spurs(tmp_path):
    rate, duration, spacing = 20_000, 4.0, 900.0
    t = np.arange(round(rate * duration)) / rate
    drift = -220 + 120 * t
    values = np.zeros(t.size, np.complex64)
    for offset in range(-2, 3):
        phase = 2 * np.pi * np.cumsum(drift + offset * spacing) / rate
        values += .055 * np.exp(1j * phase)
    values += tone(4100, rate, duration, amplitude=.8)
    rng = np.random.default_rng(22)
    values += (.07 / np.sqrt(2) * (rng.standard_normal(t.size) + 1j * rng.standard_normal(t.size))).astype(np.complex64)
    path = tmp_path / "comb.c64"; values.astype("<c8").tofile(path)
    result = detect_moving_comb(path, rate, fft_size=1024, integration_s=.25,
        spectra_per_integration=6, tone_count=5, tone_spacing_hz=spacing,
        search_hz=(-1000, 1000), max_drift_hz_s=300)
    expected = -220 + 120 * np.array([p.time_s for p in result.points])
    observed = np.array([p.center_frequency_hz for p in result.points])
    assert np.median(np.abs(observed - expected)) < 35
    assert abs(result.fitted_drift_hz_s - 120) < 30
    assert result.median_positive_tone_fraction >= .8
    assert result.score_percentile > 90
