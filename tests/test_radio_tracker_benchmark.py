import json

from leo_tracker.radio import cli


def test_tracker_summary_aggregates_rates_runtime_and_drift(tmp_path, capsys):
    reports = tmp_path/"reports"; reports.mkdir()
    for index, qualified in enumerate((True, False)):
        (reports/f"capture-{index}.json").write_text(json.dumps({
            "schema": "leo-tracker.tracker-ensemble/v1",
            "configuration": {"analysis_windows_s": [[0, 10]]},
            "metrics": {"runtime_s_by_tracker": {"dedoppler-linear/v1": 2+index}},
            "joint_tracks": [{"tracker": "dedoppler-linear/v1",
                              "qualified": qualified}],
            "identifications": [{"tracker": "dedoppler-linear/v1",
                                  "qualified": qualified}],
            "candidates": [{"tracker": "dedoppler-linear/v1",
                "qualified": qualified, "drift_hz_s": 4_000+index*1_000,
                "false_alarm_probability": .05+index*.1}]}))
    (reports/"coherent.json").write_text(json.dumps({
        "schema": "leo-tracker.coherent-doppler-ensemble/v1",
        "receiver_tracks": [{"drift_hz_s": 4200, "residual_rms_hz": 12,
                             "qualified": True}], "blocks": [{
            "receivers": [{"fll": {"drift_hz_s": 4100, "median_coherence": .9},
                "polynomial_phase": {"drift_hz_s": 4050,
                                     "phase_residual_rms_rad": .1},
                "repetition": {"best_correlation": .7}}]}]}))
    output = tmp_path/"summary.json"
    assert cli.main(["doppler-tracker-summary", str(output), str(reports)]) == 0
    assert json.loads(capsys.readouterr().out)["report_count"] == 2
    tracker = json.loads(output.read_text())["trackers"][0]
    assert tracker["candidate_count"] == 2 and tracker["qualified_count"] == 1
    assert tracker["observed_hours"] == 20/3600
    assert tracker["median_runtime_s"] == 2.5
    assert tracker["median_qualified_drift_hz_s"] == 4_000
    assert tracker["qualified_joint_track_count"] == 1
    assert tracker["qualified_tle_identification_count"] == 1
    assert tracker["median_false_alarm_probability"] == 0.1
    saved = json.loads(output.read_text())
    assert saved["coherent_report_count"] == 1
    assert saved["coherent_trackers"][0]["median_drift_hz_s"] == 4100
    assert saved["coherent_trackers"][-1]["tracker"] == "inter-block-fll-track/v1"
    assert saved["coherent_trackers"][-1]["qualified_count"] == 1
