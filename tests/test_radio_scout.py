import numpy as np

from leo_tracker.radio.scout import summarize_samples


def test_spectrum_summary_detects_tone_without_false_clipping():
    count = 4096
    time = np.arange(count) / count
    samples = (100 * np.exp(2j * np.pi * 137 * time)).astype(np.complex64)

    summary = summarize_samples(samples, 575e6)

    assert summary.center_frequency_hz == 575e6
    assert summary.power_dbfs < 0
    assert summary.peak_excess_db > 40
    assert summary.clipped_fraction == 0


def test_spectrum_summary_flags_adc_rail():
    samples = np.full(64, 2047 + 0j, dtype=np.complex64)
    assert summarize_samples(samples, 1e9).clipped_fraction == 1
