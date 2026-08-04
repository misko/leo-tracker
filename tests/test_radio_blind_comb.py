import numpy as np

from leo_tracker.radio.blind_comb import search_blind_comb


def _waterfall(kind="moving"):
    rng = np.random.default_rng(4)
    count, bins, bin_hz = 121, 512, 7_500.0
    elapsed = np.arange(count)*.25
    utc = (1_700_000_000e9+elapsed*1e9).astype(np.int64)
    frequencies = (np.arange(bins)-bins//2)*bin_hz
    spectra = rng.normal(0, .03, (2, count, bins))
    if kind == "broadband":
        spectra[:, 20:110, :] += .8
        return spectra, utc, frequencies
    spacing = 43_949.5 if kind != "wrong-spacing" else 52_000.0
    for receiver, base in enumerate((220, 280)):
        for row, time_s in enumerate(elapsed):
            shift = round(((-30_000+2_000*time_s) if kind == "moving" else 0)/bin_hz)
            for tooth in range(-4, 5):
                spectra[receiver, row, base+shift+round(tooth*spacing/bin_hz)] += 1.2
    return spectra, utc, frequencies


def _search(kind):
    spectra, utc, frequencies = _waterfall(kind)
    return search_blind_comb(spectra, utc, frequencies, nominal_offset_hz=0,
                             search_half_width_hz=1_500_000)


def test_blind_search_recovers_dual_receiver_motion_and_all_comb_teeth():
    report = _search("moving")
    candidate = report["candidates"][0]
    assert report["qualified_count"] >= 1
    assert candidate["qualified"]
    assert candidate["path_correlation"] > .99
    assert candidate["drift_rate_difference_hz_s"] < 100
    assert candidate["empirical_false_alarm_probability"] <= .1
    assert candidate["off_frequency_advantage_db"] > .03
    assert report["false_alarm_controls"]["permutations"] == 19
    for receiver in candidate["receivers"]:
        assert 1_800 < receiver["fitted_drift_hz_s"] < 2_200
        assert receiver["detected_tooth_count"] == 9


def test_blind_search_rejects_stationary_wrong_spacing_and_broadband():
    for kind, expected in (("stationary", "stationary control"),
                           ("wrong-spacing", "wrong-spacing controls"),
                           ("broadband", "broadband activity")):
        report = _search(kind)
        assert report["qualified_count"] == 0
        assert expected in " ".join(report["candidates"][0]["rejection_reasons"])
