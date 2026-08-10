import json

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
