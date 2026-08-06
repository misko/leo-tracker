import json

import pytest

from leo_tracker.radio.beacon.gain_comparison import build_gain_comparison
from leo_tracker.radio.cli import main


def _write_capture(root, name, gain_mode, observation_mode, *, confirmed, accuracy,
                   gain_db, draw):
    capture = root / "captures" / name
    capture.mkdir(parents=True)
    manifest = {"state": "complete", "sample_rate_hz": 2_500_000,
        "gain_mode": gain_mode,
        "identity": {"gain_mode_readback": [gain_mode, gain_mode]},
        "metadata": {"gain_experiment_id": "gain-ab-v1",
            "gain_random_draw_u32": draw, "agc_assignment_probability": .5,
            "assigned_gain_mode": gain_mode, "observation_mode": observation_mode},
        "stream_timing": {"sample_time_s": 120},
        "gain_telemetry": {"entries": [
            {"rx_gain_db": [gain_db, gain_db + 1]}]},
        "sample_statistics": {"receivers": [
            {"rms_magnitude": 100, "near_full_scale_fraction": 0},
            {"rms_magnitude": 80, "near_full_scale_fraction": .0001}]}}
    (capture / "manifest.json").write_text(json.dumps(manifest))
    (root / "reports" / f"{name}.json").write_text(json.dumps({
        "summary": {"exact_candidate_count": 3}}))
    followups = root / "reports" / "followups"
    followups.mkdir(exist_ok=True)
    (followups / f"{name}.json").write_text(json.dumps({
        "confirmation": {"confirmed": confirmed}}))
    decoded = root / "reports" / "decoded"
    decoded.mkdir(exist_ok=True)
    (decoded / f"{name}.json").write_text(json.dumps({"combined": {
        "soft_dual_rx": {"pilot": {"hard_symbol_accuracy": accuracy,
            "soft_mean_confidence": accuracy - .1, "rms_evm": 1 - accuracy}}}}))


def test_gain_comparison_aggregates_randomized_captures_and_strata(tmp_path, capsys):
    root = tmp_path / "store"
    (root / "captures").mkdir(parents=True)
    (root / "reports").mkdir()
    _write_capture(root, "manual-narrow", "manual", "narrow", confirmed=False,
                   accuracy=.6, gain_db=50, draw=3)
    _write_capture(root, "agc-narrow", "slow_attack", "narrow", confirmed=True,
                   accuracy=.9, gain_db=37, draw=4)
    _write_capture(root, "manual-wide", "manual", "wide", confirmed=True,
                   accuracy=.7, gain_db=50, draw=5)
    _write_capture(root, "agc-wide", "slow_attack", "wide", confirmed=True,
                   accuracy=.8, gain_db=39, draw=6)
    fingerprints = root / "reports" / "fingerprints"
    fingerprints.mkdir()
    (fingerprints / "index.json").write_text(json.dumps({
        "membership": {"manual-narrow": "family-1", "agc-narrow": "family-1"},
        "clusters": [{"cluster_id": "family-1", "member_count": 2}]}))

    output = root / "reports" / "gain-experiment" / "summary.json"
    report = build_gain_comparison(root, output)

    assert report["randomized_capture_count"] == 4
    assert report["groups"]["manual"]["capture_count"] == 2
    assert report["groups"]["slow_attack"]["confirmation_rate"] == 1
    assert report["groups"]["manual"]["median_hardware_gain_db"] == [50, 51]
    assert report["groups"]["slow_attack"]["gain_mode_readback_match_count"] == 2
    assert report["strata"]["narrow"]["manual"]["confirmation_rate"] == 0
    assert report["strata"]["narrow"]["slow_attack"]["confirmation_rate"] == 1
    assert report["agc_minus_manual"]["median_pilot_accuracy"] == pytest.approx(.2)
    assert not report["decision_guidance"]["ready"]
    assert json.loads(output.read_text())["schema"] == report["schema"]

    assert main(["starlink-beacon-gain-summary", str(root), str(output)]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["randomized_capture_count"] == 4
    assert cli["groups"]["slow_attack"]["analyzed_count"] == 2
