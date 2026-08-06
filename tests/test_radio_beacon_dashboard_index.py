import json

from leo_tracker.radio.beacon.dashboard_index import (capture_radio_parameters,
                                                       confirmed_beacon_events,
                                                       update_dashboard_index)
from leo_tracker.radio.cli import main
from leo_tracker.radio.dashboard import DETAIL_HTML, DashboardModel


def _capture(root, name, created_ns, *, confirmed=False, decoded=False):
    capture = root / "captures" / name
    capture.mkdir(parents=True)
    manifest = {"state": "complete", "created_utc_ns": created_ns,
        "sample_rate_hz": 2_500_000, "bandwidth_hz": 2_300_000,
        "center_frequency_hz": 1_709_687_500, "rf_center_hz": 11_459_687_500,
        "lnb_lo_hz": 9_750_000_000, "receiver_count": 2,
        "requested_samples_per_receiver": 300_000_000, "chunk_samples": 12_500_000,
        "dtype": "ci16_le", "layout": "sample,receiver,component",
        "chunks": [{"sample_count": 12_500_000, "bytes": 100_000_000,
                    "read_count": 48}],
        "requested_duration_s": 120, "gain_mode": "manual", "configured_gain_db": 50,
        "metadata": {"channel_number": 4, "region": "lower-edge",
                     "observation_mode": "narrow", "assigned_gain_mode": "manual",
                     "gain_experiment_id": "gain-ab-v1", "agc_assignment_probability": .5,
                     "gain_random_draw_u32": 123, "agc_settle_s": 0},
        "gain_telemetry": {"target_interval_s": 1, "entries": [
            {"rx_gain_db": [49.5, 50]}, {"rx_gain_db": [50.5, 50]}]},
        "sample_statistics": {"adc_nominal_full_scale": 2048,
            "near_full_scale_threshold": 2040, "receivers": [
                {"receiver": 0, "rms_magnitude": 91, "peak_abs_component": 900,
                 "near_full_scale_fraction": 0},
                {"receiver": 1, "rms_magnitude": 52, "peak_abs_component": 700,
                 "near_full_scale_fraction": 0}]},
        "stream_timing": {"sample_time_s": 5, "wall_span_s": 5.2,
            "host_read_duty_fraction": .96, "read_count": 48,
            "maximum_positive_host_gap_s": .01},
        "identity": {"enabled_channels": [0, 1],
                     "gain_mode_readback": ["manual", "manual"],
                     "kind": "plutoplus-paired", "implementation": "pyadi.ad9361",
                     "transport": "iio", "uri": "pluto://ip:192.168.2.1",
                     "host_temperature_c": 59.5, "radio_temperature_c": 55.25}}
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
    (root / "reports" / "fingerprints" / f"{name}.png").write_bytes(b"fingerprint")
    if confirmed:
        (root / "reports" / "frame-tracks" / f"{name}.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-conditioned-frame-track/v1",
            "summary": {"frame_observation_count": 7500,
                        "dual_valid_frame_count": 6200,
                        "dual_valid_fraction": .8267, "measured_span_s": 12.4}}))
        (root / "reports" / "tracks" / f"{name}.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-continuous-track/v1",
            "configuration": {"measurement_source": "dense_followup"},
            "summary": {"track_count": 2, "longest_valid_duration_s": 23.5,
                        "longest_dual_valid_duration_s": 23.5},
            "tracks": [{"track_id": "track-000", "observations": [],
                "summary": {"dual_valid_duration_s": 23.5,
                            "dual_valid_observation_count": 61},
                "relative_receiver_calibration": {"residual_rms_hz": 125}}]}))
        (root / "reports" / "associations" / f"{name}.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-tle-association/v1",
            "summary": {"qualified_association_count": 1},
            "associations": [{"track_id": "track-000", "qualified": True,
                "best_norad_id": 123, "best_holdout_residual_rms_hz": 210,
                "margin_to_second_hz": 330, "stability": {"passed": True},
                "candidates": [{"name": "STARLINK-X", "norad_id": 123}]}]}))
        (root / "reports" / "channel-links" / f"{name}.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-continuous-track/v1",
            "summary": {"longest_hypothesis_duration_s": 29.4},
            "tracks": [{"track_id": "channel-hypothesis-000",
                "summary": {"source_segment_count": 2,
                    "dual_valid_duration_s": 29.4,
                    "dual_valid_observation_count": 76}}]}))
        (root / "reports" / "associations" /
         f"{name}-channel-link.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-tle-association/v2",
            "summary": {"qualified_association_count": 0},
            "associations": [{"track_id": "channel-hypothesis-000",
                "qualified": False, "stability": {"passed": False},
                "candidates": [{"name": "STARLINK-Y", "norad_id": 456}]}]}))


def test_beacon_dashboard_index_is_incremental_and_drives_fast_model_path(tmp_path,
                                                                          capsys):
    root = tmp_path / "beacons"
    for directory in ("reports/followups", "reports/decoded", "reports/fingerprints",
                      "reports/frame-tracks",
                      "reports/tracks", "reports/channel-links",
                      "reports/associations", "reports/plots", "captures"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    first, second = "ch4-lower-edge-narrow-first", "ch4-lower-edge-narrow-second"
    _capture(root, first, 1_700_000_000_000_000_000, confirmed=True, decoded=True)
    _capture(root, second, 1_700_000_100_000_000_000)
    (root / "reports" / "fingerprints" / "index.json").write_text(json.dumps({
        "fingerprint_count": 1, "membership": {first: "wf-1"},
        "clusters": [{"cluster_id": "wf-1", "member_count": 2}],
        "nearest_matches": {first: [{"capture_name": second,
            "waveform_family_similarity": .9, "temporal_qpsk_similarity": .8,
            "conditional_channel_similarity": .7, "family_link": True}]}}))
    output = root / "reports" / "dashboard-index.json"

    report = update_dashboard_index(root, output)

    assert report["summary"]["analyzed_capture_count"] == 2
    assert report["summary"]["temporally_confirmed_capture_count"] == 1
    row = next(item for item in report["recordings"] if item["recording_id"] == first)
    assert row["candidate_count"] == 5
    assert row["beacon_detected_count"] == 1
    assert row["strongest_drift_hz_s"] == -4100
    assert row["pilot_accuracy"] == .8
    assert row["fingerprint_family"] == "wf-1"
    assert row["fingerprint_plot_url"] == f"/beacon-fingerprint-plots/{first}.png"
    assert row["continuous_track_count"] == 2
    assert row["conditioned_frame_count"] == 7500
    assert row["conditioned_dual_valid_frame_count"] == 6200
    assert row["longest_track_duration_s"] == 29.4
    assert row["qualified_tle_association_count"] == 1
    assert any(item["url"] == f"/beacon-tracks/{first}.json"
               for item in row["_artifacts"])
    assert any(item["url"] == f"/beacon-frame-tracks/{first}.json"
               for item in row["_artifacts"])
    assert any(item["url"] == f"/beacon-associations/{first}.json"
               for item in row["_artifacts"])
    assert any(item["url"] == f"/beacon-channel-links/{first}.json"
               for item in row["_artifacts"])
    assert any(item["url"] == f"/beacon-associations/{first}-channel-link.json"
               for item in row["_artifacts"])
    assert row["fingerprint_nearest_matches"][0]["temporal_qpsk_similarity"] == .8
    assert row["_statistics"]["exact_checks"][0]["receivers"][0][
        "pilot_frequency_offset_hz"] == -30_000
    radio = row["_statistics"]["radio_parameters"]
    assert radio["tuning"]["lnb_lo_hz"] == 9_750_000_000
    assert radio["receivers"]["gain_mode_readback"] == ["manual", "manual"]
    assert radio["receivers"]["gain_readback_by_receiver"][0]["median_db"] == 50
    assert radio["hardware"]["radio_temperature_c"] == 55.25
    assert radio["signal"]["receivers"][0]["rms_magnitude"] == 91
    assert radio["stream"]["host_read_duty_fraction"] == .96
    assert radio["capture"]["captured_samples_per_receiver"] == 12_500_000
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
    assert detail["statistics"]["radio_parameters"]["gain_experiment"][
        "experiment_id"] == "gain-ab-v1"
    assert detail["statistics"]["continuous_tracking"]["summary"][
        "longest_valid_duration_s"] == 23.5
    assert detail["statistics"]["frame_tracking"]["summary"][
        "dual_valid_frame_count"] == 6200
    assert detail["statistics"]["continuous_tracking"]["configuration"][
        "measurement_source"] == "dense_followup"
    assert detail["statistics"]["tle_association"]["summary"][
        "qualified_association_count"] == 1
    assert detail["statistics"]["continuous_linking"]["summary"][
        "longest_hypothesis_duration_s"] == 29.4
    assert detail["statistics"]["linked_tle_association"]["summary"][
        "qualified_association_count"] == 0
    assert detail["plots"] == [f"/beacon-plots/{first}.png"]
    assert "Calibrated 10 Hz tracks" in DETAIL_HTML
    assert "Conditioned 750 Hz frames" in DETAIL_HTML
    assert "Held-out TLE association" in DETAIL_HTML

    assert main(["starlink-beacon-dashboard-index", str(root), str(output),
                 "--capture-name", second]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["recording_count"] == 2
    assert cli["summary"]["analyzed_capture_count"] == 2


def test_capture_radio_parameters_handles_partial_active_manifest():
    report = capture_radio_parameters({"state": "capturing", "sample_rate_hz": 2_500_000,
        "receiver_count": 2, "gain_mode": "slow_attack", "chunks": [],
        "identity": {"enabled_channels": [0, 1],
                     "gain_mode_readback": ["slow_attack", "slow_attack"]}})
    assert report["capture"]["state"] == "capturing"
    assert report["capture"]["captured_samples_per_receiver"] == 0
    assert report["receivers"]["enabled_channels"] == [0, 1]
    assert report["receivers"]["gain_readback_by_receiver"] == [
        {"receiver": 0, "sample_count": 0, "minimum_db": None,
         "median_db": None, "maximum_db": None},
        {"receiver": 1, "sample_count": 0, "minimum_db": None,
         "median_db": None, "maximum_db": None}]


def test_confirmed_beacon_count_merges_duplicate_receiver_links_and_time_runs():
    confirmation = {"confirmed": True,
        "cross_receiver_links": [
            {"start_s": 1.0, "stop_s": 1.1},
            {"start_s": 1.1, "stop_s": 1.3},
            {"start_s": 4.0, "stop_s": 4.1}],
        "dual_receiver_links": [{"start_s": 1.0, "stop_s": 1.1}],
        "receivers": [{"links": [{"start_s": 1.2, "stop_s": 1.3}]}]}
    events = confirmed_beacon_events(confirmation)
    assert events == [
        {"start_s": 1.0, "stop_s": 1.3, "link_count": 4},
        {"start_s": 4.0, "stop_s": 4.1, "link_count": 1}]
