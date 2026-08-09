"""Transactional migration from raw/v1 storage to tiered v2 evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from .evidence_archive import (V2_RECEIPT_SCHEMA, archive_evidence_v2,
                               archive_evidence_v2_from_v1, verify_evidence)
from .qnap_lifecycle import TIERS, classify_recording


PLAN_SCHEMA = "leo-tracker.storage-regime-v2-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.storage-regime-v2-receipt/v1"
CONFIRMATION = "MIGRATE-TO-EVIDENCE-V2"


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".next.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _capture_safe(shared_root: Path, capture: Path) -> bool:
    if capture.is_symlink():
        return False
    try:
        relative = capture.resolve(strict=True).relative_to(shared_root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return len(relative.parts) == 2 and relative.parts[0] == "captures"


def _active_jobs(shared_root: Path) -> set[str]:
    queue = shared_root / "staging" / "analysis-queue"
    result = set()
    for pattern in ("*.job", "*.running.*"):
        for marker in queue.glob(pattern):
            try:
                result.add(marker.read_text(encoding="utf-8").split("\t", 1)[0].strip())
            except OSError:
                continue
    return {name for name in result if name}


def _analysis_complete(shared_root: Path, name: str) -> bool:
    receipt = _json(shared_root / "reports" / "receipts" / f"{name}.json")
    return (receipt.get("schema") == "leo-tracker.analysis-receipt/v1" and
            receipt.get("status") == "success" and receipt.get("job") == name)


def _source_bytes(manifest: dict) -> int:
    return sum(int(item.get("bytes", 0)) for item in manifest.get("chunks", []))


def build_storage_regime_plan(shared_root: Path, archive_root: Path, *,
                              minimum_age_hours: float = 6,
                              scope: str = "all") -> dict:
    """Inventory raw captures that can be promoted to the production v2 regime."""
    if minimum_age_hours < 0:
        raise ValueError("minimum age must be non-negative")
    if scope not in {"all", "raw", "archive"}:
        raise ValueError("storage regime scope must be all, raw, or archive")
    shared_root = Path(shared_root).resolve(); archive_root = Path(archive_root).resolve()
    active = _active_jobs(shared_root); now = time.time(); entries = []
    captures = shared_root / "captures"
    raw_names = set()
    for capture in sorted(captures.iterdir() if captures.is_dir() else []):
        if not capture.is_dir():
            continue
        name = capture.name; raw_names.add(name)
        if scope == "archive":
            continue
        manifest_path = capture / "manifest.json"
        manifest = _json(manifest_path); tier, reasons, evidence = classify_recording(
            shared_root, name)
        status = "eligible"
        if not _capture_safe(shared_root, capture):
            status = "unsafe_shared_path"
        elif manifest.get("state") != "complete" or not manifest.get("chunks"):
            status = "capture_incomplete"
        elif name in active:
            status = "analysis_active"
        elif now - manifest_path.stat().st_mtime < minimum_age_hours * 3600:
            status = "minimum_age_not_met"
        elif not _analysis_complete(shared_root, name):
            status = "analysis_incomplete"
        elif tier is None:
            status = "classification_unavailable"
        elif tier == 5:
            status = "eligible_pinned_archive"
        entries.append({
            "recording_id": name, "capture_path": str(capture),
            "source_manifest_sha256": _sha256(manifest_path) if manifest else None,
            "source_bytes": _source_bytes(manifest), "tier": tier,
            "tier_name": TIERS.get(tier, "unclassified"),
            "classification_reasons": reasons, "evidence": evidence,
            "status": status,
        })
    v1_receipts = archive_root / "catalog" / "receipts"
    for receipt_path in sorted(v1_receipts.glob("*.json") if
                               v1_receipts.is_dir() and scope != "raw" else []):
        name = receipt_path.stem
        if name in raw_names:
            continue
        receipt = _json(receipt_path); tier, reasons, evidence = classify_recording(
            shared_root, name)
        status = "eligible_archive_only"
        if (receipt.get("schema") != "leo-tracker.evidence-archive-receipt/v1" or
                receipt.get("status") != "verified" or not receipt.get("source_verified")):
            status = "v1_archive_unverified"
        elif now - receipt_path.stat().st_mtime < minimum_age_hours * 3600:
            status = "minimum_age_not_met"
        elif not _analysis_complete(shared_root, name):
            status = "analysis_incomplete"
        elif tier is None:
            status = "classification_unavailable"
        elif tier == 5:
            status = "eligible_archive_only_pinned"
        entries.append({
            "recording_id": name, "capture_path": None,
            "source_manifest_sha256": receipt.get("source_manifest_sha256"),
            "source_bytes": int(receipt.get("summary", {}).get("source_bytes", 0) or 0),
            "tier": tier, "tier_name": TIERS.get(tier, "unclassified"),
            "classification_reasons": reasons, "evidence": evidence,
            "status": status,
        })
    # Free the bounded raw working set before spending bandwidth compacting
    # archive-only v1 bundles. Both paths are safe, but only the former returns
    # the large raw-IQ allocation the operator is actively trying to reclaim.
    archive_only_statuses = {"eligible_archive_only",
                             "eligible_archive_only_pinned"}
    entries.sort(key=lambda item: (
        item["status"] in archive_only_statuses,
        item["tier"] if item["tier"] is not None else 99,
        item["recording_id"],
    ))
    statuses: dict[str, int] = {}; tiers: dict[str, int] = {}
    for item in entries:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
        tiers[item["tier_name"]] = tiers.get(item["tier_name"], 0) + 1
    return {
        "schema": PLAN_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "shared_root": str(shared_root), "archive_root": str(archive_root),
        "configuration": {"minimum_age_hours": minimum_age_hours, "scope": scope},
        "summary": {
            "recording_count": len(entries), "status_counts": statuses,
            "tier_counts": tiers,
            "eligible_count": (statuses.get("eligible", 0) +
                               statuses.get("eligible_archive_only", 0) +
                               statuses.get("eligible_pinned_archive", 0) +
                               statuses.get("eligible_archive_only_pinned", 0)),
            "eligible_bytes": sum(item["source_bytes"] for item in entries
                                  if item["status"] in
                                  {"eligible", "eligible_archive_only",
                                   "eligible_pinned_archive",
                                   "eligible_archive_only_pinned"}),
        },
        "entries": entries,
    }


def _remove_v1_artifacts(shared_root: Path, archive_root: Path, name: str) -> dict:
    """Remove obsolete v1 bundle and verified same-volume report duplicates."""
    receipt_path = archive_root / "catalog" / "receipts" / f"{name}.json"
    receipt = _json(receipt_path); removed_bytes = 0
    preserved_derived = []; promoted_derived = []; discarded_derived = []
    for artifact in receipt.get("derived_artifacts", []):
        relative = Path(str(artifact.get("path", "")))
        archived = archive_root / relative
        if (relative.is_absolute() or ".." in relative.parts or
                relative.parts[:1] != ("derived",) or not archived.is_file()):
            continue
        live = shared_root / "reports" / Path(*relative.parts[1:])
        expected = artifact.get("sha256")
        if not expected or _sha256(archived) != expected:
            preserved_derived.append(str(relative))
        elif live.is_file():
            if _sha256(live) != expected:
                discarded_derived.append(str(relative))
            removed_bytes += archived.stat().st_size; archived.unlink()
        else:
            live.parent.mkdir(parents=True, exist_ok=True)
            os.replace(archived, live); promoted_derived.append(str(relative))
    bundle = archive_root / "evidence" / name
    if bundle.is_dir() and bundle.parent == archive_root / "evidence":
        removed_bytes += sum(path.stat().st_size for path in bundle.rglob("*")
                             if path.is_file())
        shutil.rmtree(bundle)
    # Older archive writers could leave a resumable `<recording>.partial`
    # sibling. Once production v2 has passed source comparison and replay, it
    # is neither authoritative nor useful and must not survive convergence.
    partial = archive_root / "evidence" / f"{name}.partial"
    if partial.is_dir() and partial.parent == archive_root / "evidence":
        removed_bytes += sum(path.stat().st_size for path in partial.rglob("*")
                             if path.is_file())
        shutil.rmtree(partial)
    for path in (archive_root / "catalog" / "plans" / f"{name}.json", receipt_path,
                 archive_root / "catalog" / "v2-shadow" / "references" / f"{name}.json",
                 archive_root / "catalog" / "v2-shadow" / "plans" / f"{name}.json",
                 archive_root / "catalog" / "v2-shadow" / "comparisons" / f"{name}.json"):
        if path.is_file():
            removed_bytes += path.stat().st_size; path.unlink()
    reports = shared_root / "reports"; preserved_outputs = []; promoted_outputs = []
    for completion in (reports / "runs").glob(f"*/{name}/completion.json"):
        value = _json(completion); references = {}
        output_dir = completion.parent / "outputs"
        for key, artifact in value.get("outputs", {}).items():
            live = Path(str(artifact.get("path", "")))
            duplicate = output_dir / f"{key}{live.suffix}"
            expected = artifact.get("sha256")
            try:
                live.resolve(strict=False).relative_to(reports.resolve(strict=True))
                live_is_safe = True
            except (FileNotFoundError, OSError, ValueError):
                live_is_safe = False
            if duplicate.is_file() and live_is_safe:
                if live.is_file():
                    removed_bytes += duplicate.stat().st_size; duplicate.unlink()
                elif expected and _sha256(duplicate) == expected:
                    live.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(duplicate, live); promoted_outputs.append(str(duplicate))
                else:
                    preserved_outputs.append(str(duplicate))
            references[key] = {**artifact, "path": str(live),
                               "storage": "authoritative-reference"}
        if output_dir.is_dir() and not any(output_dir.iterdir()):
            output_dir.rmdir()
        if references:
            value["versioned_outputs"] = references; _atomic_json(completion, value)
    return {"removed_bytes": removed_bytes,
            "preserved_derived_artifacts": preserved_derived,
            "promoted_derived_artifacts": promoted_derived,
            "discarded_obsolete_derived_artifacts": discarded_derived,
            "promoted_versioned_outputs": promoted_outputs,
            "preserved_versioned_outputs": preserved_outputs}


def apply_storage_regime_plan(plan: dict, *, confirmation: str,
                              limit: int | None = None) -> dict:
    """Archive, verify, retire v1, and remove old raw one recording at a time."""
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported storage regime plan")
    if confirmation != CONFIRMATION:
        raise ValueError("storage migration confirmation token is missing or incorrect")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    shared_root = Path(plan["shared_root"]).resolve()
    archive_root = Path(plan["archive_root"]).resolve()
    lock_path = shared_root / "reports" / "reclamation" / "storage-regime-v2.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close(); raise RuntimeError("another v2 storage migration is active") from exc
    completed = []; failures = []
    for item in (entry for entry in plan.get("entries", [])
                 if entry.get("status") in {"eligible", "eligible_archive_only",
                                            "eligible_pinned_archive",
                                            "eligible_archive_only_pinned"}):
        if limit is not None and len(completed) >= limit:
            break
        name = item["recording_id"]
        archive_only = item["status"] in {"eligible_archive_only",
                                           "eligible_archive_only_pinned"}
        raw_pinned = item["status"] == "eligible_pinned_archive"
        capture = None if archive_only else Path(item["capture_path"])
        try:
            tier, _, _ = classify_recording(shared_root, name)
            if tier != item["tier"] or tier is None or (tier == 5 and not raw_pinned and
                    item["status"] != "eligible_archive_only_pinned"):
                raise ValueError("recording classification changed or became protected")
            if archive_only:
                manifest_sha = str(item["source_manifest_sha256"])
                v2 = archive_evidence_v2_from_v1(
                    name, shared_root / "reports", archive_root)
            else:
                assert capture is not None
                if not _capture_safe(shared_root, capture):
                    raise ValueError("capture path failed deletion safety check")
                manifest_sha = _sha256(capture / "manifest.json")
                if manifest_sha != item["source_manifest_sha256"]:
                    raise ValueError("capture manifest changed after migration planning")
                v2 = archive_evidence_v2(capture, shared_root / "reports", archive_root)
            if (v2.get("schema") != V2_RECEIPT_SCHEMA or
                    v2.get("source_manifest_sha256") != manifest_sha or
                    not v2.get("source_verified") or
                    not v2.get("required_event_replay_valid")):
                raise ValueError("v2 archive receipt failed the deletion gate")
            bundle = archive_root / v2["bundle"]
            verification = verify_evidence(
                bundle, capture_path=None if archive_only else capture, write=False)
            if not verification["valid"] or (
                    not archive_only and not verification["source_verified"]):
                raise ValueError("v2 evidence failed final source comparison")
            receipt_path = (shared_root / "reports" / "reclamation" /
                            "storage-regime-v2" / f"{name}.json")
            prepared = {
                "schema": RECEIPT_SCHEMA, "recording_id": name,
                "status": "prepared", "prepared_utc": datetime.now(timezone.utc).isoformat(),
                "tier": tier, "tier_name": TIERS[tier],
                "source_bytes": item["source_bytes"],
                "source_manifest_sha256": manifest_sha,
                "v2_receipt": str(Path("catalog/v2/receipts") / f"{name}.json"),
                "v2_bundle_manifest_sha256": v2["bundle_manifest_sha256"],
                "archive_only": archive_only,
                "raw_pinned": raw_pinned,
            }
            _atomic_json(receipt_path, prepared)
            retired = _remove_v1_artifacts(shared_root, archive_root, name)
            if capture is not None and not raw_pinned:
                shutil.rmtree(capture)
            final = {**prepared, "status": "complete",
                     "completed_utc": datetime.now(timezone.utc).isoformat(),
                     "raw_absent_verified": capture is None or (
                         not raw_pinned and not capture.exists()),
                     "raw_preserved_by_pin": raw_pinned,
                     "v1_retired": True, **retired}
            _atomic_json(receipt_path, final); completed.append(final)
        except (OSError, ValueError, KeyError) as exc:
            failures.append({"recording_id": name, "error": str(exc)})
    return {
        "schema": "leo-tracker.storage-regime-v2-result/v1",
        "completed_count": len(completed),
        "source_bytes_migrated": sum(item["source_bytes"] for item in completed),
        "raw_removed_bytes": sum(item["source_bytes"] for item in completed
                                 if not item["archive_only"] and not item["raw_pinned"]),
        "v1_removed_bytes": sum(item["removed_bytes"] for item in completed),
        "failure_count": len(failures), "failures": failures,
        "receipts": [str(shared_root / "reports" / "reclamation" /
                         "storage-regime-v2" / f"{item['recording_id']}.json")
                     for item in completed],
    }
