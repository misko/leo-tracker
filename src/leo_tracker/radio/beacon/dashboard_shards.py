"""Date-sharded dashboard listing that stays cheap as the corpus grows.

The monolithic index rewrote every row on every update, so a single capture
cost a full-file read-modify-write. That forced the index onto the one
single-threaded producer that could safely serialize it, which was the capture
watcher rather than the analysis server that actually writes the reports. The
result was an index frozen thousands of recordings behind.

Three properties fix that:

  * a producer writes one small file per recording and never reads the index,
    so sixteen analysis workers can emit rows concurrently without a lock;
  * compaction folds those rows into one bounded shard per UTC day, so no file
    grows without limit and a rebuild is per-day rather than corpus-wide;
  * the listing carries only what a table row displays, leaving the multi-MB
    detail to be built on demand.

Row files are a write-ahead buffer, not the durable artifact: they are removed
only after the shard containing them has been renamed into place.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid


LISTING_ROW_SCHEMA = "leo-tracker.dashboard-listing-row/v1"
SHARD_SCHEMA = "leo-tracker.dashboard-index-shard/v1"
SUMMARY_SCHEMA = "leo-tracker.dashboard-index-summary/v1"

ROWS_DIRECTORY = "dashboard-rows"
SHARD_DIRECTORY = "dashboard-index"

# Every recording name carries its UTC start, but not always in the same
# position: hop children append a session suffix, and dual-radio captures
# insert a radio identity before the stamp. The first stamp is the capture's.
_STAMP = re.compile(r"(\d{8})T(\d{6})Z")

# What a table row shows. Detail pages build the full record on demand, so
# holding statistics, plots, and artifact lists here would grow every shard
# for data the listing never renders.
LISTING_FIELDS = (
    "recording_id", "kind", "start_utc", "status", "confirmed", "decoded",
    "channel", "region", "mode", "gain", "duration_s", "sample_rate_hz",
    "if_center_hz", "rf_center_hz", "radio_id", "candidate_count",
    "dual_candidate_count", "beacon_detected_count", "continuous_track_count",
    "longest_track_duration_s", "qualified_tle_association_count",
    "fingerprint_family", "detail_url", "satellite_name", "satellite_norad_id",
    "source_fit_hz", "source_identity_agreement",
)


def _qualified_identity(path: Path) -> dict | None:
    """Best qualified identity in one association artifact, if any."""
    value = _read_json(path)
    if value is None:
        return None
    for association in value.get("associations", []):
        if not association.get("qualified"):
            continue
        primary = (association.get("stability") or {}).get("primary") or {}
        return {"norad_id": primary.get("best_norad_id"),
                "name": (primary.get("best_name") or "").strip() or None,
                "holdout_residual_rms_hz": primary.get("holdout_residual_rms_hz")}
    return None


def identity_fields(root: Path, name: str, sources: tuple[str, ...] = ()) -> dict:
    """Which satellite a recording identified, and how well each provider fitted it.

    A count of qualified associations does not say which spacecraft was found,
    which is the result the whole pipeline exists to produce. Providers are
    reported side by side because a shared identity across independently
    retrieved catalogs is far stronger evidence than either alone.

    Reading these costs one small file per provider, so callers should ask only
    for recordings already known to have qualified.
    """
    reports = Path(root) / "reports"
    fields: dict = {}
    primary = _qualified_identity(reports / "associations" / f"{name}.json")
    identities = {}
    fits = {}
    for source in sources:
        found = _qualified_identity(reports / "associations" / source / f"{name}.json")
        if found is None:
            continue
        identities[source] = found.get("norad_id")
        if found.get("holdout_residual_rms_hz") is not None:
            fits[source] = round(float(found["holdout_residual_rms_hz"]), 1)
        primary = primary or found
    if primary:
        fields["satellite_name"] = primary.get("name")
        fields["satellite_norad_id"] = primary.get("norad_id")
    if fits:
        fields["source_fit_hz"] = fits
    if len(identities) > 1:
        # Only meaningful when more than one provider actually qualified it.
        fields["source_identity_agreement"] = len(set(identities.values())) == 1
    return {key: value for key, value in fields.items() if value is not None}


def recording_date(name: str) -> str | None:
    """UTC date a recording started, from its name alone.

    Sharding must not depend on reading reports; that is the cost the old
    index paid on every rebuild.
    """
    match = _STAMP.search(name)
    if match is None:
        return None
    day = match.group(1)
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def listing_row(record: dict) -> dict:
    """Project a full dashboard record down to what a listing renders."""
    row = {field: record.get(field) for field in LISTING_FIELDS
           if record.get(field) is not None}
    row["schema"] = LISTING_ROW_SCHEMA
    return row


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(temporary, path)


def write_listing_row(root: Path, name: str, record: dict,
                      sources: tuple[str, ...] = ()) -> Path:
    """Publish one recording's listing row. Safe from concurrent producers."""
    path = Path(root) / "reports" / ROWS_DIRECTORY / f"{name}.json"
    row = listing_row(record)
    row.setdefault("recording_id", name)
    if record.get("qualified_tle_association_count"):
        row.update(identity_fields(root, name, sources))
    _atomic_json(path, row)
    return path


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def compact_shards(root: Path, *, remove_rows: bool = True) -> dict:
    """Fold pending listing rows into their UTC-day shards.

    Idempotent: a row that arrives mid-run is simply folded by the next one.
    Rows are deleted only after their shard has been renamed into place, so an
    interrupted compaction repeats work rather than losing it.
    """
    root = Path(root).resolve()
    rows_directory = root / "reports" / ROWS_DIRECTORY
    shard_directory = root / "reports" / SHARD_DIRECTORY
    pending: dict[str, dict[str, dict]] = {}
    consumed: dict[str, list[Path]] = {}
    undated = 0
    if rows_directory.is_dir():
        for path in sorted(rows_directory.glob("*.json")):
            row = _read_json(path)
            if row is None:
                continue
            name = row.get("recording_id") or path.stem
            date = recording_date(str(name))
            if date is None:
                undated += 1
                continue
            pending.setdefault(date, {})[str(name)] = row
            consumed.setdefault(date, []).append(path)

    folded = 0
    for date, rows in sorted(pending.items()):
        shard_path = shard_directory / f"{date}.json"
        existing = _read_json(shard_path) or {}
        merged = {str(item.get("recording_id")): item
                  for item in existing.get("rows", [])
                  if item.get("recording_id")}
        merged.update(rows)
        _atomic_json(shard_path, {
            "schema": SHARD_SCHEMA, "date": date,
            "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "row_count": len(merged),
            "rows": sorted(merged.values(),
                           key=lambda item: str(item.get("start_utc") or ""))})
        folded += len(rows)
        if remove_rows:
            for path in consumed.get(date, []):
                path.unlink(missing_ok=True)

    return {"folded_rows": folded, "shards_written": len(pending),
            "undated_rows": undated, **write_summary(root)}


def write_summary(root: Path) -> dict:
    """Recompute the small counters a dashboard header needs.

    Reads only shard headers' row counts, so this stays cheap no matter how
    many recordings exist.
    """
    root = Path(root).resolve()
    shard_directory = root / "reports" / SHARD_DIRECTORY
    by_date: dict[str, int] = {}
    total = confirmed = decoded = associated = 0
    for path in sorted(shard_directory.glob("*.json")):
        if path.name == "summary.json":
            continue
        shard = _read_json(path)
        if shard is None or shard.get("schema") != SHARD_SCHEMA:
            continue
        rows = shard.get("rows", [])
        by_date[str(shard.get("date") or path.stem)] = len(rows)
        total += len(rows)
        for row in rows:
            confirmed += bool(row.get("confirmed"))
            decoded += bool(row.get("decoded"))
            associated += bool(row.get("qualified_tle_association_count"))
    summary = {"schema": SUMMARY_SCHEMA,
               "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "recording_count": total, "confirmed_count": confirmed,
               "decoded_count": decoded,
               "qualified_association_count": associated,
               "by_date": by_date}
    _atomic_json(shard_directory / "summary.json", summary)
    return {"recording_count": total, "shard_count": len(by_date)}


def read_listing(root: Path, *, limit: int = 100) -> list[dict]:
    """Newest listing rows, reading only as many shards as the limit needs."""
    if limit < 1:
        raise ValueError("listing limit must be positive")
    shard_directory = Path(root).resolve() / "reports" / SHARD_DIRECTORY
    rows: list[dict] = []
    for path in sorted(shard_directory.glob("*.json"), reverse=True):
        if path.name == "summary.json":
            continue
        shard = _read_json(path)
        if shard is None or shard.get("schema") != SHARD_SCHEMA:
            continue
        rows.extend(shard.get("rows", []))
        if len(rows) >= limit:
            break
    rows.sort(key=lambda item: str(item.get("start_utc") or ""), reverse=True)
    return rows[:limit]


def migrate_index(index_path: Path, root: Path,
                  sources: tuple[str, ...] = ()) -> dict:
    """Build shards from an already-rebuilt monolithic index.

    Parsing the reports is what makes a rebuild expensive, and the monolithic
    index is that work already done. Projecting it costs one file read rather
    than rereading every report.
    """
    index = _read_json(Path(index_path))
    if index is None:
        raise ValueError(f"cannot read dashboard index: {index_path}")
    recordings = index.get("recordings")
    if isinstance(recordings, dict):
        records = list(recordings.values())
    elif isinstance(recordings, list):
        records = recordings
    else:
        raise ValueError("dashboard index has no recordings")

    root = Path(root).resolve()
    shard_directory = root / "reports" / SHARD_DIRECTORY
    by_date: dict[str, dict[str, dict]] = {}
    undated = 0
    for record in records:
        name = str(record.get("recording_id") or "")
        date = recording_date(name)
        if not name or date is None:
            undated += 1
            continue
        row = listing_row(record)
        row.setdefault("recording_id", name)
        # Roughly one recording in a hundred qualifies, so reading association
        # artifacts for the rest would cost thousands of pointless round trips
        # for a field that would be empty anyway.
        if record.get("qualified_tle_association_count"):
            row.update(identity_fields(root, name, sources))
        by_date.setdefault(date, {})[name] = row

    for date, rows in sorted(by_date.items()):
        shard_path = shard_directory / f"{date}.json"
        existing = _read_json(shard_path) or {}
        merged = {str(item.get("recording_id")): item
                  for item in existing.get("rows", [])
                  if item.get("recording_id")}
        merged.update(rows)
        _atomic_json(shard_path, {
            "schema": SHARD_SCHEMA, "date": date,
            "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "row_count": len(merged),
            "rows": sorted(merged.values(),
                           key=lambda item: str(item.get("start_utc") or ""))})

    return {"migrated_rows": sum(len(rows) for rows in by_date.values()),
            "shards_written": len(by_date), "undated_rows": undated,
            **write_summary(root)}
