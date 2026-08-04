from datetime import datetime, timezone

import numpy as np

from leo_tracker.fusion.event_matching import rank_event_against_passes


def _point(second, doppler, elevation=10):
    time = datetime.fromtimestamp(1_700_000_000 + second, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"time": time, "expected_doppler_hz": doppler,
            "elevation_deg": elevation, "range_rate_km_s": 0}


def _pass(name, norad, values):
    return {"name": name, "norad_id": norad, "passes": [{
        "rise": _point(0, values[0]), "culmination": _point(50, values[1], 60),
        "set": _point(100, values[2])}]}


def test_correct_tle_shape_ranks_first_despite_unknown_frequency_bias():
    catalog = {"satellites": [_pass("RIGHT", 1, (200_000, 0, -200_000)),
                              _pass("WRONG", 2, (100_000, 0, -100_000))]}
    seconds = np.arange(10, 91, 2)
    expected = np.interp(seconds, [0, 50, 100], [200_000, 0, -200_000])
    observed = expected + 1_250_000
    utc = ((1_700_000_000 + seconds) * 1e9).astype(np.int64)

    result = rank_event_against_passes(utc, observed, catalog)

    assert result["hypotheses"][0]["norad_id"] == 1
    assert result["hypotheses"][0]["fitted_frequency_bias_hz"] == 1_250_000
    assert result["hypotheses"][0]["residual_rms_hz"] < 1
    assert result["classification"] == "ranked-hypothesis"


def test_nearly_identical_dense_passes_are_explicitly_ambiguous():
    catalog = {"satellites": [_pass("A", 1, (200_000, 0, -200_000)),
                              _pass("B", 2, (199_000, 0, -199_000))]}
    seconds = np.arange(10, 91, 2)
    observed = np.interp(seconds, [0, 50, 100], [200_000, 0, -200_000])
    utc = ((1_700_000_000 + seconds) * 1e9).astype(np.int64)
    assert rank_event_against_passes(utc, observed, catalog)["classification"] == "ambiguous"
