"""Durable, atomic protocol for moving beacon analysis off the receiver."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid


PROTOCOL_SCHEMA = "leo-tracker.analysis-offload/v2"
RECEIPT_SCHEMA = "leo-tracker.analysis-receipt/v1"
OUTPUT_SCHEMAS = {
    "analysis": "leo-tracker.starlink-beacon-analysis/v1",
    "followup": "leo-tracker.starlink-beacon-followup/v1",
    "track": "leo-tracker.starlink-continuous-track/v1",
    "decoded": "leo-tracker.starlink-edge-decode/v1",
    "association": "leo-tracker.starlink-tle-association/v2",
}
OBSERVATION_MODES = frozenset({"narrow", "wide", "oversample", "hop"})
# Hop children record the capture-side name for the same pipeline mode. The
# live watcher passes `hop` explicitly, so only backfill saw the mismatch.
OBSERVATION_MODE_ALIASES = {"channel-hop": "hop"}


def observation_mode(name: str, metadata: dict) -> str:
    """Resolve a capture's pipeline mode from its manifest, falling back to its name."""
    mode = str(metadata.get("observation_mode") or
               ("oversample" if "oversample" in name else
                "wide" if "wide" in name else
                "hop" if name.startswith("hop-") else "narrow"))
    mode = OBSERVATION_MODE_ALIASES.get(mode, mode)
    if mode not in OBSERVATION_MODES:
        raise ValueError(f"unknown observation mode {mode!r}")
    return mode


def preserved_recordings(source_root: Path) -> list[tuple[str, Path]]:
    """Every exportable acquisition recording, oldest first.

    Hop children live one level deeper than plain captures and carry their
    session prefix in the pipeline identity, which is how the analysis queue,
    the shared working set, and the cropped receipts all name them. Quarantine
    is deliberately excluded: those recordings are interrupted and are
    reconciled by review, not by the automatic pipeline.
    """
    found: list[tuple[str, Path]] = []
    captures_root = Path(source_root) / "captures"
    if captures_root.is_dir():
        found += [(item.name, item) for item in captures_root.iterdir() if item.is_dir()]
    sessions_root = Path(source_root) / "hop-sessions"
    if sessions_root.is_dir():
        for session in sessions_root.iterdir():
            if not session.is_dir():
                continue
            found += [(f"{session.name}-{item.name}", item)
                      for item in session.iterdir() if item.is_dir()]
    return sorted(found, key=lambda item: item[1].stat().st_mtime)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _safe_identity(value: str, label: str) -> str:
    if not value or any(character not in
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                        for character in value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def create_context_bundle(context_root: Path, *, learned: Path | None = None,
                          passes: Path | None = None,
                          tle_catalog: Path | None = None) -> Path:
    """Create an immutable dependency closure and atomically publish it."""
    context_root = Path(context_root).resolve()
    inputs: dict[str, Path] = {}
    learned_report: dict | None = None
    if learned is not None and Path(learned).is_file():
        learned = Path(learned).resolve()
        learned_report = _read_json(learned)
        samples = Path(learned_report["samples"]).resolve()
        if not samples.is_file():
            raise FileNotFoundError(f"learned beacon samples are missing: {samples}")
        if _sha256(samples) != learned_report["samples_sha256"]:
            raise ValueError("learned beacon samples do not match their checksum")
        inputs["learned-beacon.npz"] = samples
    if passes is not None and Path(passes).is_file():
        inputs["passes.json"] = Path(passes).resolve()
    if tle_catalog is not None and Path(tle_catalog).is_file():
        tle_catalog = Path(tle_catalog).resolve()
        tle_value = _read_json(tle_catalog)
        if tle_value.get("schema") == "leo-tracker.tle-archive-snapshot/v1":
            archive_root = (tle_catalog.parent.parent
                            if tle_catalog.parent.name == "snapshots"
                            else tle_catalog.parent)
            tle_catalog = (archive_root / tle_value["object"]).resolve()
            if not tle_catalog.is_file():
                raise FileNotFoundError(
                    f"TLE snapshot object is missing: {tle_catalog}")
        inputs["tle-catalog.json"] = tle_catalog

    identity = hashlib.sha256()
    if learned_report is not None:
        identity.update(json.dumps(learned_report, sort_keys=True).encode())
    for name, source in sorted(inputs.items()):
        identity.update(name.encode()); identity.update(_sha256(source).encode())
    bundle_id = identity.hexdigest()[:24]
    bundles = context_root / "bundles"
    destination = bundles / bundle_id
    bundles.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        temporary = bundles / f".{bundle_id}.partial.{os.getpid()}.{uuid.uuid4().hex}"
        temporary.mkdir()
        try:
            for name, source in inputs.items():
                shutil.copyfile(source, temporary / name)
            if learned_report is not None:
                learned_report = dict(learned_report)
                # Consumers may mount the share at a different absolute path.
                # A relative dependency is resolved against the report below.
                learned_report["samples"] = "learned-beacon.npz"
                _atomic_json(temporary / "learned-beacon.json", learned_report)
            manifest = {
                "schema": PROTOCOL_SCHEMA,
                "bundle_id": bundle_id,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "files": {path.name: {"bytes": path.stat().st_size,
                                      "sha256": _sha256(path)}
                          for path in temporary.iterdir() if path.is_file()},
            }
            _atomic_json(temporary / "manifest.json", manifest)
            os.rename(temporary, destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    validate_context_bundle(destination)
    _atomic_json(context_root / "current.json", {
        "schema": PROTOCOL_SCHEMA,
        "bundle": f"bundles/{bundle_id}",
        "bundle_id": bundle_id,
    })
    return destination


def validate_context_bundle(bundle: Path) -> dict:
    bundle = Path(bundle).resolve()
    manifest = _read_json(bundle / "manifest.json")
    if manifest.get("schema") != PROTOCOL_SCHEMA:
        raise ValueError("unsupported analysis context schema")
    for name, expected in manifest.get("files", {}).items():
        path = bundle / name
        if path.stat().st_size != int(expected["bytes"]):
            raise ValueError(f"context size mismatch: {name}")
        if _sha256(path) != expected["sha256"]:
            raise ValueError(f"context checksum mismatch: {name}")
    learned = bundle / "learned-beacon.json"
    if learned.is_file():
        report = _read_json(learned)
        samples = Path(report["samples"])
        if not samples.is_absolute():
            samples = learned.parent / samples
        if _sha256(samples) != report["samples_sha256"]:
            raise ValueError("context learned-beacon checksum mismatch")
    for name in ("passes.json", "tle-catalog.json"):
        path = bundle / name
        if path.is_file():
            json.loads(path.read_text())
    return manifest


def current_context(context_root: Path) -> Path:
    context_root = Path(context_root).resolve()
    pointer = _read_json(context_root / "current.json")
    bundle = context_root / "bundles" / pointer["bundle_id"]
    validate_context_bundle(bundle)
    return bundle


def followup_has_checks(path: Path) -> bool:
    """Return whether a follow-up contains any windows worth fine analysis."""
    report = _read_json(Path(path))
    if report.get("schema") != OUTPUT_SCHEMAS["followup"]:
        raise ValueError(f"unexpected follow-up schema: {path}")
    return any(isinstance(item, dict) for item in report.get("checks", []))


def validate_outputs(root: Path, name: str, mode: str, *, context: Path | None,
                     elapsed_s: int | None = None, full_coverage: bool = False,
                     pipeline_id: str = "legacy-v1",
                     archive_root: Path | None = None) -> dict:
    """Validate the complete required output set and build its receipt."""
    root = Path(root).resolve(); reports = root / "reports"
    output_paths = {
        "analysis": reports / f"{name}.json",
        "followup": reports / "followups" / f"{name}.json",
    }
    values = {key: _read_json(path) for key, path in output_paths.items()}
    for key, value in values.items():
        if value.get("schema") != OUTPUT_SCHEMAS[key]:
            raise ValueError(f"unexpected {key} schema for {name}")
    confirmed = bool(values["followup"].get("confirmation", {}).get("confirmed"))
    has_checks = followup_has_checks(output_paths["followup"])
    if confirmed or full_coverage:
        # Wide analysis already carries its full-coverage Doppler windows, so
        # the server skips the continuous tracker for it. Requiring a track
        # artifact the pipeline never produces fails every wide recording.
        if mode != "wide":
            output_paths["track"] = reports / "tracks" / f"{name}.json"
        if has_checks and mode not in {"wide", "hop"}:
            output_paths["decoded"] = reports / "decoded" / f"{name}.json"
        if (mode != "wide" and context is not None and
                (Path(context) / "tle-catalog.json").is_file()):
            output_paths["association"] = reports / "associations" / f"{name}.json"
        for key in set(output_paths) - set(values):
            values[key] = _read_json(output_paths[key])
            if values[key].get("schema") != OUTPUT_SCHEMAS[key]:
                raise ValueError(f"unexpected {key} schema for {name}")
    pipeline_id = _safe_identity(pipeline_id, "pipeline identity")
    archive_receipt = None
    if archive_root is not None:
        archive_receipt_path = (Path(archive_root) / "catalog" / "receipts" /
                                f"{name}.json")
        archive_receipt = _read_json(archive_receipt_path)
        if (archive_receipt.get("schema") !=
                "leo-tracker.evidence-archive-receipt/v1" or
                archive_receipt.get("recording_id") != name or
                archive_receipt.get("status") != "verified" or
                not archive_receipt.get("source_verified")):
            raise ValueError(f"unverified evidence archive for {name}")
    return {
        "schema": RECEIPT_SCHEMA,
        "job": name,
        "mode": mode,
        "status": "success",
        "confirmed": confirmed,
        "full_coverage": full_coverage,
        "pipeline_id": pipeline_id,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed_s,
        "context": str(Path(context).resolve()) if context is not None else None,
        "outputs": {key: {"path": str(path), "bytes": path.stat().st_size,
                          "sha256": _sha256(path)}
                    for key, path in output_paths.items()},
        "archive_receipt": archive_receipt,
    }


def write_receipt(root: Path, receipt: dict) -> Path:
    pipeline_id = _safe_identity(str(receipt.get("pipeline_id", "legacy-v1")),
                                 "pipeline identity")
    run_root = Path(root) / "reports" / "runs" / pipeline_id / receipt["job"]
    versioned_outputs = {}
    for key, artifact in receipt.get("outputs", {}).items():
        source = Path(artifact["path"])
        destination = run_root / "outputs" / f"{key}{source.suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if _sha256(destination) != artifact["sha256"]:
                raise ValueError(
                    f"pipeline output collision for {pipeline_id}/{receipt['job']}/{key}")
        else:
            temporary = destination.with_name(
                f".{destination.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
            shutil.copyfile(source, temporary)
            if _sha256(temporary) != artifact["sha256"]:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"versioned output copy failed verification: {source}")
            os.replace(temporary, destination)
        versioned_outputs[key] = {
            "path": str(destination), "bytes": destination.stat().st_size,
            "sha256": artifact["sha256"]}
    versioned_receipt = dict(receipt)
    versioned_receipt["versioned_outputs"] = versioned_outputs
    versioned = run_root / "completion.json"
    if versioned.exists():
        existing = _read_json(versioned)
        if existing.get("versioned_outputs") != versioned_outputs:
            raise ValueError(
                f"pipeline completion collision for {pipeline_id}/{receipt['job']}")
    else:
        _atomic_json(versioned, versioned_receipt)
    path = Path(root) / "reports" / "receipts" / f"{receipt['job']}.json"
    _atomic_json(path, versioned_receipt)
    return path


def parse_job(path: Path) -> tuple[str, Path, str, Path | None]:
    fields = path.read_text().rstrip("\n").split("\t")
    if len(fields) not in {3, 4}:
        raise ValueError(f"invalid analysis job: {path}")
    return fields[0], Path(fields[1]), fields[2], (Path(fields[3]) if len(fields) == 4 else None)


def audit_completed(root: Path, *, default_context: Path | None = None) -> dict:
    """Requeue legacy/partial successes that lack a valid output receipt."""
    root = Path(root).resolve(); queue = root / "staging" / "analysis-queue"
    requeued, accepted, errors = [], [], []
    for marker in sorted((queue / "done").glob("*.job")):
        name = marker.stem
        try:
            name, _capture, mode, job_context = parse_job(marker)
            context = job_context or default_context
            if context is not None and not Path(context).is_absolute():
                context = root / context
            receipt = validate_outputs(root, name, mode, context=context)
            write_receipt(root, receipt)
            accepted.append(name)
        except Exception as exc:
            receipt_path = root / "reports" / "receipts" / f"{name}.json"
            receipt_path.unlink(missing_ok=True)
            target = queue / marker.name
            if target.exists():
                target = queue / f"recovered-{uuid.uuid4().hex}-{marker.name}"
            os.replace(marker, target)
            requeued.append(marker.name)
            errors.append(f"{type(exc).__name__}: {exc}")
    return {"accepted": accepted, "requeued": requeued, "errors": errors}


def analysis_status(root: Path, *, workers: int, pipeline_id: str,
                    archive_root: Path | None = None,
                    output: Path | None = None) -> dict:
    """Build a cheap operational snapshot for dashboards and alerting."""
    root = Path(root).resolve(); pipeline_id = _safe_identity(
        pipeline_id, "pipeline identity")
    if workers < 1:
        raise ValueError("worker count must be positive")
    queue = root / "staging" / "analysis-queue"
    ready_paths = list(queue.glob("*.job"))
    now = datetime.now(timezone.utc)
    oldest_age = (max(0.0, now.timestamp() - min(
        path.stat().st_mtime for path in ready_paths)) if ready_paths else 0.0)
    receipt_root = root / "reports" / "runs" / pipeline_id
    archived = (len(list((Path(archive_root) / "catalog" / "receipts").glob(
        "*.json"))) if archive_root is not None else None)
    report = {
        "schema": "leo-tracker.kalman-analysis-status/v1",
        "created_utc": now.isoformat(),
        "root": str(root),
        "pipeline_id": pipeline_id,
        "workers": workers,
        "queue": {
            "ready": len(ready_paths),
            "running": len(list(queue.glob("*.running.*"))),
            "succeeded": len(list((queue / "done").glob("*.job"))),
            "failed": len(list((queue / "failed").glob("*.job"))),
            "oldest_ready_age_s": oldest_age,
        },
        "versioned_completion_count": len(list(receipt_root.glob(
            "*/completion.json"))),
        "verified_archive_count": archived,
    }
    if output is not None:
        _atomic_json(Path(output), report)
    return report


def enqueue_analysis_backfill(root: Path, *, pipeline_id: str,
                              limit: int | None = None,
                              dry_run: bool = False) -> dict:
    """Queue complete shared captures lacking this pipeline's receipt."""
    root = Path(root).resolve(); pipeline_id = _safe_identity(
        pipeline_id, "pipeline identity")
    if limit is not None and limit < 1:
        raise ValueError("backfill limit must be positive")
    queue = root / "staging" / "analysis-queue"
    queue.mkdir(parents=True, exist_ok=True)
    context = current_context(root / "context")
    active_names: set[str] = set()
    for pattern in ("*.job", "*.running.*"):
        for marker in queue.glob(pattern):
            try:
                active_names.add(parse_job(marker)[0])
            except (OSError, ValueError):
                continue
    queued, skipped, errors = [], [], []
    captures_root = root / "captures"
    captures = (sorted(captures_root.iterdir(), key=lambda path: path.stat().st_mtime)
                if captures_root.is_dir() else [])
    for capture in captures:
        if not capture.is_dir():
            continue
        name = capture.name
        completion = (root / "reports" / "runs" / pipeline_id / name /
                      "completion.json")
        if completion.is_file() or name in active_names:
            skipped.append(name); continue
        try:
            manifest = _read_json(capture / "manifest.json")
            if manifest.get("state") != "complete":
                skipped.append(name); continue
            mode = observation_mode(name, manifest.get("metadata", {}))
            marker = queue / f"backfill-{pipeline_id}-{name}.job"
            payload = (f"{name}\tcaptures/{name}\t{mode}\t"
                       f"{context.relative_to(root)}\n")
            if not dry_run:
                temporary = marker.with_name(f".{marker.name}.next.{os.getpid()}")
                temporary.write_text(payload); os.replace(temporary, marker)
            queued.append(name); active_names.add(name)
            if limit is not None and len(queued) >= limit:
                break
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return {"schema": "leo-tracker.analysis-backfill/v1",
            "pipeline_id": pipeline_id, "queued": queued,
            "skipped": skipped, "errors": errors, "dry_run": dry_run}


def enqueue_export_backfill(source_root: Path, shared_root: Path, *,
                            pipeline_id: str, limit: int | None = None,
                            dry_run: bool = False) -> dict:
    """Queue preserved acquisition captures absent from the shared pipeline."""
    source_root = Path(source_root).resolve(); shared_root = Path(shared_root).resolve()
    pipeline_id = _safe_identity(pipeline_id, "pipeline identity")
    if limit is not None and limit < 1:
        raise ValueError("backfill limit must be positive")
    queue = source_root / "staging" / "analysis-queue"
    queue.mkdir(parents=True, exist_ok=True)
    active_names: set[str] = set()
    for pattern in ("*.job", "*.exporting.*"):
        for marker in queue.glob(pattern):
            try:
                active_names.add(parse_job(marker)[0])
            except (OSError, ValueError):
                continue
    queued, skipped, errors = [], [], []
    for name, capture in preserved_recordings(source_root):
        completion = (shared_root / "reports" / "runs" / pipeline_id / name /
                      "completion.json")
        shared_capture = shared_root / "captures" / name
        if (completion.is_file() or shared_capture.is_dir() or
                name in active_names):
            skipped.append(name); continue
        try:
            manifest = _read_json(capture / "manifest.json")
            if manifest.get("state") != "complete":
                skipped.append(name); continue
            mode = observation_mode(name, manifest.get("metadata", {}))
            marker = queue / f"backfill-export-{pipeline_id}-{name}.job"
            payload = f"{name}\t{capture}\t{mode}\n"
            if not dry_run:
                temporary = marker.with_name(f".{marker.name}.next.{os.getpid()}")
                temporary.write_text(payload); os.replace(temporary, marker)
            queued.append(name); active_names.add(name)
            if limit is not None and len(queued) >= limit:
                break
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return {"schema": "leo-tracker.export-backfill/v1",
            "pipeline_id": pipeline_id, "queued": queued,
            "skipped": skipped, "errors": errors, "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle")
    bundle.add_argument("context_root", type=Path)
    bundle.add_argument("--learned", type=Path)
    bundle.add_argument("--passes", type=Path)
    bundle.add_argument("--tle-catalog", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("root", type=Path); validate.add_argument("name")
    validate.add_argument("mode"); validate.add_argument("--context", type=Path)
    validate.add_argument("--elapsed-s", type=int); validate.add_argument("--write", action="store_true")
    validate.add_argument("--full-coverage", action="store_true")
    validate.add_argument("--pipeline-id", default="legacy-v1")
    validate.add_argument("--archive-root", type=Path)
    inspect_followup = commands.add_parser("inspect-followup")
    inspect_followup.add_argument("followup", type=Path)
    status = commands.add_parser("status")
    status.add_argument("root", type=Path)
    status.add_argument("--workers", type=int, required=True)
    status.add_argument("--pipeline-id", required=True)
    status.add_argument("--archive-root", type=Path)
    status.add_argument("--write", type=Path)
    enqueue = commands.add_parser("enqueue-backfill")
    enqueue.add_argument("root", type=Path)
    enqueue.add_argument("--pipeline-id", required=True)
    enqueue.add_argument("--limit", type=int)
    enqueue.add_argument("--dry-run", action="store_true")
    enqueue_export = commands.add_parser("enqueue-export-backfill")
    enqueue_export.add_argument("source_root", type=Path)
    enqueue_export.add_argument("shared_root", type=Path)
    enqueue_export.add_argument("--pipeline-id", required=True)
    enqueue_export.add_argument("--limit", type=int)
    enqueue_export.add_argument("--dry-run", action="store_true")
    audit = commands.add_parser("audit")
    audit.add_argument("root", type=Path); audit.add_argument("--context", type=Path)
    current = commands.add_parser("current")
    current.add_argument("context_root", type=Path)
    args = parser.parse_args(argv)
    if args.command == "bundle":
        print(create_context_bundle(args.context_root, learned=args.learned,
              passes=args.passes, tle_catalog=args.tle_catalog))
    elif args.command == "validate":
        receipt = validate_outputs(args.root, args.name, args.mode,
                                   context=args.context, elapsed_s=args.elapsed_s,
                                   full_coverage=args.full_coverage,
                                   pipeline_id=args.pipeline_id,
                                   archive_root=args.archive_root)
        if args.write: write_receipt(args.root, receipt)
        print(json.dumps(receipt, sort_keys=True))
    elif args.command == "inspect-followup":
        has_checks = followup_has_checks(args.followup)
        print(json.dumps({"followup": str(args.followup),
                          "has_checks": has_checks}, sort_keys=True))
        return 0 if has_checks else 1
    elif args.command == "status":
        print(json.dumps(analysis_status(
            args.root, workers=args.workers, pipeline_id=args.pipeline_id,
            archive_root=args.archive_root, output=args.write), sort_keys=True))
    elif args.command == "enqueue-backfill":
        report = enqueue_analysis_backfill(
            args.root, pipeline_id=args.pipeline_id, limit=args.limit,
            dry_run=args.dry_run)
        print(json.dumps(report, sort_keys=True))
        return 1 if report["errors"] else 0
    elif args.command == "enqueue-export-backfill":
        report = enqueue_export_backfill(
            args.source_root, args.shared_root, pipeline_id=args.pipeline_id,
            limit=args.limit, dry_run=args.dry_run)
        print(json.dumps(report, sort_keys=True))
        return 1 if report["errors"] else 0
    elif args.command == "audit":
        print(json.dumps(audit_completed(args.root, default_context=args.context), sort_keys=True))
    else:
        print(current_context(args.context_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
