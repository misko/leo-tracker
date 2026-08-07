import hashlib
import json
from pathlib import Path

import numpy as np

from leo_tracker.radio.beacon.offload import (
    RECEIPT_SCHEMA,
    audit_completed,
    create_context_bundle,
    current_context,
    validate_context_bundle,
    validate_outputs,
)
from leo_tracker.radio.beacon.template_learning import load_learned_beacon


def _learned_fixture(root: Path) -> Path:
    samples = root / "source.npz"
    np.savez(samples, template_rx0=np.ones(8), template_rx1=np.ones(8))
    report = root / "source.json"
    report.write_text(json.dumps({
        "schema": "leo-tracker.starlink-learned-bandpass-beacon/v1",
        "samples": str(samples),
        "samples_sha256": hashlib.sha256(samples.read_bytes()).hexdigest(),
        "frame_sample_count": 8,
        "validation": {"qualified": True},
    }))
    return report


def test_context_bundle_is_atomic_self_contained_and_mount_independent(tmp_path):
    learned = _learned_fixture(tmp_path)
    passes = tmp_path / "passes.json"; passes.write_text("[]")
    tle = tmp_path / "tle.json"
    tle.write_text(json.dumps({"schema": "leo-tracker.tle-catalog/v1"}))

    bundle = create_context_bundle(tmp_path / "context", learned=learned,
                                   passes=passes, tle_catalog=tle)

    assert bundle == current_context(tmp_path / "context")
    validate_context_bundle(bundle)
    bundled_report = json.loads((bundle / "learned-beacon.json").read_text())
    assert bundled_report["samples"] == "learned-beacon.npz"
    loaded, arrays = load_learned_beacon(bundle / "learned-beacon.json")
    assert loaded["validation"]["qualified"]
    assert arrays["template_rx0"].shape == (8,)
    assert not list((tmp_path / "context/bundles").glob("*.partial.*"))


def test_context_bundle_flattens_tle_archive_snapshot(tmp_path):
    archive = tmp_path / "tle-history"
    objects = archive / "objects"; objects.mkdir(parents=True)
    catalog = objects / "catalog.json"
    catalog.write_text(json.dumps({"schema": "leo-tracker.tle-catalog/v1",
                                   "content": "fixture"}))
    snapshot = archive / "latest.json"
    snapshot.write_text(json.dumps({
        "schema": "leo-tracker.tle-archive-snapshot/v1",
        "object": "objects/catalog.json",
    }))

    bundle = create_context_bundle(tmp_path / "context", tle_catalog=snapshot)

    assert json.loads((bundle / "tle-catalog.json").read_text())["schema"] == \
        "leo-tracker.tle-catalog/v1"
    assert not (bundle / "objects").exists()


def test_output_validation_requires_analysis_and_followup(tmp_path):
    reports = tmp_path / "reports"
    (reports / "followups").mkdir(parents=True)
    (reports / "sample.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-analysis/v1"}))
    (reports / "followups/sample.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-followup/v1",
                    "confirmation": {"confirmed": False}}))

    receipt = validate_outputs(tmp_path, "sample", "narrow", context=None)

    assert receipt["schema"] == RECEIPT_SCHEMA
    assert not receipt["confirmed"]


def test_audit_requeues_false_done_but_accepts_valid_negative(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    done = queue / "done"; done.mkdir(parents=True)
    reports = tmp_path / "reports"; (reports / "followups").mkdir(parents=True)
    (done / "bad.job").write_text("bad\tcaptures/bad\tnarrow\n")
    (done / "good.job").write_text("good\tcaptures/good\toversample\n")
    (reports / "good.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-analysis/v1"}))
    (reports / "followups/good.json").write_text(
        json.dumps({"schema": "leo-tracker.starlink-beacon-followup/v1",
                    "confirmation": {"confirmed": False}}))

    result = audit_completed(tmp_path)

    assert result["accepted"] == ["good"]
    assert result["requeued"] == ["bad.job"]
    assert (queue / "bad.job").is_file()
    receipt = json.loads((reports / "receipts/good.json").read_text())
    assert receipt["status"] == "success"
