import json

from leo_tracker.radio import cli
from leo_tracker.radio.wide_population import summarize_wide_reports


def _report(width, drift, *, qualified=True, observable=False,
            carrier=11_325_117_187.5):
    receiver = lambda offset: {"median_width_hz": width,
        "median_center_rf_hz": carrier+offset, "linear_drift_hz_s": drift}
    return {"schema": "leo-tracker.wide-feature-search/v1", "source": "capture.npz",
        "rf_carrier_hz": carrier, "candidates": [{"leo_like_qualified": qualified,
        "doppler_candidate_qualified": qualified,
        "polarity": "positive", "start_utc": "2026-08-03T02:13:39Z", "duration_s": 30,
        "bounding_width_hz": 112_500, "mean_drift_hz_s": drift,
        "radial_acceleration_m_s2": 84.4, "receiver_path_correlation": .99,
        "global_frequency_control_available": True, "global_frequency_control_passed": True,
        "orbital_curvature_observable": observable,
        "best_tle_curvature_resolution_bins": 1.4 if observable else .4,
        "specific_tle_identifiable": False, "tles_within_one_bin_of_best": 93,
        "common_internal_peak_count": 1, "common_internal_peak_spacings_hz": [],
        "receivers": [receiver(200_000), receiver(230_000)]}]}


def test_population_separates_instantaneously_narrow_and_broad_features(tmp_path):
    summary = summarize_wide_reports([(tmp_path/"narrow.json", _report(
                                          15_000, -3_200, observable=True)),
                                      (tmp_path/"broad.json", _report(200_000, -1_400)),
                                      (tmp_path/"rejected.json", _report(15_000, -3_000,
                                                                         qualified=False))])
    assert summary["qualified_feature_count"] == 2
    assert summary["doppler_candidate_count"] == 2
    assert {item["family"]: item["count"] for item in summary["families"]} == {
        "narrow_swept": 1, "broad_state": 1}
    narrow = next(item for item in summary["features"] if item["family"] == "narrow_swept"
                  and item["mean_drift_hz_s"] == -3_200)
    assert narrow["center_offset_from_channel_hz"] == 215_000
    assert not narrow["specific_tle_identifiable"]
    assert "morphology only" in summary["classification_scope"]
    audit = {item["family"]: item for item in summary["morphology_audit"]}
    assert audit["narrow_swept"]["all"]["count"] == 2
    assert audit["narrow_swept"]["rejected"]["count"] == 1
    assert audit["narrow_swept"]["orbital_curvature_observable_count"] == 1
    assert audit["narrow_swept"]["doppler_candidate_qualified_count"] == 1
    assert audit["broad_state"]["all"]["count"] == 1
    assert narrow["best_tle_curvature_resolution_bins"] == 1.4


def test_population_cli_reads_directory_and_ignores_other_json(tmp_path, capsys):
    reports = tmp_path/"wide"; reports.mkdir()
    (reports/"a.json").write_text(json.dumps(_report(15_000, -3_200)))
    (reports/"status.json").write_text('{"schema":"unrelated"}')
    output = tmp_path/"population.json"
    assert cli.main(["starlink-wide-feature-summary", str(output), str(reports)]) == 0
    terminal = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text())
    assert terminal["families"] == {"narrow_swept": 1}
    assert saved["report_count"] == 1
    assert saved["features"][0]["mean_drift_hz_s"] == -3_200
