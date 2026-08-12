"""Transactional owner for the live analysis DuckDB database."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from .identity import canonical_json
from .mapping import relational_rows, validate_input_manifest
from .schema import SCHEMA_NAME, SCHEMA_VERSION, initialize, validate


class AnalysisStoreUnavailable(RuntimeError):
    """Raised when the optional DuckDB analysis dependency is absent."""


def _mount_type(path: Path) -> str | None:
    """Return the longest matching Linux mount's filesystem type."""
    target = Path(path).resolve()
    candidate = target if target.exists() else target.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    matches = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:  # pragma: no cover - non-Linux development host
        return None
    for line in lines:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        trailing = after.split()
        if len(fields) < 5 or not trailing:
            continue
        mount = Path(fields[4].replace("\\040", " ")).resolve()
        if candidate == mount or candidate.is_relative_to(mount):
            matches.append((len(mount.parts), trailing[0]))
    return max(matches, default=(0, None))[1]


def require_local_database(path: Path) -> None:
    filesystem = _mount_type(path)
    if filesystem in {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"}:
        raise ValueError(
            f"live analysis DuckDB must be on Kalman-local storage, not {filesystem}: {path}")


def connect(path: Path, *, read_only: bool = False):
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - deployment environment
        raise AnalysisStoreUnavailable(
            "duckdb is required for the analysis store; install the analysis extra") from exc
    path = Path(path)
    if not read_only:
        require_local_database(path)
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path), read_only=read_only)
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='2GB'")
    return connection


def _parse_utc(value: str | None) -> datetime:
    if not value:
        raise ValueError("completion receipt lacks completed_utc")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AnalysisStore:
    """Open the database only for bounded owner operations.

    The service using this class is the sole process allowed to request
    read-write connections to the live path.
    """

    def __init__(self, database: Path, shared_root: Path):
        self.database = Path(database).resolve()
        self.shared_root = Path(shared_root).resolve()

    def initialize(self) -> dict:
        connection = connect(self.database)
        try:
            initialize(connection)
        finally:
            connection.close()
        return self.status()

    @staticmethod
    def _many(connection, sql: str, rows: list[tuple]) -> None:
        if rows:
            connection.executemany(sql, rows)

    def ingest(self, manifest_path: Path, *, fail_after: str | None = None) -> dict:
        """Authenticate and atomically commit one queued run.

        ``fail_after`` is an intentional transaction fault-injection seam used
        by tests; production callers leave it unset.
        """
        manifest, completion, documents = validate_input_manifest(
            Path(manifest_path), self.shared_root)
        rows = relational_rows(manifest, completion, documents, self.shared_root)
        run_id = manifest["run_id"]
        connection = connect(self.database)
        try:
            initialize(connection)
            if connection.execute("SELECT 1 FROM analysis_runs WHERE run_id = ?",
                                  [run_id]).fetchone():
                return {"run_id": run_id, "inserted": False,
                        "commit_sequence": connection.execute(
                            "SELECT commit_sequence FROM analysis_runs WHERE run_id = ?",
                            [run_id]).fetchone()[0]}
            connection.execute("BEGIN TRANSACTION")
            recording_id, manifest_sha = rows["recording"][:2]
            existing = connection.execute(
                "SELECT capture_manifest_sha256 FROM recordings WHERE recording_id = ?",
                [recording_id]).fetchone()
            if existing is not None and existing[0] != manifest_sha:
                raise ValueError(f"capture manifest collision for {recording_id}")
            if existing is None:
                connection.execute(
                    "INSERT INTO recordings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows["recording"])
            sequence = int(connection.execute(
                "SELECT commit_sequence FROM store_metadata WHERE singleton = TRUE"
            ).fetchone()[0]) + 1
            connection.execute(
                "INSERT INTO analysis_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, manifest["recording_id"], manifest["pipeline_id"],
                 str(manifest.get("mode") or completion.get("mode") or "unknown"),
                 _parse_utc(completion.get("completed_utc")),
                 manifest["completion"]["sha256"],
                 (manifest.get("context") or {}).get("path"),
                 bool(completion.get("confirmed")), bool(completion.get("full_coverage")),
                 canonical_json(manifest), datetime.now(timezone.utc), sequence])
            connection.execute("INSERT INTO analysis_parameters VALUES (?, ?)",
                               [run_id, rows["parameters"]])
            connection.execute(
                "INSERT INTO analysis_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows["summary"])
            self._many(connection,
                "INSERT INTO analysis_windows VALUES (?, ?, ?, ?, ?, ?)", rows["windows"])
            self._many(connection,
                "INSERT INTO probe_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows["probes"])
            self._many(connection,
                "INSERT INTO receiver_probes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows["receiver_probes"])
            if fail_after == "probes":
                raise RuntimeError("injected failure after probes")
            self._many(connection,
                "INSERT INTO followup_checks VALUES (?, ?, ?, ?, ?, ?, ?)", rows["followups"])
            self._many(connection,
                "INSERT INTO confirmed_events VALUES (?, ?, ?, ?, ?)", rows["events"])
            self._many(connection,
                "INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows["tracks"])
            self._many(connection,
                "INSERT INTO track_points VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows["track_points"])
            if rows["decode"] is not None:
                connection.execute("INSERT INTO decodes VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   rows["decode"])
            self._many(connection,
                "INSERT INTO associations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows["associations"])
            self._many(connection,
                "INSERT INTO association_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows["association_candidates"])
            self._many(connection,
                "INSERT INTO structured_documents VALUES (?, ?, ?, ?)", rows["documents"])
            self._many(connection,
                "INSERT INTO source_documents VALUES (?, ?, ?, ?, ?, ?)", rows["sources"])
            self._many(connection,
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?)", rows["artifacts"])
            connection.execute("INSERT INTO dashboard_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               rows["dashboard"])
            if fail_after == "dashboard":
                raise RuntimeError("injected failure after dashboard")
            connection.execute(
                "UPDATE store_metadata SET commit_sequence = ?, last_run_id = ?, "
                "updated_utc = ? WHERE singleton = TRUE",
                [sequence, run_id, datetime.now(timezone.utc)])
            connection.execute("COMMIT")
            return {"run_id": run_id, "recording_id": manifest["recording_id"],
                    "inserted": True, "commit_sequence": sequence,
                    "probe_count": len(rows["probes"]),
                    "receiver_probe_count": len(rows["receiver_probes"])}
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def status(self) -> dict:
        if not self.database.is_file():
            return {"schema": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
                    "database": str(self.database), "initialized": False,
                    "run_count": 0, "recording_count": 0, "commit_sequence": 0}
        connection = connect(self.database, read_only=True)
        try:
            validate(connection)
            metadata = connection.execute(
                "SELECT commit_sequence, last_run_id, "
                "CAST(updated_utc AS VARCHAR) FROM store_metadata "
                "WHERE singleton = TRUE").fetchone()
            return {"schema": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
                    "duckdb_version": connection.execute(
                        "SELECT version()").fetchone()[0],
                    "database": str(self.database), "initialized": True,
                    "database_bytes": self.database.stat().st_size,
                    "run_count": connection.execute(
                        "SELECT count(*) FROM analysis_runs").fetchone()[0],
                    "recording_count": connection.execute(
                        "SELECT count(*) FROM recordings").fetchone()[0],
                    "probe_count": connection.execute(
                        "SELECT count(*) FROM probe_checks").fetchone()[0],
                    "receiver_probe_count": connection.execute(
                        "SELECT count(*) FROM receiver_probes").fetchone()[0],
                    "commit_sequence": metadata[0], "last_run_id": metadata[1],
                    "updated_utc": metadata[2]}
        finally:
            connection.close()

    def query(self, sql: str) -> tuple[list[str], list[tuple]]:
        connection = connect(self.database, read_only=True)
        try:
            validate(connection)
            cursor = connection.execute(sql)
            columns = [item[0] for item in cursor.description]
            return columns, cursor.fetchall()
        finally:
            connection.close()

    def run_ids(self) -> set[str]:
        if not self.database.is_file():
            return set()
        connection = connect(self.database, read_only=True)
        try:
            validate(connection)
            return {row[0] for row in connection.execute(
                "SELECT run_id FROM analysis_runs").fetchall()}
        finally:
            connection.close()
