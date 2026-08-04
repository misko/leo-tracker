import numpy as np
import pytest

from leo_tracker.radio.doppler_fit import (fit_doppler_segments,
                                            fit_supported_doppler)


def test_recovers_supported_quadratic_track_and_uncertainty():
    rng = np.random.default_rng(2)
    times = np.arange(60, dtype=float)
    centered = times - np.mean(times)
    frequency = 1_000_000 - 4200*centered + 8*centered**2 + rng.normal(0, 700, times.size)

    fit = fit_supported_doppler(times, frequency, carrier_hz=11.575e9, order=2)

    assert fit.start_time_s == 0 and fit.stop_time_s == 59
    assert fit.drift_at_reference_hz_s == pytest.approx(-4200, abs=100)
    assert fit.coefficients_hz[2] == pytest.approx(8, abs=2)
    assert fit.residual_rms_hz < 1000
    assert fit.radial_acceleration_m_s2 > 100
    assert fit.drift_uncertainty_hz_s < 20


def test_channel_hop_is_split_and_never_fit_as_extreme_doppler():
    times = np.arange(40, dtype=float)
    frequency = 1e6 + 1000*times
    frequency[20:] += 2e6
    with pytest.raises(ValueError, match="channel hop"):
        fit_supported_doppler(times, frequency, carrier_hz=11.575e9,
                              hop_threshold_hz=500_000)

    fits = fit_doppler_segments(times, frequency, carrier_hz=11.575e9,
                                order=1, hop_threshold_hz=500_000)
    assert len(fits) == 2
    assert all(item.drift_at_reference_hz_s == pytest.approx(1000) for item in fits)
