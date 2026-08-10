"""Authoritative audit of convergence to the production Evidence-v2 layout."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .storage_regime import build_storage_regime_plan
from .local_reclamation import build_reclamation_plan
from .local_report_convergence import build_local_report_plan


AUDIT_SCHEMA = "leo-tracker.storage-regime-v2-audit/v1"
PRODUCER_SCHEMA = "leo-tracker.analysis-server-runtime/v1"


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _tree(root: Path, *, sample_limit: int) -> dict:
    files = 0; directories = 0; bytes_ = 0; symlinks = 0; samples = []
    if not root.is_dir():
        return {"root": str(root), "file_count": 0, "directory_count": 0,
                "bytes": 0, "symlink_count": 0, "samples": []}
    for path in root.rglob("*"):
        try:
            if path.is_symlink():
                symlinks += 1
                if len(samples) < sample_limit: samples.append(str(path))
            elif path.is_file():
                files += 1; bytes_ += path.stat().st_size
                if len(samples) < sample_limit: samples.append(str(path))
            elif path.is_dir():
                directories += 1
        except OSError:
            symlinks += 1
            if len(samples) < sample_limit: samples.append(str(path))
    return {"root": str(root), "file_count": files,
            "directory_count": directories, "bytes": bytes_,
            "symlink_count": symlinks, "samples": samples}


def _child_directories(root: Path, *, sample_limit: int) -> dict:
    paths = []
    if root.is_dir():
        paths = [path for path in root.iterdir() if path.is_dir() or path.is_symlink()]
    return {"root": str(root), "count": len(paths),
            "samples": [str(path) for path in paths[:sample_limit]]}


def _incomplete_receipts(root: Path, *, sample_limit: int) -> dict:
    incomplete = []
    for path in root.glob("*.json") if root.is_dir() else []:
        try:
            if json.loads(path.read_text()).get("status") != "complete":
                incomplete.append(str(path))
        except (OSError, ValueError, AttributeError):
            incomplete.append(str(path))
    return {"root": str(root), "count": len(incomplete),
            "samples": incomplete[:sample_limit]}


def _producer_contract(shared_root: Path, archive_root: Path, *,
                       required: bool, now: float,
                       maximum_heartbeat_age_s: float) -> dict:
    path = shared_root / "reports" / "runtime" / "analysis-server.json"
    value = _json(path) if path.is_file() else {}
    reason = None; age = None
    if not value:
        reason = "missing"
    elif value.get("schema") != PRODUCER_SCHEMA:
        reason = "unsupported_schema"
    elif value.get("state") != "running":
        reason = "not_running"
    elif not value.get("producer_contract_valid"):
        reason = "contract_invalid"
    elif value.get("archive_mode") != "required":
        reason = "archive_not_required"
    elif value.get("evidence_policy") != "tiered-v2":
        reason = "legacy_evidence_policy"
    elif value.get("archive_command") != "starlink-evidence-archive-v2":
        reason = "legacy_archive_command"
    elif value.get("archive_root") != str(archive_root):
        reason = "archive_root_mismatch"
    else:
        try:
            heartbeat = datetime.fromisoformat(
                str(value["heartbeat_utc"]).replace("Z", "+00:00"))
            age = max(0.0, now - heartbeat.timestamp())
            if age > maximum_heartbeat_age_s:
                reason = "heartbeat_stale"
        except (KeyError, TypeError, ValueError):
            reason = "heartbeat_invalid"
    return {
        "path": str(path), "required": required,
        "valid": reason is None, "reason": reason,
        "heartbeat_age_s": age,
        "maximum_heartbeat_age_s": maximum_heartbeat_age_s,
        "runtime": value,
    }


def build_storage_regime_audit(shared_root: Path, archive_root: Path, *,
                               minimum_age_hours: float = 6,
                               sample_limit: int = 20,
                               require_producer_contract: bool = False,
                               maximum_producer_heartbeat_age_s: float = 180,
                               local_root: Path | None = None) -> dict:
    """Prove whether all durable storage uses the current production layout."""
    if minimum_age_hours < 0:
        raise ValueError("minimum age must be non-negative")
    if sample_limit < 0:
        raise ValueError("sample limit must be non-negative")
    if maximum_producer_heartbeat_age_s <= 0:
        raise ValueError("maximum producer heartbeat age must be positive")
    shared_root = Path(shared_root).resolve(); archive_root = Path(archive_root).resolve()
    local_root = Path(local_root).resolve() if local_root is not None else None
    migration = build_storage_regime_plan(
        shared_root, archive_root, minimum_age_hours=minimum_age_hours,
        scope="all")
    cutoff = time.time() - minimum_age_hours * 3600
    old_raw = []
    for item in migration["entries"]:
        capture_value = item.get("capture_path")
        if not capture_value:
            continue
        capture = Path(capture_value); manifest = capture / "manifest.json"
        try:
            young = manifest.stat().st_mtime >= cutoff
        except OSError:
            young = False
        if not young and item.get("status") != "protected_pinned_current":
            old_raw.append({"recording_id": item["recording_id"],
                            "status": item.get("status"),
                            "source_bytes": item.get("source_bytes", 0)})

    legacy = {
        "v1_evidence": _child_directories(
            archive_root / "evidence", sample_limit=sample_limit),
        "v1_receipts": _tree(
            archive_root / "catalog" / "receipts", sample_limit=sample_limit),
        "v1_plans": _tree(
            archive_root / "catalog" / "plans", sample_limit=sample_limit),
        "v2_shadow": _tree(
            archive_root / "catalog" / "v2-shadow", sample_limit=sample_limit),
        "derived_duplicates": _tree(
            archive_root / "derived", sample_limit=sample_limit),
        "versioned_outputs": {},
        "incomplete_normalizer_receipts": _incomplete_receipts(
            shared_root / "reports" / "reclamation" / "legacy-layout",
            sample_limit=sample_limit),
    }
    # Completion records and authoritative references below reports/runs are
    # current. Only physical files below an `outputs` directory are legacy.
    output_files = []; output_bytes = 0
    runs = shared_root / "reports" / "runs"
    output_dirs = list(runs.glob("*/*/outputs") if runs.is_dir() else [])
    for output_dir in output_dirs:
        for path in output_dir.rglob("*"):
            try:
                if path.is_file() or path.is_symlink():
                    output_bytes += path.stat().st_size if path.is_file() else 0
                    if len(output_files) < sample_limit: output_files.append(str(path))
            except OSError:
                if len(output_files) < sample_limit: output_files.append(str(path))
    legacy["versioned_outputs"] = {
        "root": str(runs), "directory_count": len(output_dirs),
        "file_count": sum(
            1 for output_dir in output_dirs
            for path in output_dir.rglob("*") if path.is_file() or path.is_symlink()),
        "bytes": output_bytes, "samples": output_files}

    archive_only_entries = [item for item in migration["entries"]
                            if not item.get("capture_path")]
    violation_counts = {
        "old_raw": len(old_raw),
        "archive_only_v1": len(archive_only_entries),
        "v1_evidence": legacy["v1_evidence"]["count"],
        "v1_receipts": legacy["v1_receipts"]["file_count"],
        "v1_plans": legacy["v1_plans"]["file_count"],
        "v2_shadow": legacy["v2_shadow"]["file_count"],
        "derived_duplicates": legacy["derived_duplicates"]["file_count"],
        "versioned_outputs": (legacy["versioned_outputs"]["directory_count"] +
                              legacy["versioned_outputs"]["file_count"]),
        "incomplete_normalizer_receipts": legacy[
            "incomplete_normalizer_receipts"]["count"],
    }
    producer = _producer_contract(
        shared_root, archive_root, required=require_producer_contract,
        now=time.time(),
        maximum_heartbeat_age_s=maximum_producer_heartbeat_age_s)
    violation_counts["producer_contract"] = int(
        require_producer_contract and not producer["valid"])
    local = None
    if local_root is not None:
        if not local_root.is_dir():
            local = {"root": str(local_root), "missing": True,
                     "unresolved_old": [], "report_plan": None,
                     "legacy_evidence_reports": None}
            violation_counts["local_root_missing"] = 1
            violation_counts["local_unresolved_old"] = 0
            violation_counts["local_legacy_reports"] = 0
            violation_counts["local_legacy_evidence_reports"] = 0
        else:
            local_plan = build_reclamation_plan(
                local_root, shared_root, archive_root=archive_root,
                minimum_age_s=minimum_age_hours * 3600)
            allowed = {"minimum_age_not_met", "active_or_partial"}
            unresolved = [item for item in local_plan["entries"]
                          if item.get("status") not in allowed]
            report_plan = build_local_report_plan(local_root, shared_root)
            legacy_reports = _tree(
                local_root / "evidence" / "pilot_symbolwise_v3" / "reports",
                sample_limit=sample_limit)
            local = {"root": str(local_root), "missing": False,
                "reclamation_summary": local_plan["summary"],
                "unresolved_old": unresolved[:sample_limit],
                "unresolved_old_count": len(unresolved),
                "report_plan": report_plan["summary"],
                "legacy_evidence_reports": legacy_reports}
            violation_counts["local_root_missing"] = 0
            violation_counts["local_unresolved_old"] = len(unresolved)
            violation_counts["local_legacy_reports"] = report_plan[
                "summary"]["eligible_count"]
            violation_counts["local_legacy_evidence_reports"] = legacy_reports[
                "file_count"]
    return {
        "schema": AUDIT_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "shared_root": str(shared_root), "archive_root": str(archive_root),
        "configuration": {"minimum_age_hours": minimum_age_hours,
                          "sample_limit": sample_limit,
                          "require_producer_contract": require_producer_contract,
                          "maximum_producer_heartbeat_age_s":
                              maximum_producer_heartbeat_age_s},
        "converged": not any(violation_counts.values()),
        "violation_counts": violation_counts,
        "old_raw_bytes": sum(int(item["source_bytes"]) for item in old_raw),
        "old_raw_samples": old_raw[:sample_limit],
        "migration_summary": migration["summary"],
        "producer_contract": producer,
        "local": local,
        "legacy": legacy,
    }
