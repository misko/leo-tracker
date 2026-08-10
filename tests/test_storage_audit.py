import json
from datetime import datetime, timezone

from leo_tracker.radio.beacon.storage_audit import build_storage_regime_audit
from leo_tracker.radio.beacon.storage_regime import (
    CONFIRMATION, apply_storage_regime_plan, build_storage_regime_plan)
from leo_tracker.radio.cli import main

from test_storage_regime import _ready


def test_audit_reports_legacy_layout_then_proves_migrated_layout(tmp_path):
    shared, archive, capture = _ready(tmp_path, "audit-me")
    before = build_storage_regime_audit(
        shared, archive, minimum_age_hours=0)
    assert before["converged"] is False
    assert before["violation_counts"]["old_raw"] == 1
    assert before["violation_counts"]["v1_evidence"] == 1
    assert before["violation_counts"]["derived_duplicates"] > 0

    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)
    assert result["failure_count"] == 0
    after = build_storage_regime_audit(shared, archive, minimum_age_hours=0)
    assert after["converged"] is True
    assert not any(after["violation_counts"].values())


def test_audit_detects_orphan_derived_and_versioned_outputs(tmp_path):
    shared, archive, capture = _ready(tmp_path, "orphan-layout")
    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    apply_storage_regime_plan(plan, confirmation=CONFIRMATION)
    orphan = archive / "derived" / "orphan.bin"
    orphan.parent.mkdir(parents=True, exist_ok=True); orphan.write_bytes(b"legacy")
    output = shared / "reports/runs/old/orphan/outputs/unreferenced.bin"
    output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b"legacy")

    audit = build_storage_regime_audit(shared, archive, minimum_age_hours=0)
    assert audit["converged"] is False
    assert audit["violation_counts"]["derived_duplicates"] == 1
    assert audit["violation_counts"]["versioned_outputs"] == 2


def test_audit_detects_stale_shared_transient(tmp_path):
    shared, archive = tmp_path / "shared", tmp_path / "archive"
    queue = shared / "staging/analysis-queue"; queue.mkdir(parents=True)
    stale = queue / "drain.request.next.42"; stale.write_text("old")
    import os
    os.utime(stale, (1, 1))

    audit = build_storage_regime_audit(
        shared, archive, minimum_age_hours=0)

    assert audit["converged"] is False
    assert audit["violation_counts"]["stale_shared_transients"] == 1


def test_audit_cli_writes_report_and_returns_nonzero_until_converged(
        tmp_path, capsys):
    shared, archive, _ = _ready(tmp_path, "audit-cli")
    output = tmp_path / "audit.json"
    assert main(["starlink-storage-audit-v2", str(shared), str(archive),
                 "--minimum-age-hours", "0", "--output", str(output)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["converged"] is False
    assert json.loads(output.read_text())["schema"].endswith("/v1")


def test_audit_allows_only_a_verified_current_manual_pin_as_old_raw(tmp_path):
    shared, archive, capture = _ready(tmp_path, "audit-pin")
    pins = shared / "reports/retention/pins"; pins.mkdir(parents=True)
    (pins / f"{capture.name}.json").write_text('{"reason":"operator"}\n')
    plan = build_storage_regime_plan(shared, archive, minimum_age_hours=0)
    result = apply_storage_regime_plan(plan, confirmation=CONFIRMATION)
    assert result["raw_removed_bytes"] == 0

    audit = build_storage_regime_audit(
        shared, archive, minimum_age_hours=0)
    assert audit["converged"] is True
    assert audit["violation_counts"]["old_raw"] == 0


def test_audit_rejects_interrupted_legacy_normalization_receipt(tmp_path):
    shared, archive = tmp_path / "shared", tmp_path / "archive"
    receipt = shared / "reports/reclamation/legacy-layout/pending.json"
    receipt.parent.mkdir(parents=True); receipt.write_text('{"status":"prepared"}\n')
    audit = build_storage_regime_audit(shared, archive, minimum_age_hours=0)
    assert audit["converged"] is False
    assert audit["violation_counts"]["incomplete_normalizer_receipts"] == 1


def test_audit_can_require_a_fresh_tiered_v2_producer_contract(tmp_path):
    shared, archive = tmp_path / "shared", tmp_path / "archive"
    missing = build_storage_regime_audit(
        shared, archive, minimum_age_hours=0,
        require_producer_contract=True)
    assert missing["converged"] is False
    assert missing["violation_counts"]["producer_contract"] == 1
    assert missing["producer_contract"]["reason"] == "missing"

    runtime = shared / "reports/runtime/analysis-server.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({
        "schema": "leo-tracker.analysis-server-runtime/v1",
        "state": "running",
        "heartbeat_utc": datetime.now(timezone.utc).isoformat(),
        "producer_contract_valid": True,
        "archive_mode": "required",
        "archive_root": str(archive),
        "evidence_policy": "tiered-v2",
        "archive_command": "starlink-evidence-archive-v2",
    }))
    current = build_storage_regime_audit(
        shared, archive, minimum_age_hours=0,
        require_producer_contract=True)
    assert current["converged"] is True
    assert current["violation_counts"]["producer_contract"] == 0
    assert current["producer_contract"]["valid"] is True


def test_audit_can_prove_local_acquisition_storage_convergence(tmp_path):
    shared, archive, local = (tmp_path / "shared", tmp_path / "archive",
                              tmp_path / "local")
    stale = local / "captures/stale"; stale.mkdir(parents=True)
    (stale / "manifest.json").write_text(json.dumps({
        "state": "interrupted", "chunks": []}))
    report = local / "reports/plots/legacy.png"
    report.parent.mkdir(parents=True); report.write_bytes(b"plot")
    legacy = local / "evidence/pilot_symbolwise_v3/reports/legacy.json"
    legacy.parent.mkdir(parents=True); legacy.write_text("{}")
    obsolete = local / "staging/old.confirmed"
    obsolete.parent.mkdir(parents=True); obsolete.write_bytes(b"")

    audit = build_storage_regime_audit(
        shared, archive, minimum_age_hours=0, local_root=local)

    assert audit["converged"] is False
    assert audit["violation_counts"]["local_unresolved_old"] == 1
    assert audit["violation_counts"]["local_legacy_reports"] == 1
    assert audit["violation_counts"]["local_legacy_evidence_reports"] == 1
    assert audit["violation_counts"]["local_obsolete_artifacts"] == 1
    assert audit["local"]["obsolete_artifacts"]["count"] == 1
    assert audit["local"]["unresolved_old"][0]["durable_copy"] == "empty_terminal"
