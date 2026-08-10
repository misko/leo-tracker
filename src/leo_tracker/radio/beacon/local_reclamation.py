"""Receipt-driven reclamation of acquisition-host raw IQ duplicates.

The shared QNAP copy remains complete.  This module never deletes shared raw
IQ or cropped evidence; it only removes an exact, verified recording directory
below the configured local acquisition root after successful Kalman analysis.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from .qnap_lifecycle import _archive_gate


PLAN_SCHEMA = "leo-tracker.local-reclamation-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.local-reclamation-receipt/v1"
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _recordings(local_root: Path) -> list[tuple[str, Path, str]]:
    rows: list[tuple[str, Path, str]] = []
    for store, kind in (("captures", "capture"), ("quarantine", "quarantine")):
        for manifest in sorted((local_root / store).glob("*/manifest.json")):
            rows.append((manifest.parent.name, manifest.parent, kind))
    for manifest in sorted((local_root / "hop-sessions").glob("*/*/manifest.json")):
        session = manifest.parents[1].name
        rows.append((f"{session}-{manifest.parent.name}", manifest.parent, "hop"))
    return rows


def _safe_local_path(local_root: Path, path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        relative = path.resolve(strict=True).relative_to(local_root.resolve(strict=True))
    except (FileNotFoundError, ValueError, OSError):
        return False
    parts = relative.parts
    return ((len(parts) == 2 and parts[0] in {"captures", "quarantine"}) or
            (len(parts) == 3 and parts[0] == "hop-sessions"))


def _verify_empty_terminal(path: Path) -> tuple[bool, str, str | None]:
    """Prove that an interrupted recording contains metadata but no IQ."""
    manifest_path = path / "manifest.json"
    manifest = _json(manifest_path)
    try:
        children = list(path.iterdir())
    except OSError:
        return False, "local_source_missing", None
    if manifest.get("state") != "interrupted" or manifest.get("chunks"):
        return False, "local_capture_incomplete", None
    if any(item.name != "manifest.json" or not item.is_file() for item in children):
        return False, "unmanifested_local_payload", None
    return True, "eligible", _sha256(manifest_path)


def _active(local_root: Path, shared_root: Path, name: str) -> bool:
    local_queue = local_root / "staging" / "analysis-queue"
    shared_queue = shared_root / "staging" / "analysis-queue"
    for root in (local_queue, shared_queue):
        for pattern in ("*.job", "*.exporting.*", "*.running.*"):
            for marker in root.glob(pattern):
                try:
                    if name in marker.name or name in marker.read_text(encoding="utf-8"):
                        return True
                except OSError:
                    return True
    return (shared_root / "staging" / "incoming" / f"{name}.partial").exists()


def _verify_shared(local_manifest_path: Path, shared_capture: Path, *,
                   verify_sha256: bool) -> tuple[bool, str, int, str | None]:
    local_manifest = _json(local_manifest_path)
    local_sha = _sha256(local_manifest_path) if local_manifest else None
    shared_manifest_path = shared_capture / "manifest.json"
    shared_manifest = _json(shared_manifest_path)
    if not shared_manifest:
        return False, "missing_qnap_copy", 0, local_sha
    if local_sha is None:
        return False, "local_manifest_invalid", 0, None
    if _sha256(shared_manifest_path) != local_sha:
        return False, "qnap_manifest_mismatch", 0, local_sha
    chunks = local_manifest.get("chunks", [])
    if not chunks:
        return False, "manifest_has_no_chunks", 0, local_sha
    total = 0
    for chunk in chunks:
        relative = chunk.get("path")
        if not isinstance(relative, str) or Path(relative).name != relative:
            return False, "unsafe_chunk_path", total, local_sha
        target = shared_capture / relative
        try:
            size = target.stat().st_size
        except OSError:
            return False, "missing_qnap_chunk", total, local_sha
        expected_size = int(chunk.get("bytes", -1))
        if size != expected_size:
            return False, "qnap_chunk_size_mismatch", total, local_sha
        if verify_sha256 and _sha256(target) != chunk.get("sha256"):
            return False, "qnap_chunk_sha256_mismatch", total, local_sha
        total += expected_size
    return True, "eligible", total, local_sha


def _verify_durable(local_manifest_path: Path, shared_root: Path,
                    archive_root: Path | None, name: str, *,
                    verify_sha256: bool) -> tuple[bool, str, int, str | None, str | None]:
    """Accept either an exact QNAP raw duplicate or production-v2 evidence."""
    valid, reason, source_bytes, manifest_sha = _verify_shared(
        local_manifest_path, shared_root / "captures" / name,
        verify_sha256=verify_sha256)
    if valid:
        return True, reason, source_bytes, manifest_sha, "qnap_raw"
    if archive_root is None or not manifest_sha:
        return False, reason, source_bytes, manifest_sha, None
    archive_valid, archive_reason, _ = _archive_gate(
        shared_root, archive_root, name, manifest_sha)
    if not archive_valid:
        return False, archive_reason, source_bytes, manifest_sha, None
    manifest = _json(local_manifest_path)
    source_bytes = sum(int(chunk.get("bytes", 0))
                       for chunk in manifest.get("chunks", []))
    return True, "eligible", source_bytes, manifest_sha, "evidence_v2"


def _verify_analysis(shared_root: Path, name: str,
                     pipeline_id: str | None) -> tuple[bool, str, dict]:
    receipt = _json(shared_root / "reports" / "receipts" / f"{name}.json")
    if (receipt.get("schema") != "leo-tracker.analysis-receipt/v1" or
            receipt.get("status") != "success" or receipt.get("job") != name):
        return False, "analysis_incomplete", receipt
    if pipeline_id and receipt.get("pipeline_id") != pipeline_id:
        return False, "analysis_pipeline_mismatch", receipt
    analysis = receipt.get("outputs", {}).get("analysis", {})
    output = Path(str(analysis.get("path", "")))
    if not output.is_file() or output.stat().st_size != int(analysis.get("bytes", -1)):
        return False, "analysis_output_missing", receipt
    return True, "eligible", receipt


def build_reclamation_plan(local_root: Path, shared_root: Path, *,
                           archive_root: Path | None = None,
                           verify_sha256: bool = False,
                           minimum_age_s: float = 300,
                           pipeline_id: str | None = None) -> dict:
    """Return a deterministic plan; no filesystem object is removed."""
    local_root, shared_root = Path(local_root).resolve(), Path(shared_root).resolve()
    archive_root = Path(archive_root).resolve() if archive_root is not None else None
    now = time.time(); entries = []
    for name, local_path, kind in _recordings(local_root):
        reason = "eligible"; source_bytes = 0; manifest_sha = None
        durable_copy = None
        manifest_path = local_path / "manifest.json"
        manifest = _json(manifest_path)
        if not _NAME.fullmatch(name) or not _safe_local_path(local_root, local_path):
            reason = "unsafe_local_path"
        elif now - manifest_path.stat().st_mtime < minimum_age_s:
            reason = "minimum_age_not_met"
        elif _active(local_root, shared_root, name):
            reason = "active_or_partial"
        elif manifest.get("state") == "interrupted" and not manifest.get("chunks"):
            valid, reason, manifest_sha = _verify_empty_terminal(local_path)
            if valid:
                durable_copy = "empty_terminal"
        elif (manifest.get("state") not in {"complete", "interrupted"} or
              not manifest.get("chunks")):
            reason = "local_capture_incomplete"
        else:
            valid, reason, source_bytes, manifest_sha, durable_copy = _verify_durable(
                manifest_path, shared_root, archive_root, name,
                verify_sha256=verify_sha256)
            if valid and durable_copy != "evidence_v2":
                valid, reason, _ = _verify_analysis(shared_root, name, pipeline_id)
        entries.append({"recording_id": name, "kind": kind,
            "local_path": str(local_path),
            "shared_path": str(shared_root / "captures" / name),
            "source_manifest_sha256": manifest_sha,
            "source_bytes": source_bytes, "status": reason,
            "durable_copy": durable_copy,
            "verification": "sha256" if verify_sha256 else "manifest_and_size"})
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {"schema": PLAN_SCHEMA, "created_utc": _utc_now(),
        "local_root": str(local_root), "shared_root": str(shared_root),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "configuration": {"verify_sha256": verify_sha256,
            "minimum_age_s": minimum_age_s, "pipeline_id": pipeline_id},
        "summary": {"recording_count": len(entries), "status_counts": counts,
            "eligible_count": counts.get("eligible", 0),
            "eligible_bytes": sum(item["source_bytes"] for item in entries
                                  if item["status"] == "eligible")},
        "entries": entries}


def apply_reclamation_plan(plan: dict, *, limit: int | None = None) -> dict:
    """Revalidate and remove eligible local copies, publishing QNAP receipts."""
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported reclamation plan schema")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    local_root = Path(plan["local_root"]).resolve()
    shared_root = Path(plan["shared_root"]).resolve()
    archive_value = plan.get("archive_root")
    archive_root = Path(archive_value).resolve() if archive_value else None
    config = plan.get("configuration", {})
    lock_path = local_root / "staging" / "local-reclamation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    removed = []; deferred = []
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another local reclaimer is active") from exc
        eligible = [item for item in plan.get("entries", [])
                    if item.get("status") == "eligible"]
        eligible.sort(key=lambda item: item["recording_id"])
        for original in eligible[:limit]:
            name = original["recording_id"]
            local_path = Path(original["local_path"])
            receipt_path = (shared_root / "reports" / "reclamation" / "local" /
                            f"{name}.json")
            if not local_path.exists():
                previous = _json(receipt_path)
                if previous.get("status") == "removed":
                    removed.append(previous); continue
                deferred.append({"recording_id": name,
                                 "reason": "local_source_missing_without_receipt"})
                continue
            if not _safe_local_path(local_root, local_path):
                deferred.append({"recording_id": name, "reason": "unsafe_local_path"})
                continue
            manifest_path = local_path / "manifest.json"
            if original.get("durable_copy") == "empty_terminal":
                valid, reason, manifest_sha = _verify_empty_terminal(local_path)
                source_bytes, durable_copy, analysis = 0, "empty_terminal", {}
            else:
                valid, reason, source_bytes, manifest_sha, durable_copy = _verify_durable(
                    manifest_path, shared_root, archive_root, name,
                    verify_sha256=bool(config.get("verify_sha256")))
                analysis = {}
            if valid and durable_copy != "evidence_v2" and durable_copy != "empty_terminal":
                valid, reason, analysis = _verify_analysis(
                    shared_root, name, config.get("pipeline_id"))
            if valid and _active(local_root, shared_root, name):
                valid, reason = False, "active_or_partial"
            if not valid:
                deferred.append({"recording_id": name, "reason": reason}); continue
            prepared = {"schema": RECEIPT_SCHEMA, "recording_id": name,
                "status": "prepared", "prepared_utc": _utc_now(),
                "local_path": str(local_path),
                "shared_path": str(shared_root / "captures" / name),
                "source_manifest_sha256": manifest_sha,
                "source_bytes": source_bytes,
                "durable_copy": durable_copy,
                "verification": original.get("verification"),
                "analysis_pipeline_id": analysis.get("pipeline_id"),
                "analysis_completed_utc": analysis.get("completed_utc")}
            _atomic_json(receipt_path, prepared)
            shutil.rmtree(local_path)
            completed = {**prepared, "status": "removed", "removed_utc": _utc_now(),
                         "local_absent_verified": not local_path.exists()}
            _atomic_json(receipt_path, completed); removed.append(completed)
            if original.get("kind") == "hop":
                parent = local_path.parent
                try:
                    parent.rmdir()
                except OSError:
                    pass
    return {"schema": "leo-tracker.local-reclamation-result/v1",
        "completed_utc": _utc_now(), "removed_count": len(removed),
        "removed_bytes": sum(int(item.get("source_bytes", 0)) for item in removed),
        "deferred_count": len(deferred), "deferred": deferred,
        "receipts": [str(shared_root / "reports" / "reclamation" / "local" /
                         f"{item['recording_id']}.json") for item in removed]}
