import pytest

from leo_tracker.radio.doppler_observation import (
    classify_doppler_observation, overhead_equivalent_altitude_m,
)


def test_starlink_like_drift_is_preserved_without_a_tle():
    pair = {"association": {"broadband": False, "time_iou": .9,
        "centered_path_correlation": .88, "rx0_drift_hz_s": -3925,
        "rx1_drift_hz_s": -3950}}
    event = {"duration_s": 9.8}

    result = classify_doppler_observation(pair, event, event, 11_481_194_110.6)

    assert result["qualified"]
    assert result["identified"] is False
    assert result["frequency_direction"] == "decreasing"
    assert result["radial_acceleration_m_s2"] == pytest.approx(102.8, rel=.01)
    assert result["overhead_equivalent_altitude_km"] == pytest.approx(560, rel=.05)
    assert result["starlink_altitude_zone"]


def test_uncorrelated_or_disagreeing_receivers_are_retained_but_not_promoted():
    pair = {"association": {"broadband": False, "time_iou": .9,
        "centered_path_correlation": .2, "rx0_drift_hz_s": -4000,
        "rx1_drift_hz_s": 2000}}
    result = classify_doppler_observation(
        pair, {"duration_s": 5}, {"duration_s": 5}, 11.5e9)
    assert not result["qualified"]
    assert "receiver paths are not correlated" in result["rejection_reasons"]
    assert "receiver drift estimates disagree" in result["rejection_reasons"]


def test_altitude_proxy_rejects_zero_acceleration():
    assert overhead_equivalent_altitude_m(0) is None
