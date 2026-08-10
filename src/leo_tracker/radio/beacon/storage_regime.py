"""Transactional migration from raw/v1 storage to tiered v2 evidence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from .evidence_archive import (V2_RECEIPT_SCHEMA, archive_evidence_v2,
                               archive_evidence_v2_from_v1, verify_evidence)
from .qnap_lifecycle import (_archive_gate, TIERS, classify_recording,
                             qnap_storage_mutation_lock)


PLAN_SCHEMA = "leo-tracker.storage-regime-v2-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.storage-regime-v2-receipt/v1"
CONFIRMATION = "MIGRATE-TO-EVIDENCE-V2"
PRIMARY_LEASE_SCHEMA = "leo-tracker.storage-regime-v2-primary/v1"


def storage_primary_lease_is_fresh(path: Path, *, now: float | None = None,
                                   maximum_age_s: float = 120) -> bool:
    """Return whether a remote primary currently owns storage convergence."""
    if maximum_age_s <= 0:
        raise ValueError("maximum primary lease age must be positive")
    value = _json(Path(path))
    try:
        updated = float(value["updated_epoch_s"])
    except (KeyError, TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    return bool(value.get("schema") == PRIMARY_LEASE_SCHEMA and
                value.get("state") == "running" and
                0 <= current - updated <= maximum_age_s)


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


def _verified_v1_fallback_reports(archive_root: Path, name: str) -> Path | None:
    """Return a read-only report fallback only when its v1 hashes are intact."""
    receipt = _json(archive_root / "catalog" / "receipts" / f"{name}.json")
    if (receipt.get("schema") != "leo-tracker.evidence-archive-receipt/v1" or
            receipt.get("status") != "verified" or
            not receipt.get("source_verified")):
        return None
    verified_analysis = False
    for artifact in receipt.get("derived_artifacts", []):
        relative = Path(str(artifact.get("path", "")))
        if relative.suffix != ".json":
            continue
        archived = archive_root / relative
        expected = artifact.get("sha256")
        if (relative.is_absolute() or ".." in relative.parts or
                relative.parts[:1] != ("derived",) or archived.is_symlink() or
                not archived.is_file() or not expected or
                _sha256(archived) != expected):
            return None
        if relative == Path("derived") / f"{name}.json":
            verified_analysis = True
    return archive_root / "derived" if verified_analysis else None


def _classify_recording(shared_root: Path, archive_root: Path, name: str
                        ) -> tuple[int | None, list[str], dict]:
    tier, reasons, evidence = classify_recording(shared_root, name)
    if tier is not None:
        return tier, reasons, evidence
    fallback = _verified_v1_fallback_reports(archive_root, name)
    if fallback is None:
        return tier, reasons, evidence
    tier, reasons, evidence = classify_recording(
        shared_root, name, fallback_reports=fallback)
    if tier is not None:
        reasons = ["verified_v1_derived_fallback", *reasons]
    return tier, reasons, evidence


def _analysis_complete(shared_root: Path, name: str,
                       archive_root: Path | None = None) -> bool:
    receipt = _json(shared_root / "reports" / "receipts" / f"{name}.json")
    if (receipt.get("schema") == "leo-tracker.analysis-receipt/v1" and
            receipt.get("status") == "success" and receipt.get("job") == name):
        return True
    # Older workers could finish and atomically publish the authoritative
    # pipeline completion, then fail while refreshing the convenience receipt
    # because its versioned-output layout had changed. The analysis protocol
    # itself treats this completion as done during backfill/audit, so retention
    # must not strand the same recording forever by requiring the secondary
    # receipt. Production-v2 replay remains the stronger raw-deletion gate.
    runs = shared_root / "reports" / "runs"
    for completion in runs.glob(f"*/{name}/completion.json"):
        value = _json(completion)
        if (value.get("schema") == "leo-tracker.analysis-receipt/v1" and
                value.get("status") == "success" and value.get("job") == name and
                isinstance(value.get("outputs"), dict) and value["outputs"]):
            return True
    if (archive_root is not None and
            _verified_v1_fallback_reports(archive_root, name) is not None):
        return True
    return False


def _materialize_verified_v1_derivatives(shared_root: Path, archive_root: Path,
                                         name: str) -> list[str]:
    """Restore hash-verified v1 reports before transitive v2 planning.

    Copy rather than move: if later replay validation fails, the v1 receipt and
    every path it references remain valid. The ordinary retirement transaction
    removes the now-redundant archive copies only after v2 succeeds.
    """
    if _verified_v1_fallback_reports(archive_root, name) is None:
        return []
    receipt = _json(archive_root / "catalog" / "receipts" / f"{name}.json")
    verified: list[tuple[Path, Path, str]] = []
    for artifact in receipt.get("derived_artifacts", []):
        relative = Path(str(artifact.get("path", "")))
        archived = archive_root / relative; expected = str(artifact.get("sha256", ""))
        if (relative.is_absolute() or ".." in relative.parts or
                relative.parts[:1] != ("derived",) or archived.is_symlink() or
                not archived.is_file() or not expected or
                _sha256(archived) != expected):
            raise ValueError("verified v1 derivative is missing or hash-invalid")
        verified.append((archived, shared_root / "reports" /
                         Path(*relative.parts[1:]), expected))
    restored = []
    for archived, live, expected in verified:
        if live.is_file():
            continue
        live.parent.mkdir(parents=True, exist_ok=True)
        temporary = live.with_name(live.name + f".next.{os.getpid()}")
        with archived.open("rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, length=8 << 20)
            target.flush(); os.fsync(target.fileno())
        if _sha256(temporary) != expected:
            temporary.unlink(missing_ok=True)
            raise ValueError("verified v1 derivative copy failed verification")
        os.replace(temporary, live); restored.append(str(live))
    return restored


def _source_bytes(manifest: dict) -> int:
    return sum(int(item.get("bytes", 0)) for item in manifest.get("chunks", []))


def _summary(entries: list[dict]) -> dict:
    statuses: dict[str, int] = {}; tiers: dict[str, int] = {}
    eligible = {"eligible", "eligible_archive_only", "eligible_pinned_archive",
                "eligible_archive_only_pinned"}
    for item in entries:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
        tiers[item["tier_name"]] = tiers.get(item["tier_name"], 0) + 1
    return {"recording_count": len(entries), "status_counts": statuses,
        "tier_counts": tiers,
        "eligible_count": sum(item["status"] in eligible for item in entries),
        "eligible_bytes": sum(item["source_bytes"] for item in entries
                              if item["status"] in eligible)}


def build_storage_regime_plan(shared_root: Path, archive_root: Path, *,
                              minimum_age_hours: float = 6,
                              scope: str = "all",
                              eligible_limit: int | None = None,
                              auto_archive_slots: int = 1) -> dict:
    """Inventory raw captures that can be promoted to the production v2 regime."""
    if minimum_age_hours < 0:
        raise ValueError("minimum age must be non-negative")
    if scope not in {"all", "auto", "raw", "archive"}:
        raise ValueError("storage regime scope must be all, auto, raw, or archive")
    if eligible_limit is not None and eligible_limit < 1:
        raise ValueError("eligible planning limit must be positive")
    if auto_archive_slots < 1:
        raise ValueError("automatic archive slots must be positive")
    if scope == "all" and eligible_limit is not None:
        raise ValueError("eligible planning limit requires auto, raw, or archive scope")
    if scope == "auto":
        # A bounded raw-only plan can never reach archive-only V1 while capture
        # continuously creates more age-eligible raw. Reserve the first apply
        # slot for one archive-only record, then devote the rest to raw. The
        # unbounded audit behavior remains raw-first and only falls back after
        # raw is exhausted.
        reserved = (min(auto_archive_slots, eligible_limit - 1)
                    if eligible_limit is not None and eligible_limit > 1 else 0)
        raw_limit = (max(1, eligible_limit - reserved)
                     if eligible_limit is not None else None)
        raw = build_storage_regime_plan(
            shared_root, archive_root, minimum_age_hours=minimum_age_hours,
            scope="raw", eligible_limit=raw_limit)
        if raw["summary"]["eligible_count"]:
            if reserved:
                archive = build_storage_regime_plan(
                    shared_root, archive_root,
                    minimum_age_hours=minimum_age_hours,
                    scope="archive", eligible_limit=reserved)
                archive_eligible = [item for item in archive["entries"]
                    if item["status"] in {"eligible_archive_only",
                                          "eligible_archive_only_pinned"}]
                if archive_eligible:
                    selected = archive_eligible[:reserved]
                    selected_ids = {item["recording_id"] for item in selected}
                    entries = [*selected, *raw["entries"],
                        *(item for item in archive["entries"]
                          if item["recording_id"] not in selected_ids)]
                    return {**raw, "entries": entries,
                        "configuration": {**raw["configuration"],
                            "scope": "auto",
                            "active_scope": "raw_with_archive_fairness",
                            "archive_reserved_slots": len(selected),
                            "inventory_complete": False},
                        "summary": _summary(entries)}
            raw["configuration"] = {**raw["configuration"], "scope": "auto",
                                    "active_scope": "raw"}
            return raw
        archive = build_storage_regime_plan(
            shared_root, archive_root, minimum_age_hours=minimum_age_hours,
            scope="archive", eligible_limit=eligible_limit)
        archive["configuration"] = {**archive["configuration"], "scope": "auto",
                                    "active_scope": "archive"}
        return archive
    shared_root = Path(shared_root).resolve(); archive_root = Path(archive_root).resolve()
    active = _active_jobs(shared_root); now = time.time(); entries = []
    eligible_seen = 0; inventory_complete = True
    captures = shared_root / "captures"
    raw_names = set()
    for capture in sorted(captures.iterdir() if captures.is_dir() else []):
        if not capture.is_dir():
            continue
        name = capture.name; raw_names.add(name)
        if scope == "archive":
            continue
        manifest_path = capture / "manifest.json"
        manifest = _json(manifest_path); tier, reasons, evidence = _classify_recording(
            shared_root, archive_root, name)
        manifest_sha = _sha256(manifest_path) if manifest else None
        archive_valid, _, archived_receipt = _archive_gate(
            shared_root, archive_root, name, str(manifest_sha or ""))
        if tier is None and archive_valid:
            try:
                tier = int(archived_receipt["evidence_tier"])
                reasons = ["verified_v2_classification_fallback", *reasons]
                evidence = {**evidence, "verified_v2_receipt": True}
            except (KeyError, TypeError, ValueError):
                tier = None
        status = "eligible"
        if not _capture_safe(shared_root, capture):
            status = "unsafe_shared_path"
        elif (manifest.get("state") not in {"complete", "interrupted"} or
              not manifest.get("chunks")):
            status = "capture_incomplete"
        elif name in active:
            status = "analysis_active"
        elif now - manifest_path.stat().st_mtime < minimum_age_hours * 3600:
            status = "minimum_age_not_met"
        elif (not archive_valid and
              not _analysis_complete(shared_root, name, archive_root)):
            status = "analysis_incomplete"
        elif tier is None:
            status = "classification_unavailable"
        elif tier == 5:
            migration = _json(shared_root / "reports" / "reclamation" /
                              "storage-regime-v2" / f"{name}.json")
            archive_valid, _, _ = _archive_gate(
                shared_root, archive_root, name, str(manifest_sha))
            if (archive_valid and migration.get("schema") == RECEIPT_SCHEMA and
                    migration.get("status") == "complete" and
                    migration.get("source_manifest_sha256") == manifest_sha and
                    migration.get("raw_pinned") is True and
                    migration.get("raw_preserved_by_pin") is True and
                    migration.get("v1_retired") is True):
                status = "protected_pinned_current"
            else:
                status = "eligible_pinned_archive"
        entries.append({
            "recording_id": name, "capture_path": str(capture),
            "source_manifest_sha256": manifest_sha,
            "source_bytes": _source_bytes(manifest), "tier": tier,
            "tier_name": TIERS.get(tier, "unclassified"),
            "classification_reasons": reasons, "evidence": evidence,
            "status": status,
        })
        if status in {"eligible", "eligible_pinned_archive"}:
            eligible_seen += 1
            if eligible_limit is not None and eligible_seen >= eligible_limit:
                inventory_complete = False
                break
    v1_receipts = archive_root / "catalog" / "receipts"
    for receipt_path in sorted(v1_receipts.glob("*.json") if
                               v1_receipts.is_dir() and scope != "raw" else []):
        name = receipt_path.stem
        if name in raw_names:
            continue
        receipt = _json(receipt_path); tier, reasons, evidence = _classify_recording(
            shared_root, archive_root, name)
        status = "eligible_archive_only"
        if (receipt.get("schema") != "leo-tracker.evidence-archive-receipt/v1" or
                receipt.get("status") != "verified" or not receipt.get("source_verified")):
            status = "v1_archive_unverified"
        elif now - receipt_path.stat().st_mtime < minimum_age_hours * 3600:
            status = "minimum_age_not_met"
        elif not _analysis_complete(shared_root, name, archive_root):
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
        if status in {"eligible_archive_only", "eligible_archive_only_pinned"}:
            eligible_seen += 1
            if eligible_limit is not None and eligible_seen >= eligible_limit:
                inventory_complete = False
                break
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
    return {
        "schema": PLAN_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "shared_root": str(shared_root), "archive_root": str(archive_root),
        "configuration": {"minimum_age_hours": minimum_age_hours, "scope": scope,
                          "eligible_limit": eligible_limit,
                          "inventory_complete": inventory_complete},
        "summary": _summary(entries),
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


def _migrate_storage_item(item: dict, shared_root: Path, archive_root: Path) -> dict:
    """Execute one independent deletion-last storage transaction."""
    name = item["recording_id"]
    archive_only = item["status"] in {"eligible_archive_only",
                                       "eligible_archive_only_pinned"}
    raw_pinned = item["status"] == "eligible_pinned_archive"
    capture = None if archive_only else Path(item["capture_path"])
    restored = _materialize_verified_v1_derivatives(
        shared_root, archive_root, name)
    tier, _, _ = _classify_recording(shared_root, archive_root, name)
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
        "archive_only": archive_only, "raw_pinned": raw_pinned,
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
             "v1_retired": True,
             "restored_v1_derived_artifacts": restored, **retired}
    _atomic_json(receipt_path, final)
    return final


def apply_storage_regime_plan(plan: dict, *, confirmation: str,
                              limit: int | None = None,
                              workers: int = 1,
                              mutation_lock_held: bool = False) -> dict:
    """Run independent verified transactions, optionally with bounded concurrency."""
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported storage regime plan")
    if confirmation != CONFIRMATION:
        raise ValueError("storage migration confirmation token is missing or incorrect")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if workers < 1:
        raise ValueError("workers must be positive")
    shared_root = Path(plan["shared_root"]).resolve()
    archive_root = Path(plan["archive_root"]).resolve()
    if not mutation_lock_held:
        with qnap_storage_mutation_lock(shared_root):
            return apply_storage_regime_plan(
                plan, confirmation=confirmation, limit=limit,
                workers=workers, mutation_lock_held=True)
    items = [entry for entry in plan.get("entries", [])
             if entry.get("status") in {"eligible", "eligible_archive_only",
                                        "eligible_pinned_archive",
                                        "eligible_archive_only_pinned"}]
    if limit is not None:
        items = items[:limit]

    def attempt(item: dict) -> tuple[dict | None, dict | None]:
        try:
            return _migrate_storage_item(item, shared_root, archive_root), None
        except (OSError, ValueError, KeyError) as exc:
            return None, {"recording_id": item["recording_id"], "error": str(exc)}
    completed = []; failures = []
    if items:
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
            for final, failure in executor.map(attempt, items):
                if final is not None:
                    completed.append(final)
                if failure is not None:
                    failures.append(failure)
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
