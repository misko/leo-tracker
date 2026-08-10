"""Retire acquisition-host artifacts absent from the current storage regime."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from .qnap_lifecycle import _archive_gate


PLAN_SCHEMA = "leo-tracker.local-obsolete-artifact-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.local-obsolete-artifact-receipt/v1"
FOLLOWUP_SCHEMA = "leo-tracker.starlink-beacon-followup/v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_identity(root: Path) -> tuple[int, str]:
    """Return a stable size/hash identity while rejecting links and devices."""
    digest = hashlib.sha256(); bytes_ = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"unsafe scratch artifact: {path}")
        digest.update(("d\0" if path.is_dir() else "f\0").encode())
        digest.update(relative.encode()); digest.update(b"\0")
        if path.is_file():
            size = path.stat().st_size; bytes_ += size
            digest.update(str(size).encode()); digest.update(b"\0")
            digest.update(_sha256(path).encode()); digest.update(b"\0")
    return bytes_, digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def inventory_obsolete_local_artifacts(local_root: Path, *,
                                        sample_limit: int = 20) -> dict:
    """Fast local-only inventory used by the convergence audit."""
    root = Path(local_root).resolve()
    groups = {
        "legacy_confirmation_markers": sorted(
            root.joinpath("staging").glob("*.confirmed")),
        "frame_baseline_scratch": sorted(root.glob("tmp-frame-baseline.*")),
        "legacy_checkpoints": sorted(
            root.joinpath("checkpoints").iterdir())
            if root.joinpath("checkpoints").is_dir() else [],
    }
    result = {}
    for name, paths in groups.items():
        result[name] = {"count": len(paths),
            "samples": [str(path) for path in paths[:sample_limit]]}
    result["count"] = sum(item["count"] for item in result.values())
    return result


def _followup_gate(path: Path) -> tuple[bool, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    valid = (value.get("schema") == FOLLOWUP_SCHEMA and
             value.get("confirmation", {}).get("confirmed") is True)
    return valid, _sha256(path) if valid else None


def _canonical_frame_authorities(shared_root: Path, recording_id: str) -> list[dict]:
    """Return the current canonical frame product when it supersedes scratch."""
    report = shared_root / "reports/frame-tracks" / f"{recording_id}.json"
    samples = shared_root / "reports/frame-tracks" / f"{recording_id}.npz"
    followup = shared_root / "reports/followups" / f"{recording_id}.json"
    try:
        value = json.loads(report.read_text(encoding="utf-8"))
        valid_followup, _ = _followup_gate(followup)
    except (OSError, ValueError):
        return []
    if (not str(value.get("schema", "")).startswith(
            ("leo-tracker.starlink-conditioned-frame-track/")) or
            not samples.is_file() or not valid_followup):
        return []
    return [{"path": str(path), "sha256": _sha256(path)}
            for path in (report, samples, followup)]


def build_local_artifact_plan(local_root: Path, shared_root: Path, *,
                              archive_root: Path | None = None,
                              minimum_age_s: float = 6 * 3600) -> dict:
    if minimum_age_s < 0:
        raise ValueError("minimum age must be non-negative")
    local_root = Path(local_root).resolve(); shared_root = Path(shared_root).resolve()
    archive_root = Path(archive_root).resolve() if archive_root is not None else None
    cutoff = time.time() - minimum_age_s; entries = []

    staging = local_root / "staging"
    for source in sorted(staging.glob("*.confirmed")) if staging.is_dir() else []:
        status = "minimum_age_not_met"
        followup = shared_root / "reports/followups" / f"{source.stem}.json"
        followup_sha = None
        if source.is_symlink() or not source.is_file():
            status = "unsafe_local_artifact"
        elif source.stat().st_mtime < cutoff:
            valid, followup_sha = _followup_gate(followup)
            status = "eligible" if valid else "confirmation_unverified"
        entries.append({"kind": "legacy_confirmation_marker",
            "local_path": str(source), "relative_path": str(source.relative_to(local_root)),
            "bytes": source.stat().st_size if source.is_file() else 0,
            "sha256": _sha256(source) if source.is_file() and not source.is_symlink() else None,
            "status": status, "authority_path": str(followup),
            "authority_sha256": followup_sha})

    for source in sorted(local_root.glob("tmp-frame-baseline.*")):
        status = "minimum_age_not_met"; recording_id = None; receipt_path = None
        authorities: list[dict] = []
        try:
            bytes_, digest = _tree_identity(source)
        except (OSError, ValueError):
            bytes_, digest, status = 0, None, "unsafe_local_artifact"
        if status != "unsafe_local_artifact" and source.stat().st_mtime < cutoff:
            try:
                baseline = json.loads((source / "baseline.json").read_text())
                recording_id = Path(str(baseline["capture"])).name
            except (OSError, ValueError, KeyError, TypeError):
                status = "scratch_authority_unverified"
            else:
                if archive_root is None:
                    status = "scratch_authority_unverified"
                else:
                    receipt_path = archive_root / "catalog/v2/receipts" / f"{recording_id}.json"
                    try:
                        receipt = json.loads(receipt_path.read_text())
                    except (OSError, ValueError):
                        receipt = {}
                    valid, _, _ = _archive_gate(
                        shared_root, archive_root, recording_id,
                        str(receipt.get("source_manifest_sha256", "")))
                    status = "eligible" if valid else "scratch_authority_unverified"
                    if valid:
                        authorities = [{"path": str(receipt_path),
                                        "sha256": _sha256(receipt_path)}]
                if status != "eligible":
                    authorities = _canonical_frame_authorities(
                        shared_root, recording_id)
                    if authorities:
                        status = "eligible"
        entries.append({"kind": "frame_baseline_scratch", "local_path": str(source),
            "relative_path": str(source.relative_to(local_root)), "bytes": bytes_,
            "sha256": digest, "status": status, "recording_id": recording_id,
            "authority_path": str(receipt_path) if receipt_path else None,
            "authority_sha256": (_sha256(receipt_path)
                                  if receipt_path and status == "eligible" and
                                  not authorities else None),
            "authority_files": authorities})

    checkpoints = local_root / "checkpoints"
    for source in sorted(checkpoints.iterdir()) if checkpoints.is_dir() else []:
        status = "minimum_age_not_met"
        if source.is_symlink() or not source.is_file():
            status = "unsafe_local_artifact"
        elif source.stat().st_mtime < cutoff:
            status = "eligible"
        entries.append({"kind": "legacy_checkpoint", "local_path": str(source),
            "relative_path": str(source.relative_to(local_root)),
            "bytes": source.stat().st_size if source.is_file() else 0,
            "sha256": _sha256(source) if source.is_file() and not source.is_symlink() else None,
            "status": status, "authority_path": None, "authority_sha256": None})

    counts: dict[str, int] = {}
    for item in entries:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"schema": PLAN_SCHEMA, "created_utc": _now(),
        "local_root": str(local_root), "shared_root": str(shared_root),
        "archive_root": str(archive_root) if archive_root else None,
        "minimum_age_s": minimum_age_s, "entries": entries,
        "summary": {"artifact_count": len(entries), "status_counts": counts,
            "eligible_count": counts.get("eligible", 0),
            "eligible_bytes": sum(item["bytes"] for item in entries
                                  if item["status"] == "eligible")}}


def apply_local_artifact_plan(plan: dict) -> dict:
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported local artifact plan schema")
    local_root = Path(plan["local_root"]).resolve()
    shared_root = Path(plan["shared_root"]).resolve()
    lock_path = local_root / "staging/local-obsolete-artifact.lock"
    receipt_path = shared_root / "reports/reclamation/local-obsolete-artifacts.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    removed = []; deferred = []
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another local artifact convergence is active") from exc
        for item in plan.get("entries", []):
            if item.get("status") != "eligible":
                continue
            source = Path(item["local_path"])
            try:
                relative = source.resolve(strict=True).relative_to(local_root)
                if source.is_symlink() or relative.as_posix() != item["relative_path"]:
                    raise ValueError("unsafe or changed local path")
                if item["kind"] == "frame_baseline_scratch":
                    bytes_, digest = _tree_identity(source)
                else:
                    if not source.is_file(): raise ValueError("local file disappeared")
                    bytes_, digest = source.stat().st_size, _sha256(source)
                if bytes_ != item["bytes"] or digest != item["sha256"]:
                    raise ValueError("local artifact changed after planning")
                authority = item.get("authority_path")
                for authority_item in item.get("authority_files", []):
                    authority_path = Path(authority_item["path"])
                    if (not authority_path.is_file() or
                            _sha256(authority_path) != authority_item.get("sha256")):
                        raise ValueError("durable authority changed after planning")
                if authority and not item.get("authority_files"):
                    authority_path = Path(authority)
                    if not authority_path.is_file() or _sha256(authority_path) != item.get("authority_sha256"):
                        raise ValueError("durable authority changed after planning")
                removed.append({k: item.get(k) for k in
                    ("kind", "relative_path", "bytes", "sha256", "authority_path",
                     "authority_sha256", "authority_files")})
            except Exception as exc:
                deferred.append({"relative_path": item.get("relative_path"),
                    "reason": f"{type(exc).__name__}: {exc}"})
        prepared = {"schema": RECEIPT_SCHEMA, "status": "prepared",
            "prepared_utc": _now(), "local_root": str(local_root),
            "shared_root": str(shared_root), "removed": removed,
            "deferred": deferred}
        _atomic_json(receipt_path, prepared)
        for item in removed:
            source = local_root / item["relative_path"]
            if item["kind"] == "frame_baseline_scratch":
                shutil.rmtree(source)
            else:
                source.unlink()
        for directory in (local_root / "checkpoints",):
            try: directory.rmdir()
            except OSError: pass
        completed = {**prepared, "status": "complete" if not deferred else "deferred",
            "completed_utc": _now(), "removed_count": len(removed),
            "removed_bytes": sum(item["bytes"] for item in removed)}
        _atomic_json(receipt_path, completed)
    return completed
