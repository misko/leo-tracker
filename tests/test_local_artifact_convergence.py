import json
import os
from pathlib import Path

import pytest

from leo_tracker.radio.beacon import local_artifact_convergence as module
from leo_tracker.radio.beacon.local_artifact_convergence import (
    apply_local_artifact_plan, build_local_artifact_plan,
    inventory_obsolete_local_artifacts)


def _old(path: Path) -> None:
    os.utime(path, (1, 1))


def _followup(shared: Path, name: str, *, confirmed: bool = True) -> Path:
    path = shared / "reports/followups" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-followup/v1",
        "confirmation": {"confirmed": confirmed}}))
    return path


def test_confirmation_marker_requires_confirmed_shared_followup(tmp_path):
    local, shared = tmp_path / "local", tmp_path / "shared"
    staging = local / "staging"; staging.mkdir(parents=True)
    good = staging / "capture-good.confirmed"; good.write_bytes(b""); _old(good)
    bad = staging / "capture-bad.confirmed"; bad.write_bytes(b""); _old(bad)
    _followup(shared, "capture-good"); _followup(shared, "capture-bad", confirmed=False)

    plan = build_local_artifact_plan(local, shared, minimum_age_s=0)

    assert {item["relative_path"]: item["status"] for item in plan["entries"]} == {
        "staging/capture-bad.confirmed": "confirmation_unverified",
        "staging/capture-good.confirmed": "eligible"}
    result = apply_local_artifact_plan(plan)
    assert result["removed_count"] == 1
    assert not good.exists() and bad.exists()
    receipt = json.loads((shared / "reports/reclamation/local-obsolete-artifacts.json").read_text())
    assert receipt["status"] == "complete"


def test_checkpoint_is_retired_but_young_artifacts_are_preserved(tmp_path):
    local, shared = tmp_path / "local", tmp_path / "shared"
    checkpoints = local / "checkpoints"; checkpoints.mkdir(parents=True)
    old = checkpoints / "old.json"; old.write_text("{}\n"); _old(old)
    young = checkpoints / "young.json"; young.write_text("{}\n")

    plan = build_local_artifact_plan(local, shared, minimum_age_s=60)
    statuses = {item["relative_path"]: item["status"] for item in plan["entries"]}
    assert statuses["checkpoints/old.json"] == "eligible"
    assert statuses["checkpoints/young.json"] == "minimum_age_not_met"
    apply_local_artifact_plan(plan)
    assert not old.exists() and young.exists()


def test_verified_v2_gates_frame_baseline_scratch(tmp_path, monkeypatch):
    local, shared, archive = tmp_path / "local", tmp_path / "shared", tmp_path / "archive"
    scratch = local / "tmp-frame-baseline.abc"; scratch.mkdir(parents=True)
    (scratch / "baseline.json").write_text(json.dumps({"capture": "/old/capture-001"}))
    (scratch / "baseline.npz").write_bytes(b"samples"); _old(scratch)
    receipt = archive / "catalog/v2/receipts/capture-001.json"
    receipt.parent.mkdir(parents=True); receipt.write_text(json.dumps({
        "source_manifest_sha256": "abc"}))
    monkeypatch.setattr(module, "_archive_gate", lambda *args: (True, "eligible", {}))

    plan = build_local_artifact_plan(
        local, shared, archive_root=archive, minimum_age_s=0)

    assert plan["entries"][0]["status"] == "eligible"
    assert apply_local_artifact_plan(plan)["removed_count"] == 1
    assert not scratch.exists()


def test_canonical_frame_product_can_supersede_old_scratch(tmp_path):
    local, shared = tmp_path / "local", tmp_path / "shared"
    scratch = local / "tmp-frame-baseline.abc"; scratch.mkdir(parents=True)
    (scratch / "baseline.json").write_text(json.dumps({"capture": "/old/capture-001"}))
    (scratch / "baseline.npz").write_bytes(b"old samples"); _old(scratch)
    frames = shared / "reports/frame-tracks"; frames.mkdir(parents=True)
    (frames / "capture-001.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-conditioned-frame-track/v3"}))
    (frames / "capture-001.npz").write_bytes(b"current samples")
    _followup(shared, "capture-001")

    plan = build_local_artifact_plan(local, shared, minimum_age_s=0)

    assert plan["entries"][0]["status"] == "eligible"
    assert len(plan["entries"][0]["authority_files"]) == 3
    assert apply_local_artifact_plan(plan)["removed_count"] == 1
    assert not scratch.exists()


def test_changed_artifact_is_deferred_without_deletion(tmp_path):
    local, shared = tmp_path / "local", tmp_path / "shared"
    staging = local / "staging"; staging.mkdir(parents=True)
    marker = staging / "capture.confirmed"; marker.write_bytes(b""); _old(marker)
    _followup(shared, "capture")
    plan = build_local_artifact_plan(local, shared, minimum_age_s=0)
    marker.write_text("changed")

    result = apply_local_artifact_plan(plan)

    assert result["status"] == "deferred"
    assert result["removed_count"] == 0 and marker.exists()


def test_inventory_counts_only_obsolete_shapes(tmp_path):
    root = tmp_path / "local"; (root / "staging").mkdir(parents=True)
    (root / "staging/old.confirmed").write_bytes(b"")
    (root / "staging/current.lock").write_bytes(b"")
    (root / "tmp-frame-baseline.x").mkdir()
    (root / "checkpoints").mkdir(); (root / "checkpoints/old").write_bytes(b"")

    result = inventory_obsolete_local_artifacts(root)

    assert result["count"] == 3
