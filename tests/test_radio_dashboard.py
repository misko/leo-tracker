import json
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import pytest
import numpy as np

from leo_tracker.radio.dashboard import DashboardModel, make_handler
from leo_tracker.radio.physics import doppler_radial_acceleration_m_s2


def _write_fixture(root: Path):
    root.mkdir(); (root / "plots").mkdir()
    start = 1_700_000_000_000_000_000
    summary = {"state": "running", "started_utc": "2023-11-14T22:13:20Z",
               "updated_utc": "2023-11-14T22:20:00Z", "requested_hours": 12,
               "channel": {"rf_center_hz": 11_575_000_000}}
    (root / "summary.json").write_text(json.dumps(summary))
    record = {"chunk": 0, "detected": True, "first_utc_ns": start,
              "last_utc_ns": start + 400_000_000_000, "wall_duration_s": 400,
              "tracks": [{"fitted_drift_hz_s": -4000},
                         {"fitted_drift_hz_s": -4200}],
              "significance": [{"false_alarm_probability": .03}],
              "receiver_agreement": {"correlation": .9}, "rejection_reasons": []}
    (root / "index.jsonl").write_text(json.dumps(record) + "\n{partial")
    (root / "plots/chunk-00000.png").write_bytes(b"png-test")
    catalog = {"carrier_hz": 11_575_000_000, "generated_at": "fixture",
        "observer": {"latitude_deg": 1, "longitude_deg": 2}, "satellites": [{
        "name": "STARLINK-TEST ", "norad_id": 123, "passes": [{
          "rise": {"time": "2023-11-14T22:13:00Z", "expected_doppler_hz": 200_000,
                   "range_rate_km_s": -5},
          "culmination": {"time": "2023-11-14T22:16:00Z", "elevation_deg": 60,
                          "expected_doppler_hz": 0, "range_rate_km_s": 0},
          "set": {"time": "2023-11-14T22:20:00Z", "expected_doppler_hz": -200_000,
                  "range_rate_km_s": 5}}]}]}
    (root / "passes.json").write_text(json.dumps(catalog))


def test_dashboard_snapshot_reports_progress_pass_match_and_physics(tmp_path):
    root = tmp_path / "watch"; _write_fixture(root)
    model = DashboardModel(root)
    now = datetime(2023, 11, 14, 22, 19, tzinfo=timezone.utc)

    snapshot = model.snapshot(now)

    assert snapshot["status"]["completed_chunks"] == 1
    assert snapshot["status"]["coverage_hours"] == pytest.approx(400 / 3600)
    assert snapshot["status"]["retained_sample_hours"] == pytest.approx(
        4096 * 262_144 / 30_720_000 / 3600)
    candidate = snapshot["detected"]["detections"][0]
    assert candidate["mean_drift_hz_s"] == -4100
    assert candidate["radial_acceleration_m_s2"] > 100
    assert candidate["best_tle_match"]["norad_id"] == 123
    assert candidate["overlapping_expected_passes"] == 1
    assert doppler_radial_acceleration_m_s2(-1000, 10e9) == pytest.approx(29.9792458)


def test_dashboard_coalesces_repeated_live_snapshots(tmp_path):
    root = tmp_path / "watch"; _write_fixture(root)
    model = DashboardModel(root)
    calls = {"status": 0}
    def status():
        calls["status"] += 1
        return {"state": "running"}
    model.status = status
    model.expected_passes = lambda: {}
    model.detections = lambda: {}
    model.logs = lambda: {}
    model.waterfalls = lambda: {}
    model.dither_comparisons = lambda: {}
    model.beacon = lambda: {}

    first = model.snapshot()
    second = model.snapshot()

    assert second is first
    assert calls["status"] == 1
    model._snapshot_cache_created -= 5
    assert model.snapshot() is not first
    assert calls["status"] == 2


def test_dashboard_http_e2e_serves_html_json_and_plot(tmp_path):
    root = tmp_path / "watch"; _write_fixture(root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DashboardModel(root)))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        index_html = urlopen(base + "/", timeout=2).read()
        assert b"LEO / Starlink recordings" in index_html
        assert b"/api/recordings" in index_html
        assert b"<img" not in index_html
        assert b"<object" not in index_html
        assert b"Beacons detected" in index_html
        recordings = json.loads(urlopen(base + "/api/recordings", timeout=2).read())
        assert recordings["schema"] == "leo-tracker.dashboard-recording-index/v1"
        assert recordings["recordings"][0]["recording_id"] == "chunk-00000"
        assert recordings["recordings"][0]["candidate_count"] == 0
        detail_url = recordings["recordings"][0]["detail_url"]
        assert b"Capture summary" in urlopen(base + detail_url, timeout=2).read()
        detail = json.loads(urlopen(base + "/api" + detail_url, timeout=2).read())
        assert detail["kind"] == "sweep"
        assert detail["plots"][0].startswith("/plots/chunk-00000.png")
        snapshot = json.loads(urlopen(base + "/api/snapshot", timeout=2).read())
        assert snapshot["status"]["detection_count"] == 1
        assert urlopen(base + "/plots/chunk-00000.png", timeout=2).read() == b"png-test"
        with pytest.raises(Exception):
            urlopen(base + "/plots/../passes.json", timeout=2)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def _write_v2_fixture(root: Path):
    (root / "chunks").mkdir(parents=True)
    (root / "analysis").mkdir()
    (root / "plots").mkdir()
    stem = "chunk-00007-ch4-20260802T183226Z"
    np.savez_compressed(
        root / "chunks" / f"{stem}.npz",
        utc_ns=np.array([1_700_000_000_000_000_000,
                         1_700_000_120_000_000_000], dtype=np.int64),
        center_frequency_hz=np.array(1_830_117_187.5),
        lnb_lo_hz=np.array(9_750_000_000.0),
        sample_rate_hz=np.array(30_720_000.0),
        bandwidth_hz=np.array(20_000_000.0),
        gain_mode=np.array("manual"), configured_gain_db=np.array(50.0),
        frequency_offsets_hz=np.linspace(-15_360_000, 15_360_000, 8192, endpoint=False),
        psd_db_quantization_db=np.array(.01),
        samples_per_snapshot=np.array(262_144),
    )
    analysis = {
        "schema": "leo-tracker.measurement-analysis/v2",
        "capture_identity": {"observation_mode": "fixed"},
        "carrier_hz": 11_580_117_187.5,
        "measurement": {"observation_span_s": 120, "retained_sample_time_s": 1.5},
        "tle_guided_search": {"qualified_count": 1, "candidates": [{
            "qualified": True, "name": "STARLINK-TEST", "norad_id": 123,
            "signal_model": "single-tone", "stationary_improvement_db": .2,
            "trace_correlation": .9, "rejection_reasons": []}]},
        "events": [[{"receiver": 0}], [{"receiver": 1}]],
        "joint_events": [{"qualification": {"qualified": True}, "association": {
            "association_score": .9,
            "centered_path_correlation": .95,
            "drift_difference_hz_s": 200,
            "rx0_drift_hz_s": -4000,
            "rx1_drift_hz_s": -4200,
        }, "doppler_observation": {"qualified": True, "mean_drift_hz_s": -4100,
            "starlink_altitude_zone": True}}],
    }
    (root / "analysis" / f"{stem}.json").write_text(json.dumps(analysis))
    (root / "plots" / f"{stem}.png").write_bytes(b"v2-png")
    (root / "status.json").write_text(json.dumps({
        "stage": "capturing", "updated_utc": "2023-11-14T22:16:00Z"}))
    return stem


def test_dashboard_reads_v2_measurement_watch_and_keeps_legacy_compatibility(tmp_path):
    root = tmp_path / "v2-watch"
    stem = _write_v2_fixture(root)
    # A missing wide report from an older resolution is historical, not a
    # pending item in the active 8192-bin baseline generation.
    old_stem = "chunk-00006-ch3-20260802T183000Z"
    old_analysis = json.loads((root / "analysis" / f"{stem}.json").read_text())
    (root / "analysis" / f"{old_stem}.json").write_text(json.dumps(old_analysis))
    np.savez_compressed(root / "chunks" / f"{old_stem}.npz",
        utc_ns=np.array([1_699_999_800_000_000_000, 1_699_999_920_000_000_000], np.int64),
        center_frequency_hz=np.array(1_580_117_187.5),
        lnb_lo_hz=np.array(9_750_000_000.0), sample_rate_hz=np.array(30_720_000.0),
        samples_per_snapshot=np.array(262_144),
        frequency_offsets_hz=np.linspace(-15_360_000, 15_360_000, 4096, endpoint=False))
    snapshot = DashboardModel(root).snapshot(
        datetime(2023, 11, 14, 22, 17, tzinfo=timezone.utc))

    assert snapshot["status"]["state"] == "capturing"
    assert snapshot["status"]["deadline_utc"] is None
    assert snapshot["status"]["completed_chunks"] == 2
    assert snapshot["status"]["retained_sample_hours"] == pytest.approx(3 / 3600)
    assert snapshot["status"]["detection_count"] == 2
    assert snapshot["status"]["joint_event_count"] == 2
    assert snapshot["status"]["qualified_event_count"] == 2
    assert snapshot["status"]["tle_guided_candidate_count"] == 2
    assert snapshot["status"]["frequency_bins"] == 8192
    assert snapshot["status"]["frequency_bin_width_hz"] == 3750
    assert snapshot["status"]["psd_quantization_db"] == .01
    assert not snapshot["status"]["resolution_baseline_ready"]
    assert snapshot["status"]["pending_wide_analysis_chunks"] == 1
    detection = snapshot["detected"]["detections"][0]
    assert detection["mean_drift_hz_s"] == -4100
    assert detection["joint_event_count"] == 1
    assert detection["event_counts"] == [1, 1]
    assert detection["plot_url"].startswith(f"/plots/{stem}.png?v=")
    assert snapshot["logs"]["events"][0]["joint_event_count"] == 1
    waterfall = snapshot["waterfalls"]["waterfalls"][0]
    assert waterfall["chunk"] == 7
    assert waterfall["event_counts"] == [1, 1]
    assert waterfall["joint_event_count"] == 1
    assert waterfall["qualified_event_count"] == 1
    assert waterfall["doppler_observation_count"] == 1
    assert waterfall["tle_guided_candidate_count"] == 1
    assert waterfall["best_tle_candidate"]["norad_id"] == 123
    assert waterfall["plot_url"].startswith(f"/plots/{stem}.png?v=")


def test_dashboard_reads_hybrid_dwell_artifact_names(tmp_path):
    root = tmp_path / "hybrid-watch"
    old_stem = _write_v2_fixture(root)
    dwell_stem = "dwell-00003-20260804T001019Z"
    (root / "analysis" / f"{old_stem}.json").rename(
        root / "analysis" / f"{dwell_stem}.json")
    (root / "chunks" / f"{old_stem}.npz").rename(
        root / "chunks" / f"{dwell_stem}.npz")
    (root / "plots" / f"{old_stem}.png").rename(
        root / "plots" / f"{dwell_stem}.png")

    records = DashboardModel(root).records()

    assert len(records) == 1
    assert records[0]["chunk"] == 3
    assert records[0]["capture_id"] == dwell_stem
    assert records[0]["plot_name"] == f"{dwell_stem}.png"
    waterfall = DashboardModel(root).waterfalls()["waterfalls"][0]
    assert waterfall["capture_id"] == dwell_stem
    assert waterfall["sample_rate_hz"] == 30_720_000
    assert waterfall["bandwidth_hz"] == 20_000_000
    assert waterfall["gain_mode"] == "manual"
    assert waterfall["configured_gain_db"] == 50
    assert waterfall["observation_mode"] == "fixed"
    assert waterfall["plot_url"].startswith(f"/plots/{dwell_stem}.png?v=")


def test_dashboard_publishes_structured_observation_json(tmp_path):
    root = tmp_path/"structured-watch"; stem = _write_v2_fixture(root)
    (root/"observations").mkdir()
    report = {"schema": "leo-tracker.doppler-observations/v1", "summary": {
        "track_count": 7, "accepted_track_count": 2, "boundary_test_count": 1,
        "sky_fixed_count": 1, "baseband_fixed_count": 0,
        "ambiguous_boundary_count": 0}}
    (root/"observations"/f"{stem}.json").write_text(json.dumps(report))
    model = DashboardModel(root); waterfall = model.waterfalls()["waterfalls"][0]
    assert waterfall["structured_observation_summary"]["sky_fixed_count"] == 1
    assert waterfall["structured_observation_url"] == f"/observations/{stem}.json"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(model))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        payload = json.loads(urlopen(
            f"http://127.0.0.1:{server.server_port}{waterfall['structured_observation_url']}",
            timeout=2).read())
        assert payload["schema"] == "leo-tracker.doppler-observations/v1"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_dashboard_distinguishes_moving_rf_from_orbital_shape(tmp_path):
    root = tmp_path / "v2-watch"
    stem = _write_v2_fixture(root)
    analysis_path = root / "analysis" / f"{stem}.json"
    analysis = json.loads(analysis_path.read_text())
    analysis["joint_events"] = []
    analysis_path.write_text(json.dumps(analysis))
    (root / "wide").mkdir()
    candidate = {
        "moving_rf_qualified": True,
        "leo_like_qualified": True,
        "orbital_shape_qualified": False,
        "orbital_curvature_observable": False,
        "best_tle_curvature_resolution_bins": .42,
        "specific_tle_identifiable": False,
        "polarity": "positive", "duration_s": 25.0,
        "bounding_width_hz": 112_500.0, "receiver_path_correlation": .986,
        "tles_within_one_bin_of_best": 39, "rejection_reasons": [],
        "orbital_shape_rejection_reasons": [
            "best TLE does not beat affine drift control by one FFT bin"],
        "receivers": [
            {"linear_drift_hz_s": -3300, "path_rf_hz": [11.575e9]},
            {"linear_drift_hz_s": -3500, "path_rf_hz": [11.5751e9]}],
        "tle_comparisons": [{"rms_error_hz": 4766.0,
                             "affine_drift_rms_error_hz": 4433.0}],
    }
    (root / "wide" / f"{stem}.json").write_text(json.dumps({
        "schema": "leo-tracker.wide-feature-search/v1", "candidates": [candidate]}))

    model = DashboardModel(root)
    snapshot = model.snapshot(datetime(2023, 11, 14, 22, 17, tzinfo=timezone.utc))

    detection = snapshot["detected"]["detections"][0]
    assert detection["classification"] == (
        "coherent moving RF; LEO-rate compatible; orbital curvature unproven")
    assert detection["wide_feature_moving_rf_count"] == 1
    assert detection["wide_feature_leo_rate_count"] == 1
    assert detection["wide_feature_orbital_shape_count"] == 0
    assert detection["best_tle_match"] is None
    assert detection["mean_drift_hz_s"] == -3400
    waterfall = snapshot["waterfalls"]["waterfalls"][0]
    assert waterfall["wide_feature_orbital_shape_count"] == 0
    assert waterfall["best_wide_feature_candidate"]["tle_comparisons"][0][
        "affine_drift_rms_error_hz"] == 4433.0
    assert waterfall["best_wide_feature_candidate"][
        "best_tle_curvature_resolution_bins"] == .42

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(model))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        index = json.loads(urlopen(base + "/api/recordings", timeout=2).read())
        detail_url = index["recordings"][0]["detail_url"]
        detail = json.loads(urlopen(base + "/api" + detail_url, timeout=2).read())
        candidate = detail["statistics"]["best_wide_feature_candidate"]
        assert not candidate["orbital_shape_qualified"]
        assert candidate["tle_comparisons"][0]["affine_drift_rms_error_hz"] == 4433
        assert candidate["best_tle_curvature_resolution_bins"] == .42
        assert b"Complete statistics JSON" in urlopen(base + detail_url, timeout=2).read()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_dashboard_exposes_tle_integrated_candidate_without_event_fit(tmp_path):
    root = tmp_path / "v2-watch"
    stem = _write_v2_fixture(root)
    path = root / "analysis" / f"{stem}.json"
    analysis = json.loads(path.read_text())
    analysis["joint_events"] = []
    analysis["tle_guided_search"] = {"qualified_count": 1, "candidates": [{
        "qualified": True, "name": "STARLINK-TEST", "norad_id": 123,
        "predicted_doppler_span_hz": 300_000,
        "joint_score_db": .4, "stationary_improvement_db": .2,
        "trace_correlation": .9,
        "receivers": [{"receiver": 0, "frequency_bias_hz": -100_000},
                      {"receiver": 1, "frequency_bias_hz": 80_000}],
    }]}
    path.write_text(json.dumps(analysis))

    snapshot = DashboardModel(root).snapshot(
        datetime(2023, 11, 14, 22, 17, tzinfo=timezone.utc))

    assert snapshot["status"]["detection_count"] == 1
    detection = snapshot["detected"]["detections"][0]
    assert detection["classification"] == "TLE-integrated Doppler-path candidate"
    assert detection["receiver_drifts_hz_s"] == []
    assert detection["mean_drift_hz_s"] is None
    assert detection["radial_acceleration_m_s2"] is None
    assert detection["equivalent_radial_velocity_change_m_s"] is None
    assert detection["tle_guided_candidate"]["norad_id"] == 123
    json.dumps(snapshot, allow_nan=False)


def test_dashboard_http_serves_v2_named_plot(tmp_path):
    root = tmp_path / "v2-watch"
    stem = _write_v2_fixture(root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DashboardModel(root)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        snapshot = json.loads(urlopen(base + "/api/snapshot", timeout=2).read())
        assert snapshot["status"]["completed_chunks"] == 1
        waterfalls = json.loads(urlopen(base + "/api/waterfalls", timeout=2).read())
        assert waterfalls["waterfalls"][0]["chunk"] == 7
        response = urlopen(base + f"/plots/{stem}.png", timeout=2)
        assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert response.read() == b"v2-png"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_dashboard_plot_url_changes_when_plot_is_regenerated(tmp_path):
    root = tmp_path / "v2-watch"
    stem = _write_v2_fixture(root)
    model = DashboardModel(root)
    first = model.waterfalls()["waterfalls"][0]["plot_url"]
    plot = root / "plots" / f"{stem}.png"
    current = plot.stat().st_mtime_ns
    plot.touch()
    if plot.stat().st_mtime_ns == current:
        import os
        os.utime(plot, ns=(current + 1_000_000_000, current + 1_000_000_000))
    second = model.waterfalls()["waterfalls"][0]["plot_url"]

    assert first != second


def test_dashboard_reports_live_v2_watcher_before_first_chunk(tmp_path):
    root = tmp_path / "empty-v2-watch"
    root.mkdir()
    (root / "status.json").write_text(json.dumps({
        "stage": "capturing", "updated_utc": "2023-11-14T22:16:00Z"}))

    status = DashboardModel(root).status(
        datetime(2023, 11, 14, 22, 17, tzinfo=timezone.utc))

    assert status["state"] == "capturing"
    assert status["completed_chunks"] == 0
    assert status["update_age_s"] == 60


def test_dashboard_caches_large_pass_catalog_until_atomic_refresh(tmp_path):
    root = tmp_path/"passes"; root.mkdir(); path = root/"passes.json"
    first = {"satellites": []}; path.write_text(json.dumps(first))
    model = DashboardModel(root)
    catalog, rows = model._catalog()
    assert catalog == first and rows == []
    cached = model._catalog()[0]
    assert cached is catalog
    path.write_text(json.dumps({"satellites": [], "generated_at": "new"}))
    refreshed = model._catalog()[0]
    assert refreshed["generated_at"] == "new" and refreshed is not catalog


def test_dashboard_publishes_tracker_ensemble_counts(tmp_path):
    root = tmp_path/"tracker-watch"; stem = _write_v2_fixture(root)
    (root/"tracker-ensemble").mkdir()
    (root/"tracker-ensemble"/f"{stem}.json").write_text(json.dumps({
        "schema": "leo-tracker.tracker-ensemble/v1",
        "joint_tracks": [{"qualified": True}],
        "identifications": [{"qualified": True, "compatible": True,
                             "name": "STARLINK-TEST",
                             "tracker": "dedoppler-linear/v1"}],
        "metrics": {"candidate_count_by_tracker": {"dedoppler-linear/v1": 8},
            "qualified_count_by_tracker": {"dedoppler-linear/v1": 3},
            "runtime_s_by_tracker": {"dedoppler-linear/v1": 1.25}}}))
    tracker_plot = root/"plots"/f"{stem}-trackers.png"
    tracker_plot.write_bytes(b"png")
    (root/"coherent").mkdir()
    (root/"coherent"/f"{stem}.json").write_text(json.dumps({
        "schema": "leo-tracker.coherent-doppler-ensemble/v1",
        "blocks": [{"receivers": []}],
        "joint_track": {"qualified": True, "mean_drift_hz_s": 4000}}))
    waterfall = DashboardModel(root).waterfalls()["waterfalls"][0]
    assert waterfall["tracker_ensemble_available"]
    assert waterfall["tracker_candidate_count_by_tracker"]["dedoppler-linear/v1"] == 8
    assert waterfall["tracker_qualified_count_by_tracker"]["dedoppler-linear/v1"] == 3
    assert waterfall["tracker_qualified_joint_count"] == 1
    assert waterfall["tracker_qualified_tle_identification_count"] == 1
    assert waterfall["tracker_tle_compatible_count"] == 1
    assert waterfall["tracker_best_tle_identification"]["name"] == "STARLINK-TEST"
    assert waterfall["coherent_analysis_available"]
    assert waterfall["coherent_block_count"] == 1
    assert waterfall["coherent_joint_track"]["qualified"]
    assert waterfall["tracker_plot_url"].startswith(f"/plots/{stem}-trackers.png?v=")


def test_dashboard_cache_invalidates_when_late_tracker_sidecar_arrives(tmp_path):
    root = tmp_path/"late-tracker"; stem = _write_v2_fixture(root)
    model = DashboardModel(root)
    assert not model.waterfalls()["waterfalls"][0]["tracker_ensemble_available"]
    (root/"tracker-ensemble").mkdir()
    (root/"tracker-ensemble"/f"{stem}.json").write_text(json.dumps({
        "schema": "leo-tracker.tracker-ensemble/v1", "joint_tracks": [],
        "metrics": {}}))
    assert model.waterfalls()["waterfalls"][0]["tracker_ensemble_available"]


def test_dashboard_publishes_and_serves_beacon_decode_artifacts(tmp_path):
    observation_root = tmp_path / "watch"
    observation_root.mkdir()
    beacon_root = tmp_path / "beacons"
    name = "ch4-lower-edge-narrow-20260805T171900Z"
    capture = beacon_root / "captures" / name
    reports = beacon_root / "reports"
    (reports / "followups").mkdir(parents=True)
    (reports / "decoded").mkdir()
    (reports / "fingerprints").mkdir()
    capture.mkdir(parents=True)
    manifest = {"state": "complete", "created_utc_ns": 1_700_000_000_000_000_000,
        "sample_rate_hz": 2_500_000, "bandwidth_hz": 2_300_000,
        "center_frequency_hz": 1_709_687_500, "rf_center_hz": 11_459_687_500,
        "gain_mode": "manual", "configured_gain_db": 50,
        "metadata": {"channel_number": 4, "region": "lower-edge"}}
    (capture / "manifest.json").write_text(json.dumps(manifest))
    report = {"schema": "leo-tracker.starlink-beacon-analysis/v1",
        "capture_manifest": manifest, "summary": {"exact_candidate_count": 1},
        "analysis": {"exact_acquisition_method": "pilot_symbolwise_v3"},
        "exact_checks": []}
    (reports / f"{name}.json").write_text(json.dumps(report))
    (reports / "followups" / f"{name}.json").write_text(json.dumps({
        "confirmation": {"confirmed": True}}))
    decode = {"schema": "leo-tracker.starlink-edge-decode/v1", "combined": {
        "minimum_pilot_accuracy": .719, "minimum_sss_accuracy": .375,
        "minimum_frame_count": 7}}
    (reports / "decoded" / f"{name}.json").write_text(json.dumps(decode))
    (reports / "decoded" / f"{name}.png").write_bytes(b"decode-png")
    fingerprint = {"schema": "leo-tracker.starlink-waveform-fingerprint/v1",
        "capture_name": name, "interpretation": {"satellite_identity_claim": False}}
    (reports / "fingerprints" / f"{name}.json").write_text(json.dumps(fingerprint))
    (reports / "fingerprints" / f"{name}.png").write_bytes(b"fingerprint-png")
    (reports / "fingerprints" / "index.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-waveform-fingerprint-index/v1",
        "fingerprint_count": 1, "membership": {name: "wf-test"},
        "clusters": [{"cluster_id": "wf-test", "member_count": 1}],
        "nearest_matches": {name: []}}))

    model = DashboardModel(observation_root, beacon_root=beacon_root)
    row = model.beacon()["captures"][0]
    assert row["decode"] == decode["combined"]
    assert row["decode_url"] == f"/beacon-decodes/{name}.json"
    assert row["decode_plot_url"] == f"/beacon-decode-plots/{name}.png"
    assert row["fingerprint_url"] == f"/beacon-fingerprints/{name}.json"
    assert row["fingerprint_cluster_id"] == "wf-test"
    assert row["fingerprint_cluster_size"] == 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(model))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        dashboard_html = urlopen(base + "/", timeout=2).read()
        assert b'id="recordings"' in dashboard_html
        assert b"<img" not in dashboard_html
        recordings = json.loads(urlopen(base + "/api/recordings", timeout=2).read())
        assert recordings["summary"]["analyzed_capture_count"] == 1
        assert recordings["summary"]["temporally_confirmed_capture_count"] == 1
        assert recordings["summary"]["decoded_capture_count"] == 1
        assert recordings["summary"]["fingerprint_count"] == 1
        index_row = next(item for item in recordings["recordings"]
                         if item["recording_id"] == name)
        assert index_row["confirmed"]
        assert index_row["decoded"]
        assert index_row["dual_candidate_count"] == 1
        assert index_row["single_receiver_candidate_count"] == 0
        assert index_row["beacon_detected_count"] == 1
        assert index_row["decode_frame_count"] == 7
        assert index_row["pilot_accuracy"] == .719
        assert index_row["fingerprint_family"] == "wf-test"
        assert index_row["fingerprint_family_size"] == 1
        detail_page = urlopen(base + index_row["detail_url"], timeout=2).read()
        assert b"Diagnostic plot" in detail_page
        assert b"Radio parameters" in detail_page
        assert b"Hardware gain readback" in detail_page
        assert b"Temporal QPSK fingerprint" in detail_page
        assert b"Nearest fingerprint comparisons" in detail_page
        assert b"Doppler and predicted-pass matching" in detail_page
        assert b"Dual-receiver detector and decode evidence" in detail_page
        assert b"Raw signal and analysis plots" in detail_page
        detail = json.loads(urlopen(
            base + "/api" + index_row["detail_url"], timeout=2).read())
        assert detail["statistics"]["radio_parameters"]["tuning"][
            "sample_rate_hz"] == 2_500_000
        assert detail["statistics"]["fingerprint_plot_url"] == (
            f"/beacon-fingerprint-plots/{name}.png")
        assert row["decode_plot_url"] in detail["plots"]
        assert row["fingerprint_url"] in [item["url"] for item in detail["artifacts"]]
        served = json.loads(urlopen(base + row["decode_url"], timeout=2).read())
        assert served["combined"]["minimum_frame_count"] == 7
        assert urlopen(base + row["decode_plot_url"], timeout=2).read() == b"decode-png"
        served_fingerprint = json.loads(urlopen(
            base + row["fingerprint_url"], timeout=2).read())
        assert not served_fingerprint["interpretation"]["satellite_identity_claim"]
        assert urlopen(base + detail["statistics"]["fingerprint_plot_url"],
                       timeout=2).read() == b"fingerprint-png"
        fingerprint_svg = urlopen(
            base + "/beacon-fingerprint-map.svg", timeout=2).read()
        assert fingerprint_svg.startswith(b'<svg xmlns="http://www.w3.org/2000/svg"')
        assert b"Nearest-neighbor fingerprint evidence map" in fingerprint_svg
        assert b"No linked family yet" in fingerprint_svg
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
