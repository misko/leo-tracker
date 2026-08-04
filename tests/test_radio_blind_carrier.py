import numpy as np

from leo_tracker.radio.blind_carrier import search_blind_carriers


def _waterfall(kind="moving"):
    rng = np.random.default_rng(81); count, bins, bin_hz = 121, 512, 7_500.0
    elapsed = np.arange(count)*.25
    utc = (1_700_000_000e9+elapsed*1e9).astype(np.int64)
    frequencies = (np.arange(bins)-bins//2)*bin_hz
    spectra = rng.normal(0, .025, (2, count, bins))
    if kind == "broadband":
        spectra[:, 20:110] += .8
    else:
        for receiver, base in enumerate((210, 290)):
            for row, time_s in enumerate(elapsed):
                shift = round(((-30_000+2_000*time_s) if kind == "moving" else 0)/bin_hz)
                spectra[receiver, row, base+shift] += 1.4
                spectra[receiver, row, base+shift+6] += .5
    return spectra, utc, frequencies


def _search(kind):
    spectra, utc, frequencies = _waterfall(kind)
    return search_blind_carriers(spectra, utc, frequencies, nominal_offset_hz=0,
                                 search_half_width_hz=1_600_000)


def test_alternating_time_validation_recovers_dual_receiver_carrier():
    report = _search("moving"); candidate = report["candidates"][0]
    assert report["qualified_count"] == 1
    assert candidate["empirical_false_alarm_probability"] <= .1
    assert report["false_alarm_controls"]["permutations"] == 99
    assert candidate["path_correlation"] > .99
    assert candidate["drift_rate_difference_hz_s"] < 100
    assert all(1_800 < receiver["fitted_drift_hz_s"] < 2_200
               for receiver in candidate["receivers"])
    assert all(profile["peak_count"] >= 2
               for profile in candidate["motion_compensated_profiles"])


def test_carrier_search_rejects_stationary_and_broadband():
    for kind, expected in (("stationary", "three bins"),
                           ("broadband", "broadband activity")):
        report = _search(kind)
        assert report["qualified_count"] == 0
        assert expected in " ".join(report["candidates"][0]["rejection_reasons"])
