"""Transactional normalization of report artifacts left in legacy layouts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from .qnap_lifecycle import qnap_storage_mutation_lock


PLAN_SCHEMA = "leo-tracker.legacy-layout-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.legacy-layout-receipt/v1"
CONFIRMATION = "NORMALIZE-LEGACY-LAYOUT"


def legacy_normalization_ready(primary_plan: dict) -> bool:
    """Return true only after a complete zero-work archive migration scan."""
    config = primary_plan.get("configuration", {})
    return bool(config.get("active_scope") == "archive" and
                config.get("inventory_complete") is True and
                primary_plan.get("summary", {}).get("eligible_count") == 0)


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
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


def _safe_below(root: Path, path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _entry_id(kind: str, source: str, key: str = "") -> str:
    return hashlib.sha256(f"{kind}\0{source}\0{key}".encode()).hexdigest()


def _file_entry(kind: str, source: Path, destination: Path, *,
                shared_reports: Path, archive_derived: Path,
                completion: Path | None = None, output_key: str = "",
                expected_sha256: str | None = None) -> dict:
    source_string = str(source); key = output_key
    base = {"entry_id": _entry_id(kind, source_string, key), "kind": kind,
            "source_path": source_string, "destination_path": str(destination),
            "completion_path": str(completion) if completion else None,
            "output_key": key, "expected_sha256": expected_sha256}
    source_root = archive_derived if kind == "derived" else shared_reports
    if not _safe_below(source_root, source) or not source.is_file():
        return {**base, "status": "unsafe_source"}
    if not _safe_below(shared_reports, destination):
        return {**base, "status": "unsafe_destination"}
    source_sha = _sha256(source); source_bytes = source.stat().st_size
    base.update(source_sha256=source_sha, source_bytes=source_bytes)
    if destination.is_file():
        destination_sha = _sha256(destination)
        if kind == "versioned_output" and expected_sha256 and not (
                source_sha == expected_sha256 or destination_sha == expected_sha256):
            return {**base, "destination_sha256": destination_sha,
                    "status": "hash_conflict"}
        if kind == "versioned_output" and not expected_sha256 and (
                source_sha != destination_sha):
            return {**base, "destination_sha256": destination_sha,
                    "status": "hash_conflict_without_recorded_hash"}
        return {**base, "destination_sha256": destination_sha,
                "action": "remove_obsolete", "status": "eligible"}
    if destination.exists():
        return {**base, "status": "destination_not_regular_file"}
    if kind == "versioned_output" and (
            not expected_sha256 or source_sha != expected_sha256):
        return {**base, "status": "promotion_hash_unverified"}
    return {**base, "destination_sha256": None,
            "action": "promote", "status": "eligible"}


def build_legacy_layout_plan(shared_root: Path, archive_root: Path, *,
                             eligible_limit: int | None = None) -> dict:
    """Inventory legacy report copies without mutating either store."""
    if eligible_limit is not None and eligible_limit < 1:
        raise ValueError("eligible limit must be positive")
    shared_root = Path(shared_root).resolve(); archive_root = Path(archive_root).resolve()
    reports = shared_root / "reports"; derived = archive_root / "derived"
    entries = []; eligible = 0; inventory_complete = True

    def append(entry: dict) -> bool:
        nonlocal eligible, inventory_complete
        entries.append(entry)
        if entry.get("status") == "eligible": eligible += 1
        if eligible_limit is not None and eligible >= eligible_limit:
            inventory_complete = False
            return False
        return True

    if derived.is_dir():
        for source in sorted(path for path in derived.rglob("*")
                             if path.is_file() or path.is_symlink()):
            relative = source.relative_to(derived)
            if not append(_file_entry(
                    "derived", source, reports / relative,
                    shared_reports=reports, archive_derived=derived)):
                break

    if inventory_complete:
        runs = reports / "runs"
        for completion in sorted(runs.glob("*/*/completion.json")
                                 if runs.is_dir() else []):
            document = _json(completion); outputs = document.get("outputs", {})
            output_dir = completion.parent / "outputs"
            referenced = set()
            if isinstance(outputs, dict):
                for key, artifact in outputs.items():
                    if not isinstance(artifact, dict) or Path(str(key)).name != key:
                        continue
                    destination = Path(str(artifact.get("path", "")))
                    duplicate = output_dir / f"{key}{destination.suffix}"
                    referenced.add(duplicate)
                    if duplicate.is_file() or duplicate.is_symlink():
                        entry = _file_entry(
                            "versioned_output", duplicate, destination,
                            shared_reports=reports, archive_derived=derived,
                            completion=completion, output_key=key,
                            expected_sha256=artifact.get("sha256"))
                        if not append(entry): break
                    elif destination.is_file():
                        versioned = document.get("versioned_outputs", {}).get(key, {})
                        if versioned.get("storage") != "authoritative-reference" or (
                                versioned.get("path") != str(destination)):
                            if not append({
                                "entry_id": _entry_id(
                                    "reference_metadata", str(completion), key),
                                "kind": "reference_metadata", "status": "eligible",
                                "action": "rewrite_reference",
                                "source_path": None,
                                "destination_path": str(destination),
                                "destination_sha256": _sha256(destination),
                                "completion_path": str(completion),
                                "completion_sha256": _sha256(completion),
                                "output_key": key, "source_bytes": 0}):
                                break
                if not inventory_complete: break
            if output_dir.is_dir():
                children = [path for path in output_dir.rglob("*")
                            if path.is_file() or path.is_symlink()]
                for source in children:
                    if source not in referenced:
                        if not append({
                            "entry_id": _entry_id("unreferenced_output", str(source)),
                            "kind": "unreferenced_output", "source_path": str(source),
                            "source_bytes": source.stat().st_size if source.is_file() else 0,
                            "status": "unreferenced_output"}):
                            break
                if not children:
                    append({"entry_id": _entry_id(
                                "empty_output_directory", str(output_dir)),
                            "kind": "empty_output_directory", "status": "eligible",
                            "action": "remove_empty", "source_path": str(output_dir),
                            "source_bytes": 0, "completion_path": str(completion)})
            if not inventory_complete: break

    counts = {}; kinds = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    return {"schema": PLAN_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "shared_root": str(shared_root), "archive_root": str(archive_root),
            "configuration": {"eligible_limit": eligible_limit,
                              "inventory_complete": inventory_complete},
            "summary": {"entry_count": len(entries), "eligible_count": eligible,
                        "eligible_bytes": sum(int(x.get("source_bytes", 0))
                                              for x in entries
                                              if x["status"] == "eligible"),
                        "status_counts": counts, "kind_counts": kinds},
            "entries": entries}


def _rewrite_reference(entry: dict, reports: Path) -> None:
    completion = Path(entry["completion_path"]); destination = Path(
        entry["destination_path"]); key = entry["output_key"]
    if not _safe_below(reports / "runs", completion) or not completion.is_file():
        raise ValueError("unsafe or missing completion record")
    if not _safe_below(reports, destination) or not destination.is_file():
        raise ValueError("unsafe or missing authoritative output")
    document = _json(completion); artifact = document.get("outputs", {}).get(key)
    if not isinstance(artifact, dict) or artifact.get("path") != str(destination):
        raise ValueError("completion output changed after planning")
    versioned = dict(document.get("versioned_outputs", {}))
    versioned[key] = {**artifact, "path": str(destination),
                      "storage": "authoritative-reference"}
    document["versioned_outputs"] = versioned
    _atomic_json(completion, document)


def apply_legacy_layout_plan(plan: dict, *, confirmation: str,
                             limit: int | None = None,
                             mutation_lock_held: bool = False) -> dict:
    """Apply bounded, deletion-last legacy normalization transactions."""
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported legacy layout plan")
    if confirmation != CONFIRMATION:
        raise ValueError("legacy normalization confirmation token is missing or incorrect")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    shared = Path(plan["shared_root"]).resolve(); archive = Path(plan["archive_root"]).resolve()
    if not mutation_lock_held:
        with qnap_storage_mutation_lock(shared):
            return apply_legacy_layout_plan(
                plan, confirmation=confirmation, limit=limit,
                mutation_lock_held=True)
    reports = shared / "reports"; derived = archive / "derived"
    entries = [x for x in plan.get("entries", []) if x.get("status") == "eligible"]
    if limit is not None: entries = entries[:limit]
    completed = []; failures = []
    for entry in entries:
        receipt_path = (reports / "reclamation" / "legacy-layout" /
                        f"{entry['entry_id']}.json")
        prepared = {**entry, "schema": RECEIPT_SCHEMA, "status": "prepared",
                    "prepared_utc": datetime.now(timezone.utc).isoformat()}
        try:
            kind = entry["kind"]; action = entry["action"]
            source = Path(entry["source_path"]) if entry.get("source_path") else None
            destination = Path(entry["destination_path"]) if entry.get(
                "destination_path") else None
            if source is not None and action != "remove_empty":
                root = derived if kind == "derived" else reports
                if not _safe_below(root, source) or not source.is_file() or (
                        _sha256(source) != entry.get("source_sha256")):
                    raise ValueError("legacy source changed after planning")
            if destination is not None and destination.is_file() and entry.get(
                    "destination_sha256") and _sha256(destination) != entry[
                        "destination_sha256"]:
                raise ValueError("authoritative destination changed after planning")
            _atomic_json(receipt_path, prepared)
            if action == "remove_obsolete":
                if kind == "versioned_output": _rewrite_reference(entry, reports)
                assert source is not None; source.unlink()
            elif action == "promote":
                assert source is not None and destination is not None
                if destination.exists(): raise ValueError("promotion destination appeared")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                if _sha256(destination) != entry["source_sha256"]:
                    raise ValueError("promoted output failed verification")
                if kind == "versioned_output": _rewrite_reference(entry, reports)
            elif action == "rewrite_reference":
                _rewrite_reference(entry, reports)
            elif action == "remove_empty":
                assert source is not None
                if (not _safe_below(reports / "runs", source) or
                        not source.is_dir() or any(source.iterdir())):
                    raise ValueError("output directory is no longer empty or safe")
                source.rmdir()
            else:
                raise ValueError("unsupported legacy normalization action")
            if kind == "versioned_output" and source is not None:
                try:
                    if source.parent.is_dir() and not any(source.parent.iterdir()):
                        source.parent.rmdir()
                except OSError:
                    pass
            final = {**prepared, "status": "complete",
                     "completed_utc": datetime.now(timezone.utc).isoformat()}
            _atomic_json(receipt_path, final); completed.append(final)
        except (OSError, ValueError, KeyError) as exc:
            failures.append({"entry_id": entry.get("entry_id"), "error": str(exc)})
    return {"schema": "leo-tracker.legacy-layout-result/v1",
            "completed_count": len(completed), "failure_count": len(failures),
            "failures": failures,
            "removed_bytes": sum(int(x.get("source_bytes", 0)) for x in completed
                                 if x.get("action") == "remove_obsolete"),
            "promoted_bytes": sum(int(x.get("source_bytes", 0)) for x in completed
                                  if x.get("action") == "promote"),
            "receipts": [str(reports / "reclamation" / "legacy-layout" /
                             f"{x['entry_id']}.json") for x in completed]}
