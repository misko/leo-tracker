import numpy as np
import pytest

from leo_tracker.fusion import fit_doppler_track
from leo_tracker.radio import doppler_signal, extract_frequency_ridge


def test_extracted_doppler_prefers_correct_curve_to_time_shifted_control():
    sample_rate_hz = 20_000
    duration_s = 2.0

    def geometric_doppler(time_s):
        centered = time_s - duration_s / 2
        return 900 * np.tanh(-2.2 * centered) + 55 * centered**2

    receiver_offset_hz = 1_250.0
    receiver_drift_hz_s = 7.0
    samples = doppler_signal(
        lambda time: geometric_doppler(time) + receiver_offset_hz + receiver_drift_hz_s * (time - duration_s / 2),
        sample_rate_hz,
        duration_s,
        amplitude=0.3,
        noise_std=0.025,
        seed=8,
    )
    track = extract_frequency_ridge(
        samples,
        sample_rate_hz,
        fft_size=1024,
        hop_size=256,
        search_hz=(-2_000, 3_000),
        max_step_hz=100,
    )
    times = np.array([point.time_s for point in track.points])
    observed = np.array([point.frequency_hz for point in track.points])
    uncertainty = np.array([point.uncertainty_hz for point in track.points])

    correct = fit_doppler_track(times, observed, geometric_doppler(times), uncertainty)
    wrong = fit_doppler_track(
        times,
        observed,
        geometric_doppler(np.clip(times + 0.35, 0, duration_s)),
        uncertainty,
    )

    assert correct.frequency_offset_hz == pytest.approx(receiver_offset_hz, abs=20)
    assert correct.frequency_drift_hz_s == pytest.approx(receiver_drift_hz_s, abs=20)
    assert correct.residual_rms_hz < 15
    assert wrong.residual_rms_hz > correct.residual_rms_hz * 5


def test_fit_rejects_non_positive_uncertainty():
    with pytest.raises(ValueError, match="positive"):
        fit_doppler_track([0, 1, 2], [1, 2, 3], [0, 0, 0], [1, 0, 1])
