import json
import os

import pytest

from leo_tracker.radio.beacon.shared_transient_convergence import (
    CONFIRMATION, apply_shared_transient_plan, build_shared_transient_plan,
    inventory_stale_shared_transients)


def _old(path): os.utime(path, (1, 1))


def test_old_atomic_next_is_receipt_gated_and_removed(tmp_path):
    shared, archive = tmp_path / "shared", tmp_path / "archive"
    queue = shared / "staging/analysis-queue"; queue.mkdir(parents=True)
    stale = queue / "drain.request.next.123"; stale.write_text("request\n"); _old(stale)

    plan = build_shared_transient_plan(shared, archive, minimum_age_s=0)
    assert plan["summary"]["eligible_count"] == 1
    with pytest.raises(ValueError, match="confirmation token"):
        apply_shared_transient_plan(plan, confirmation="")
    result = apply_shared_transient_plan(plan, confirmation=CONFIRMATION)
    assert result["removed_count"] == 1 and not stale.exists()
    receipt = json.loads((shared / "reports/reclamation/shared-transients.json").read_text())
    assert receipt["status"] == "complete"


def test_upload_partial_requires_final_capture_authority(tmp_path):
    shared, archive = tmp_path / "shared", tmp_path / "archive"
    incoming = shared / "staging/incoming"; incoming.mkdir(parents=True)
    partial = incoming / "capture.partial"; partial.mkdir()
    (partial / "chunk-00000.ci16").write_bytes(b"partial"); _old(partial)
    unresolved = build_shared_transient_plan(shared, archive, minimum_age_s=0)
    assert unresolved["entries"][0]["status"] == "resumable_upload_partial"

    capture = shared / "captures/capture"; capture.mkdir(parents=True)
    (capture / "manifest.json").write_text(json.dumps({
        "state": "complete", "chunks": [{"path": "chunk-00000.ci16"}]}))
    eligible = build_shared_transient_plan(shared, archive, minimum_age_s=0)
    assert eligible["entries"][0]["status"] == "eligible"


def test_young_partial_is_not_a_violation(tmp_path):
    shared, archive = tmp_path / "shared", tmp_path / "archive"
    incoming = shared / "staging/incoming"; incoming.mkdir(parents=True)
    (incoming / "active.partial").mkdir()
    plan = build_shared_transient_plan(shared, archive, minimum_age_s=3600)
    assert plan["entries"][0]["status"] == "minimum_age_not_met"
    assert inventory_stale_shared_transients(
        shared, archive, minimum_age_s=3600)["count"] == 0


def test_changed_transient_is_deferred(tmp_path):
    shared, archive = tmp_path / "shared", tmp_path / "archive"
    queue = shared / "staging/analysis-queue"; queue.mkdir(parents=True)
    stale = queue / "x.next.1"; stale.write_text("old"); _old(stale)
    plan = build_shared_transient_plan(shared, archive, minimum_age_s=0)
    stale.write_text("changed")
    result = apply_shared_transient_plan(plan, confirmation=CONFIRMATION)
    assert result["status"] == "deferred" and stale.exists()
