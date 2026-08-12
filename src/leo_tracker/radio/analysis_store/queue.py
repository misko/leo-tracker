"""Durable single-consumer inbox for analysis-store transactions."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from .ingest import AnalysisStore
from .mapping import build_input_manifest, enqueue_input, enqueue_manifest


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(temporary, path)


@contextmanager
def owner_lock(store_root: Path):
    """Exclusive local lock held by the sole live-database owner process."""
    root = Path(store_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / ".owner.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"another process owns the analysis store: {root}") from exc
        yield
    finally:
        handle.close()


class StoreQueue:
    def __init__(self, store_root: Path, shared_root: Path, database: Path | None = None,
                 *, create: bool = True):
        self.root = Path(store_root).resolve()
        self.shared_root = Path(shared_root).resolve()
        self.database = (Path(database).resolve() if database else
                         self.root / "live" / "analysis.duckdb")
        self.store = AnalysisStore(self.database, self.shared_root)
        if create:
            for name in ("inbox", "running", "failed"):
                (self.root / name).mkdir(parents=True, exist_ok=True)

    def enqueue(self, recording_id: str, pipeline_id: str) -> dict:
        path, manifest = enqueue_input(self.shared_root, self.root,
                                       recording_id, pipeline_id)
        return {"path": str(path), "run_id": manifest["run_id"],
                "recording_id": recording_id, "queued": path.is_file()}

    def recover(self) -> list[str]:
        recovered = []
        for path in sorted((self.root / "running").glob("*.json")):
            target = self.root / "inbox" / path.name
            if target.exists():
                path.unlink()
            else:
                os.replace(path, target)
            recovered.append(path.stem)
        return recovered

    def process_next(self) -> dict | None:
        try:
            source = next(iter(sorted((self.root / "inbox").glob("*.json"))))
        except StopIteration:
            return None
        running = self.root / "running" / source.name
        try:
            os.replace(source, running)
        except FileNotFoundError:
            return None
        try:
            result = self.store.ingest(running)
            running.unlink()
            return result
        except Exception as exc:
            failed = self.root / "failed" / running.name
            os.replace(running, failed)
            _atomic_json(failed.with_suffix(".error.json"), {
                "schema": "leo-tracker.analysis-store-error/v1",
                "run_id": failed.stem,
                "failed_utc": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__, "error": str(exc),
            })
            raise

    def drain(self, *, limit: int | None = None, continue_on_error: bool = False) -> dict:
        inserted = duplicates = failures = 0
        results = []
        while limit is None or len(results) < limit:
            try:
                result = self.process_next()
            except Exception as exc:
                failures += 1
                results.append({"inserted": False, "error": str(exc)})
                if not continue_on_error:
                    break
                continue
            if result is None:
                break
            results.append(result)
            inserted += bool(result["inserted"])
            duplicates += not bool(result["inserted"])
        return {"inserted": inserted, "duplicates": duplicates,
                "failures": failures, "processed": len(results), "results": results}

    def counts(self) -> dict:
        def count(directory: str, pattern: str = "*.json") -> int:
            return sum(1 for _ in (self.root / directory).glob(pattern))
        ready = sorted((self.root / "inbox").glob("*.json"))
        oldest_age = None
        if ready:
            try:
                oldest_age = max(0.0, time.time() - min(
                    path.stat().st_mtime for path in ready))
            except OSError:
                pass
        failed = sum(1 for path in (self.root / "failed").glob("*.json")
                     if not path.name.endswith(".error.json"))
        return {"ready": len(ready), "running": count("running"),
                "failed": failed, "oldest_ready_age_s": oldest_age}


def completion_runs(shared_root: Path):
    """Yield deterministic historical run identities for explicit backfill."""
    runs = Path(shared_root).resolve() / "reports" / "runs"
    for pipeline in sorted(runs.iterdir() if runs.is_dir() else []):
        if not pipeline.is_dir():
            continue
        for recording in sorted(pipeline.iterdir()):
            if recording.is_dir() and (recording / "completion.json").is_file():
                yield recording.name, pipeline.name


def enqueue_backfill(queue: StoreQueue, *, limit: int | None = None,
                     dry_run: bool = False) -> dict:
    planned, queued, skipped, errors = [], [], [], []
    known = queue.store.run_ids()
    scanned = 0
    for recording_id, pipeline_id in completion_runs(queue.shared_root):
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        try:
            manifest = build_input_manifest(queue.shared_root, recording_id, pipeline_id)
            if manifest["run_id"] in known:
                skipped.append(manifest["run_id"])
                continue
            planned.append({"recording_id": recording_id, "pipeline_id": pipeline_id,
                            "run_id": manifest["run_id"]})
            if not dry_run:
                path = enqueue_manifest(queue.root, manifest)
                queued.append({"path": str(path), "run_id": manifest["run_id"],
                               "recording_id": recording_id, "queued": True})
        except Exception as exc:
            errors.append({"recording_id": recording_id, "pipeline_id": pipeline_id,
                           "error": str(exc)})
    return {"dry_run": dry_run, "scanned": scanned,
            "planned": planned, "queued": queued,
            "skipped_existing": skipped, "errors": errors}


def reconcile_batch(queue: StoreQueue, *, scan_limit: int = 100) -> dict:
    """Scan one bounded, persistent slice of completion receipts for gaps."""
    if scan_limit < 1:
        raise ValueError("reconciliation scan limit must be positive")
    items = [(f"{pipeline}/{recording}", recording, pipeline)
             for recording, pipeline in completion_runs(queue.shared_root)]
    cursor_path = queue.root / "reconciliation.json"
    try:
        cursor = json.loads(cursor_path.read_text()).get("cursor")
    except (OSError, ValueError):
        cursor = None
    start = next((index for index, item in enumerate(items)
                  if cursor is None or item[0] > cursor), len(items))
    ordered = items[start:] + items[:start]
    selected = ordered[:scan_limit]
    known = queue.store.run_ids()
    queued, present, errors = [], [], []
    for key, recording_id, pipeline_id in selected:
        try:
            manifest = build_input_manifest(queue.shared_root, recording_id, pipeline_id)
            if manifest["run_id"] in known:
                present.append(manifest["run_id"])
            else:
                path = enqueue_manifest(queue.root, manifest)
                queued.append({"run_id": manifest["run_id"], "path": str(path)})
        except Exception as exc:
            errors.append({"recording_id": recording_id, "pipeline_id": pipeline_id,
                           "error": str(exc)})
    next_cursor = selected[-1][0] if selected else cursor
    _atomic_json(cursor_path, {
        "schema": "leo-tracker.analysis-store-reconciliation/v1",
        "cursor": next_cursor, "scanned": len(selected),
        "updated_utc": datetime.now(timezone.utc).isoformat(),
    })
    return {"scanned": len(selected), "queued": queued, "present": present,
            "errors": errors, "cursor": next_cursor}
