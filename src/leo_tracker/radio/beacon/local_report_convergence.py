"""Converge legacy acquisition-host reports into the shared authority."""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .qnap_lifecycle import _archive_gate


PLAN_SCHEMA = "leo-tracker.local-report-convergence-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.local-report-convergence-receipt/v1"
# The live watcher reads or updates these paths. They are operational state,
# not historical analysis duplicates, and remain on the acquisition host.
OPERATIONAL_PREFIXES = ("learned-beacons/", "calibration/", "gain-experiment/")
_RECORDING = re.compile(r"^(.*?\d{8}T\d{6}Z)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def build_local_report_plan(local_root: Path, shared_root: Path, *,
                            archive_root: Path | None = None) -> dict:
    local_root = Path(local_root).resolve(); shared_root = Path(shared_root).resolve()
    archive_root = Path(archive_root).resolve() if archive_root is not None else None
    reports = local_root / "reports"; entries = []
    for source in sorted(reports.rglob("*")) if reports.is_dir() else []:
        if source.is_dir():
            continue
        relative = source.relative_to(reports)
        relative_text = relative.as_posix()
        status = "eligible"
        if any(relative_text.startswith(prefix) for prefix in OPERATIONAL_PREFIXES):
            status = "preserve_operational"
        elif source.is_symlink() or not source.is_file() or ".." in relative.parts:
            status = "unsafe_local_artifact"
        entries.append({"relative_path": relative_text,
            "local_path": str(source),
            "shared_path": str(shared_root / "reports" / relative),
            "bytes": source.stat().st_size if source.is_file() else 0,
            "sha256": _sha256(source) if status == "eligible" else None,
            "status": status})
    legacy_reports = local_root / "evidence/pilot_symbolwise_v3/reports"
    for source in sorted(legacy_reports.glob("*")) if legacy_reports.is_dir() else []:
        if not source.is_file() or source.is_symlink():
            continue
        match = _RECORDING.match(source.name); name = match.group(1) if match else None
        status = "legacy_evidence_unverified"; receipt_path = None
        if name and archive_root is not None:
            receipt_path = archive_root / "catalog/v2/receipts" / f"{name}.json"
            try:
                receipt = json.loads(receipt_path.read_text())
            except (OSError, ValueError):
                receipt = {}
            valid, _, _ = _archive_gate(
                shared_root, archive_root, name,
                str(receipt.get("source_manifest_sha256", "")))
            if valid:
                status = "eligible_v2_obsolete"
        entries.append({"relative_path": source.name,
            "local_path": str(source), "shared_path": None,
            "bytes": source.stat().st_size, "sha256": _sha256(source),
            "status": status, "recording_id": name,
            "v2_receipt": str(receipt_path) if receipt_path else None})
    counts: dict[str, int] = {}
    for item in entries:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"schema": PLAN_SCHEMA, "created_utc": _now(),
        "local_root": str(local_root), "shared_root": str(shared_root),
        "archive_root": str(archive_root) if archive_root is not None else None,
        "operational_prefixes": list(OPERATIONAL_PREFIXES), "entries": entries,
        "summary": {"file_count": len(entries), "status_counts": counts,
            "eligible_count": (counts.get("eligible", 0) +
                               counts.get("eligible_v2_obsolete", 0)),
            "eligible_bytes": sum(item["bytes"] for item in entries
                                  if item["status"] in {
                                      "eligible", "eligible_v2_obsolete"})}}


def _publish_missing(source: Path, target: Path, digest: str,
                     staging: Path) -> str:
    """Publish without overwriting a report that appeared concurrently."""
    if target.is_file():
        return "existing_authority"
    target.parent.mkdir(parents=True, exist_ok=True); staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / f"{digest}.{os.getpid()}.{uuid.uuid4().hex}.partial"
    shutil.copyfile(source, temporary)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    if _sha256(temporary) != digest:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"staged report checksum mismatch: {source}")
    try:
        os.link(temporary, target)
        state = "copied_legacy"
    except FileExistsError:
        state = "existing_authority"
    except OSError as exc:
        # Some NFS configurations reject hard links. O_EXCL still guarantees
        # that a concurrent authoritative writer is never overwritten.
        if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV}:
            raise
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
        except FileExistsError:
            state = "existing_authority"
        else:
            with os.fdopen(descriptor, "wb") as output, temporary.open("rb") as input_:
                shutil.copyfileobj(input_, output, 8 << 20)
                output.flush(); os.fsync(output.fileno())
            state = "copied_legacy"
    finally:
        temporary.unlink(missing_ok=True)
    return state


def apply_local_report_plan(plan: dict) -> dict:
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported local report plan schema")
    local_root = Path(plan["local_root"]).resolve()
    shared_root = Path(plan["shared_root"]).resolve()
    reports = local_root / "reports"
    lock_path = local_root / "staging/local-report-convergence.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = shared_root / "reports/reclamation/local-reports.json"
    staging = shared_root / "staging/local-report-convergence"
    migrated = []; deferred = []
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another local report convergence is active") from exc
        for item in plan.get("entries", []):
            if item.get("status") not in {"eligible", "eligible_v2_obsolete"}:
                continue
            source = Path(item["local_path"])
            try:
                source_root = (local_root / "evidence/pilot_symbolwise_v3/reports"
                               if item["status"] == "eligible_v2_obsolete"
                               else reports)
                relative = source.resolve(strict=True).relative_to(
                    source_root.resolve(strict=True))
                if source.is_symlink() or ".." in relative.parts:
                    raise ValueError("unsafe local artifact")
                digest = _sha256(source)
                if digest != item.get("sha256") or source.stat().st_size != item.get("bytes"):
                    raise ValueError("local artifact changed after planning")
                if item["status"] == "eligible_v2_obsolete":
                    state = "v2_current_authority"
                    target = Path(item["v2_receipt"])
                else:
                    target = Path(item["shared_path"])
                    state = _publish_missing(source, target, digest, staging)
                    if not target.is_file():
                        raise ValueError("shared authority was not published")
                migrated.append({"relative_path": relative.as_posix(),
                    "bytes": item["bytes"], "sha256": digest,
                    "destination_state": state,
                    "shared_bytes": target.stat().st_size,
                    "local_path": str(source)})
            except Exception as exc:
                deferred.append({"relative_path": item.get("relative_path"),
                    "reason": f"{type(exc).__name__}: {exc}"})
        prepared = {"schema": RECEIPT_SCHEMA, "status": "prepared",
            "prepared_utc": _now(), "local_root": str(local_root),
            "shared_root": str(shared_root), "migrated": migrated,
            "deferred": deferred}
        _atomic_json(receipt_path, prepared)
        if not deferred:
            for item in migrated:
                source = Path(item["local_path"])
                if source.is_file() and _sha256(source) == item["sha256"]:
                    source.unlink()
            for directory in sorted(
                    (item for item in reports.rglob("*") if item.is_dir()),
                    key=lambda item: len(item.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        completed = {**prepared,
            "status": "complete" if not deferred else "deferred",
            "completed_utc": _now(),
            "removed_count": (len(migrated) if not deferred else 0),
        "removed_bytes": (sum(item["bytes"] for item in migrated)
                              if not deferred else 0)}
        _atomic_json(receipt_path, completed)
    return completed
