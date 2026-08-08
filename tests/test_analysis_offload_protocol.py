import hashlib
import json
from pathlib import Path

import numpy as np

from leo_tracker.radio.beacon.offload import (
    RECEIPT_SCHEMA,
    analysis_status,
    audit_completed,
    create_context_bundle,
    current_context,
    enqueue_analysis_backfill,
    enqueue_export_backfill,
    followup_has_checks,
    validate_context_bundle,
    validate_outputs,
    write_receipt,
)
from leo_tracker.radio.beacon.template_learning import load_learned_beacon


def _learned_fixture(root: Path) -> Path:
    samples = root / "source.npz"
    np.savez(samples, template_rx0=np.ones(8), template_rx1=np.ones(8))
    report = root / "source.json"
    report.write_text(json.dumps({
        "schema": "leo-tracker.starlink-learned-bandpass-beacon/v1",
        "samples": str(samples),
        "samples_sha256": hashlib.sha256(samples.read_bytes()).hexdigest(),
        "frame_sample_count": 8,
        "validation": {"qualified": True},
    }))
    return report


def test_context_bundle_is_atomic_self_contained_and_mount_independent(tmp_path):
    learned = _learned_fixture(tmp_path)
    passes = tmp_path / "passes.json"; passes.write_text("[]")
    tle = tmp_path / "tle.json"
    tle.write_text(json.dumps({"schema": "leo-tracker.tle-catalog/v1"}))

    bundle = create_context_bundle(tmp_path / "context", learned=learned,
                                   passes=passes, tle_catalog=tle)

    assert bundle == current_context(tmp_path / "context")
    validate_context_bundle(bundle)
    bundled_report = json.loads((bundle / "learned-beacon.json").read_text())
    assert bundled_report["samples"] == "learned-beacon.npz"
    loaded, arrays = load_learned_beacon(bundle / "learned-beacon.json")
    assert loaded["validation"]["qualified"]
    assert arrays["template_rx0"].shape == (8,)
    assert not list((tmp_path / "context/bundles").glob("*.partial.*"))


def test_context_bundle_flattens_tle_archive_snapshot(tmp_path):
    archive = tmp_path / "tle-history"
    objects = archive / "objects"; objects.mkdir(parents=True)
    catalog = objects / "catalog.json"
    catalog.write_text(json.dumps({"schema": "leo-tracker.tle-catalog/v1",
                                   "content": "fixture"}))
    snapshot = archive / "latest.json"
    snapshot.write_text(json.dumps({
        "schema": "leo-tracker.tle-archive-snapshot/v1",
        "object": "objects/catalog.json",
    }))

    bundle = create_context_bundle(tmp_path / "context", tle_catalog=snapshot)

    assert json.loads((bundle / "tle-catalog.json").read_text())["schema"] == \
        "leo-tracker.tle-catalog/v1"
    assert not (bundle / "objects").exists()


def test_output_validation_requires_analysis_and_followup(tmp_path):
    reports = tmp_path / "reports"
    (reports / "followups").mkdir(parents=True)
    (reports / "sample.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-analysis/v1"}))
    (reports / "followups/sample.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-followup/v1",
                    "confirmation": {"confirmed": False}}))

    receipt = validate_outputs(tmp_path, "sample", "narrow", context=None)

    assert receipt["schema"] == RECEIPT_SCHEMA
    assert not receipt["confirmed"]
    assert receipt["outputs"]["analysis"]["sha256"]


def test_followup_check_inspection_distinguishes_empty_and_candidate_sets(tmp_path):
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-followup/v1", "checks": []}))
    assert not followup_has_checks(followup)
    followup.write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-followup/v1",
        "checks": [{"candidate": False}]}))
    assert followup_has_checks(followup)


def test_full_coverage_receipt_requires_track_archive_and_versioned_output(tmp_path):
    reports = tmp_path / "reports"
    (reports / "followups").mkdir(parents=True)
    (reports / "tracks").mkdir()
    (reports / "sample.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1"}))
    (reports / "followups/sample.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-followup/v1", "checks": [],
        "confirmation": {"confirmed": False}}))
    (reports / "tracks/sample.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1"}))
    receipt_path = tmp_path / "archive/catalog/receipts/sample.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps({
        "schema": "leo-tracker.evidence-archive-receipt/v1",
        "recording_id": "sample", "status": "verified",
        "source_verified": True}))

    receipt = validate_outputs(
        tmp_path, "sample", "narrow", context=None, full_coverage=True,
        pipeline_id="kalman-full-test", archive_root=tmp_path / "archive")
    write_receipt(tmp_path, receipt)

    assert receipt["full_coverage"]
    assert receipt["archive_receipt"]["source_verified"]
    assert receipt["outputs"]["track"]["bytes"] > 0
    versioned = reports / "runs/kalman-full-test/sample/completion.json"
    versioned_receipt = json.loads(versioned.read_text())
    assert versioned_receipt["pipeline_id"] == "kalman-full-test"
    preserved = reports / "runs/kalman-full-test/sample/outputs/track.json"
    assert preserved.read_bytes() == (reports / "tracks/sample.json").read_bytes()
    assert versioned_receipt["versioned_outputs"]["track"]["sha256"]


def test_analysis_status_reports_queue_age_versioned_runs_and_archives(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    (queue / "done").mkdir(parents=True)
    (queue / "failed").mkdir()
    ready = queue / "one.job"; ready.write_text("job")
    (queue / "two.running.0").write_text("job")
    (queue / "done/three.job").write_text("job")
    completion = tmp_path / "reports/runs/kalman-test/three/completion.json"
    completion.parent.mkdir(parents=True); completion.write_text("{}")
    archived = tmp_path / "archive/catalog/receipts/three.json"
    archived.parent.mkdir(parents=True); archived.write_text("{}")

    report = analysis_status(
        tmp_path, workers=16, pipeline_id="kalman-test",
        archive_root=tmp_path / "archive",
        output=tmp_path / "reports/status.json")

    assert report["queue"]["ready"] == 1
    assert report["queue"]["running"] == 1
    assert report["queue"]["succeeded"] == 1
    assert report["queue"]["oldest_ready_age_s"] >= 0
    assert report["versioned_completion_count"] == 1
    assert report["verified_archive_count"] == 1
    assert json.loads((tmp_path / "reports/status.json").read_text()) == report


def test_backfill_is_versioned_bounded_atomic_and_idempotent(tmp_path):
    create_context_bundle(tmp_path / "context")
    captures = tmp_path / "captures"; captures.mkdir()
    for name, mode in (("one", "narrow"), ("two", "oversample"),
                       ("three", "wide")):
        capture = captures / name; capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "state": "complete", "metadata": {"observation_mode": mode}}))
    completed = tmp_path / "reports/runs/kalman-v1/one/completion.json"
    completed.parent.mkdir(parents=True); completed.write_text("{}")

    preview = enqueue_analysis_backfill(
        tmp_path, pipeline_id="kalman-v1", limit=1, dry_run=True)
    assert preview["queued"] == ["two"]
    assert not list((tmp_path / "staging/analysis-queue").glob("*.job"))

    first = enqueue_analysis_backfill(tmp_path, pipeline_id="kalman-v1", limit=1)
    second = enqueue_analysis_backfill(tmp_path, pipeline_id="kalman-v1", limit=1)

    assert first["queued"] == ["two"]
    assert second["queued"] == ["three"]
    jobs = sorted((tmp_path / "staging/analysis-queue").glob("*.job"))
    assert len(jobs) == 2
    assert jobs[0].read_text().split("\t")[3].startswith("context/bundles/")


def test_export_backfill_queues_only_missing_preserved_sources(tmp_path):
    source = tmp_path / "source"; shared = tmp_path / "shared"
    captures = source / "captures"; captures.mkdir(parents=True)
    for name in ("one", "two", "three"):
        capture = captures / name; capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "state": "complete", "metadata": {"observation_mode": "narrow"}}))
    (shared / "captures/two").mkdir(parents=True)
    completed = shared / "reports/runs/kalman-v1/one/completion.json"
    completed.parent.mkdir(parents=True); completed.write_text("{}")

    preview = enqueue_export_backfill(
        source, shared, pipeline_id="kalman-v1", dry_run=True)
    result = enqueue_export_backfill(
        source, shared, pipeline_id="kalman-v1", limit=1)
    repeated = enqueue_export_backfill(
        source, shared, pipeline_id="kalman-v1", limit=1)

    assert preview["queued"] == ["three"]
    assert result["queued"] == ["three"]
    assert repeated["queued"] == []
    job = next((source / "staging/analysis-queue").glob("*.job"))
    fields = job.read_text().rstrip().split("\t")
    assert fields == ["three", str(captures / "three"), "narrow"]


def test_full_coverage_wide_receipt_does_not_require_a_skipped_track(tmp_path):
    """The server skips doppler_track for wide, so requiring it fails every wide job.

    Wide recordings used to die earlier, in acquire, which hid this: once they
    got that far they could never produce a receipt.
    """
    reports = tmp_path / "reports"
    (reports / "followups").mkdir(parents=True)
    (reports / "sample.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1"}))
    (reports / "followups/sample.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-followup/v1", "checks": [],
        "confirmation": {"confirmed": False}}))

    receipt = validate_outputs(tmp_path, "sample", "wide", context=None,
                               full_coverage=True, pipeline_id="kalman-full-test")

    assert receipt["status"] == "success"
    assert "track" not in receipt["outputs"]
    # Every other mode still must produce one.
    (reports / "tracks").mkdir()
    (reports / "tracks/sample.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1"}))
    for mode in ("narrow", "oversample", "hop"):
        assert "track" in validate_outputs(
            tmp_path, "sample", mode, context=None, full_coverage=True,
            pipeline_id="kalman-full-test")["outputs"]


def test_hop_children_are_backfillable_under_their_session_identity(tmp_path):
    """Hop recordings were unreachable: wrong mode name, and never enumerated.

    A hop child's manifest says `channel-hop` while the pipeline mode is `hop`,
    and its directory lives one level below `hop-sessions/`. Backfill rejected
    the first and never saw the second, so no hop capture could be archived.
    """
    source = tmp_path / "source"; shared = tmp_path / "shared"
    (source / "captures").mkdir(parents=True)
    segment = source / "hop-sessions/hop-lower-edge-20260807T201908Z-b00/03-ch4-lower-edge"
    segment.mkdir(parents=True)
    (segment / "manifest.json").write_text(json.dumps({
        "state": "complete", "metadata": {"observation_mode": "channel-hop"}}))
    interrupted = source / "hop-sessions/hop-lower-edge-20260807T201908Z-b01/00-ch1-lower-edge"
    interrupted.mkdir(parents=True)
    (interrupted / "manifest.json").write_text(json.dumps({
        "state": "interrupted", "metadata": {"observation_mode": "channel-hop"}}))
    result = enqueue_export_backfill(source, shared, pipeline_id="kalman-v1")

    name = "hop-lower-edge-20260807T201908Z-b00-03-ch4-lower-edge"
    assert result["queued"] == [name]
    assert result["errors"] == []
    job = next((source / "staging/analysis-queue").glob("*.job"))
    assert job.read_text().rstrip().split("\t") == [name, str(segment), "hop"]


def test_quarantined_prefixes_are_queued_but_empty_ones_are_not(tmp_path):
    """Interrupted captures hold a checksummed prefix, not damage.

    All 30 quarantined field recordings verified chunk-for-chunk; they were
    unreachable only because the pipeline required state == complete. Six wrote
    no chunks at all and carry nothing to analyse.
    """
    source = tmp_path / "source"; shared = tmp_path / "shared"
    (source / "captures").mkdir(parents=True)
    partial = source / "quarantine/ch4-lower-edge-narrow-20260805T154051Z"
    partial.mkdir(parents=True)
    (partial / "manifest.json").write_text(json.dumps({
        "state": "interrupted", "metadata": {"observation_mode": "narrow"},
        "chunks": [{"path": "chunk-000000.ci16", "sample_count": 12_500_000}]}))
    empty = source / "quarantine/ch4-lower-edge-20260805T035929Z"
    empty.mkdir(parents=True)
    (empty / "manifest.json").write_text(json.dumps({
        "state": "interrupted", "metadata": {"observation_mode": "narrow"},
        "chunks": []}))

    result = enqueue_export_backfill(source, shared, pipeline_id="kalman-v1")

    assert result["queued"] == [partial.name]
    assert empty.name in result["skipped"]
    assert result["errors"] == []


def test_analysis_backfill_accepts_the_capture_side_hop_mode_name(tmp_path):
    create_context_bundle(tmp_path / "context")
    captures = tmp_path / "captures"; captures.mkdir()
    capture = captures / "hop-lower-edge-20260807T201908Z-b00-03-ch4-lower-edge"
    capture.mkdir()
    (capture / "manifest.json").write_text(json.dumps({
        "state": "complete", "metadata": {"observation_mode": "channel-hop"}}))

    result = enqueue_analysis_backfill(tmp_path, pipeline_id="kalman-v1")

    assert result["errors"] == []
    assert result["queued"] == [capture.name]
    job = next((tmp_path / "staging/analysis-queue").glob("*.job"))
    assert job.read_text().split("\t")[2] == "hop"


def test_audit_requeues_false_done_but_accepts_valid_negative(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    done = queue / "done"; done.mkdir(parents=True)
    reports = tmp_path / "reports"; (reports / "followups").mkdir(parents=True)
    (done / "bad.job").write_text("bad\tcaptures/bad\tnarrow\n")
    (done / "good.job").write_text("good\tcaptures/good\toversample\n")
    (reports / "good.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-analysis/v1"}))
    (reports / "followups/good.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-followup/v1",
                    "confirmation": {"confirmed": False}}))

    result = audit_completed(tmp_path)

    assert result["accepted"] == ["good"]
    assert result["requeued"] == ["bad.job"]
    assert (queue / "bad.job").is_file()
    receipt = json.loads((reports / "receipts/good.json").read_text())
    assert receipt["status"] == "success"
