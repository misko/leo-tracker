import json
import fcntl
import os
from pathlib import Path
import threading
import time

import pytest

import leo_tracker.radio.cli as cli_module
from leo_tracker.radio.cli import main
from leo_tracker.radio.beacon.evidence_archive import archive_evidence
from leo_tracker.radio.beacon.storage_regime import (
    CONFIRMATION, PLAN_SCHEMA, PRIMARY_LEASE_SCHEMA,
    apply_storage_regime_plan, build_storage_regime_plan,
    storage_primary_lease_is_fresh,
)
from leo_tracker.radio.beacon.qnap_lifecycle import QNAP_STORAGE_MUTATION_LOCK
import leo_tracker.radio.beacon.storage_regime as storage_regime_module

from test_evidence_archive import _capture, _reports


def test_primary_storage_lease_requires_current_running_schema(tmp_path):
    lease = tmp_path / "primary.json"
    lease.write_text(json.dumps({"schema": PRIMARY_LEASE_SCHEMA,
        "state": "running", "updated_epoch_s": 100}))
    assert storage_primary_lease_is_fresh(
        lease, now=150, maximum_age_s=60)
    assert not storage_primary_lease_is_fresh(
        lease, now=161, maximum_age_s=60)
    lease.write_text(json.dumps({"schema": PRIMARY_LEASE_SCHEMA,
        "state": "stopped", "updated_epoch_s": 150}))
    assert not storage_primary_lease_is_fresh(
        lease, now=150, maximum_age_s=60)
    lease.write_text("not json")
    assert not storage_primary_lease_is_fresh(
        lease, now=150, maximum_age_s=60)
    with pytest.raises(ValueError, match="positive"):
        storage_primary_lease_is_fresh(lease, maximum_age_s=0)


def _ready(tmp_path: Path, name: str = "migration-confirmed") -> tuple[Path, Path, Path]:
    shared = tmp_path / "shared"; capture, _ = _capture(shared, name)
    reports = _reports(shared, name)
    (reports / "plots").mkdir(exist_ok=True)
    (reports / "plots" / f"{name}.png").write_bytes(b"plot")
    (reports / "receipts").mkdir()
    (reports / "receipts" / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.analysis-receipt/v1", "status": "success", "job": name,
    }) + "\n")
    rows = [
        {"exact_candidate_count": 1, "single_receiver_candidate_count": 0,
         "followup_trigger_count": 1, "doppler_track_qualified": False},
        {"followup": str(reports / "followups" / f"{name}.json"),
         "confirmed": True, "trigger_count": 1},
        {"track": str(reports / "tracks" / f"{name}.json"), "track_count": 1,
         "longest_dual_valid_duration_s": 1, "dual_valid_observation_count": 12},
        {"association": str(reports / "associations" / f"{name}.json"),
         "qualified_association_count": 0},
    ]
    (reports / f"{name}.worker.log").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")
    archive = tmp_path / "archive"
    archive_evidence(capture, reports, archive)
    old = 1_700_000_000
    os.utime(capture / "manifest.json", (old, old))
    return shared, archive, capture


def test_storage_regime_migrates_exact_v2_then_removes_raw_v1_and_duplicates(tmp_path):
    shared, archive, capture = _ready(tmp_path)
    name = capture.name
    live = shared / "reports" / f"{name}.json"
    duplicate = (shared / "reports" / "runs" / "kalman-full-v1" / name /
                 "outputs" / "analysis.json")
    duplicate.parent.mkdir(parents=True); duplicate.write_bytes(live.read_bytes())
    import hashlib
    completion = duplicate.parent.parent / "completion.json"
    completion.write_text(json.dumps({"outputs": {"analysis": {
        "path": str(live), "bytes": live.stat().st_size,
        "sha256": hashlib.sha256(live.read_bytes()).hexdigest(),
    }}, "versioned_outputs": {"analysis": {"path": str(duplicate)}}}) + "\n")
    legacy_partial = archive / "evidence" / f"{name}.partial"
    legacy_partial.mkdir()
    (legacy_partial / "interrupted.ci16.next").write_bytes(b"obsolete")
    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)

    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION, limit=1)

    assert result["completed_count"] == 1
    assert result["failure_count"] == 0
    assert result["raw_removed_bytes"] > 0
    assert not capture.exists()
    assert not (archive / "evidence" / name).exists()
    assert not legacy_partial.exists()
    assert not (archive / "catalog" / "receipts" / f"{name}.json").exists()
    assert not (archive / "derived" / "plots" / f"{name}.png").exists()
    assert not duplicate.exists()
    versioned = json.loads(completion.read_text())["versioned_outputs"]["analysis"]
    assert versioned["path"] == str(live)
    assert versioned["storage"] == "authoritative-reference"
    assert (archive / "evidence-v2" / name / "manifest.json").is_file()
    assert (archive / "catalog" / "v2" / "receipts" / f"{name}.json").is_file()
    v2_receipt = json.loads((archive / "catalog" / "v2" / "receipts" /
                             f"{name}.json").read_text())
    assert v2_receipt["summary"]["source_bytes"] > 0
    assert 0 < v2_receipt["summary"]["storage_fraction"] <= 1
    receipt = json.loads((shared / "reports" / "reclamation" /
                          "storage-regime-v2" / f"{name}.json").read_text())
    assert receipt["status"] == "complete"
    assert receipt["raw_absent_verified"] is True
    assert receipt["v1_retired"] is True


def test_storage_regime_promotes_missing_derived_and_discards_obsolete_copy(tmp_path):
    shared, archive, capture = _ready(tmp_path, "derived-normalization")
    name = capture.name
    live_plot = shared / "reports" / "plots" / f"{name}.png"
    archived_plot = archive / "derived" / "plots" / f"{name}.png"
    assert archived_plot.read_bytes() == b"plot"
    live_plot.unlink()

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)

    assert result["completed_count"] == 1
    assert live_plot.read_bytes() == b"plot"
    assert not archived_plot.exists()
    receipt = json.loads((shared / "reports" / "reclamation" /
                          "storage-regime-v2" / f"{name}.json").read_text())
    assert str(live_plot) in receipt["restored_v1_derived_artifacts"]
    assert receipt["promoted_derived_artifacts"] == []


def test_storage_regime_keeps_current_live_derived_over_obsolete_archive(tmp_path):
    shared, archive, capture = _ready(tmp_path, "derived-newer-live")
    name = capture.name
    live_plot = shared / "reports" / "plots" / f"{name}.png"
    archived_plot = archive / "derived" / "plots" / f"{name}.png"
    live_plot.write_bytes(b"newer authoritative plot")

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)

    assert result["completed_count"] == 1
    assert live_plot.read_bytes() == b"newer authoritative plot"
    assert not archived_plot.exists()
    receipt = json.loads((shared / "reports" / "reclamation" /
                          "storage-regime-v2" / f"{name}.json").read_text())
    assert "derived/plots/derived-newer-live.png" in receipt[
        "discarded_obsolete_derived_artifacts"]


def test_storage_regime_is_dry_run_by_default_and_requires_token(tmp_path):
    shared, archive, capture = _ready(tmp_path)
    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["summary"]["eligible_count"] == 1
    with pytest.raises(ValueError, match="confirmation token"):
        apply_storage_regime_plan(plan, confirmation="")
    assert capture.exists()


def test_storage_regime_accepts_authoritative_pipeline_completion(tmp_path):
    shared, archive, capture = _ready(tmp_path, "completion-only")
    name = capture.name
    top_level = shared / "reports" / "receipts" / f"{name}.json"
    receipt = json.loads(top_level.read_text())
    completion = (shared / "reports" / "runs" / "kalman-v1" / name /
                  "completion.json")
    completion.parent.mkdir(parents=True)
    completion.write_text(json.dumps({
        **receipt, "pipeline_id": "kalman-v1",
        "outputs": {"analysis": {"path": str(shared / "reports" / f"{name}.json"),
                                   "bytes": 1, "sha256": "recorded"}},
    }))
    top_level.unlink()

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)

    entry = next(item for item in plan["entries"] if item["recording_id"] == name)
    assert entry["status"] == "eligible"


def test_storage_regime_migrates_durable_interrupted_prefix(tmp_path):
    shared, archive, capture = _ready(tmp_path, "interrupted-prefix")
    manifest_path = capture / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["state"] = "interrupted"
    manifest["captured_samples_per_receiver"] += 500
    manifest_path.write_text(json.dumps(manifest))

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    entry = next(item for item in plan["entries"]
                 if item["recording_id"] == capture.name)
    assert entry["status"] == "eligible"

    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)

    assert result["completed_count"] == 1
    assert not capture.exists()
    assert (archive / "evidence-v2" / "interrupted-prefix" / "manifest.json").is_file()


def test_storage_regime_preserves_manual_pin(tmp_path):
    shared, archive, capture = _ready(tmp_path)
    pins = shared / "reports" / "retention" / "pins"; pins.mkdir(parents=True)
    (pins / f"{capture.name}.json").write_text('{"reason":"operator"}\n')

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)

    assert plan["entries"][0]["status"] == "eligible_pinned_archive"
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)
    assert result["completed_count"] == 1
    assert result["raw_removed_bytes"] == 0
    assert capture.exists()
    assert not (archive / "evidence" / capture.name).exists()
    assert (archive / "evidence-v2" / capture.name).exists()

    converged = build_storage_regime_plan(
        shared, archive, minimum_age_hours=0, scope="raw")
    assert converged["summary"]["eligible_count"] == 0
    assert converged["entries"][0]["status"] == "protected_pinned_current"


def test_storage_regime_failure_never_removes_raw_or_v1(tmp_path, monkeypatch):
    shared, archive, capture = _ready(tmp_path)
    v1 = archive / "evidence" / capture.name

    def fail(*args, **kwargs):
        raise OSError("simulated extraction failure")

    monkeypatch.setattr(storage_regime_module, "archive_evidence_v2", fail)
    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)

    assert result["completed_count"] == 0
    assert result["failure_count"] == 1
    assert capture.exists()
    assert v1.exists()


def test_storage_regime_recrops_archive_only_v1_and_retires_old_shape(tmp_path):
    shared, archive, capture = _ready(tmp_path, "archive-only-confirmed")
    name = capture.name
    # Simulate raw which the older tier-0 lifecycle already reclaimed.
    import shutil
    shutil.rmtree(capture)

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    entry = next(item for item in plan["entries"] if item["recording_id"] == name)
    assert entry["status"] == "eligible_archive_only"
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION, limit=1)

    assert result["completed_count"] == 1
    assert result["failure_count"] == 0
    assert result["raw_removed_bytes"] == 0
    assert result["source_bytes_migrated"] > 0
    assert not (archive / "evidence" / name).exists()
    assert (archive / "evidence-v2" / name / "manifest.json").is_file()
    receipt = json.loads((archive / "catalog" / "v2" / "receipts" /
                          f"{name}.json").read_text())
    assert receipt["source_verified"] is True
    assert receipt["source_verification"] == "transitive-v1-byte-copy"


def test_archive_only_v1_restores_verified_reports_before_v2_recrop(tmp_path):
    import shutil

    shared, archive, capture = _ready(tmp_path, "archive-only-restored-reports")
    name = capture.name
    v1_receipt = json.loads((archive / "catalog" / "receipts" /
                             f"{name}.json").read_text())
    shutil.rmtree(capture)
    (shared / "reports" / f"{name}.worker.log").unlink()
    (shared / "reports" / "receipts" / f"{name}.json").unlink()
    for artifact in v1_receipt["derived_artifacts"]:
        relative = Path(artifact["path"])
        live = shared / "reports" / Path(*relative.parts[1:])
        live.unlink()

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    entry = next(item for item in plan["entries"] if item["recording_id"] == name)

    assert entry["status"] == "eligible_archive_only"
    assert entry["tier_name"] == "confirmed_beacon"
    assert "verified_v1_derived_fallback" in entry["classification_reasons"]
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)

    assert result["completed_count"] == 1
    assert result["failure_count"] == 0
    assert (shared / "reports" / f"{name}.json").is_file()
    assert (shared / "reports" / "followups" / f"{name}.json").is_file()
    assert not (archive / "evidence" / name).exists()
    v2 = json.loads((archive / "catalog" / "v2" / "receipts" /
                     f"{name}.json").read_text())
    assert v2["evidence_tier_name"] == "confirmed_beacon"
    migration = json.loads((shared / "reports" / "reclamation" /
                            "storage-regime-v2" / f"{name}.json").read_text())
    assert migration["restored_v1_derived_artifacts"]


def test_archive_only_v1_with_invalid_derived_hash_stays_protected(tmp_path):
    import shutil

    shared, archive, capture = _ready(tmp_path, "archive-only-invalid-report")
    name = capture.name
    shutil.rmtree(capture)
    (shared / "reports" / f"{name}.worker.log").unlink()
    (shared / "reports" / "receipts" / f"{name}.json").unlink()
    (shared / "reports" / f"{name}.json").unlink()
    (archive / "derived" / f"{name}.json").write_text("{}\n")

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    entry = next(item for item in plan["entries"] if item["recording_id"] == name)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)

    assert entry["status"] == "analysis_incomplete"
    assert entry["tier"] is None
    assert result["completed_count"] == 0
    assert (archive / "evidence" / name).is_dir()
    assert (archive / "catalog" / "receipts" / f"{name}.json").is_file()
    assert not (archive / "evidence-v2" / name).exists()


def test_verified_v2_allows_raw_retirement_when_legacy_success_receipt_is_missing(
        tmp_path):
    shared, archive, capture = _ready(tmp_path, "v2-is-authority")
    name = capture.name
    from leo_tracker.radio.beacon.evidence_archive import archive_evidence_v2
    archive_evidence_v2(capture, shared / "reports", archive)
    (shared / "reports/receipts" / f"{name}.json").unlink()
    for completion in (shared / "reports/runs").glob(f"*/{name}/completion.json"):
        completion.unlink()

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    entry = next(item for item in plan["entries"] if item["recording_id"] == name)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)

    assert entry["status"] == "eligible"
    assert result["failure_count"] == 0
    assert not capture.exists()


def test_storage_regime_prioritizes_raw_before_archive_only(tmp_path):
    import shutil

    shared, archive, raw_capture = _ready(tmp_path, "z-raw-confirmed")
    other_shared, other_archive, other_capture = _ready(
        tmp_path / "other", "a-archive-only-confirmed")
    archive_name = other_capture.name
    shutil.copytree(other_shared / "reports", shared / "reports", dirs_exist_ok=True)
    shutil.copytree(other_archive / "evidence" / archive_name,
                    archive / "evidence" / archive_name)
    shutil.copy2(other_archive / "catalog" / "receipts" / f"{archive_name}.json",
                 archive / "catalog" / "receipts" / f"{archive_name}.json")

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    eligible = [item for item in plan["entries"]
                if item["status"].startswith("eligible")]

    assert eligible[0]["recording_id"] == raw_capture.name
    assert eligible[0]["status"] == "eligible"
    assert eligible[1]["recording_id"] == archive_name
    assert eligible[1]["status"] == "eligible_archive_only"


def test_storage_regime_auto_scans_raw_then_falls_back_to_archive(tmp_path):
    import shutil

    shared, archive, capture = _ready(tmp_path, "raw-first")
    raw_plan = build_storage_regime_plan(
        shared, archive, minimum_age_hours=0, scope="auto")
    assert raw_plan["configuration"]["active_scope"] == "raw"
    assert {item["recording_id"] for item in raw_plan["entries"]} == {capture.name}

    shutil.rmtree(capture)
    archive_plan = build_storage_regime_plan(
        shared, archive, minimum_age_hours=0, scope="auto")
    assert archive_plan["configuration"]["active_scope"] == "archive"
    assert archive_plan["entries"][0]["status"] == "eligible_archive_only"


def test_bounded_auto_reserves_first_apply_slot_for_archive_only(tmp_path):
    import shutil

    shared, archive, raw_capture = _ready(tmp_path, "z-raw")
    other_shared, other_archive, other_capture = _ready(
        tmp_path / "other", "a-archive-only")
    name = other_capture.name
    shutil.copytree(other_shared / "reports", shared / "reports", dirs_exist_ok=True)
    shutil.copytree(other_archive / "evidence" / name,
                    archive / "evidence" / name)
    shutil.copy2(other_archive / "catalog" / "receipts" / f"{name}.json",
                 archive / "catalog" / "receipts" / f"{name}.json")

    plan = build_storage_regime_plan(
        shared, archive, minimum_age_hours=0, scope="auto", eligible_limit=4)
    eligible = [item for item in plan["entries"]
                if item["status"].startswith("eligible")]

    assert plan["configuration"]["active_scope"] == "raw_with_archive_fairness"
    assert plan["configuration"]["archive_reserved_slots"] == 1
    assert eligible[0]["recording_id"] == name
    assert eligible[0]["status"] == "eligible_archive_only"
    assert eligible[1]["recording_id"] == raw_capture.name


def test_bounded_auto_reserves_configured_archive_slots(tmp_path):
    import shutil

    shared, archive, _ = _ready(tmp_path, "z-raw")
    for index in range(3):
        other_shared, other_archive, capture = _ready(
            tmp_path / f"other-{index}", f"a-archive-{index}")
        shutil.copytree(other_shared / "reports", shared / "reports",
                        dirs_exist_ok=True)
        shutil.copytree(other_archive / "evidence" / capture.name,
                        archive / "evidence" / capture.name)
        shutil.copy2(other_archive / "catalog/receipts" / f"{capture.name}.json",
                     archive / "catalog/receipts" / f"{capture.name}.json")

    plan = build_storage_regime_plan(
        shared, archive, minimum_age_hours=0, scope="auto",
        eligible_limit=6, auto_archive_slots=3)
    eligible = [item for item in plan["entries"]
                if item["status"].startswith("eligible")]

    assert plan["configuration"]["archive_reserved_slots"] == 3
    assert [item["status"] for item in eligible[:3]] == [
        "eligible_archive_only"] * 3


def test_auto_archive_slots_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="archive slots"):
        build_storage_regime_plan(
            tmp_path / "shared", tmp_path / "archive",
            scope="auto", eligible_limit=4, auto_archive_slots=0)


def test_storage_regime_bounded_inventory_stops_after_eligible_limit(tmp_path):
    first_shared, first_archive, first = _ready(tmp_path / "first", "a-first")
    second_shared, second_archive, second = _ready(tmp_path / "second", "b-second")
    import shutil
    shutil.copytree(second, first_shared / "captures" / second.name)
    shutil.copytree(second_shared / "reports", first_shared / "reports",
                    dirs_exist_ok=True)

    plan = build_storage_regime_plan(
        first_shared, first_archive, minimum_age_hours=0, scope="raw",
        eligible_limit=1)

    assert plan["summary"]["eligible_count"] == 1
    assert plan["configuration"]["inventory_complete"] is False
    assert plan["entries"][0]["recording_id"] == first.name
    with pytest.raises(ValueError, match="requires auto, raw, or archive"):
        build_storage_regime_plan(first_shared, first_archive,
                                  minimum_age_hours=0, scope="all",
                                  eligible_limit=1)


def test_storage_regime_runs_bounded_independent_transactions_concurrently(
        tmp_path, monkeypatch):
    shared = tmp_path / "shared"; archive = tmp_path / "archive"
    plan = {"schema": PLAN_SCHEMA, "shared_root": str(shared),
            "archive_root": str(archive), "entries": [
                {"recording_id": "one", "status": "eligible", "source_bytes": 10},
                {"recording_id": "two", "status": "eligible", "source_bytes": 20},
            ]}
    state = {"active": 0, "maximum": 0}; guard = threading.Lock()

    def fake_migrate(item, _shared, _archive):
        with guard:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(.05)
        with guard:
            state["active"] -= 1
        return {"recording_id": item["recording_id"],
                "source_bytes": item["source_bytes"], "removed_bytes": 0,
                "archive_only": False, "raw_pinned": False}

    monkeypatch.setattr(storage_regime_module, "_migrate_storage_item", fake_migrate)
    result = apply_storage_regime_plan(
        plan, confirmation=CONFIRMATION, limit=2, workers=2)

    assert result["completed_count"] == 2
    assert result["raw_removed_bytes"] == 30
    assert state["maximum"] == 2
    with pytest.raises(ValueError, match="workers must be positive"):
        apply_storage_regime_plan(plan, confirmation=CONFIRMATION, workers=0)


def test_storage_regime_shares_mutation_lock_with_qnap_lifecycle(tmp_path):
    shared = tmp_path / "shared"; archive = tmp_path / "archive"
    plan = {"schema": PLAN_SCHEMA, "shared_root": str(shared),
            "archive_root": str(archive), "entries": []}
    lock_path = (shared / "reports" / "reclamation" /
                 QNAP_STORAGE_MUTATION_LOCK)
    lock_path.parent.mkdir(parents=True)

    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another QNAP storage mutation"):
            apply_storage_regime_plan(plan, confirmation=CONFIRMATION)


def test_storage_regime_cli_apply_locks_before_inventory(
        tmp_path, monkeypatch, capsys):
    shared = tmp_path / "shared"; archive = tmp_path / "archive"
    lock_path = (shared / "reports" / "reclamation" /
                 QNAP_STORAGE_MUTATION_LOCK)
    lock_path.parent.mkdir(parents=True)
    called = False

    def fail_if_inventory_runs(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("inventory ran while the mutation lock was held")

    monkeypatch.setattr(cli_module, "build_storage_regime_plan",
                        fail_if_inventory_runs)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(["starlink-storage-regime-v2", str(shared), str(archive),
                     "--apply", "--confirm", CONFIRMATION]) == 1

    assert called is False
    assert "another QNAP storage mutation is active" in capsys.readouterr().err


def test_archive_only_gap_fails_without_retiring_v1(tmp_path):
    shared, archive, capture = _ready(tmp_path, "archive-only-gap")
    name = capture.name
    import shutil
    shutil.rmtree(capture)
    v1_manifest = archive / "evidence" / name / "manifest.json"
    value = json.loads(v1_manifest.read_text())
    # Preserve a valid bundle but remove coverage needed by the future plan.
    removed = value["clips"].pop()
    (archive / "evidence" / name / removed["path"]).unlink()
    v1_manifest.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    v1_receipt = archive / "catalog" / "receipts" / f"{name}.json"
    old = json.loads(v1_receipt.read_text())
    import hashlib
    old["bundle_manifest_sha256"] = hashlib.sha256(v1_manifest.read_bytes()).hexdigest()
    v1_receipt.write_text(json.dumps(old) + "\n")

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION, limit=1)

    assert result["completed_count"] == 0
    assert result["failure_count"] == 1
    assert (archive / "evidence" / name).exists()
