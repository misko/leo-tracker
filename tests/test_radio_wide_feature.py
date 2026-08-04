from datetime import datetime, timedelta, timezone

import numpy as np

from leo_tracker.radio.wide_feature import (_internal_translation_path,
                                            estimate_global_frequency_correction,
                                            search_wide_features)


def _catalog(start, stop_shift=-75_000, midpoint_shift=0):
    midpoint = start+timedelta(seconds=20)
    stop = start+timedelta(seconds=40)
    def point(time, doppler, elevation=20):
        return {"time": time.isoformat().replace("+00:00", "Z"),
                "expected_doppler_hz": doppler, "elevation_deg": elevation}
    return {"carrier_hz": 11_325_117_187.5,
        "satellites": [{"name": "STARLINK-SYNTHETIC", "norad_id": 42,
        "passes": [{"rise": point(start, 75_000),
                    "culmination": point(midpoint, midpoint_shift, 70),
                    "set": point(stop, stop_shift)}]}]}


def _feature(moving=True, internal=False):
    count, bins, bin_hz = 41, 256, 7_500.0
    residual = np.zeros((2, count, bins))
    for row in range(8, 33):
        shift = -(row-8)//2 if moving else 0
        residual[:, row, 90+shift:141+shift] = -.55 if internal else -1
        if internal:
            for peak in (100, 112, 124, 136):
                residual[:, row, peak+shift-1:peak+shift+2] = -1
    elapsed = np.arange(count, dtype=float)
    axes = np.asarray([(np.arange(bins)-bins//2)*bin_hz+offset
                       for offset in (11_325_000_000, 11_325_030_000)])
    return residual, elapsed, axes


def test_wide_feature_qualifies_common_motion_but_not_specific_tle_without_margin():
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    residual, elapsed, axes = _feature(moving=True)
    report = search_wide_features(residual, elapsed, axes,
        start_utc_s=start.timestamp(), pass_catalog=_catalog(start))
    candidate = report["candidates"][0]
    assert report["qualified_count"] == 1
    assert report["doppler_candidate_count"] == 1
    assert candidate["leo_like_qualified"]
    assert candidate["polarity"] == "negative"
    assert candidate["receiver_path_correlation"] > .99
    assert candidate["receivers"][0]["linear_drift_hz_s"] < 0
    assert candidate["tle_comparisons"][0]["stationary_improvement_hz"] > 0
    assert candidate["specific_tle_identifiable"]
    assert candidate["moving_rf_qualified"]
    assert candidate["doppler_candidate_qualified"]
    assert candidate["internal_translation_consistent"]
    assert not candidate["orbital_shape_qualified"]
    assert not candidate["orbital_curvature_observable"]
    assert candidate["best_tle_curvature_resolution_bins"] < 1
    assert "less than one FFT bin" in " ".join(
        candidate["orbital_shape_rejection_reasons"])
    assert candidate["tle_comparisons"][0]["affine_drift_improvement_hz"] <= 7_500
    assert report["rf_carrier_hz"] == 11_325_117_187.5
    assert candidate["radial_acceleration_m_s2"] > 90
    assert candidate["radial_velocity_change_m_s"] > 2_000


def test_orbital_curvature_observability_is_measured_against_fft_resolution():
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    residual, elapsed, axes = _feature(moving=True)
    report = search_wide_features(residual, elapsed, axes,
        start_utc_s=start.timestamp(),
        pass_catalog=_catalog(start, midpoint_shift=60_000))

    candidate = report["candidates"][0]
    assert candidate["orbital_curvature_observable"]
    assert candidate["best_tle_predicted_curvature_rms_hz"] >= report["bin_width_hz"]
    assert candidate["best_tle_curvature_resolution_bins"] >= 1


def test_stationary_wide_feature_fails_doppler_controls():
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    residual, elapsed, axes = _feature(moving=False)
    report = search_wide_features(residual, elapsed, axes,
        start_utc_s=start.timestamp(), pass_catalog=_catalog(start))
    assert report["qualified_count"] == 0
    rejection = " ".join(report["candidates"][0]["rejection_reasons"])
    assert "drift direction" in rejection or "stationary control" in rejection


def test_partially_overlapping_tle_pass_is_not_endpoint_extrapolated():
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    residual, elapsed, axes = _feature(moving=True)
    catalog = _catalog(start)
    # The feature occupies roughly t=8..32 s, while this pass begins at t=15.
    # np.interp would otherwise manufacture a constant prediction for t=8..15.
    catalog["satellites"][0]["passes"][0]["rise"]["time"] = (
        start+timedelta(seconds=15)).isoformat().replace("+00:00", "Z")

    report = search_wide_features(residual, elapsed, axes,
        start_utc_s=start.timestamp(), pass_catalog=catalog)
    candidate = report["candidates"][0]

    assert candidate["tle_comparisons"] == []
    assert not candidate["leo_like_qualified"]
    assert "no complete TLE comparison" in " ".join(candidate["rejection_reasons"])


def test_capture_boundary_censoring_is_warning_not_doppler_rejection():
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    residual, elapsed, axes = _feature(moving=True)
    # Move the otherwise valid component to touch the first analyzed row.
    residual[:, :25] = residual[:, 8:33]
    residual[:, 25:] = 0
    report = search_wide_features(residual, elapsed, axes,
        start_utc_s=start.timestamp(), pass_catalog=_catalog(start))
    candidates = [item for item in report["candidates"]
                  if "capture start boundary" in " ".join(item["measurement_warnings"])]
    assert candidates
    assert "capture start boundary" not in " ".join(candidates[0]["rejection_reasons"])
    assert candidates[0]["moving_rf_qualified"]


def test_motion_compensated_internal_peaks_must_repeat_on_both_receivers():
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    residual, elapsed, axes = _feature(moving=True, internal=True)
    report = search_wide_features(residual, elapsed, axes,
        start_utc_s=start.timestamp(), pass_catalog=_catalog(start))
    candidate = report["candidates"][0]
    assert candidate["internal_profile_correlation"] > .99
    assert candidate["common_internal_peak_count"] >= 3
    spacings = candidate["common_internal_peak_spacings_hz"]
    assert sum(abs(value-90_000) <= 7_500 for value in spacings) >= 2


def test_global_texture_registration_recovers_receiver_wide_lo_translation():
    rng = np.random.default_rng(52)
    bins, bin_hz = 512, 7_500.0
    profile = rng.normal(0, 1, bins)
    profile = np.convolve(profile, np.ones(3)/3, mode="same")
    shifts = np.arange(-8, 9)
    spectra = np.asarray([[np.roll(profile, shift) for shift in shifts],
                          [np.roll(profile, shift) for shift in shifts]])
    correction, correlation = estimate_global_frequency_correction(
        spectra, bin_hz, maximum_shift_hz=75_000)
    expected = -shifts*bin_hz
    assert np.allclose(correction[0], expected)
    assert np.allclose(correction[1], expected)
    assert np.min(correlation) > .9


def test_internal_translation_rejects_centroid_motion_from_power_switching():
    bins, bin_hz = 96, 3_750.0
    x = np.arange(bins)
    left = np.exp(-.5*((x-30)/2)**2)
    right = np.exp(-.5*((x-62)/2)**2)
    rows = np.asarray([(1-alpha)*left+alpha*right
                       for alpha in np.linspace(.1, .9, 30)])
    # The power centroid sweeps strongly although both ridges are stationary.
    centroid = np.asarray([np.sum(row*x)/np.sum(row)*bin_hz for row in rows])
    path, correlation = _internal_translation_path(
        rows, bin_hz=bin_hz, expected_path_hz=centroid,
        maximum_error_bins=40)
    centroid_slope = np.polyfit(np.arange(rows.shape[0]), centroid, 1)[0]
    texture_slope = np.polyfit(np.arange(rows.shape[0]), path, 1)[0]
    assert centroid_slope > 3_000
    assert abs(texture_slope) < centroid_slope/4
    assert np.nanmedian(correlation) > .8
