import hashlib
import json
from pathlib import Path

from leo_tracker.radio.beacon.local_report_convergence import (
    RECEIPT_SCHEMA, apply_local_report_plan, build_local_report_plan)
from leo_tracker.radio.cli import main


def test_missing_reports_copy_existing_authority_wins_and_operational_state_stays(
        tmp_path):
    local, shared = tmp_path / "local", tmp_path / "shared"
    missing = local / "reports/decoded/old.npz"
    collision = local / "reports/tracks/same.json"
    learned = local / "reports/learned-beacons/active.json"
    learned_target = local / "reports/learned-beacons/model.json"
    for path, payload in ((missing, b"legacy science"),
                          (collision, b"old interpretation"),
                          (learned_target, b"live model")):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
    learned.symlink_to(learned_target.name)
    authority = shared / "reports/tracks/same.json"
    authority.parent.mkdir(parents=True); authority.write_bytes(b"current authority")

    plan = build_local_report_plan(local, shared)
    assert plan["summary"]["status_counts"] == {
        "eligible": 2, "preserve_operational": 2}
    result = apply_local_report_plan(plan)

    assert result["status"] == "complete"
    assert result["removed_count"] == 2
    assert (shared / "reports/decoded/old.npz").read_bytes() == b"legacy science"
    assert authority.read_bytes() == b"current authority"
    assert not missing.exists() and not collision.exists()
    assert learned.is_symlink() and learned.read_bytes() == b"live model"
    receipt = json.loads(
        (shared / "reports/reclamation/local-reports.json").read_text())
    assert receipt["schema"] == RECEIPT_SCHEMA
    assert {item["destination_state"] for item in receipt["migrated"]} == {
        "copied_legacy", "existing_authority"}


def test_changed_local_report_is_deferred_without_deletion(tmp_path):
    local, shared = tmp_path / "local", tmp_path / "shared"
    source = local / "reports/plots/one.png"
    source.parent.mkdir(parents=True); source.write_bytes(b"before")
    plan = build_local_report_plan(local, shared)
    source.write_bytes(b"after")

    result = apply_local_report_plan(plan)

    assert result["status"] == "deferred"
    assert result["removed_count"] == 0
    assert source.read_bytes() == b"after"


def test_local_report_cli_is_dry_run_by_default(tmp_path, capsys):
    local, shared = tmp_path / "local", tmp_path / "shared"
    source = local / "reports/one.json"
    source.parent.mkdir(parents=True); source.write_text("{}")
    output = tmp_path / "plan.json"

    assert main(["starlink-local-report-converge", str(local), str(shared),
                 "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["eligible_count"] == 1
    assert source.exists() and output.is_file()


def test_legacy_evidence_debug_report_retires_only_behind_verified_v2(tmp_path):
    local, shared, archive = (tmp_path / "local", tmp_path / "shared",
                              tmp_path / "archive")
    name = "ch4-lower-edge-narrow-20260805T171900Z"
    report = local / "evidence/pilot_symbolwise_v3/reports" / f"{name}-dense.png"
    report.parent.mkdir(parents=True); report.write_bytes(b"legacy debug plot")
    bundle = archive / "evidence-v2" / name; bundle.mkdir(parents=True)
    manifest = bundle / "manifest.json"; manifest.write_text("{}")
    receipt = archive / "catalog/v2/receipts" / f"{name}.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "schema": "leo-tracker.evidence-archive-receipt/v2",
        "status": "verified", "source_verified": True,
        "required_event_replay_valid": True, "policy": "tiered-v2",
        "source_manifest_sha256": "source-hash", "bundle": f"evidence-v2/{name}",
        "bundle_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }))

    without_v2 = build_local_report_plan(local, shared)
    with_v2 = build_local_report_plan(local, shared, archive_root=archive)

    assert without_v2["summary"]["eligible_count"] == 0
    assert with_v2["summary"]["eligible_count"] == 1
    result = apply_local_report_plan(with_v2)
    assert result["removed_count"] == 1
    assert result["migrated"][0]["destination_state"] == "v2_current_authority"
    assert not report.exists()
