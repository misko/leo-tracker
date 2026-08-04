from datetime import datetime, timedelta, timezone

import numpy as np

from leo_tracker.radio.tle_search import search_tle_doppler


def _catalog(start: datetime, doppler=(-120_000, 0, 120_000), duration_s=20):
    times = [start, start+timedelta(seconds=duration_s/2),
             start+timedelta(seconds=duration_s)]
    points = [{"time": t.isoformat().replace("+00:00", "Z"),
               "expected_doppler_hz": value, "range_rate_km_s": 0}
              for t, value in zip(times, doppler)]
    return {"satellites": [{"name": "STARLINK-SYNTHETIC", "norad_id": 42,
        "passes": [{"rise": points[0],
                    "culmination": {**points[1], "elevation_deg": 70},
                    "set": points[2]}]}]}


def test_tle_guided_search_finds_moving_dual_receiver_signal():
    rng = np.random.default_rng(3)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins, bin_hz = 201, 256, 7_500
    utc = np.array([(start+timedelta(seconds=i/10)).timestamp()*1e9
                    for i in range(count)], dtype=np.int64)
    frequencies = (np.arange(bins)-bins//2)*bin_hz
    spectra = rng.normal(0, .04, (2, count, bins))
    shifts = np.rint(np.linspace(-120_000, 120_000, count)/bin_hz).astype(int)
    for receiver, base in enumerate((80, 170)):
        spectra[receiver, np.arange(count), base+shifts] += 1.2

    report = search_tle_doppler(spectra, utc, frequencies, _catalog(start))

    assert report["qualified_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["qualified"]
    assert candidate["predicted_doppler_span_bins"] >= 30
    assert [point["expected_doppler_hz"] for point in candidate["predicted_points"]] == [
        -120_000, 0, 120_000]
    assert candidate["joint_activity_fraction"] > .95
    assert candidate["stationary_improvement_db"] > .5


def test_tle_guided_search_finds_negative_moving_dual_receiver_signal():
    rng = np.random.default_rng(31)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins, bin_hz = 201, 256, 7_500
    utc = np.array([(start+timedelta(seconds=i/10)).timestamp()*1e9
                    for i in range(count)], dtype=np.int64)
    frequencies = (np.arange(bins)-bins//2)*bin_hz
    spectra = rng.normal(0, .04, (2, count, bins))
    shifts = np.rint(np.linspace(-120_000, 120_000, count)/bin_hz).astype(int)
    for receiver, base in enumerate((80, 170)):
        spectra[receiver, np.arange(count), base+shifts] -= 1.2

    report = search_tle_doppler(spectra, utc, frequencies, _catalog(start))

    assert report["qualified_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["qualified"]
    assert candidate["signal_model"] == "single-tone-negative"
    assert candidate["polarity"] == "negative"
    assert candidate["stationary_improvement_db"] > .5


def test_tle_guided_search_does_not_force_noise_candidate():
    rng = np.random.default_rng(4)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins = 101, 128
    utc = np.array([(start+timedelta(seconds=i/5)).timestamp()*1e9
                    for i in range(count)], dtype=np.int64)
    frequencies = (np.arange(bins)-bins//2)*7_500
    spectra = rng.normal(0, .05, (2, count, bins))

    report = search_tle_doppler(spectra, utc, frequencies, _catalog(start))

    assert report["qualified_count"] == 0
    assert not report["candidates"][0]["qualified"]
    assert report["candidates"][0]["rejection_reasons"]


def test_tle_guided_search_recovers_one_short_beam_dwell_in_long_capture():
    rng = np.random.default_rng(12)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins, bin_hz = 601, 256, 7_500
    elapsed = np.linspace(0, 120, count)
    utc = np.asarray([(start+timedelta(seconds=float(value))).timestamp()*1e9
                      for value in elapsed], dtype=np.int64)
    frequencies = (np.arange(bins)-bins//2)*bin_hz
    spectra = rng.normal(0, .04, (2, count, bins))
    doppler = np.linspace(-120_000, 120_000, count)
    shifts = np.rint(doppler/bin_hz).astype(int)
    active = (elapsed >= 42) & (elapsed <= 67)
    active_rows = np.flatnonzero(active)
    for receiver, base in enumerate((80, 170)):
        spectra[receiver, active_rows, base+shifts[active_rows]] += 1.2

    report = search_tle_doppler(
        spectra, utc, frequencies, _catalog(start, duration_s=120))

    assert report["qualified_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["qualified"]
    assert 25 <= candidate["window_duration_s"] <= 30.1
    assert candidate["joint_activity_fraction"] > .5
    assert candidate["stationary_improvement_db"] > .3


def test_tle_guided_nine_tone_comb_recovers_subthreshold_individual_tones():
    rng = np.random.default_rng(21)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins, bin_hz = 201, 256, 7_500
    utc = np.asarray([(start+timedelta(seconds=index/10)).timestamp()*1e9
                      for index in range(count)], dtype=np.int64)
    frequencies = (np.arange(bins)-bins//2)*bin_hz
    spectra = rng.normal(0, .025, (2, count, bins))
    shifts = np.rint(np.linspace(-120_000, 120_000, count)/bin_hz).astype(int)
    teeth = np.rint(np.arange(-4, 5)*43_900/bin_hz).astype(int)
    for receiver, base in enumerate((80, 170)):
        for tooth in teeth:
            spectra[receiver, np.arange(count), base+tooth+shifts] += .25

    report = search_tle_doppler(spectra, utc, frequencies, _catalog(start))

    assert report["qualified_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["signal_model"] == "nine-tone-43.9khz-comb"
    assert candidate["joint_score_db"] > .3
    assert candidate["stationary_improvement_db"] > .2
    assert min(receiver["comb_spacing_improvement_db"]
               for receiver in candidate["receivers"]) > .2


def test_correlated_broadband_burst_cannot_pass_comb_controls():
    rng = np.random.default_rng(33)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins, bin_hz = 201, 256, 7_500
    utc = np.asarray([(start+timedelta(seconds=index/10)).timestamp()*1e9
                      for index in range(count)], dtype=np.int64)
    frequencies = (np.arange(bins)-bins//2)*bin_hz
    spectra = rng.normal(0, .025, (2, count, bins))
    spectra[:, 40:160, :] += .5

    report = search_tle_doppler(spectra, utc, frequencies, _catalog(start))

    assert report["qualified_count"] == 0
    candidate = report["candidates"][0]
    assert not candidate["qualified"]
    assert ("stationary-path control" in " ".join(candidate["rejection_reasons"])
            or "wrong-spacing controls" in " ".join(candidate["rejection_reasons"]))
    assert "common broadband activity" in " ".join(candidate["rejection_reasons"])
    assert candidate["common_broadband_row_fraction"] > .2


def test_dense_tle_catalog_does_not_promote_noise_by_multiple_searches():
    rng = np.random.default_rng(44)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins = 201, 128
    utc = np.asarray([(start+timedelta(seconds=index/10)).timestamp()*1e9
                      for index in range(count)], dtype=np.int64)
    frequencies = (np.arange(bins)-bins//2)*7_500
    spectra = rng.normal(0, .05, (2, count, bins))
    satellites = []
    for index in range(40):
        span = 50_000 + index*5_000
        satellite = _catalog(start, doppler=(-span, 0, span))["satellites"][0]
        satellite = {**satellite, "name": f"STARLINK-NOISE-{index}",
                     "norad_id": 10_000+index}
        satellites.append(satellite)

    report = search_tle_doppler(
        spectra, utc, frequencies, {"satellites": satellites})

    assert report["overlapping_passes"] == 40
    assert report["qualified_count"] == 0
    assert all(not candidate["qualified"] for candidate in report["candidates"])
