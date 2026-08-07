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
        inputs["tle-catalog.json"] = Path(tle_catalog).resolve()

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


def validate_outputs(root: Path, name: str, mode: str, *, context: Path | None,
                     elapsed_s: int | None = None) -> dict:
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
    if confirmed:
        output_paths["track"] = reports / "tracks" / f"{name}.json"
        if mode not in {"wide", "hop"}:
            output_paths["decoded"] = reports / "decoded" / f"{name}.json"
        if context is not None and (Path(context) / "tle-catalog.json").is_file():
            output_paths["association"] = reports / "associations" / f"{name}.json"
        for key in set(output_paths) - set(values):
            values[key] = _read_json(output_paths[key])
            if values[key].get("schema") != OUTPUT_SCHEMAS[key]:
                raise ValueError(f"unexpected {key} schema for {name}")
    return {
        "schema": RECEIPT_SCHEMA,
        "job": name,
        "mode": mode,
        "status": "success",
        "confirmed": confirmed,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed_s,
        "context": str(Path(context).resolve()) if context is not None else None,
        "outputs": {key: str(path) for key, path in output_paths.items()},
    }


def write_receipt(root: Path, receipt: dict) -> Path:
    path = Path(root) / "reports" / "receipts" / f"{receipt['job']}.json"
    _atomic_json(path, receipt)
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
                                   context=args.context, elapsed_s=args.elapsed_s)
        if args.write: write_receipt(args.root, receipt)
        print(json.dumps(receipt, sort_keys=True))
    elif args.command == "audit":
        print(json.dumps(audit_completed(args.root, default_context=args.context), sort_keys=True))
    else:
        print(current_context(args.context_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
