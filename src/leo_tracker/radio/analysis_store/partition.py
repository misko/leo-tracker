"""Pipeline- and date-partitioned Parquet projection of analysis runs.

Each partition is built in a throwaway local DuckDB and exported to Parquet, so
the schema, its types, and its foreign keys stay the single source of truth
while the published artefact is immutable columnar files that any host can read
over NFS without taking a lock.

Nothing here is authoritative. A partition can be deleted and rebuilt from the
completion receipts at any time.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import uuid

from ..beacon.probe_index import report_date
from .ingest import AnalysisStore, connect
from .mapping import build_input_manifest, load_authenticated, relational_rows
from .schema import initialize

PARTITION_SCHEMA = "leo-tracker.analysis-index/v1"
PARTITION_DIRECTORY = "analysis-index"

#: Tables written to Parquet, named rather than discovered so that a schema
#: change is a deliberate edit here. ``structured_documents`` is deliberately
#: absent: its payloads duplicate report files that ``source_documents`` already
#: records by path, size and digest, and that DuckDB can read in place.
PROJECTED_TABLES = (
    "recordings", "analysis_runs", "analysis_parameters", "analysis_summary",
    "analysis_windows", "probe_checks", "receiver_probes", "followup_checks",
    "confirmed_events", "tracks", "track_points", "decodes", "associations",
    "association_candidates", "source_documents", "artifacts",
    "dashboard_records",
)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    os.replace(temporary, path)


def _null_counts(connection, table: str) -> dict:
    """Columns that arrived null, and how many rows.

    Recorded so schema drift is visible in the manifest rather than discovered
    in a query months later. This corpus gained ``identity.receiver_labels``
    partway through, so partitions either side of that change project the same
    column with and without values, and nothing about the Parquet says so.
    Columns with no nulls are omitted, which keeps presence meaningful.
    """
    columns = [row[0] for row in connection.execute(f"DESCRIBE {table}").fetchall()]
    if not columns:
        return {}
    counts = connection.execute(
        "SELECT " + ", ".join(f'count(*) - count("{column}")' for column in columns) +
        f" FROM {table}").fetchone()
    return {column: int(count)
            for column, count in zip(columns, counts) if count}


def partition_dir(root: Path, pipeline: str, date: str) -> Path:
    return (Path(root) / "reports" / PARTITION_DIRECTORY /
            f"pipeline={pipeline}" / f"date={date}")


def manifest_path(root: Path, pipeline: str, date: str) -> Path:
    return partition_dir(root, pipeline, date) / "partition.json"


def temporary_path(target: Path) -> Path:
    """Where one builder stages a table before the atomic rename.

    Named for this host and process, so two builders never write the same file.
    That is what makes an overlap wasteful rather than destructive, and it holds
    whether or not the advisory lock did its job. The suffix also keeps staged
    files out of a ``*.parquet`` glob, so a reader never sees a partial table.
    """
    node = platform.node().split(".")[0] or "unknown"
    return target.with_suffix(f".parquet.{node}.{os.getpid()}.next")


@contextmanager
def build_lock(root: Path):
    """Advisory, non-blocking: yields whether this caller may build.

    An optimisation, not a guarantee. The partitions sit on an NFS mount more
    than one host can see, and cross-host locking is not dependable, so
    correctness rests on the staging names and the atomic rename instead.
    """
    path = Path(root) / "reports" / PARTITION_DIRECTORY / ".build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()


def completion_receipts(shared_root: Path):
    """Yield ``(pipeline, recording_id, completion_path)`` for every success.

    Deliberately walks the contract path rather than globbing reports, and does
    not depend on the store queue, which this projection replaces.
    """
    runs = Path(shared_root).resolve() / "reports" / "runs"
    for pipeline in sorted(runs.iterdir() if runs.is_dir() else []):
        if not pipeline.is_dir():
            continue
        for recording in sorted(pipeline.iterdir()):
            receipt = recording / "completion.json"
            if recording.is_dir() and receipt.is_file():
                yield pipeline.name, recording.name, receipt


def partition_sources(shared_root: Path, pipeline: str, date: str) -> dict:
    """The receipts one partition is built from, as ``{recording_id: sha256}``.

    Identified by digest rather than by count. A run re-analysed in place keeps
    the count identical while changing the answer, and that is precisely the
    case this corpus produces: ``kalman-full-v1`` re-runs recordings
    ``legacy-v1`` already covered.
    """
    sources = {}
    for found_pipeline, recording_id, receipt in completion_receipts(shared_root):
        if found_pipeline != pipeline or report_date(recording_id) != date:
            continue
        sources[recording_id] = _sha256(receipt)
    return sources


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def receipt_signature(path: Path) -> list:
    """A receipt's cheap identity: size and modification time."""
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def partition_signatures(shared_root: Path, pipeline: str, date: str) -> dict:
    """One partition's receipt signatures, as ``{recording_id: [bytes, mtime]}``."""
    signatures = {}
    for found_pipeline, recording_id, receipt in completion_receipts(shared_root):
        if found_pipeline != pipeline or report_date(recording_id) != date:
            continue
        signatures[recording_id] = receipt_signature(receipt)
    return signatures


def signatures_by_unit(shared_root: Path) -> dict[tuple[str, str], dict]:
    """Every partition's receipt signatures from one walk, without reading.

    Digesting the corpus costs about 280 seconds against 0.1 for stat, so the
    drift scan a timer runs must not hash. Digests remain what the manifest
    records and what a mismatch is confirmed against; stat only decides where
    to look.
    """
    units: dict[tuple[str, str], dict] = {}
    for pipeline, recording_id, receipt in completion_receipts(shared_root):
        date = report_date(recording_id)
        if date is None:
            continue
        units.setdefault((pipeline, date), {})[recording_id] = receipt_signature(receipt)
    return units


def sources_by_unit(shared_root: Path) -> dict[tuple[str, str], dict]:
    """Every partition's sources from a single walk of the receipts.

    ``partition_sources`` re-walks the corpus for one unit, which is the right
    shape for building one partition and the wrong one for asking about all of
    them: fourteen units over twenty-four thousand receipts is fourteen walks.
    """
    units: dict[tuple[str, str], dict] = {}
    for pipeline, recording_id, receipt in completion_receipts(shared_root):
        date = report_date(recording_id)
        if date is None:
            continue
        units.setdefault((pipeline, date), {})[recording_id] = _sha256(receipt)
    return units


def partition_units(shared_root: Path) -> list[tuple[str, str]]:
    """Every ``(pipeline, date)`` the receipts imply, sorted."""
    units = set()
    for pipeline, recording_id, _ in completion_receipts(shared_root):
        date = report_date(recording_id)
        if date is not None:
            units.add((pipeline, date))
    return sorted(units)


def partition_is_current(root: Path, pipeline: str, date: str, signatures: dict, *,
                         shared_root: Path | None = None) -> bool:
    """Whether a built partition still matches the receipts it came from.

    Two tiers. Matching stat signatures settle it outright, which is the case
    almost every scan hits and costs no reads. When a signature moved, the
    recorded digest decides, so a receipt merely re-copied or touched does not
    provoke a rebuild of its whole partition, and one genuinely rewritten does.

    Identity is a digest rather than a count on purpose: re-analysis rewrites a
    receipt without changing how many there are, and this corpus re-runs
    recordings a second pipeline already covered.
    """
    try:
        recorded = json.loads(manifest_path(root, pipeline, date).read_text())
    except (OSError, ValueError):
        return False
    if (recorded.get("schema") != PARTITION_SCHEMA or
            not (partition_dir(root, pipeline, date) / "analysis_runs.parquet").is_file()):
        return False
    if recorded.get("signatures") == signatures:
        return True
    recorded_sources = recorded.get("sources") or {}
    if shared_root is None or set(signatures) != set(recorded_sources):
        return False
    runs = Path(shared_root).resolve() / "reports" / "runs"
    for recording_id, signature in signatures.items():
        if signature == (recorded.get("signatures") or {}).get(recording_id):
            continue
        receipt = runs / pipeline / recording_id / "completion.json"
        if not receipt.is_file() or _sha256(receipt) != recorded_sources.get(recording_id):
            return False
    return True


def build_partition(root: Path, shared_root: Path, pipeline: str, date: str, *,
                    work_dir: Path | None = None, memory_limit: str = "2GB",
                    threads: int = 2) -> dict:
    """Project one ``(pipeline, date)`` into Parquet.

    Builds into a throwaway local DuckDB so the declared schema still enforces
    types and foreign keys, then exports each table. Referential integrity is
    therefore checked per partition rather than per row, which is what replaces
    the constraints Parquet cannot express.
    """
    root, shared_root = Path(root), Path(shared_root)
    sources = partition_sources(shared_root, pipeline, date)
    signatures = partition_signatures(shared_root, pipeline, date)
    if not sources:
        return {"pipeline": pipeline, "date": date, "run_count": 0,
                "built": False, "reason": "no receipts for this partition"}

    target_dir = partition_dir(root, pipeline, date)
    target_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=work_dir, prefix="analysis-index-"))
    staged: list[tuple[Path, Path]] = []
    try:
        database = work / "partition.duckdb"
        store = AnalysisStore(database, shared_root)
        connection = connect(database)
        try:
            connection.execute(f"SET memory_limit='{memory_limit}'")
            connection.execute(f"SET threads={int(threads)}")
            initialize(connection)
            excluded = []
            for recording_id in sorted(sources):
                try:
                    manifest = build_input_manifest(shared_root, recording_id,
                                                    pipeline)
                    manifest, completion, documents = load_authenticated(
                        manifest, shared_root)
                    rows = relational_rows(manifest, completion, documents,
                                           shared_root)
                    store.commit_run(connection, manifest, completion, rows)
                except Exception as exc:
                    # A report is one mutable path shared by every pipeline and
                    # every re-analysis, while receipts are per-pipeline and
                    # immutable. Re-analysing a recording therefore leaves older
                    # receipts attesting to a file that no longer exists, and
                    # roughly a tenth of this corpus is in that state
                    # permanently. Aborting would mean the projection can never
                    # be built at all; excluding the run keeps it buildable, and
                    # naming the run keeps the loss visible rather than silent.
                    excluded.append({"recording_id": recording_id,
                                     "error_type": type(exc).__name__,
                                     "error": str(exc)[:200]})
            counts, nulls = {}, {}
            for table in PROJECTED_TABLES:
                final = target_dir / f"{table}.parquet"
                temporary = temporary_path(final)
                connection.execute(
                    f"COPY {table} TO '{temporary}' (FORMAT parquet, COMPRESSION zstd)")
                staged.append((temporary, final))
                counts[table] = connection.execute(
                    f"SELECT count(*) FROM {table}").fetchone()[0]
                arrived_null = _null_counts(connection, table)
                if arrived_null:
                    nulls[table] = arrived_null
        finally:
            connection.close()
        # Reported so an operator can size the scratch a large partition needs.
        # The build database still commits once per run, so it carries the same
        # block slack the retired live store did; it is simply thrown away.
        work_bytes = database.stat().st_size if database.is_file() else 0

        for temporary, final in staged:
            os.replace(temporary, final)
        staged = []
        # The manifest is the commit marker, so it lands only once every table
        # has. A build interrupted before this leaves a stale manifest beside
        # newer data, which reads as not-current and rebuilds.
        record = {"schema": PARTITION_SCHEMA, "pipeline": pipeline, "date": date,
                  "run_count": len(sources), "sources": sources, "rows": counts,
                  "signatures": signatures, "excluded": excluded,
                  "excluded_count": len(excluded),
                  "nulls": nulls, "tables": list(PROJECTED_TABLES),
                  "built_utc": datetime.now(timezone.utc).isoformat()}
        _atomic_json(manifest_path(root, pipeline, date), record)
        return {**record, "built": True, "work_bytes": work_bytes}
    finally:
        for temporary, _ in staged:
            Path(temporary).unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def read_connection(root: Path, *, memory_limit: str = "2GB", threads: int = 2):
    """A DuckDB connection with a view per table spanning every partition.

    In memory and read-only in effect: it opens no database file and takes no
    lock, which is what lets any number of readers on any host query while a
    builder writes. ``union_by_name`` lets an older partition lack a column a
    newer one gained, so adding a column stays backward compatible.
    """
    import duckdb
    connection = duckdb.connect()
    connection.execute(f"SET memory_limit='{memory_limit}'")
    connection.execute(f"SET threads={int(threads)}")
    base = Path(root) / "reports" / PARTITION_DIRECTORY
    for table in PROJECTED_TABLES:
        glob = f"{base}/pipeline=*/date=*/{table}.parquet"
        connection.execute(
            f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{glob}', "
            "hive_partitioning = true, union_by_name = true)")
    connection.execute("""
        CREATE VIEW current_runs AS SELECT * EXCLUDE (_rank) FROM (
          SELECT analysis_runs.*, row_number() OVER (
            PARTITION BY recording_id
            ORDER BY completed_utc DESC, commit_sequence DESC, run_id DESC) AS _rank
          FROM analysis_runs) WHERE _rank = 1""")
    return connection


def query(root: Path, sql: str) -> list[tuple]:
    """Run one statement against the projection and return its rows."""
    connection = read_connection(root)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def partition_status(root: Path, shared_root: Path) -> dict:
    """What is built, what has drifted, and what arrived null.

    Reports receipts against rows so a partition that silently stopped being
    rebuilt is visible as a number rather than as a missing answer.
    """
    partitions, stale = [], 0
    for (pipeline, date), signatures in sorted(
            signatures_by_unit(shared_root).items()):
        current = partition_is_current(root, pipeline, date, signatures,
                                       shared_root=shared_root)
        stale += 0 if current else 1
        entry = {"pipeline": pipeline, "date": date, "receipts": len(signatures),
                 "current": current}
        try:
            recorded = json.loads(manifest_path(root, pipeline, date).read_text())
        except (OSError, ValueError):
            entry["built"] = None
        else:
            entry["built"] = recorded.get("run_count")
            entry["built_utc"] = recorded.get("built_utc")
            entry["rows"] = recorded.get("rows", {}).get("analysis_runs")
            # Surfaced here because a partition that projected a tenth of its
            # receipts is still "current"; without a number, that reads as
            # completeness.
            entry["excluded"] = recorded.get("excluded_count", 0)
            if recorded.get("nulls"):
                entry["null_columns"] = {
                    table: sorted(columns)
                    for table, columns in recorded["nulls"].items()}
        partitions.append(entry)
    return {"schema": PARTITION_SCHEMA, "partitions": partitions,
            "partition_count": len(partitions), "stale_count": stale,
            "excluded_total": sum(entry.get("excluded") or 0
                                  for entry in partitions)}


def build(root: Path, shared_root: Path, *, rebuild: bool = False,
          limit: int | None = None, work_dir: Path | None = None) -> dict:
    """Rebuild every partition whose receipts no longer match what was built.

    A refresh firing while one is still running is normal rather than an error:
    the timer fires on a fixed period and a build takes as long as its partition
    is large. Overlap is reported, not raised.
    """
    with build_lock(root) as held:
        if not held:
            return {"schema": PARTITION_SCHEMA, "built": [], "current": [],
                    "partition_count": 0, "skipped_locked": True}
        results, skipped = [], []
        for (pipeline, date), signatures in sorted(
                signatures_by_unit(shared_root).items()):
            if limit is not None and len(results) >= limit:
                break
            if not rebuild and partition_is_current(
                    root, pipeline, date, signatures, shared_root=shared_root):
                skipped.append({"pipeline": pipeline, "date": date,
                                "run_count": len(signatures)})
                continue
            results.append(build_partition(root, shared_root, pipeline, date,
                                           work_dir=work_dir))
        return {"schema": PARTITION_SCHEMA, "built": results, "current": skipped,
                "partition_count": len(results) + len(skipped),
                "skipped_locked": False}
