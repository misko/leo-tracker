import json
import os
from pathlib import Path

import pytest

from leo_tracker.radio.beacon.evidence_archive import archive_evidence
from leo_tracker.radio.beacon.storage_regime import (
    CONFIRMATION, PLAN_SCHEMA, apply_storage_regime_plan,
    build_storage_regime_plan,
)
import leo_tracker.radio.beacon.storage_regime as storage_regime_module

from test_evidence_archive import _capture, _reports


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
    assert "derived/plots/derived-normalization.png" in receipt[
        "promoted_derived_artifacts"]


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
