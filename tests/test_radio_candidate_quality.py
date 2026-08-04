from leo_tracker.radio.candidate_quality import qualify_joint_event


def _credible():
    return ({
        "association": {"time_iou": .8, "centered_path_correlation": .95,
                        "drift_difference_hz_s": 200},
        "fits": [{"points": 40, "residual_rms_hz": 5_000,
                  "drift_uncertainty_hz_s": 500}],
        "tle_matching": {"classification": "ranked-hypothesis", "hypotheses": [{
            "score": .7, "drift_difference_hz_s": 300, "residual_rms_hz": 8_000}]},
    }, {"duration_s": 6}, {"duration_s": 5})


def test_credible_dual_receiver_tle_track_qualifies():
    report, rx0, rx1 = _credible()
    result = qualify_joint_event(report, rx0, rx1)
    assert result["qualified"]
    assert result["rejection_reasons"] == []


def test_short_chance_tle_overlap_is_rejected():
    report, rx0, rx1 = _credible()
    rx1["duration_s"] = .6
    report["association"]["drift_difference_hz_s"] = 28_000
    report["fits"] = [{"points": 6, "residual_rms_hz": 4_000,
                       "drift_uncertainty_hz_s": 10_000}]
    result = qualify_joint_event(report, rx0, rx1)
    assert not result["qualified"]
    assert "insufficient dual-receiver duration" in result["rejection_reasons"]
    assert "receiver drift estimates disagree" in result["rejection_reasons"]
    assert "no stable supported Doppler fit" in result["rejection_reasons"]


def test_missing_tle_evidence_never_qualifies():
    report, rx0, rx1 = _credible()
    report["tle_matching"] = None
    result = qualify_joint_event(report, rx0, rx1)
    assert not result["qualified"]
    assert "TLE catalog was not supplied" in result["rejection_reasons"]
