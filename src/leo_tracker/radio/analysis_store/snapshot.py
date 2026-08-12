"""Verified immutable publication of read-only DuckDB generations."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from .ingest import AnalysisStore, connect
from .schema import SCHEMA_NAME, SCHEMA_VERSION, validate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(temporary, path)


def verify_snapshot(path: Path) -> dict:
    connection = connect(path, read_only=True)
    try:
        validate(connection)
        metadata = connection.execute(
            "SELECT commit_sequence, last_run_id FROM store_metadata "
            "WHERE singleton = TRUE").fetchone()
        return {"schema": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
                "duckdb_version": connection.execute(
                    "SELECT version()").fetchone()[0],
                "commit_sequence": metadata[0], "last_run_id": metadata[1],
                "run_count": connection.execute(
                    "SELECT count(*) FROM analysis_runs").fetchone()[0],
                "recording_count": connection.execute(
                    "SELECT count(*) FROM recordings").fetchone()[0]}
    finally:
        connection.close()


def publish_snapshot(store: AnalysisStore, store_root: Path,
                     publication_root: Path) -> dict:
    """Checkpoint, copy, read back, and atomically publish one generation."""
    status = store.status()
    if not status.get("initialized"):
        raise ValueError("analysis store is not initialized")
    connection = connect(store.database)
    try:
        connection.execute("CHECKPOINT")
    finally:
        connection.close()

    generation = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") +
                  f"-{int(status['commit_sequence']):012d}")
    local_dir = Path(store_root).resolve() / "snapshots"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_candidate = local_dir / f"analysis-{generation}.duckdb"
    shutil.copy2(store.database, local_candidate)
    local_verification = verify_snapshot(local_candidate)
    if local_verification["commit_sequence"] != status["commit_sequence"]:
        raise ValueError("local snapshot commit sequence mismatch")
    digest = sha256_file(local_candidate)

    root = Path(publication_root).resolve()
    snapshots = root / "snapshots"
    receipts = root / "publication"
    snapshots.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    final = snapshots / local_candidate.name
    private = snapshots / f".{local_candidate.name}.partial.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        shutil.copy2(local_candidate, private)
        if sha256_file(private) != digest:
            raise ValueError("shared snapshot read-back digest mismatch")
        shared_verification = verify_snapshot(private)
        if shared_verification != local_verification:
            raise ValueError("shared snapshot verification differs from local candidate")
        os.replace(private, final)
    except Exception:
        private.unlink(missing_ok=True)
        raise
    receipt = {
        "schema": "leo-tracker.analysis-store-publication/v1",
        "generation": generation, "created_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot": str(final), "bytes": final.stat().st_size, "sha256": digest,
        **shared_verification,
    }
    try:
        reconciliation = json.loads(
            (Path(store_root).resolve() / "reconciliation.json").read_text())
        receipt["reconciliation_cursor"] = reconciliation.get("cursor")
        receipt["reconciliation_updated_utc"] = reconciliation.get("updated_utc")
    except (OSError, ValueError):
        receipt["reconciliation_cursor"] = None
        receipt["reconciliation_updated_utc"] = None
    atomic_json(receipts / f"{generation}.json", receipt)
    pointer = {"schema": "leo-tracker.analysis-store-current/v1",
               "generation": generation,
               "snapshot": str(Path("snapshots") / final.name),
               "bytes": receipt["bytes"], "sha256": digest,
               "commit_sequence": receipt["commit_sequence"],
               "published_utc": receipt["created_utc"]}
    atomic_json(root / "current.json", pointer)
    return receipt
