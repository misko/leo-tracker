import numpy as np

from leo_tracker.radio.events import detect_spectral_events


def test_segments_short_chirp_without_forcing_full_duration_track():
    rng = np.random.default_rng(3)
    times = np.arange(100, dtype=float)
    frequencies = np.arange(256, dtype=float) * 10_000
    values = rng.normal(0, .04, (times.size, frequencies.size))
    for time_index in range(25, 46):
        center = 80 + (time_index - 25)
        values[time_index, center-2:center+3] += 1.2

    events = detect_spectral_events(values, times, frequencies, threshold_db=.25,
                                    min_support_pixels=20)

    positive = max((event for event in events if event.polarity == "positive"),
                   key=lambda event: event.support_pixels)
    assert 23 <= positive.start_time_s <= 27
    assert 44 <= positive.stop_time_s <= 47
    assert len(positive.centroid_hz) < len(times) / 2
    assert positive.centroid_hz[-1] > positive.centroid_hz[0]
    assert not positive.broadband


def test_distinguishes_negative_block_and_full_band_event():
    rng = np.random.default_rng(7)
    times = np.arange(80, dtype=float)
    frequencies = np.arange(128, dtype=float) * 20_000
    values = rng.normal(0, .03, (80, 128))
    values[10:20, :] += 1.0
    values[40:60, 45:65] -= 1.0

    events = detect_spectral_events(values, times, frequencies, threshold_db=.25,
                                    min_support_pixels=20, broadband_fraction=.6)

    assert any(event.polarity == "positive" and event.broadband for event in events)
    negative = [event for event in events if event.polarity == "negative" and not event.broadband]
    assert negative and negative[0].start_time_s <= 41 and negative[0].stop_time_s >= 58


def test_noise_only_produces_no_events():
    rng = np.random.default_rng(9)
    values = rng.normal(0, .03, (60, 128))
    assert detect_spectral_events(values, np.arange(60), np.arange(128),
                                  threshold_db=.3) == []
