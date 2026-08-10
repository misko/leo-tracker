import hashlib
import json
import os
from pathlib import Path
import subprocess

import numpy as np
import pytest

from leo_tracker.radio.beacon.artifact import BeaconCapture, capture_beacon_iq
from leo_tracker.radio.beacon.evidence_archive import (
    AUDIT_SCHEMA, BUNDLE_SCHEMA, PLAN_SCHEMA, SHADOW_SCHEMA, VERIFICATION_SCHEMA,
    _adapt_plan_to_verified_source_bundle,
    archive_evidence, archive_evidence_v2, archive_evidence_v2_from_v1,
    audit_evidence, build_evidence_v2_shadow,
    extract_evidence, materialize_evidence_clip, compare_evidence_plan_coverage,
    plan_evidence, repair_evidence_v2_summaries, verify_evidence,
)
import leo_tracker.radio.beacon.evidence_archive as evidence_archive_module
from leo_tracker.radio.cli import main
from leo_tracker.radio.paired import PairedSampleBlock


def test_archive_only_plan_relocates_optional_control_into_verified_v1_coverage():
    plan = {
        "schema": PLAN_SCHEMA, "recording_id": "old", "sample_rate_hz": 1000,
        "source_total_samples_per_receiver": 10_000,
        "policy": {"name": "tiered-v2", "control_count": 1,
                   "control_duration_s": .5},
        "required_events": [{"event_id": "event-000", "first_sample": 6200,
                             "stop_sample": 6300, "reason": "exact_candidate"}],
        "intervals": [
            {"interval_id": "clip-000", "first_sample": 3000,
             "stop_sample": 3500, "sample_count": 500,
             "reasons": ["deterministic_control"]},
            {"interval_id": "clip-001", "first_sample": 6000,
             "stop_sample": 7000, "sample_count": 1000,
             "reasons": ["exact_candidate"]},
        ],
        "summary": {},
    }
    source = {
        "schema": BUNDLE_SCHEMA, "recording_id": "old", "clips": [
            {"first_sample": 0, "stop_sample": 1000,
             "reasons": ["deterministic_control"],
             "first_utc_ns": 1_000_000_000, "stop_utc_ns": 2_000_000_000,
             "utc_mapping_method": "test", "utc_uncertainty_s": .1},
            {"first_sample": 5500, "stop_sample": 7500,
             "reasons": ["exact_candidate"],
             "first_utc_ns": 6_500_000_000, "stop_utc_ns": 8_500_000_000,
             "utc_mapping_method": "test", "utc_uncertainty_s": .1},
        ],
    }

    adapted = _adapt_plan_to_verified_source_bundle(plan, source)

    assert [(item["first_sample"], item["stop_sample"]) for item in
            adapted["intervals"]] == [(250, 750), (6000, 7000)]
    assert adapted["intervals"][0]["first_utc_ns"] == 1_250_000_000
    assert adapted["intervals"][0]["stop_utc_ns"] == 1_750_000_000
    assert adapted["policy"]["archive_only_control_relocated_samples"] == 500
    assert adapted["policy"]["archive_only_control_unavailable_samples"] == 0
    assert adapted["summary"]["selected_samples_per_receiver"] == 1500
    assert adapted["summary"]["coverage_fraction"] == .15


def test_archive_only_plan_never_relocates_missing_signal_interval():
    plan = {
        "schema": PLAN_SCHEMA, "recording_id": "old", "sample_rate_hz": 1000,
        "source_total_samples_per_receiver": 10_000,
        "policy": {"name": "tiered-v2"}, "required_events": [],
        "intervals": [{"first_sample": 6000, "stop_sample": 7000,
                       "reasons": ["exact_candidate"]}], "summary": {},
    }
    source = {"schema": BUNDLE_SCHEMA, "recording_id": "old", "clips": [{
        "first_sample": 0, "stop_sample": 1000,
        "reasons": ["deterministic_control"]}]}

    with pytest.raises(ValueError, match="does not cover a required v2 interval"):
        _adapt_plan_to_verified_source_bundle(plan, source)


def _capture(root: Path, name: str = "sample-narrow") -> tuple[Path, np.ndarray]:
    path = root / "captures" / name
    count = 10_000
    base = np.arange(count, dtype=np.float32)
    rx0 = ((base % 1000) - 500 + 1j * ((base * 3) % 1000 - 500)).astype(np.complex64)
    rx1 = ((base * 5) % 1200 - 600 + 1j * ((base * 7) % 1200 - 600)).astype(np.complex64)
    blocks = [PairedSampleBlock(rx0[start:start + 777], rx1[start:start + 777], start,
                                1_700_000_000_000_000_000 + start * 1_000_000,
                                read_duration_ns=777_000_000)
              for start in range(0, count, 777)]
    capture_beacon_iq(blocks, path, sample_rate_hz=1000,
                      center_frequency_hz=1_709_687_500,
                      bandwidth_hz=900, duration_s=10, chunk_s=2,
                      lnb_lo_hz=9_750_000_000,
                      metadata={"observation_mode": "narrow"})
    expected = np.empty((count, 2, 2), dtype="<i2")
    expected[:, 0, 0] = rx0.real.astype("<i2"); expected[:, 0, 1] = rx0.imag.astype("<i2")
    expected[:, 1, 0] = rx1.real.astype("<i2"); expected[:, 1, 1] = rx1.imag.astype("<i2")
    return path, expected


def _reports(root: Path, name: str) -> Path:
    reports = root / "reports"
    (reports / "followups").mkdir(parents=True)
    (reports / "tracks").mkdir()
    (reports / "decoded").mkdir()
    (reports / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1",
        "exact_checks": [{"start_s": 4.0, "duration_s": .1,
                          "candidate": True, "receiver_candidates": [True, True]}],
        "windows": [{"start_s": 8.0, "duration_s": .2, "doppler_like": True}],
    }) + "\n")
    (reports / "followups" / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-followup/v1",
        "confirmation": {"confirmed": True}, "settings": {"window_s": .01},
        "checks": [{"start_s": 4.0, "candidate": True},
                   {"start_s": 4.2, "candidate": False}],
    }) + "\n")
    (reports / "tracks" / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "tracks": [{"observations": [{"start_sample": 3900},
                                       {"start_sample": 4500}]}],
    }) + "\n")
    (reports / "decoded" / f"{name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-edge-decode/v1",
        "selected_observation": {"start_s": 4.1, "duration_s": .1},
    }) + "\n")
    return reports


def _tree_hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_plan_extract_and_verify_preserve_exact_dual_rx_samples(tmp_path):
    source_root = tmp_path / "source"
    capture, expected = _capture(source_root)
    reports = _reports(source_root, capture.name)
    before = _tree_hashes(capture)
    plan_path = tmp_path / "plan.json"

    plan = plan_evidence(capture, reports, plan_path, guard_s=.5,
                         control_duration_s=.2, control_count=2)

    assert plan["schema"] == PLAN_SCHEMA
    assert plan["summary"]["interval_count"] >= 3
    assert {reason for item in plan["intervals"] for reason in item["reasons"]} >= {
        "exact_candidate", "confirmed_dense_followup", "continuous_doppler_track",
        "decoded_symbols", "broadband_or_window_candidate", "deterministic_control"}
    bundle_path = tmp_path / "qnap" / "evidence" / capture.name
    bundle = extract_evidence(capture, plan_path, bundle_path)
    verification = verify_evidence(bundle_path, capture_path=capture)

    assert bundle["schema"] == BUNDLE_SCHEMA
    assert verification["schema"] == VERIFICATION_SCHEMA
    assert verification["valid"] is True
    assert all(item["source_equal"] for item in verification["checks"])
    assert _tree_hashes(capture) == before
    for clip in bundle["clips"]:
        actual = np.fromfile(bundle_path / clip["path"], dtype="<i2").reshape(-1, 2, 2)
        assert np.array_equal(actual, expected[clip["first_sample"]:clip["stop_sample"]])


def test_plan_accepts_checksummed_interrupted_prefix(tmp_path):
    source_root = tmp_path / "source"
    capture, _ = _capture(source_root, "interrupted-prefix")
    reports = _reports(source_root, capture.name)
    manifest_path = capture / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["state"] = "interrupted"
    manifest["captured_samples_per_receiver"] += 500
    manifest_path.write_text(json.dumps(manifest))

    plan = plan_evidence(capture, reports, policy="tiered-v2")

    assert plan["schema"] == PLAN_SCHEMA
    assert plan["summary"]["interval_count"] > 0
    assert max(item["stop_sample"] for item in plan["intervals"]) <= 10_000


def test_tiered_v2_reduces_confirmed_coverage_but_preserves_required_events(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source)
    reports = _reports(source, capture.name)

    v1 = plan_evidence(capture, reports, policy="conservative-v1")
    v2 = plan_evidence(capture, reports, policy="tiered-v2")
    comparison = compare_evidence_plan_coverage(v1, v2)

    assert v2["evidence_tier_name"] == "confirmed_beacon"
    assert v2["policy"] == {
        "name": "tiered-v2", "guard_s": 2.0, "control_duration_s": .5,
        "control_count": 2, "confirmed_followup": True,
        "selection": "interesting event spans plus tier-sized controls",
    }
    assert comparison["valid"] is True
    assert comparison["missing_required_event_count"] == 0
    assert v2["summary"]["coverage_fraction"] < v1["summary"]["coverage_fraction"]
    assert "confirmed_dense_followup_control" not in {
        reason for item in v2["intervals"] for reason in item["reasons"]}


def test_tiered_v2_strict_negative_keeps_one_tiny_deterministic_control(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source)
    reports = source / "reports"; reports.mkdir()
    (reports / f"{capture.name}.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1",
        "exact_checks": [], "windows": [],
    }) + "\n")

    plan = plan_evidence(capture, reports, policy="tiered-v2")

    assert plan["evidence_tier"] == 0
    assert plan["required_events"] == []
    assert plan["summary"]["interval_count"] == 1
    assert plan["summary"]["coverage_fraction"] == pytest.approx(.01)
    assert plan["intervals"][0]["reasons"] == ["deterministic_control"]


def test_plan_coverage_gate_rejects_candidate_missing_required_event(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source)
    reports = _reports(source, capture.name)
    reference = plan_evidence(capture, reports, policy="tiered-v2")
    candidate = json.loads(json.dumps(reference))
    candidate["intervals"] = [item for item in candidate["intervals"]
                              if "broadband_or_window_candidate" not in item["reasons"]]

    comparison = compare_evidence_plan_coverage(reference, candidate)

    assert comparison["valid"] is False
    assert comparison["missing_required_event_count"] >= 1
    legacy = json.loads(json.dumps(reference)); legacy.pop("required_events")
    with pytest.raises(ValueError, match="predates required-event"):
        compare_evidence_plan_coverage(legacy, candidate)


def test_v2_shadow_batch_is_non_destructive_replay_gated_and_projects_savings(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source)
    reports = _reports(source, capture.name); archive = tmp_path / "archive"
    archive_evidence(capture, reports, archive)
    before = _tree_hashes(source)

    result = build_evidence_v2_shadow(source, archive)

    assert result["schema"] == SHADOW_SCHEMA
    assert result["summary"]["recording_count"] == 1
    assert result["summary"]["failure_count"] == 0
    assert result["entries"][0]["replay_gate_valid"] is True
    assert result["entries"][0]["projected_savings_bytes"] > 0
    assert (archive / "catalog" / "v2-shadow" / "summary.json").is_file()
    assert _tree_hashes(source) == before


def test_extract_is_idempotent_and_rejects_changed_source_manifest(tmp_path):
    root = tmp_path / "source"; capture, _ = _capture(root); reports = _reports(root, capture.name)
    plan_path = tmp_path / "plan.json"; plan_evidence(capture, reports, plan_path)
    output = tmp_path / "archive" / capture.name
    first = extract_evidence(capture, plan_path, output)
    second = extract_evidence(capture, plan_path, output)
    assert second["summary"] == first["summary"]
    manifest = json.loads((capture / "manifest.json").read_text())
    manifest["metadata"]["changed"] = True
    (capture / "manifest.json").write_text(json.dumps(manifest) + "\n")
    with pytest.raises(ValueError, match="manifest changed"):
        extract_evidence(capture, plan_path, tmp_path / "other")


def test_evidence_tooling_refuses_all_outputs_below_source_root(tmp_path):
    root = tmp_path / "source"; capture, _ = _capture(root); reports = _reports(root, capture.name)
    with pytest.raises(ValueError, match="source storage root"):
        plan_evidence(capture, reports, root / "catalog" / "plan.json")
    external_plan = tmp_path / "plan.json"; plan_evidence(capture, reports, external_plan)
    with pytest.raises(ValueError, match="source storage root"):
        extract_evidence(capture, external_plan, root / "evidence" / capture.name)
    with pytest.raises(ValueError, match="inside source storage"):
        archive_evidence(capture, reports, root / "qnap")


def test_verifier_detects_corrupt_clip_and_audit_reports_it(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source); reports = _reports(source, capture.name)
    plan = tmp_path / "plan.json"; plan_evidence(capture, reports, plan, guard_s=.1)
    evidence = tmp_path / "evidence"; bundle = evidence / capture.name
    extract_evidence(capture, plan, bundle)
    clip = next(bundle.glob("*.ci16"))
    with clip.open("r+b") as stream:
        stream.seek(0); stream.write(b"broken!!")
    result = verify_evidence(bundle, capture_path=capture, write=False)
    assert result["valid"] is False
    audit = audit_evidence(source, evidence)
    assert audit["schema"] == AUDIT_SCHEMA
    assert audit["verified_bundle_count"] == 0
    assert audit["invalid"] == [capture.name]


def test_interrupted_extraction_resumes_verified_completed_clips(tmp_path, monkeypatch):
    source = tmp_path / "source"; capture, _ = _capture(source); reports = _reports(source, capture.name)
    plan = tmp_path / "plan.json"
    plan_evidence(capture, reports, plan, guard_s=.1,
                  control_duration_s=.1, control_count=2)
    output = tmp_path / "qnap" / capture.name
    original = evidence_archive_module._copy_interval
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated NFS interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence_archive_module, "_copy_interval", interrupted)
    with pytest.raises(OSError, match="simulated NFS"):
        extract_evidence(capture, plan, output)
    partial = output.with_name(output.name + ".partial")
    partial_manifest = json.loads((partial / "manifest.json").read_text())
    assert partial_manifest["state"] == "extracting"
    assert len(partial_manifest["clips"]) == 1
    completed = partial / partial_manifest["clips"][0]["path"]
    completed_hash = hashlib.sha256(completed.read_bytes()).hexdigest()

    monkeypatch.setattr(evidence_archive_module, "_copy_interval", original)
    extract_evidence(capture, plan, output)

    assert hashlib.sha256((output / completed.name).read_bytes()).hexdigest() == completed_hash
    assert verify_evidence(output, capture_path=capture, write=False)["valid"] is True


def test_cli_evidence_pipeline_and_audit(tmp_path, capsys):
    source = tmp_path / "source"; capture, _ = _capture(source); reports = _reports(source, capture.name)
    plan = tmp_path / "plan.json"; bundle = tmp_path / "qnap" / capture.name
    assert main(["starlink-evidence-plan", str(capture), str(reports), str(plan),
                 "--guard-s", ".1", "--control-count", "1"]) == 0
    assert main(["starlink-evidence-extract", str(capture), str(plan), str(bundle)]) == 0
    assert main(["starlink-evidence-verify", str(bundle), "--source", str(capture)]) == 0
    audit_path = tmp_path / "audit.json"
    assert main(["starlink-evidence-audit", str(source), str(bundle.parent),
                 "--output", str(audit_path)]) == 0
    assert json.loads(audit_path.read_text())["verified_bundle_count"] == 1
    assert '"valid": true' in capsys.readouterr().out


def test_archive_copies_derived_artifacts_and_writes_verified_receipt(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source); reports = _reports(source, capture.name)
    plot = reports / "plots" / f"{capture.name}.png"
    plot.parent.mkdir(); plot.write_bytes(b"diagnostic plot")
    qnap = tmp_path / "qnap"

    receipt = archive_evidence(capture, reports, qnap, guard_s=.1,
                               control_duration_s=.1, control_count=1)

    assert receipt["status"] == "verified"
    assert receipt["source_verified"] is True
    assert (qnap / "catalog" / "receipts" / f"{capture.name}.json").is_file()
    assert (qnap / "derived" / "plots" / plot.name).read_bytes() == plot.read_bytes()
    assert (qnap / "evidence" / capture.name / "source-manifest.json").is_file()
    # Re-publication is idempotent and never mutates the source.
    assert archive_evidence(capture, reports, qnap)["source_manifest_sha256"] == \
        receipt["source_manifest_sha256"]


def test_v2_archive_is_source_verified_and_does_not_duplicate_reports(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source)
    reports = _reports(source, capture.name)
    plot = reports / "plots" / f"{capture.name}.png"
    plot.parent.mkdir(); plot.write_bytes(b"diagnostic plot")
    archive = tmp_path / "archive"

    receipt = archive_evidence_v2(capture, reports, archive)

    assert receipt["schema"] == "leo-tracker.evidence-archive-receipt/v2"
    assert receipt["policy"] == "tiered-v2"
    assert receipt["source_verified"] is True
    assert receipt["required_event_replay_valid"] is True
    assert receipt["derived_artifacts_duplicated"] is False
    assert (archive / "evidence-v2" / capture.name / "manifest.json").is_file()
    assert not (archive / "derived").exists()
    assert verify_evidence(archive / receipt["bundle"], capture_path=capture,
                           write=False)["valid"] is True
    assert archive_evidence_v2(capture, reports, archive) == receipt


def test_v2_archive_derives_source_bytes_for_legacy_manifest(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source, "legacy-bytes")
    reports = _reports(source, capture.name); archive = tmp_path / "archive"
    manifest_path = capture / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("stored_bytes", None)
    expected = sum(int(item["bytes"]) for item in manifest["chunks"])
    manifest_path.write_text(json.dumps(manifest))

    receipt = archive_evidence_v2(capture, reports, archive)

    assert receipt["summary"]["source_bytes"] == expected
    assert receipt["summary"]["stored_bytes"] < expected
    assert receipt["summary"]["storage_fraction"] == pytest.approx(
        receipt["summary"]["stored_bytes"] / expected)


def test_repair_v2_summary_updates_metadata_without_changing_iq(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source, "repair-summary")
    reports = _reports(source, capture.name); archive = tmp_path / "archive"
    receipt = archive_evidence_v2(capture, reports, archive)
    bundle = archive / receipt["bundle"]
    before_iq = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in bundle.glob("*.ci16")}
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"] = {"clip_count": len(manifest["clips"]),
                           "stored_bytes": sum(x["bytes"] for x in manifest["clips"]),
                           "source_bytes": 0, "storage_fraction": 1e9}
    manifest_path.write_text(json.dumps(manifest))
    receipt_path = archive / "catalog/v2/receipts/repair-summary.json"
    broken_receipt = json.loads(receipt_path.read_text())
    broken_receipt["summary"] = manifest["summary"]
    broken_receipt["bundle_manifest_sha256"] = hashlib.sha256(
        manifest_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(broken_receipt))

    result = repair_evidence_v2_summaries(archive)

    assert result["repaired"] == ["repair-summary"]
    fixed = json.loads(receipt_path.read_text())
    assert fixed["summary"]["source_bytes"] > fixed["summary"]["stored_bytes"]
    assert fixed["summary"]["storage_fraction"] < 1
    assert fixed["summary_repaired_utc"]
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in bundle.glob("*.ci16")} == before_iq
    assert verify_evidence(bundle, capture_path=capture, write=False)["valid"]


def test_archive_only_v2_recrop_rebuilds_its_own_interrupted_partial(tmp_path, monkeypatch):
    source = tmp_path / "source"; capture, _ = _capture(source, "archive-resume")
    reports = _reports(source, capture.name); archive = tmp_path / "archive"
    archive_evidence(capture, reports, archive)
    import shutil
    shutil.rmtree(capture)
    original = evidence_archive_module._copy_interval_from_bundle
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated recrop interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence_archive_module, "_copy_interval_from_bundle", interrupted)
    with pytest.raises(OSError, match="simulated recrop"):
        archive_evidence_v2_from_v1(capture.name, reports, archive)
    assert (archive / "evidence-v2" / f"{capture.name}.partial" / "resume.json").is_file()

    monkeypatch.setattr(evidence_archive_module, "_copy_interval_from_bundle", original)
    receipt = archive_evidence_v2_from_v1(capture.name, reports, archive)
    assert receipt["status"] == "verified"
    assert not (archive / "evidence-v2" / f"{capture.name}.partial").exists()


def test_materialized_clip_is_standard_replayable_capture_with_original_indexes(tmp_path):
    source = tmp_path / "source"; capture, expected = _capture(source); reports = _reports(source, capture.name)
    plan = tmp_path / "plan.json"; plan_evidence(capture, reports, plan, guard_s=.1,
                                                  control_duration_s=.1, control_count=1)
    bundle = tmp_path / "qnap" / capture.name; extracted = extract_evidence(capture, plan, bundle)
    selected = extracted["clips"][0]
    replay = tmp_path / "replay"

    materialized = materialize_evidence_clip(bundle, selected["interval_id"], replay)
    opened = BeaconCapture.open(replay, verify=True)

    assert materialized["metadata"]["evidence"]["source_first_sample"] == \
        selected["first_sample"]
    assert np.array_equal(opened.read_window(0, selected["sample_count"]),
                          expected[selected["first_sample"]:selected["stop_sample"], :, 0] +
                          1j * expected[selected["first_sample"]:selected["stop_sample"], :, 1])
    assert materialize_evidence_clip(bundle, selected["interval_id"], replay)[
        "captured_samples_per_receiver"] == selected["sample_count"]


def test_archive_shell_pipeline_is_end_to_end_and_source_preserving(tmp_path):
    source = tmp_path / "source"; capture, _ = _capture(source); _reports(source, capture.name)
    before = _tree_hashes(source)
    qnap = tmp_path / "qnap"
    repo = Path(__file__).parents[1]
    result = subprocess.run([
        "bash", str(repo / "scripts/starlink-evidence-archive.sh"),
        "--recording", capture.name, str(source), str(qnap)],
        env=os.environ | {"LEO_TRACKER_REPO": str(repo),
                          "LEO_EVIDENCE_MINIMUM_QNAP_FREE_GB": "1"},
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "evidence_done" in result.stdout
    assert _tree_hashes(source) == before
    assert json.loads((qnap / "catalog" / "v2" / "receipts" /
                       f"{capture.name}.json").read_text())["status"] == "verified"
