import json

from leo_tracker.radio.beacon.dashboard_index import update_dashboard_index
from leo_tracker.radio.cli import main
from leo_tracker.radio.dashboard import DashboardModel


def _capture(root, name, created_ns, *, confirmed=False, decoded=False):
    capture = root / "captures" / name
    capture.mkdir(parents=True)
    manifest = {"state": "complete", "created_utc_ns": created_ns,
        "sample_rate_hz": 2_500_000, "bandwidth_hz": 2_300_000,
        "center_frequency_hz": 1_709_687_500, "rf_center_hz": 11_459_687_500,
        "requested_duration_s": 120, "gain_mode": "manual", "configured_gain_db": 50,
        "metadata": {"channel_number": 4, "region": "lower-edge",
                     "observation_mode": "narrow"}}
    (capture / "manifest.json").write_text(json.dumps(manifest))
    report = {"schema": "leo-tracker.starlink-beacon-analysis/v1",
        "capture_manifest": manifest,
        "summary": {"exact_candidate_count": 2,
            "single_receiver_candidate_count": 3,
            "exact_qualified_count": 1, "exact_temporal_coverage_fraction": .02},
        "analysis": {"exact_acquisition_method": "pilot_symbolwise_v3"},
        "exact_checks": [{"start_s": 7, "candidate": True, "qualified": True,
            "cfo_difference_hz": 1200, "receivers": [
                {"pss": {"peak_to_median": 4},
                 "pilot": {"score_margin": .2, "frequency_offset_hz": -30_000},
                 "acquisition": {"match_score_margin": .1}}]}]}
    (root / "reports" / f"{name}.json").write_text(json.dumps(report))
    (root / "reports" / "followups" / f"{name}.json").write_text(json.dumps({
        "confirmation": {"confirmed": confirmed,
            "cross_receiver_links": ([{"start_s": 6, "stop_s": 8,
                                         "drift_hz_s": -4100}]
                                     if confirmed else [])},
        "overlapping_passes": [{"name": "STARLINK-X", "norad_id": 123}]}))
    if decoded:
        (root / "reports" / "decoded" / f"{name}.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-edge-decode/v1", "combined": {
                "minimum_frame_count": 6, "minimum_pilot_accuracy": .7,
                "soft_dual_rx": {"pilot": {"hard_symbol_accuracy": .8,
                    "soft_mean_confidence": .75, "rms_evm": .4}}}}))
    (root / "reports" / "plots" / f"{name}.png").write_bytes(b"png")


def test_beacon_dashboard_index_is_incremental_and_drives_fast_model_path(tmp_path,
                                                                          capsys):
    root = tmp_path / "beacons"
    for directory in ("reports/followups", "reports/decoded", "reports/fingerprints",
                      "reports/plots", "captures"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    first, second = "ch4-lower-edge-narrow-first", "ch4-lower-edge-narrow-second"
    _capture(root, first, 1_700_000_000_000_000_000, confirmed=True, decoded=True)
    _capture(root, second, 1_700_000_100_000_000_000)
    (root / "reports" / "fingerprints" / "index.json").write_text(json.dumps({
        "fingerprint_count": 1, "membership": {first: "wf-1"},
        "clusters": [{"cluster_id": "wf-1", "member_count": 2}]}))
    output = root / "reports" / "dashboard-index.json"

    report = update_dashboard_index(root, output)

    assert report["summary"]["analyzed_capture_count"] == 2
    assert report["summary"]["temporally_confirmed_capture_count"] == 1
    row = next(item for item in report["recordings"] if item["recording_id"] == first)
    assert row["candidate_count"] == 5
    assert row["strongest_drift_hz_s"] == -4100
    assert row["pilot_accuracy"] == .8
    assert row["fingerprint_family"] == "wf-1"
    assert row["_statistics"]["exact_checks"][0]["receivers"][0][
        "pilot_frequency_offset_hz"] == -30_000
    assert row["_plots"] == [f"/beacon-plots/{first}.png"]
    assert row["_artifacts"][0]["url"] == f"/beacon-analyses/{first}.json"

    observation = tmp_path / "watch"
    observation.mkdir()
    model = DashboardModel(observation, beacon_root=root)
    model.beacon = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("persisted recording index must bypass the heavyweight beacon model"))
    index = model.recordings()
    assert len(index["recordings"]) == 2
    assert "_statistics" not in index["recordings"][0]
    detail = model.recording_detail("beacon", first)
    assert detail["statistics"]["confirmation"]["confirmed"]
    assert detail["plots"] == [f"/beacon-plots/{first}.png"]

    assert main(["starlink-beacon-dashboard-index", str(root), str(output),
                 "--capture-name", second]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["recording_count"] == 2
    assert cli["summary"]["analyzed_capture_count"] == 2
