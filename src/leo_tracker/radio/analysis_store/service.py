"""Long-running single owner for ingest and snapshot publication."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import time

from .queue import StoreQueue, owner_lock, reconcile_batch
from .snapshot import atomic_json, publish_snapshot


def service_status(queue: StoreQueue, *, last_publication: dict | None = None,
                   last_error: dict | None = None) -> dict:
    return {"schema": "leo-tracker.analysis-store-runtime/v1",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "store": queue.store.status(), "queue": queue.counts(),
            "last_publication": last_publication, "last_error": last_error}


def _run_service_owned(store_root: Path, shared_root: Path, *,
                       database: Path | None = None,
                       publication_root: Path | None = None,
                       runtime_output: Path | None = None, poll_s: float = 2.0,
                       snapshot_interval_s: float = 300.0,
                       reconciliation_interval_s: float = 600.0,
                       reconciliation_limit: int = 100,
                       once: bool = False) -> dict:
    if poll_s <= 0 or snapshot_interval_s <= 0 or reconciliation_interval_s <= 0:
        raise ValueError("store service intervals must be positive")
    queue = StoreQueue(store_root, shared_root, database)
    queue.store.initialize()
    queue.recover()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous_handlers = {}
    if not once:
        for event in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[event] = signal.signal(event, stop)
    last_publication = None
    last_published_sequence = -1
    last_snapshot_attempt = 0.0
    last_reconciliation_attempt = 0.0
    last_reconciliation = None
    last_error = None
    processed = 0
    try:
        while not stopping:
            try:
                result = queue.process_next()
                if result is not None:
                    processed += 1
                    last_error = None
            except Exception as exc:
                result = None
                last_error = {"stage": "ingest", "error_type": type(exc).__name__,
                              "error": str(exc),
                              "created_utc": datetime.now(timezone.utc).isoformat()}
            status = queue.store.status()
            now = time.monotonic()
            if now - last_reconciliation_attempt >= reconciliation_interval_s:
                last_reconciliation_attempt = now
                try:
                    last_reconciliation = reconcile_batch(
                        queue, scan_limit=reconciliation_limit)
                except Exception as exc:
                    last_error = {"stage": "reconciliation",
                                  "error_type": type(exc).__name__, "error": str(exc),
                                  "created_utc": datetime.now(timezone.utc).isoformat()}
            if (publication_root is not None and
                    status["commit_sequence"] != last_published_sequence and
                    now - last_snapshot_attempt >= snapshot_interval_s):
                last_snapshot_attempt = now
                try:
                    last_publication = publish_snapshot(
                        queue.store, store_root, publication_root)
                    last_published_sequence = status["commit_sequence"]
                    last_error = None
                except Exception as exc:
                    last_error = {"stage": "snapshot", "error_type": type(exc).__name__,
                                  "error": str(exc),
                                  "created_utc": datetime.now(timezone.utc).isoformat()}
            report = service_status(queue, last_publication=last_publication,
                                    last_error=last_error)
            report["last_reconciliation"] = last_reconciliation
            if runtime_output is not None:
                atomic_json(Path(runtime_output), report)
            # Reconciliation runs after the first empty dequeue and can add
            # work. In one-shot mode, keep ownership until that bounded batch
            # has also reached a terminal queue state.
            if once and result is None and queue.counts()["ready"] == 0:
                return {**report, "processed": processed}
            if result is None:
                time.sleep(min(poll_s, 60.0))
        final = service_status(queue, last_publication=last_publication,
                               last_error=last_error)
        final["last_reconciliation"] = last_reconciliation
        return {**final, "processed": processed}
    finally:
        for event, handler in previous_handlers.items():
            signal.signal(event, handler)


def run_service(store_root: Path, shared_root: Path, *, database: Path | None = None,
                publication_root: Path | None = None,
                runtime_output: Path | None = None, poll_s: float = 2.0,
                snapshot_interval_s: float = 300.0,
                reconciliation_interval_s: float = 600.0,
                reconciliation_limit: int = 100,
                once: bool = False) -> dict:
    with owner_lock(store_root):
        return _run_service_owned(
            store_root, shared_root, database=database,
            publication_root=publication_root, runtime_output=runtime_output,
            poll_s=poll_s, snapshot_interval_s=snapshot_interval_s,
            reconciliation_interval_s=reconciliation_interval_s,
            reconciliation_limit=reconciliation_limit, once=once)
