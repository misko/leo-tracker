"""Audit and retire stale transient artifacts in the shared LEO roots."""
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
import uuid

from .qnap_lifecycle import _archive_gate, qnap_storage_mutation_lock


PLAN_SCHEMA = "leo-tracker.shared-transient-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.shared-transient-receipt/v1"
CONFIRMATION = "DELETE-STALE-LEO-TRANSIENTS"
_ATOMIC_NEXT = re.compile(r"^[A-Za-z0-9._-]+\.next\.\d+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path: Path) -> tuple[int, str]:
    if path.is_symlink():
        raise ValueError("symlinks are not safe transient artifacts")
    if path.is_file():
        return path.stat().st_size, _sha256(path)
    if not path.is_dir():
        raise ValueError("transient artifact must be a file or directory")
    digest = hashlib.sha256(); bytes_ = 0
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink() or not (child.is_file() or child.is_dir()):
            raise ValueError(f"unsafe transient child: {child}")
        digest.update(("d\0" if child.is_dir() else "f\0").encode())
        digest.update(relative.encode()); digest.update(b"\0")
        if child.is_file():
            size = child.stat().st_size; bytes_ += size
            digest.update(str(size).encode()); digest.update(b"\0")
            digest.update(_sha256(child).encode()); digest.update(b"\0")
    return bytes_, digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _capture_authority(path: Path) -> dict | None:
    manifest = path / "manifest.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (value.get("state") not in {"complete", "interrupted"} or
            not value.get("chunks")):
        return None
    return {"path": str(manifest), "sha256": _sha256(manifest)}


def inventory_stale_shared_transients(shared_root: Path, archive_root: Path, *,
                                      minimum_age_s: float,
                                      sample_limit: int = 20) -> dict:
    if minimum_age_s < 0:
        raise ValueError("minimum age must be non-negative")
    shared = Path(shared_root).resolve(); archive = Path(archive_root).resolve()
    cutoff = time.time() - minimum_age_s; paths = []
    candidates = [
        *((shared / "staging/analysis-queue").glob("*.next.*")
          if (shared / "staging/analysis-queue").is_dir() else []),
        *((shared / "staging/incoming").glob("*.partial")
          if (shared / "staging/incoming").is_dir() else []),
        *((archive / "evidence-v2").glob("*.partial")
          if (archive / "evidence-v2").is_dir() else []),
    ]
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                paths.append(path)
        except OSError:
            paths.append(path)
    return {"count": len(paths), "samples": [str(path) for path in paths[:sample_limit]]}


def build_shared_transient_plan(shared_root: Path, archive_root: Path, *,
                                minimum_age_s: float = 6 * 3600) -> dict:
    if minimum_age_s < 0:
        raise ValueError("minimum age must be non-negative")
    shared = Path(shared_root).resolve(); archive = Path(archive_root).resolve()
    cutoff = time.time() - minimum_age_s; entries = []

    queue = shared / "staging/analysis-queue"
    for path in sorted(queue.glob("*.next.*")) if queue.is_dir() else []:
        status = "minimum_age_not_met"
        try:
            mtime = path.stat().st_mtime
            if (path.is_symlink() or not path.is_file() or
                    not _ATOMIC_NEXT.fullmatch(path.name)):
                status = "unsafe_transient"
            elif mtime < cutoff:
                status = "eligible"
            bytes_, digest = (_identity(path) if status != "unsafe_transient"
                              else (0, None))
            path.stat()
        except FileNotFoundError:
            continue
        entries.append({"kind": "stale_atomic_next", "path": str(path),
            "relative_path": str(path.relative_to(shared)), "bytes": bytes_,
            "sha256": digest, "status": status, "authority": None})

    incoming = shared / "staging/incoming"
    for path in sorted(incoming.glob("*.partial")) if incoming.is_dir() else []:
        status = "minimum_age_not_met"; authority = None
        try:
            mtime = path.stat().st_mtime
            bytes_, digest = _identity(path)
            path.stat()
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            bytes_, digest, status = 0, None, "unsafe_transient"
        if status != "unsafe_transient" and mtime < cutoff:
            authority = _capture_authority(shared / "captures" / path.name.removesuffix(".partial"))
            status = "eligible" if authority else "resumable_upload_partial"
        entries.append({"kind": "incoming_upload_partial", "path": str(path),
            "relative_path": str(path.relative_to(shared)), "bytes": bytes_,
            "sha256": digest, "status": status, "authority": authority})

    evidence = archive / "evidence-v2"
    for path in sorted(evidence.glob("*.partial")) if evidence.is_dir() else []:
        status = "minimum_age_not_met"; authority = None
        try:
            mtime = path.stat().st_mtime
            bytes_, digest = _identity(path)
            path.stat()
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            bytes_, digest, status = 0, None, "unsafe_transient"
        if status != "unsafe_transient" and mtime < cutoff:
            name = path.name.removesuffix(".partial")
            receipt_path = archive / "catalog/v2/receipts" / f"{name}.json"
            try: receipt = json.loads(receipt_path.read_text())
            except (OSError, ValueError): receipt = {}
            valid, _, _ = _archive_gate(
                shared, archive, name, str(receipt.get("source_manifest_sha256", "")))
            if valid:
                authority = {"path": str(receipt_path), "sha256": _sha256(receipt_path)}
                status = "eligible"
            else:
                status = "incomplete_archive_partial"
        entries.append({"kind": "evidence_v2_partial", "path": str(path),
            "relative_path": str(path.relative_to(archive)), "bytes": bytes_,
            "sha256": digest, "status": status, "authority": authority})

    counts: dict[str, int] = {}
    for item in entries: counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"schema": PLAN_SCHEMA, "created_utc": _now(),
        "shared_root": str(shared), "archive_root": str(archive),
        "minimum_age_s": minimum_age_s, "entries": entries,
        "summary": {"artifact_count": len(entries), "status_counts": counts,
            "eligible_count": counts.get("eligible", 0),
            "eligible_bytes": sum(item["bytes"] for item in entries
                                  if item["status"] == "eligible"),
            "unresolved_count": sum(count for status, count in counts.items()
                if status not in {"eligible", "minimum_age_not_met"})}}


def apply_shared_transient_plan(plan: dict, *, confirmation: str,
                                mutation_lock_held: bool = False) -> dict:
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported shared transient plan")
    if confirmation != CONFIRMATION:
        raise ValueError("shared transient confirmation token is missing or incorrect")
    shared = Path(plan["shared_root"]).resolve(); archive = Path(plan["archive_root"]).resolve()
    if not mutation_lock_held:
        with qnap_storage_mutation_lock(shared):
            return apply_shared_transient_plan(
                plan, confirmation=confirmation, mutation_lock_held=True)
    receipt_path = shared / "reports/reclamation/shared-transients.json"
    removed = []; deferred = []
    for item in plan.get("entries", []):
        if item.get("status") != "eligible": continue
        base = archive if item["kind"] == "evidence_v2_partial" else shared
        path = Path(item["path"])
        try:
            relative = path.resolve(strict=True).relative_to(base)
            if path.is_symlink() or relative.as_posix() != item["relative_path"]:
                raise ValueError("unsafe or changed transient path")
            bytes_, digest = _identity(path)
            if bytes_ != item["bytes"] or digest != item["sha256"]:
                raise ValueError("transient changed after planning")
            authority = item.get("authority")
            if authority:
                target = Path(authority["path"])
                if not target.is_file() or _sha256(target) != authority["sha256"]:
                    raise ValueError("transient authority changed after planning")
            removed.append({k: item.get(k) for k in
                ("kind", "relative_path", "bytes", "sha256", "authority")})
        except Exception as exc:
            deferred.append({"relative_path": item.get("relative_path"),
                "reason": f"{type(exc).__name__}: {exc}"})
    prepared = {"schema": RECEIPT_SCHEMA, "status": "prepared",
        "prepared_utc": _now(), "shared_root": str(shared),
        "archive_root": str(archive), "removed": removed, "deferred": deferred}
    _atomic_json(receipt_path, prepared)
    for item in removed:
        base = archive if item["kind"] == "evidence_v2_partial" else shared
        path = base / item["relative_path"]
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    result = {**prepared, "status": "complete" if not deferred else "deferred",
        "completed_utc": _now(), "removed_count": len(removed),
        "removed_bytes": sum(item["bytes"] for item in removed)}
    _atomic_json(receipt_path, result)
    return result
