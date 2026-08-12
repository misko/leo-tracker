"""Read-only repository and verified local snapshot cache for dashboards."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
import uuid

from .ingest import connect
from .snapshot import sha256_file, verify_snapshot


class SnapshotCache:
    def __init__(self, pointer: Path, cache_dir: Path):
        self.pointer = Path(pointer).resolve()
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._generation = None
        self._path: Path | None = None
        self._lock = threading.RLock()

    def current(self) -> Path:
        with self._lock:
            try:
                pointer = json.loads(self.pointer.read_text())
                generation = str(pointer["generation"])
                relative = Path(pointer["snapshot"])
                source = (self.pointer.parent / relative).resolve()
                if not source.is_relative_to(self.pointer.parent):
                    raise ValueError("snapshot pointer escapes publication root")
                target = self.cache_dir / f"analysis-{generation}.duckdb"
                if generation != self._generation or not target.is_file():
                    private = target.with_name(
                        f".{target.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
                    shutil.copy2(source, private)
                    if (private.stat().st_size != int(pointer["bytes"]) or
                            sha256_file(private) != pointer["sha256"]):
                        private.unlink(missing_ok=True)
                        raise ValueError("dashboard snapshot cache verification failed")
                    verify_snapshot(private)
                    os.replace(private, target)
                    self._generation, self._path = generation, target
                return self._path or target
            except Exception:
                if self._path is not None and self._path.is_file():
                    return self._path
                raise


class DuckDBAnalysisRepository:
    def __init__(self, database: Path | None = None, *, pointer: Path | None = None,
                 cache_dir: Path | None = None):
        if database is None and (pointer is None or cache_dir is None):
            raise ValueError("repository needs a database or pointer and cache directory")
        self.database = None if database is None else Path(database).resolve()
        self.cache = (None if pointer is None else SnapshotCache(pointer, cache_dir))

    def _path(self) -> Path:
        return self.database if self.cache is None else self.cache.current()

    def recent_recordings(self, limit: int = 100) -> list[dict]:
        if limit < 1:
            raise ValueError("listing limit must be positive")
        connection = connect(self._path(), read_only=True)
        try:
            rows = connection.execute(
                "SELECT listing_json FROM current_dashboard_records "
                "ORDER BY start_utc DESC NULLS LAST, recording_id DESC LIMIT ?", [limit]
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()

    def recording_detail(self, recording_id: str) -> dict | None:
        connection = connect(self._path(), read_only=True)
        try:
            row = connection.execute(
                "SELECT detail_json FROM current_dashboard_records WHERE recording_id = ?",
                [recording_id]).fetchone()
            return None if row is None else json.loads(row[0])
        finally:
            connection.close()

    def summary(self) -> dict:
        connection = connect(self._path(), read_only=True)
        try:
            row = connection.execute("""
                SELECT count(*), count(*) FILTER (WHERE confirmed),
                       count(*) FILTER (WHERE decoded),
                       count(*) FILTER (WHERE associated)
                FROM current_dashboard_records
            """).fetchone()
            return {"analyzed_capture_count": row[0],
                    "temporally_confirmed_capture_count": row[1],
                    "decoded_capture_count": row[2],
                    "tle_association_capture_count": row[3]}
        finally:
            connection.close()
