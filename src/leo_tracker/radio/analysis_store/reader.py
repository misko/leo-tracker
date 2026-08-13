"""Dashboard reads served from the partitioned Parquet projection.

The dashboard used to rebuild its listing by walking JSON shards. This answers
the same three questions with one query each, against files that any host can
read over NFS without taking a lock or copying anything.

Nothing here writes. A builder may be rewriting a partition while these queries
run: it stages under a name a ``*.parquet`` glob cannot match and renames
atomically, so a reader sees the previous partition or the next one, never a
half-written table.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading

from .partition import PARTITION_DIRECTORY, read_connection


class ParquetAnalysisRepository:
    """Listing, detail and summary from the analysis index.

    Holds one connection rather than one per request. Its views resolve their
    glob when a query runs, not when the view is created, so a partition built
    after this object was constructed is picked up without reconnecting.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._connection = None

    def available(self) -> bool:
        """Whether there is a projection to read at all.

        Checked before connecting so a capture-only host, or one where the
        backfill has not run, degrades to the JSON path rather than raising.
        """
        base = self.root / "reports" / PARTITION_DIRECTORY
        return any(base.glob("pipeline=*/date=*/analysis_runs.parquet"))

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _query(self, sql: str, parameters: list | None = None) -> list[tuple]:
        with self._lock:
            if self._connection is None:
                self._connection = read_connection(self.root)
            return self._connection.execute(sql, parameters or []).fetchall()

    def recent_recordings(self, limit: int = 100) -> list[dict]:
        if limit < 1:
            raise ValueError("listing limit must be positive")
        rows = self._query(
            "SELECT listing_json FROM current_dashboard_records "
            "ORDER BY start_utc DESC NULLS LAST, recording_id DESC LIMIT ?", [limit])
        return [json.loads(row[0]) for row in rows]

    def recording_detail(self, recording_id: str) -> dict | None:
        rows = self._query(
            "SELECT detail_json FROM current_dashboard_records WHERE recording_id = ?",
            [recording_id])
        return None if not rows else json.loads(rows[0][0])

    def summary(self) -> dict:
        row = self._query("""
            SELECT count(*), count(*) FILTER (WHERE confirmed),
                   count(*) FILTER (WHERE decoded), count(*) FILTER (WHERE associated)
            FROM current_dashboard_records""")[0]
        return {"analyzed_capture_count": row[0],
                "temporally_confirmed_capture_count": row[1],
                "decoded_capture_count": row[2],
                "tle_association_capture_count": row[3]}
