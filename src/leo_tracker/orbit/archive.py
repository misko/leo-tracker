"""Immutable, content-addressed TLE history for retrospective simulation."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
from typing import Any

from .artifacts import TLECatalogArtifact, parse_catalog, utc_iso

ARCHIVE_SCHEMA = "leo-tracker.tle-archive/v1"
SNAPSHOT_SCHEMA = "leo-tracker.tle-archive-snapshot/v1"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True)+"\n",
                         encoding="utf-8")
    temporary.replace(path)


def archive_catalog(artifact: TLECatalogArtifact, archive_dir: Path, *,
                    label: str = "starlink") -> dict[str, Any]:
    """Archive one validated catalog and record when it was observed.

    Full catalog content is stored once by SHA-256. Timestamped snapshot
    records remain small when an upstream catalog has not changed.
    """
    root = Path(archive_dir)
    objects = root/"objects"; snapshots = root/"snapshots"
    objects.mkdir(parents=True, exist_ok=True); snapshots.mkdir(parents=True, exist_ok=True)
    tles = parse_catalog(artifact.content, source=artifact.source_url,
                         retrieved_at=artifact.retrieved_at)
    stamp = artifact.retrieved_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    object_path = objects/f"{artifact.sha256}.json"
    if not object_path.exists():
        _atomic_json(object_path, artifact.to_dict())
    snapshot_name = f"{stamp}-{label}-{artifact.sha256[:12]}.json"
    snapshot_path = snapshots/snapshot_name
    record = {"schema": SNAPSHOT_SCHEMA, "label": label,
        "retrieved_at": utc_iso(artifact.retrieved_at),
        "source_url": artifact.source_url, "catalog_sha256": artifact.sha256,
        "object": str(object_path.relative_to(root)), "satellite_count": len(tles),
        "tle_epoch_min": utc_iso(min(item.epoch for item in tles)),
        "tle_epoch_max": utc_iso(max(item.epoch for item in tles))}
    _atomic_json(snapshot_path, record)
    lock_path = root/"index.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index_path = root/"index.json"
        try: index = json.loads(index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            index = {"schema": ARCHIVE_SCHEMA, "snapshots": []}
        if index.get("schema") != ARCHIVE_SCHEMA:
            raise ValueError("unsupported TLE archive index schema")
        entry = {**record, "snapshot": str(snapshot_path.relative_to(root))}
        existing = {(item["retrieved_at"], item["catalog_sha256"], item["label"])
                    for item in index["snapshots"]}
        if (entry["retrieved_at"], entry["catalog_sha256"], entry["label"]) not in existing:
            index["snapshots"].append(entry)
        index["snapshots"].sort(key=lambda item: item["retrieved_at"])
        index["updated_at"] = utc_iso(datetime.now(timezone.utc))
        _atomic_json(index_path, index)
        _atomic_json(root/"latest.json", entry)
    return entry
