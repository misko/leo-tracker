import fcntl
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from leo_tracker.radio.beacon.local_reclamation import (
    PLAN_SCHEMA, RECEIPT_SCHEMA, apply_reclamation_plan,
    build_reclamation_plan)
from leo_tracker.radio.cli import main


def _write_recording(local: Path, shared: Path, name: str = "capture-001",
                     *, state: str = "complete", payload: bytes = b"radio-data",
                     store: str = "captures"):
    source = local / store / name
    destination = shared / "captures" / name
    source.mkdir(parents=True); destination.mkdir(parents=True)
    chunk = {"path": "chunk-000000.ci16", "bytes": len(payload),
             "sha256": hashlib.sha256(payload).hexdigest()}
    manifest = {"state": state, "chunks": [chunk]}
    for root in (source, destination):
        (root / chunk["path"]).write_bytes(payload)
        (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    analysis = shared / "reports" / f"{name}.json"
    analysis.parent.mkdir(parents=True, exist_ok=True); analysis.write_text("{}")
    receipts = shared / "reports" / "receipts"; receipts.mkdir(exist_ok=True)
    (receipts / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.analysis-receipt/v1", "status": "success",
        "job": name, "pipeline_id": "kalman-full-v1",
        "completed_utc": "2026-08-08T00:00:00Z",
        "outputs": {"analysis": {"path": str(analysis), "bytes": 2}}}))
    return source, destination


def _write_v2(archive: Path, source: Path, name: str) -> Path:
    bundle = archive / "evidence-v2" / name
    bundle.mkdir(parents=True)
    bundle_manifest = bundle / "manifest.json"
    bundle_manifest.write_text("{}\n")
    receipt = archive / "catalog/v2/receipts" / f"{name}.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "schema": "leo-tracker.evidence-archive-receipt/v2",
        "status": "verified", "source_verified": True,
        "required_event_replay_valid": True, "policy": "tiered-v2",
        "source_manifest_sha256": hashlib.sha256(
            (source / "manifest.json").read_bytes()).hexdigest(),
        "bundle": f"evidence-v2/{name}",
        "bundle_manifest_sha256": hashlib.sha256(
            bundle_manifest.read_bytes()).hexdigest(),
    }))
    return receipt


def test_plan_and_apply_remove_only_local_verified_duplicate(tmp_path):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    source, destination = _write_recording(local, shared)

    plan = build_reclamation_plan(local, shared, minimum_age_s=0,
                                  pipeline_id="kalman-full-v1")
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["summary"] == {"recording_count": 1,
        "status_counts": {"eligible": 1}, "eligible_count": 1,
        "eligible_bytes": len(b"radio-data")}

    result = apply_reclamation_plan(plan)
    assert result["removed_count"] == 1
    assert not source.exists()
    assert (destination / "chunk-000000.ci16").read_bytes() == b"radio-data"
    receipt = json.loads((shared / "reports/reclamation/local/capture-001.json").read_text())
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert receipt["status"] == "removed"
    assert receipt["local_absent_verified"] is True


def test_reclamation_is_idempotent_from_completed_receipt(tmp_path):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    _write_recording(local, shared)
    plan = build_reclamation_plan(local, shared, minimum_age_s=0)
    apply_reclamation_plan(plan)

    repeated = apply_reclamation_plan(plan)
    assert repeated["removed_count"] == 1
    assert repeated["removed_bytes"] == len(b"radio-data")


def test_verified_v2_reclaims_local_raw_after_qnap_raw_is_already_gone(tmp_path):
    local, shared, archive = (tmp_path / "nvme", tmp_path / "qnap",
                              tmp_path / "archive")
    source, destination = _write_recording(local, shared)
    _write_v2(archive, source, source.name)
    shutil.rmtree(destination)

    plan = build_reclamation_plan(
        local, shared, archive_root=archive, minimum_age_s=0)
    assert plan["entries"][0]["status"] == "eligible"
    assert plan["entries"][0]["durable_copy"] == "evidence_v2"
    result = apply_reclamation_plan(plan)
    assert result["removed_count"] == 1
    assert not source.exists()
    receipt = json.loads((shared / "reports/reclamation/local/capture-001.json").read_text())
    assert receipt["durable_copy"] == "evidence_v2"


def test_verified_v2_is_sufficient_without_separate_analysis_receipt(tmp_path):
    local, shared, archive = (tmp_path / "nvme", tmp_path / "qnap",
                              tmp_path / "archive")
    source, destination = _write_recording(local, shared)
    _write_v2(archive, source, source.name)
    shutil.rmtree(destination)
    (shared / "reports/receipts/capture-001.json").unlink()

    plan = build_reclamation_plan(
        local, shared, archive_root=archive, minimum_age_s=0)
    assert plan["entries"][0]["status"] == "eligible"
    assert apply_reclamation_plan(plan)["removed_count"] == 1
    assert not source.exists()


def test_verified_v2_wins_over_shared_raw_when_analysis_receipt_is_missing(tmp_path):
    local, shared, archive = (tmp_path / "nvme", tmp_path / "qnap",
                              tmp_path / "archive")
    source, destination = _write_recording(local, shared)
    _write_v2(archive, source, source.name)
    (shared / "reports/receipts/capture-001.json").unlink()

    plan = build_reclamation_plan(
        local, shared, archive_root=archive, minimum_age_s=0)

    assert plan["entries"][0]["status"] == "eligible"
    assert plan["entries"][0]["durable_copy"] == "evidence_v2"
    assert apply_reclamation_plan(plan)["removed_count"] == 1
    assert not source.exists()
    assert destination.exists()


def test_verified_v2_reclaims_quarantined_interrupted_prefix(tmp_path):
    local, shared, archive = (tmp_path / "nvme", tmp_path / "qnap",
                              tmp_path / "archive")
    source, destination = _write_recording(
        local, shared, state="interrupted", store="quarantine")
    _write_v2(archive, source, source.name)
    shutil.rmtree(destination)

    plan = build_reclamation_plan(
        local, shared, archive_root=archive, minimum_age_s=0)
    assert plan["entries"][0]["kind"] == "quarantine"
    assert plan["entries"][0]["status"] == "eligible"
    assert apply_reclamation_plan(plan)["removed_count"] == 1
    assert not source.exists()


def test_verified_v2_reclaims_legacy_local_evidence_capture(tmp_path):
    local, shared, archive = (tmp_path / "nvme", tmp_path / "qnap",
                              tmp_path / "archive")
    source, destination = _write_recording(
        local, shared, store="evidence/pilot_symbolwise_v3")
    _write_v2(archive, source, source.name)
    shutil.rmtree(destination)

    plan = build_reclamation_plan(
        local, shared, archive_root=archive, minimum_age_s=0)
    assert plan["entries"][0]["kind"] == "legacy_evidence"
    assert apply_reclamation_plan(plan)["removed_count"] == 1
    assert not source.exists()


def test_empty_interrupted_metadata_is_reclaimed_but_unmanifested_payload_is_not(
        tmp_path):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    empty = local / "captures/empty"; empty.mkdir(parents=True)
    (empty / "manifest.json").write_text(json.dumps({
        "state": "interrupted", "chunks": []}))
    unsafe = local / "quarantine/unsafe"; unsafe.mkdir(parents=True)
    (unsafe / "manifest.json").write_text(json.dumps({
        "state": "interrupted", "chunks": []}))
    (unsafe / "orphan.ci16").write_bytes(b"science")

    plan = build_reclamation_plan(local, shared, minimum_age_s=0)
    by_name = {item["recording_id"]: item for item in plan["entries"]}
    assert by_name["empty"]["status"] == "eligible"
    assert by_name["empty"]["durable_copy"] == "empty_terminal"
    assert by_name["unsafe"]["status"] == "unmanifested_local_payload"
    assert apply_reclamation_plan(plan)["removed_count"] == 1
    assert not empty.exists()
    assert unsafe.exists()


def test_v2_fallback_accepts_analyzed_interrupted_prefix_but_rejects_stale_receipt(
        tmp_path):
    local, shared, archive = (tmp_path / "nvme", tmp_path / "qnap",
                              tmp_path / "archive")
    source, destination = _write_recording(local, shared, state="interrupted")
    receipt = _write_v2(archive, source, source.name)
    shutil.rmtree(destination)
    plan = build_reclamation_plan(
        local, shared, archive_root=archive, minimum_age_s=0)
    assert plan["entries"][0]["status"] == "eligible"

    value = json.loads(receipt.read_text())
    value["source_manifest_sha256"] = "0" * 64
    receipt.write_text(json.dumps(value))
    stale = build_reclamation_plan(
        local, shared, archive_root=archive, minimum_age_s=0)
    assert stale["entries"][0]["status"] == "evidence_archive_stale"
    assert source.exists()


@pytest.mark.parametrize(("mutation", "reason"), [
    ("invalid_state", "local_capture_incomplete"),
    ("missing_receipt", "analysis_incomplete"),
    ("wrong_pipeline", "analysis_pipeline_mismatch"),
    ("missing_chunk", "missing_qnap_chunk"),
    ("size", "qnap_chunk_size_mismatch"),
    ("manifest", "qnap_manifest_mismatch"),
])
def test_safety_gates_defer_unverified_recordings(tmp_path, mutation, reason):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    source, destination = _write_recording(local, shared)
    if mutation == "invalid_state":
        value = json.loads((source / "manifest.json").read_text())
        value["state"] = "capturing"
        (source / "manifest.json").write_text(json.dumps(value, sort_keys=True))
        (destination / "manifest.json").write_text(json.dumps(value, sort_keys=True))
    elif mutation == "missing_receipt":
        (shared / "reports/receipts/capture-001.json").unlink()
    elif mutation == "wrong_pipeline":
        pass
    elif mutation == "missing_chunk":
        (destination / "chunk-000000.ci16").unlink()
    elif mutation == "size":
        (destination / "chunk-000000.ci16").write_bytes(b"short")
    elif mutation == "manifest":
        value = json.loads((destination / "manifest.json").read_text())
        value["extra"] = True
        (destination / "manifest.json").write_text(json.dumps(value, sort_keys=True))
    pipeline = "different-v1" if mutation == "wrong_pipeline" else None
    plan = build_reclamation_plan(local, shared, minimum_age_s=0,
                                  pipeline_id=pipeline)
    assert plan["entries"][0]["status"] == reason
    assert source.exists()


def test_full_sha256_gate_detects_same_size_corruption(tmp_path):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    source, destination = _write_recording(local, shared)
    (destination / "chunk-000000.ci16").write_bytes(b"corrupt!!!")

    plan = build_reclamation_plan(local, shared, minimum_age_s=0,
                                  verify_sha256=True)
    assert plan["entries"][0]["status"] == "qnap_chunk_sha256_mismatch"
    assert source.exists()


def test_active_queue_marker_and_partial_transfer_block_reclamation(tmp_path):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    source, _ = _write_recording(local, shared)
    queue = local / "staging/analysis-queue"; queue.mkdir(parents=True)
    marker = queue / "capture-001.job"; marker.write_text("capture-001")
    assert build_reclamation_plan(local, shared, minimum_age_s=0)["entries"][0][
        "status"] == "active_or_partial"
    marker.unlink(); partial = shared / "staging/incoming/capture-001.partial"
    partial.mkdir(parents=True)
    assert build_reclamation_plan(local, shared, minimum_age_s=0)["entries"][0][
        "status"] == "active_or_partial"
    assert source.exists()


def test_reclaimer_lock_prevents_concurrent_apply(tmp_path):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    _write_recording(local, shared)
    plan = build_reclamation_plan(local, shared, minimum_age_s=0)
    lock_path = local / "staging/local-reclamation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another local reclaimer"):
            apply_reclamation_plan(plan)


def test_cli_defaults_to_dry_run_and_can_apply_bounded_batch(tmp_path, capsys):
    local, shared = tmp_path / "nvme", tmp_path / "qnap"
    _write_recording(local, shared, "capture-001")
    _write_recording(local, shared, "capture-002")
    output = tmp_path / "plan.json"
    base = ["starlink-storage-reconcile", str(local), str(shared),
            "--minimum-age-s", "0", "--output", str(output)]
    assert main(base) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert output.is_file()
    assert main(base + ["--apply", "--limit", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["removed_count"] == 1
    assert sum(path.is_dir() for path in (local / "captures").iterdir()) == 1
