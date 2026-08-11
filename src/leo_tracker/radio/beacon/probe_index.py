"""A queryable projection of the per-probe facts held in beacon reports.

A report is the durable record of one capture and is deliberately fat: it keeps
every acquisition hypothesis so a result can be re-examined years later. That
makes it a poor thing to ask questions of. Counting detections per LNB across
the corpus means opening seventeen thousand files totalling 34 GB, which takes
about five minutes and forces most analysis into sampling.

This module projects the handful of per-probe columns that questions actually
use into day-partitioned Parquet, roughly a thousandfold smaller, and leaves
the reports untouched as the system of record. Nothing here is authoritative:
a partition can be deleted and rebuilt from the reports at any time.

Two properties are deliberate rather than incidental.

The column schema is written out rather than inferred. Inference would walk
every nested array in a multi-megabyte document and exhaust memory on a capture
host; naming the columns avoids that. More importantly it is a contract, so a
report whose shape has drifted fails the ingest instead of quietly yielding
nulls that look like an absence of detections.

Partitions record what they were built from. A derived table that silently
disagrees with its source is worse than no derived table, because both produce
plausible answers and only one is right.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

PROBE_INDEX_SCHEMA = "leo-tracker.probe-index/v1"
PARTITION_DIRECTORY = "probe-index"
#: Recording stamps carry the UTC date, so a partition is decided by the name
#: rather than by a file time that changes whenever an artifact is re-copied.
_STAMP = re.compile(r"(\d{8})T\d{6}Z")

#: The columns read out of each report. Written out rather than inferred: this
#: is what keeps ingest inside a few hundred megabytes, and what makes a report
#: whose shape has changed fail loudly instead of returning nulls.
PROBE_COLUMNS = {
    "capture_manifest": (
        "STRUCT("
        " identity STRUCT(radio_id VARCHAR, serial VARCHAR,"
        "                 receiver_labels VARCHAR[]),"
        " metadata STRUCT(channel_number INTEGER, region VARCHAR,"
        "                 observation_mode VARCHAR),"
        " created_utc_ns BIGINT,"
        " gain_mode VARCHAR,"
        " sample_statistics STRUCT(receivers STRUCT("
        "     rms_magnitude DOUBLE, near_full_scale_fraction DOUBLE)[]))"),
    "exact_checks": (
        "STRUCT("
        " start_s DOUBLE, candidate BOOLEAN, qualified BOOLEAN,"
        " cfo_difference_hz DOUBLE,"
        " receiver_candidates BOOLEAN[], receiver_qualified BOOLEAN[],"
        " receivers STRUCT(acquisition STRUCT(exact_match STRUCT("
        "     frequency_offset_hz DOUBLE, epoch_sample INTEGER,"
        "     score DOUBLE), match_score_margin DOUBLE))[])[]"),
    "lnb_calibration": "STRUCT(applied BOOLEAN, reason VARCHAR)",
}

#: One row per probe per receiver. A dual-receiver capture is two observations
#: of one instant, and the whole point of the pair is that they can disagree,
#: so they are kept apart rather than folded together.
_PROJECTION = """
SELECT
  regexp_extract(filename, '[^/]+$')                            AS report,
  r.capture_manifest.identity.radio_id                          AS radio,
  r.capture_manifest.identity.serial                            AS serial,
  r.capture_manifest.metadata.channel_number                    AS channel,
  r.capture_manifest.metadata.region                            AS region,
  r.capture_manifest.metadata.observation_mode                  AS mode,
  r.capture_manifest.gain_mode                                  AS gain_mode,
  r.capture_manifest.created_utc_ns / 1e9                       AS capture_utc,
  coalesce(r.lnb_calibration.applied, false)                    AS calibrated,
  ck.start_s                                                    AS start_s,
  ck.candidate                                                  AS dual_candidate,
  ck.qualified                                                  AS dual_qualified,
  ck.cfo_difference_hz                                          AS cfo_difference_hz,
  rxi                                                           AS rx,
  r.capture_manifest.identity.receiver_labels[rxi + 1]          AS lnb,
  ck.receiver_candidates[rxi + 1]                               AS candidate,
  ck.receiver_qualified[rxi + 1]                                AS qualified,
  ck.receivers[rxi + 1].acquisition.exact_match.frequency_offset_hz AS offset_hz,
  ck.receivers[rxi + 1].acquisition.exact_match.epoch_sample        AS epoch,
  ck.receivers[rxi + 1].acquisition.match_score_margin              AS margin,
  r.capture_manifest.sample_statistics.receivers[rxi + 1].rms_magnitude AS rms,
  r.capture_manifest.sample_statistics.receivers[rxi + 1].near_full_scale_fraction
                                                                AS near_full_scale
FROM read_json(?, columns=$columns, filename=true,
               maximum_object_size=100000000) r,
     UNNEST(r.exact_checks) AS t(ck),
     UNNEST([0, 1]) AS u(rxi)
"""


class ProbeIndexUnavailable(RuntimeError):
    """Raised when DuckDB is absent, so a caller can degrade rather than fail."""


def _connect(memory_limit: str = "1200MB", threads: int = 2):
    """Open DuckDB, bounded so an ingest cannot starve a capture host.

    Imported here rather than at module scope: the capture path must not fail
    to start because an analysis dependency is missing.
    """
    try:
        import duckdb
    except ImportError as exc:                       # pragma: no cover - env
        raise ProbeIndexUnavailable(
            "duckdb is required for the probe index; "
            "install the 'analysis' extra") from exc
    connection = duckdb.connect()
    connection.execute(f"SET memory_limit='{memory_limit}'")
    connection.execute(f"SET threads={int(threads)}")
    connection.execute("SET preserve_insertion_order=false")
    return connection


def report_date(name: str) -> str | None:
    """UTC date a report belongs to, from its recording stamp."""
    found = _STAMP.search(str(name))
    if not found:
        return None
    stamp = found.group(1)
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"


def partition_path(root: Path, date: str) -> Path:
    return Path(root) / "reports" / PARTITION_DIRECTORY / f"date={date}" / "probes.parquet"


def _manifest_path(root: Path, date: str) -> Path:
    return partition_path(root, date).with_name("partition.json")


def source_reports(root: Path, date: str, pattern: str = "*narrow*") -> list[Path]:
    """Reports belonging to one UTC day, by recording stamp."""
    reports = Path(root) / "reports"
    return sorted(path for path in reports.glob(f"{pattern}.json")
                  if report_date(path.name) == date)


def partition_is_current(root: Path, date: str, sources: list[Path]) -> bool:
    """Whether a built partition still matches the reports it came from.

    Compares the source count rather than trusting the file to exist. A day
    that gained reports after being built would otherwise answer questions
    from a stale projection, which is the failure this whole module exists to
    avoid reproducing.
    """
    try:
        recorded = json.loads(_manifest_path(root, date).read_text())
    except (OSError, ValueError):
        return False
    return (partition_path(root, date).is_file()
            and recorded.get("source_count") == len(sources)
            and recorded.get("schema") == PROBE_INDEX_SCHEMA)


def build_partition(root: Path, date: str, *, pattern: str = "*narrow*",
                    memory_limit: str = "1200MB", threads: int = 2) -> dict:
    """Project one UTC day of reports into Parquet."""
    sources = source_reports(root, date, pattern)
    target = partition_path(root, date)
    if not sources:
        return {"date": date, "source_count": 0, "rows": 0, "built": False,
                "reason": "no reports for this date"}
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(memory_limit, threads)
    columns = "{" + ", ".join(f"'{k}': '{v}'" for k, v in PROBE_COLUMNS.items()) + "}"
    query = _PROJECTION.replace("$columns", columns)
    temporary = target.with_suffix(".parquet.next")
    connection.execute(
        f"COPY ({query}) TO '{temporary}' (FORMAT parquet, COMPRESSION zstd)",
        [[str(path) for path in sources]])
    rows = connection.execute("SELECT count(*) FROM read_parquet(?)",
                              [str(temporary)]).fetchone()[0]
    os.replace(temporary, target)
    manifest = {"schema": PROBE_INDEX_SCHEMA, "date": date,
                "source_count": len(sources), "rows": int(rows),
                "built_utc": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"),
                "pattern": pattern}
    _manifest_path(root, date).write_text(json.dumps(manifest, indent=2,
                                                    sort_keys=True) + "\n")
    return {**manifest, "built": True}


def build(root: Path, *, pattern: str = "*narrow*", rebuild: bool = False,
          limit: int | None = None) -> dict:
    """Project every day that is missing or has gained reports since it was built."""
    reports = Path(root) / "reports"
    dates = sorted({report_date(path.name)
                    for path in reports.glob(f"{pattern}.json")} - {None})
    built, skipped = [], []
    for date in dates:
        sources = source_reports(root, date, pattern)
        if not rebuild and partition_is_current(root, date, sources):
            skipped.append(date)
            continue
        built.append(build_partition(root, date, pattern=pattern))
        if limit is not None and len(built) >= limit:
            break
    return {"schema": PROBE_INDEX_SCHEMA, "dates": len(dates),
            "built": built, "skipped": skipped,
            "rows": sum(item["rows"] for item in built)}


def query(root: Path, sql: str, *, memory_limit: str = "1200MB",
          threads: int = 2) -> list[tuple]:
    """Run one query against every partition.

    ``probes`` is bound to the whole partitioned dataset, so a caller writes
    ordinary SQL and never names a path.
    """
    pattern = str(Path(root) / "reports" / PARTITION_DIRECTORY /
                  "date=*" / "probes.parquet")
    connection = _connect(memory_limit, threads)
    connection.execute(
        f"CREATE VIEW probes AS SELECT * FROM read_parquet('{pattern}', "
        f"hive_partitioning=true)")
    return connection.execute(sql).fetchall()


def partition_status(root: Path, *, pattern: str = "*narrow*") -> list[dict]:
    """What is built, and whether it still matches its reports."""
    reports = Path(root) / "reports"
    dates = sorted({report_date(path.name)
                    for path in reports.glob(f"{pattern}.json")} - {None})
    status = []
    for date in dates:
        sources = source_reports(root, date, pattern)
        current = partition_is_current(root, date, sources)
        recorded = {}
        try:
            recorded = json.loads(_manifest_path(root, date).read_text())
        except (OSError, ValueError):
            pass
        status.append({"date": date, "source_count": len(sources),
                       "indexed_count": recorded.get("source_count"),
                       "rows": recorded.get("rows"), "current": current})
    return status
