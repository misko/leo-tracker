import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb", reason="analysis store needs analysis extra")

from leo_tracker.radio.analysis_store.identity import run_id_for_manifest
from leo_tracker.radio.analysis_store import ingest as ingest_module
from leo_tracker.radio.analysis_store.ingest import AnalysisStore
from leo_tracker.radio.analysis_store.mapping import (build_input_manifest,
                                                       enqueue_input)
from leo_tracker.radio.analysis_store.queue import (StoreQueue, enqueue_backfill,
                                                     owner_lock, reconcile_batch)
from leo_tracker.radio.analysis_store.repository import (DuckDBAnalysisRepository,
                                                          SnapshotCache)
from leo_tracker.radio.analysis_store.service import run_service
from leo_tracker.radio.analysis_store.snapshot import publish_snapshot
from leo_tracker.radio.dashboard import DashboardModel


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _completed_run(root: Path, *,
                   name="ch4-lower-edge-narrow-20260811T120000Z",
                   pipeline="kalman-test", probes=3, confirmed=False):
    reports = root / "reports"
    for relative in ("followups", "tracks", "decoded", "associations",
                     "frame-tracks", "channel-links", "fragment-associations",
                     "fragment-diagnostics", "fingerprints", "plots"):
        (reports / relative).mkdir(parents=True, exist_ok=True)
    capture = {
        "state": "complete", "receiver_count": 2,
        "created_utc_ns": 1_786_000_000_000_000_000,
        "center_frequency_hz": 1_709_687_500.0,
        "rf_center_hz": 11_459_687_500.0,
        "sample_rate_hz": 2_500_000.0, "bandwidth_hz": 2_300_000.0,
        "gain_mode": "manual", "configured_gain_db": 50.0,
        "requested_duration_s": 60.0,
        "identity": {"radio_id": "pluto-19f2", "serial": "abc",
                     "receiver_labels": ["lnb-c", "lnb-d"]},
        "metadata": {"channel_number": 4, "region": "lower-edge",
                     "observation_mode": "narrow"},
        "sample_statistics": {"receivers": [
            {"rms_magnitude": 300.0, "near_full_scale_fraction": 0.0},
            {"rms_magnitude": 310.0, "near_full_scale_fraction": 0.01}]},
        "chunks": [],
    }
    checks = []
    for index in range(probes):
        checks.append({"start_s": float(index), "duration_s": .01,
            "candidate": index == 0, "qualified": False,
            "followup_trigger": index == 0, "cfo_difference_hz": 1200.0,
            "epoch_difference_samples": 2,
            "receiver_candidates": [index == 0, False],
            "receiver_qualified": [False, False],
            "receivers": [{"acquisition": {
                "exact_match": {"frequency_offset_hz": 1000.0 + side,
                                "epoch_sample": 40 + side, "score": .5},
                "match_score_margin": .1 + side / 100}}
                for side in (0, 1)]})
    analysis = {"schema": "leo-tracker.starlink-beacon-analysis/v1",
        "created_utc": "2026-08-11T12:02:00+00:00",
        "capture_manifest": capture,
        "analysis": {"exact_acquisition_method": "pilot_symbolwise_v3",
                     "exact_interval_s": 1.0},
        "windows": [{"start_s": 0.0, "duration_s": 1.0, "qualified": False}],
        "exact_checks": checks,
        "summary": {"window_count": 1, "qualified_window_count": 0,
                    "exact_check_count": probes, "exact_candidate_count": 1,
                    "exact_qualified_count": 0,
                    "single_receiver_candidate_count": 1,
                    "single_receiver_qualified_count": 0,
                    "followup_trigger_count": 1, "exact_sampled_time_s": probes * .01,
                    "exact_temporal_coverage_fraction": probes * .01 / 60}}
    followup = {"schema": "leo-tracker.starlink-beacon-followup/v1",
        "checks": [{"start_s": 0.0, "duration_s": .01, "candidate": True,
                    "qualified": False}],
        "confirmation": {"confirmed": confirmed,
            "cross_receiver_links": ([{"start_s": 0.0, "stop_s": .2}]
                                     if confirmed else [])}}
    analysis_path = reports / f"{name}.json"
    followup_path = reports / "followups" / f"{name}.json"
    analysis_path.write_text(json.dumps(analysis))
    followup_path.write_text(json.dumps(followup))
    (reports / "plots" / f"{name}.png").write_bytes(b"plot")
    completion = {"schema": "leo-tracker.analysis-receipt/v1", "job": name,
        "mode": "narrow", "status": "success", "confirmed": confirmed,
        "full_coverage": False, "pipeline_id": pipeline,
        "completed_utc": "2026-08-11T12:03:00+00:00", "context": None,
        "outputs": {
            "analysis": {"path": str(analysis_path),
                         "bytes": analysis_path.stat().st_size,
                         "sha256": _sha(analysis_path)},
            "followup": {"path": str(followup_path),
                         "bytes": followup_path.stat().st_size,
                         "sha256": _sha(followup_path)}}}
    receipt = reports / "runs" / pipeline / name / "completion.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps(completion))
    return name, pipeline


def _enqueue_process(arguments):
    shared_root, store_root, name, pipeline = arguments
    return enqueue_input(Path(shared_root), Path(store_root), name, pipeline)[1]["run_id"]


def test_manifest_is_content_addressed_and_inventories_explicit_outputs(tmp_path):
    name, pipeline = _completed_run(tmp_path)

    manifest = build_input_manifest(tmp_path, name, pipeline)

    assert manifest["run_id"] == run_id_for_manifest(manifest)
    assert set(manifest["documents"]) == {"analysis", "followup"}
    assert set(manifest["artifacts"]) == {"analysis_plot"}
    assert manifest["documents"]["analysis"]["schema"] == \
        "leo-tracker.starlink-beacon-analysis/v1"


def test_context_bundle_manifest_is_part_of_run_identity_and_revalidated(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    context = tmp_path / "context" / "bundles" / "context-test"
    context.mkdir(parents=True)
    context_manifest = context / "manifest.json"
    context_manifest.write_text(json.dumps({
        "schema": "leo-tracker.analysis-offload/v2",
        "bundle_id": context.name, "files": {},
    }))
    completion_path = (tmp_path / "reports" / "runs" / pipeline / name /
                       "completion.json")
    completion = json.loads(completion_path.read_text())
    completion["context"] = str(context)
    completion_path.write_text(json.dumps(completion))
    store_root = tmp_path / "store"

    manifest_path, manifest = enqueue_input(tmp_path, store_root, name, pipeline)
    assert manifest["context"]["bundle_id"] == "context-test"
    context_manifest.write_text(context_manifest.read_text() + "\n")

    with pytest.raises(ValueError, match="context manifest changed"):
        AnalysisStore(store_root / "live.duckdb", tmp_path).ingest(manifest_path)


def test_live_database_refuses_network_filesystem(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest_module, "_mount_type", lambda _path: "nfs4")

    with pytest.raises(ValueError, match="Kalman-local"):
        AnalysisStore(tmp_path / "live.duckdb", tmp_path).initialize()


def test_transactional_ingest_is_idempotent_and_queryable(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    store_root = tmp_path / "store"
    manifest_path, manifest = enqueue_input(tmp_path, store_root, name, pipeline)
    store = AnalysisStore(store_root / "live.duckdb", tmp_path)

    first = store.ingest(manifest_path)
    second = store.ingest(manifest_path)

    assert first["inserted"] is True and second["inserted"] is False
    assert first["receiver_probe_count"] == 6
    status = store.status()
    assert status["run_count"] == status["recording_count"] == 1
    assert status["duckdb_version"].lstrip("v") == duckdb.__version__
    assert store.query(
        "SELECT parameters_json->>'$.exact_acquisition_method' "
        "FROM analysis_parameters")[1] == [("pilot_symbolwise_v3",)]
    columns, rows = store.query(
        "SELECT report, lnb, count(*) n FROM probes GROUP BY report, lnb ORDER BY lnb")
    assert columns == ["report", "lnb", "n"]
    assert rows == [(name, "lnb-c", 3), (name, "lnb-d", 3)]


def test_track_decode_and_association_facts_are_typed_child_rows(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    reports = tmp_path / "reports"
    (reports / "tracks" / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "tracks": [{"track_id": "track-000", "consensus": {"qualified": True},
                    "summary": {"valid_duration_s": 4.0},
                    "observations": [{"time_s": 1.0,
                                      "utc": "2026-08-11T12:00:01Z",
                                      "lock_valid": True}]}]}))
    (reports / "decoded" / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-edge-decode/v1",
        "combined": {"minimum_frame_count": 7, "minimum_sss_accuracy": .5,
                     "soft_dual_rx": {"pilot": {"hard_symbol_accuracy": .75,
                                                  "soft_mean_confidence": .8,
                                                  "rms_evm": .2}}}}))
    (reports / "associations" / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-tle-association/v2",
        "associations": [{"track_id": "track-000", "qualified": True,
            "best_norad_id": 12345, "best_name": "STARLINK-TEST",
            "candidates": [{"rank": 1, "norad_id": 12345,
                "name": "STARLINK-TEST", "train_residual_rms_hz": 100.0,
                "holdout_residual_rms_hz": 120.0}]}]}))
    store_root = tmp_path / "store"
    manifest_path, _ = enqueue_input(tmp_path, store_root, name, pipeline)
    store = AnalysisStore(store_root / "live.duckdb", tmp_path)

    store.ingest(manifest_path)

    assert store.query("SELECT count(*) FROM tracks")[1][0][0] == 1
    assert store.query("SELECT count(*) FROM track_points")[1][0][0] == 1
    assert store.query("SELECT frame_count, pilot_accuracy FROM decodes")[1] == [(7, .75)]
    assert store.query("SELECT best_norad_id FROM associations")[1] == [(12345,)]
    assert store.query("SELECT holdout_residual_rms_hz FROM association_candidates")[1] == [(120.0,)]


@pytest.mark.parametrize("failure", ["probes", "dashboard"])
def test_injected_ingest_failure_rolls_back_every_table(tmp_path, failure):
    name, pipeline = _completed_run(tmp_path)
    store_root = tmp_path / "store"
    manifest_path, _ = enqueue_input(tmp_path, store_root, name, pipeline)
    store = AnalysisStore(store_root / "live.duckdb", tmp_path)

    with pytest.raises(RuntimeError, match="injected failure"):
        store.ingest(manifest_path, fail_after=failure)

    assert store.status()["run_count"] == 0
    assert store.query("SELECT count(*) FROM recordings")[1][0][0] == 0


def test_queue_recovers_running_request_and_drains_exactly_once(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    queue = StoreQueue(tmp_path / "store", tmp_path)
    queued = queue.enqueue(name, pipeline)
    source = Path(queued["path"])
    running = queue.root / "running" / source.name
    source.replace(running)

    assert queue.recover() == [queued["run_id"]]
    result = queue.drain()

    assert result["inserted"] == 1 and result["failures"] == 0
    assert queue.counts()["ready"] == 0
    assert queue.store.status()["run_count"] == 1


def test_changed_source_after_enqueue_is_quarantined_without_database_rows(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    queue = StoreQueue(tmp_path / "store", tmp_path)
    queue.enqueue(name, pipeline)
    analysis = tmp_path / "reports" / f"{name}.json"
    analysis.write_text(analysis.read_text() + "\n")

    with pytest.raises(ValueError, match="changed after enqueue"):
        queue.process_next()

    assert queue.store.status()["run_count"] == 0
    assert queue.counts()["failed"] == 1

    good_name, good_pipeline = _completed_run(
        tmp_path, name="ch4-lower-edge-narrow-20260811T130000Z")
    queue.enqueue(good_name, good_pipeline)
    result = queue.drain(limit=1, continue_on_error=True)
    assert result["processed"] == result["inserted"] == 1
    assert queue.store.status()["run_count"] == 1


def test_snapshot_is_verified_and_repository_reads_local_cache(tmp_path):
    name, pipeline = _completed_run(tmp_path, confirmed=True)
    queue = StoreQueue(tmp_path / "store", tmp_path)
    queue.enqueue(name, pipeline); queue.drain()

    receipt = publish_snapshot(queue.store, queue.root, tmp_path / "published")
    cache = SnapshotCache(tmp_path / "published/current.json", tmp_path / "cache")
    repository = DuckDBAnalysisRepository(
        pointer=tmp_path / "published/current.json", cache_dir=tmp_path / "cache")

    cached = cache.current()
    assert cached.is_file() and _sha(cached) == receipt["sha256"]
    assert repository.summary()["temporally_confirmed_capture_count"] == 1
    assert repository.recent_recordings()[0]["recording_id"] == name
    assert repository.recording_detail(name)["recording_id"] == name


def test_snapshot_cache_keeps_last_verified_generation_on_bad_pointer(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    queue = StoreQueue(tmp_path / "store", tmp_path)
    queue.enqueue(name, pipeline); queue.drain()
    publish_snapshot(queue.store, queue.root, tmp_path / "published")
    cache = SnapshotCache(tmp_path / "published/current.json", tmp_path / "cache")
    first = cache.current()
    (tmp_path / "published/current.json").write_text(json.dumps({
        "generation": "bad", "snapshot": "snapshots/missing.duckdb",
        "bytes": 1, "sha256": "0" * 64}))

    assert cache.current() == first


def test_single_owner_service_recovers_ingests_publishes_and_reports_health(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    store_root = tmp_path / "store"
    enqueue_input(tmp_path, store_root, name, pipeline)
    runtime = tmp_path / "runtime.json"

    result = run_service(
        store_root, tmp_path, publication_root=tmp_path / "published",
        runtime_output=runtime, once=True, snapshot_interval_s=.001)

    assert result["processed"] == 1
    assert result["store"]["run_count"] == 1
    assert result["last_publication"]["run_count"] == 1
    assert json.loads(runtime.read_text())["last_error"] is None


def test_second_store_owner_is_refused(tmp_path):
    store_root = tmp_path / "store"
    with owner_lock(store_root):
        with pytest.raises(RuntimeError, match="another process owns"):
            run_service(store_root, tmp_path, once=True)


def test_backfill_dry_run_is_bounded_and_writes_nothing(tmp_path):
    first = _completed_run(tmp_path)
    _completed_run(tmp_path, name="ch4-lower-edge-narrow-20260811T130000Z",
                   pipeline=first[1])
    queue = StoreQueue(tmp_path / "store", tmp_path, create=False)

    result = enqueue_backfill(queue, limit=1, dry_run=True)

    assert result["scanned"] == 1
    assert len(result["planned"]) == 1
    assert not queue.root.exists()
    assert not list((queue.root / "inbox").glob("*.json"))


def test_bounded_reconciliation_recovers_a_missed_worker_enqueue(tmp_path):
    name, pipeline = _completed_run(tmp_path)
    queue = StoreQueue(tmp_path / "store", tmp_path)
    queue.store.initialize()

    result = reconcile_batch(queue, scan_limit=1)
    queue.drain()

    assert result["scanned"] == 1
    assert result["queued"][0]["run_id"]
    assert queue.store.status()["run_count"] == 1
    again = reconcile_batch(queue, scan_limit=1)
    assert again["queued"] == [] and len(again["present"]) == 1


def test_one_shot_service_ingests_work_discovered_by_reconciliation(tmp_path):
    _completed_run(tmp_path)

    result = run_service(
        tmp_path / "store", tmp_path, once=True,
        snapshot_interval_s=.001, reconciliation_interval_s=.001,
        reconciliation_limit=1)

    assert result["processed"] == 1
    assert result["store"]["run_count"] == 1
    assert result["last_reconciliation"]["scanned"] == 1


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_sixteen_producers_enqueue_while_one_owner_commits(tmp_path):
    pipeline = "kalman-test"
    names = []
    for index in range(16):
        name = f"ch4-lower-edge-narrow-20260811T1200{index:02d}Z"
        _completed_run(tmp_path, name=name, pipeline=pipeline, probes=1)
        names.append(name)
    store_root = tmp_path / "store"
    arguments = [(str(tmp_path), str(store_root), name, pipeline) for name in names]

    with multiprocessing.get_context("fork").Pool(16) as pool:
        run_ids = pool.map(_enqueue_process, arguments)

    assert len(set(run_ids)) == 16
    queue = StoreQueue(store_root, tmp_path)
    result = queue.drain()
    assert result["inserted"] == 16 and result["failures"] == 0
    assert queue.store.status()["run_count"] == 16


def test_dashboard_reads_listing_and_detail_from_verified_snapshot(tmp_path):
    name, pipeline = _completed_run(tmp_path, confirmed=True)
    queue = StoreQueue(tmp_path / "store", tmp_path)
    queue.enqueue(name, pipeline); queue.drain()
    publish_snapshot(queue.store, queue.root, tmp_path / "published")
    capture_dir = tmp_path / "captures" / name
    capture_dir.mkdir(parents=True)
    (capture_dir / "manifest.json").write_text(json.dumps({
        "state": "complete", "created_utc_ns": 1_786_000_000_000_000_000,
        "identity": {}, "metadata": {},
    }))
    watch = tmp_path / "watch"; watch.mkdir()
    model = DashboardModel(
        watch, beacon_root=tmp_path,
        analysis_store_pointer=tmp_path / "published" / "current.json",
        analysis_store_cache=tmp_path / "dashboard-cache")
    # The immutable snapshot is now sufficient for historical listing and
    # detail even if the compatibility documents are temporarily unavailable.
    (tmp_path / "reports" / f"{name}.json").unlink()
    (tmp_path / "reports" / "followups" / f"{name}.json").unlink()

    index = model.recordings()
    detail = model.recording_detail("beacon", name)

    row = next(item for item in index["recordings"] if item["recording_id"] == name)
    assert row["confirmed"] is True
    assert index["summary"]["analyzed_capture_count"] == 1
    assert not any(row.get("status") == "analyzing"
                   for row in index["recordings"] if row["recording_id"] == name)
    assert detail["recording_id"] == name
    assert detail["statistics"]["summary"]["exact_check_count"] == 3


def test_deployment_keeps_workers_as_manifest_producers_and_dashboard_read_only():
    root = Path(__file__).resolve().parents[1]
    server = (root / "scripts/starlink-analysis-server.sh").read_text()
    store_unit = (root / "deploy/leo-tracker-analysis-store.service").read_text()
    dashboard_unit = (root / "deploy/systemd/leo-tracker-dashboard.service").read_text()

    assert "starlink-analysis-store enqueue" in server
    assert "analysis_store_enqueue_failed" in server
    assert "starlink-analysis-store service" in store_unit
    assert "StateDirectory=leo-tracker-analysis-store" in store_unit
    assert "--publication-root /mnt/qnap01/mouse9911/leo/database" in store_unit
    assert "--analysis-store-pointer" in dashboard_unit
    assert "--analysis-store-cache /var/cache/leo-tracker-analysis-store" in dashboard_unit
