import fcntl
import hashlib
import json
from pathlib import Path
import shutil

import pytest

import leo_tracker.radio.cli as cli_module
from leo_tracker.radio.beacon.qnap_lifecycle import (
    CONFIRMATION, PLAN_SCHEMA, QNAP_STORAGE_MUTATION_LOCK,
    apply_qnap_lifecycle_plan,
    build_qnap_lifecycle_plan)
from leo_tracker.radio.cli import main


def _atomic_fixture(shared: Path, archive: Path, name: str, tier: int):
    capture = shared / "captures" / name; capture.mkdir(parents=True)
    payload = (name + "-iq").encode()
    (capture / "chunk.ci16").write_bytes(payload)
    manifest = {"state": "complete", "chunks": [{"path": "chunk.ci16",
        "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]}
    manifest_path = capture / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    reports = shared / "reports"; reports.mkdir(exist_ok=True)
    summary = {"exact_candidate_count": 0, "single_receiver_candidate_count": 0,
               "followup_trigger_count": 0, "doppler_track_qualified": False}
    if tier == 1: summary["exact_candidate_count"] = 1
    if tier == 2: summary["doppler_track_qualified"] = True
    (reports / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1", "summary": summary}))
    followups = reports / "followups"; followups.mkdir(exist_ok=True)
    (followups / f"{name}.json").write_text(json.dumps({"trigger_count": 1 if tier >= 1 else 0,
        "schema": "leo-tracker.starlink-beacon-followup/v1",
        "confirmation": {"confirmed": tier == 3}}))
    tracks = reports / "tracks"; tracks.mkdir(exist_ok=True)
    (tracks / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1", "summary": {
        "longest_dual_valid_duration_s": 10 if tier == 2 else 0,
        "dual_valid_observation_count": 30 if tier == 2 else 0}}))
    associations = reports / "associations"; associations.mkdir(exist_ok=True)
    (associations / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-tle-association/v2", "summary": {
        "qualified_association_count": 1 if tier == 4 else 0}}))
    receipts = reports / "receipts"; receipts.mkdir(exist_ok=True)
    (receipts / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.analysis-receipt/v1", "status": "success", "job": name}))
    worker_rows = [summary,
        {"followup": str(followups / f"{name}.json"),
         "confirmed": tier == 3, "trigger_count": 1 if tier >= 1 else 0},
        {"track": str(tracks / f"{name}.json"), "track_count": 1 if tier == 2 else 0,
         "longest_dual_valid_duration_s": 10 if tier == 2 else 0,
         "dual_valid_observation_count": 30 if tier == 2 else 0},
        {"association": str(associations / f"{name}.json"),
         "qualified_association_count": 1 if tier == 4 else 0}]
    (reports / f"{name}.worker.log").write_text(
        "\n".join(json.dumps(row) for row in worker_rows) + "\n")
    if tier == 5:
        pins = reports / "retention" / "pins"; pins.mkdir(parents=True)
        (pins / f"{name}.json").write_text('{"reason":"test"}')
    bundle = archive / "evidence-v2" / name; bundle.mkdir(parents=True)
    bundle_manifest = bundle / "manifest.json"; bundle_manifest.write_text("{}")
    archive_receipts = archive / "catalog" / "v2" / "receipts"
    archive_receipts.mkdir(parents=True, exist_ok=True)
    (archive_receipts / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.evidence-archive-receipt/v2", "status": "verified",
        "source_verified": True, "recording_id": name,
        "required_event_replay_valid": True, "policy": "tiered-v2",
        "source_manifest_sha256": manifest_sha,
        "bundle": f"evidence-v2/{name}",
        "bundle_manifest_sha256": hashlib.sha256(bundle_manifest.read_bytes()).hexdigest(),
        "summary": {"storage_fraction": .25}}))
    return capture


def test_plan_orders_no_signal_first_and_protects_identities_and_pins(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    for tier in range(6):
        _atomic_fixture(shared, archive, f"capture-{tier}", tier)

    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0,
                                     maximum_tier=3)
    assert plan["schema"] == PLAN_SCHEMA
    assert [item["tier"] for item in plan["entries"]] == list(range(6))
    assert [item["tier_name"] for item in plan["entries"]] == [
        "strict_negative", "weak_candidate", "tracked_signal",
        "confirmed_beacon", "qualified_identity", "manual_pin"]
    assert [item["status"] for item in plan["entries"]] == [
        "eligible", "eligible", "eligible", "eligible",
        "protected_by_tier", "protected_by_tier"]


def test_default_policy_only_makes_strict_negatives_eligible(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    for tier in range(4): _atomic_fixture(shared, archive, f"capture-{tier}", tier)
    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0)
    assert plan["summary"]["eligible_count"] == 1
    assert plan["entries"][0]["tier_name"] == "strict_negative"


def test_structured_reports_classify_when_worker_log_is_corrupt(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    for tier in range(5):
        name = f"capture-{tier}"
        _atomic_fixture(shared, archive, name, tier)
        (shared / "reports" / f"{name}.worker.log").write_bytes(b"\0" * 4096)

    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0,
                                     maximum_tier=4)

    assert [item["tier"] for item in plan["entries"]] == list(range(5))
    assert all(item["status"] == "eligible" for item in plan["entries"])


def test_stale_or_missing_evidence_archive_blocks_raw_deletion(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    capture = _atomic_fixture(shared, archive, "capture-0", 0)
    receipt = archive / "catalog/v2/receipts/capture-0.json"
    value = json.loads(receipt.read_text()); value["source_manifest_sha256"] = "wrong"
    receipt.write_text(json.dumps(value))
    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0)
    assert plan["entries"][0]["status"] == "evidence_archive_stale"
    assert capture.exists()


def test_apply_requires_token_and_pressure_then_preserves_evidence(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    capture = _atomic_fixture(shared, archive, "capture-0", 0)
    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0)
    with pytest.raises(ValueError, match="confirmation token"):
        apply_qnap_lifecycle_plan(plan, confirmation="", trigger_free_gb=1,
                                  target_free_gb=2)
    free_gb = shutil.disk_usage(shared).free / 1e9
    result = apply_qnap_lifecycle_plan(plan, confirmation=CONFIRMATION,
        trigger_free_gb=free_gb + 1, target_free_gb=free_gb + 2, limit=1)
    assert result["removed_count"] == 1
    assert not capture.exists()
    assert (archive / "evidence-v2/capture-0/manifest.json").is_file()
    assert (shared / "reports/capture-0.json").is_file()
    receipt = json.loads((shared / "reports/reclamation/qnap/capture-0.json").read_text())
    assert receipt["evidence_archive_preserved"] is True
    assert receipt["qnap_raw_absent_verified"] is True


def test_no_pressure_means_apply_removes_nothing(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    capture = _atomic_fixture(shared, archive, "capture-0", 0)
    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0)
    result = apply_qnap_lifecycle_plan(plan, confirmation=CONFIRMATION,
        trigger_free_gb=0, target_free_gb=1)
    assert result["pressure_triggered"] is False
    assert capture.exists()


def test_ignore_pressure_reclaims_verified_identity_but_never_manual_pin(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    identity = _atomic_fixture(shared, archive, "capture-4", 4)
    pin = _atomic_fixture(shared, archive, "capture-5", 5)
    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0,
                                     maximum_tier=4)
    result = apply_qnap_lifecycle_plan(plan, confirmation=CONFIRMATION,
        trigger_free_gb=0, target_free_gb=1, pressure_required=False)
    assert result["removed_count"] == 1
    assert not identity.exists()
    assert pin.exists()


def test_qnap_reclaimer_lock_blocks_concurrent_apply(tmp_path):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    _atomic_fixture(shared, archive, "capture-0", 0)
    plan = build_qnap_lifecycle_plan(shared, archive, minimum_age_hours=0)
    lock_path = shared / "reports/reclamation" / QNAP_STORAGE_MUTATION_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another QNAP storage mutation"):
            apply_qnap_lifecycle_plan(plan, confirmation=CONFIRMATION,
                trigger_free_gb=0, target_free_gb=1)


def test_qnap_cli_apply_locks_before_inventory(tmp_path, monkeypatch, capsys):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    lock_path = shared / "reports/reclamation" / QNAP_STORAGE_MUTATION_LOCK
    lock_path.parent.mkdir(parents=True)
    called = False

    def fail_if_inventory_runs(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("inventory ran while the mutation lock was held")

    monkeypatch.setattr(cli_module, "build_qnap_lifecycle_plan",
                        fail_if_inventory_runs)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(["starlink-qnap-lifecycle", str(shared), str(archive),
                     "--apply", "--confirm", CONFIRMATION]) == 1

    assert called is False
    assert "another QNAP storage mutation is active" in capsys.readouterr().err


def test_cli_is_dry_run_by_default(tmp_path, capsys):
    shared, archive = tmp_path / "qnap", tmp_path / "cropped"
    capture = _atomic_fixture(shared, archive, "capture-0", 0)
    output = tmp_path / "plan.json"
    assert main(["starlink-qnap-lifecycle", str(shared), str(archive),
        "--minimum-age-hours", "0", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert json.loads(output.read_text())["enabled"] is False
    assert capture.exists()
