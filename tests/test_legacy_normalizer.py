import hashlib
import json
from pathlib import Path

import pytest

from leo_tracker.radio.beacon.legacy_normalizer import (
    CONFIRMATION, apply_legacy_layout_plan, build_legacy_layout_plan,
    legacy_normalization_complete, legacy_normalization_ready)
from leo_tracker.radio.cli import main


def _roots(tmp_path: Path):
    shared = tmp_path / "shared"; archive = tmp_path / "archive"
    (shared / "reports").mkdir(parents=True); (archive / "derived").mkdir(parents=True)
    return shared, archive


def _completion(shared: Path, name: str, payload: bytes, *, live=True,
                expected=True):
    reports = shared / "reports"; canonical = reports / f"{name}.json"
    duplicate = reports / "runs/pipeline" / name / "outputs/analysis.json"
    duplicate.parent.mkdir(parents=True); duplicate.write_bytes(payload)
    if live: canonical.write_bytes(payload)
    artifact = {"path": str(canonical), "bytes": len(payload)}
    if expected: artifact["sha256"] = hashlib.sha256(payload).hexdigest()
    completion = duplicate.parent.parent / "completion.json"
    completion.write_text(json.dumps({"outputs": {"analysis": artifact},
        "versioned_outputs": {"analysis": {"path": str(duplicate)}}}))
    return canonical, duplicate, completion


def test_normalizer_removes_obsolete_derived_but_preserves_current(tmp_path):
    shared, archive = _roots(tmp_path)
    legacy = archive / "derived/report.json"; legacy.write_bytes(b"old")
    current = shared / "reports/report.json"; current.write_bytes(b"current")
    plan = build_legacy_layout_plan(shared, archive)
    result = apply_legacy_layout_plan(plan, confirmation=CONFIRMATION)
    assert result["failure_count"] == 0
    assert not legacy.exists(); assert current.read_bytes() == b"current"


def test_normalizer_promotes_missing_derived_without_data_loss(tmp_path):
    shared, archive = _roots(tmp_path)
    legacy = archive / "derived/sub/report.bin"
    legacy.parent.mkdir(); legacy.write_bytes(b"only-copy")
    plan = build_legacy_layout_plan(shared, archive)
    result = apply_legacy_layout_plan(plan, confirmation=CONFIRMATION)
    current = shared / "reports/sub/report.bin"
    assert result["promoted_bytes"] == len(b"only-copy")
    assert current.read_bytes() == b"only-copy"; assert not legacy.exists()


def test_normalizer_rewrites_reference_and_removes_versioned_copy(tmp_path):
    shared, archive = _roots(tmp_path)
    current, duplicate, completion = _completion(shared, "sample", b"same")
    plan = build_legacy_layout_plan(shared, archive)
    result = apply_legacy_layout_plan(plan, confirmation=CONFIRMATION)
    assert result["failure_count"] == 0; assert not duplicate.exists()
    value = json.loads(completion.read_text())["versioned_outputs"]["analysis"]
    assert value["path"] == str(current)
    assert value["storage"] == "authoritative-reference"


def test_normalizer_promotes_hash_verified_versioned_output(tmp_path):
    shared, archive = _roots(tmp_path)
    current, duplicate, completion = _completion(
        shared, "promote", b"authoritative", live=False)
    plan = build_legacy_layout_plan(shared, archive)
    result = apply_legacy_layout_plan(plan, confirmation=CONFIRMATION)
    assert result["failure_count"] == 0
    assert current.read_bytes() == b"authoritative"; assert not duplicate.exists()
    assert json.loads(completion.read_text())["versioned_outputs"]["analysis"][
        "storage"] == "authoritative-reference"


def test_normalizer_fails_closed_on_unhashed_conflict_and_unreferenced_file(tmp_path):
    shared, archive = _roots(tmp_path)
    current, duplicate, _ = _completion(
        shared, "conflict", b"old", expected=False)
    current.write_bytes(b"new")
    orphan = duplicate.parent / "orphan.bin"; orphan.write_bytes(b"unique")
    plan = build_legacy_layout_plan(shared, archive)
    assert plan["summary"]["eligible_count"] == 0
    assert plan["summary"]["status_counts"] == {
        "hash_conflict_without_recorded_hash": 1, "unreferenced_output": 1}
    assert duplicate.exists() and orphan.exists()


def test_normalizer_requires_token_and_cli_is_dry_run(tmp_path, capsys):
    shared, archive = _roots(tmp_path)
    (archive / "derived/item").write_bytes(b"x")
    plan = build_legacy_layout_plan(shared, archive)
    with pytest.raises(ValueError, match="confirmation token"):
        apply_legacy_layout_plan(plan, confirmation="")
    assert main(["starlink-storage-normalize-legacy", str(shared), str(archive),
                 "--planning-limit", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_normalizer_removes_empty_versioned_output_directory(tmp_path):
    shared, archive = _roots(tmp_path)
    current, duplicate, _ = _completion(shared, "empty", b"same")
    duplicate.unlink()
    plan = build_legacy_layout_plan(shared, archive)
    entry = next(x for x in plan["entries"]
                 if x["kind"] == "empty_output_directory")
    assert entry["status"] == "eligible"
    result = apply_legacy_layout_plan(plan, confirmation=CONFIRMATION)
    assert result["failure_count"] == 0
    assert not duplicate.parent.exists(); assert current.exists()


@pytest.mark.parametrize("configuration,eligible,expected", [
    ({"active_scope": "raw", "inventory_complete": True}, 0, False),
    ({"active_scope": "archive", "inventory_complete": False}, 0, False),
    ({"active_scope": "archive", "inventory_complete": True}, 1, False),
    ({"active_scope": "archive", "inventory_complete": True}, 0, True),
])
def test_orphan_handoff_requires_complete_zero_work_archive_inventory(
        configuration, eligible, expected):
    assert legacy_normalization_ready({
        "configuration": configuration,
        "summary": {"eligible_count": eligible}}) is expected


def test_normalizer_completion_requires_schema_complete_inventory_and_zero_work():
    complete = {
        "schema": "leo-tracker.legacy-layout-plan/v1",
        "configuration": {"inventory_complete": True},
        "summary": {"eligible_count": 0},
    }
    assert legacy_normalization_complete(complete)
    assert not legacy_normalization_complete({**complete, "schema": "legacy"})
    assert not legacy_normalization_complete({
        **complete, "configuration": {"inventory_complete": False}})
    assert not legacy_normalization_complete({
        **complete, "summary": {"eligible_count": 1}})
