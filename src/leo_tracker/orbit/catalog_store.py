"""Read-only multi-source orbit catalog store and atomic publisher."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from sgp4.api import Satrec

from .artifacts import TLECatalogArtifact, parse_catalog, parse_utc, utc_iso
from .tle import TLE


SNAPSHOT_SCHEMA = "leo-tracker.catalog-store-snapshot/v1"
INDEX_SCHEMA = "leo-tracker.catalog-store-index/v1"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class CatalogError(RuntimeError):
    """Base class for catalog-store failures."""


class CatalogNotFound(CatalogError):
    pass


class CatalogCorrupt(CatalogError):
    pass


class CatalogStale(CatalogError):
    pass


def _identity(value: str, field: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise ValueError(f"unsafe {field}: {value!r}")
    return value


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}")
    with temporary.open("wb") as stream:
        stream.write(content); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogCorrupt(f"cannot read catalog metadata: {path}") from exc
    if not isinstance(value, dict):
        raise CatalogCorrupt(f"catalog metadata is not an object: {path}")
    return value


@dataclass(frozen=True)
class CatalogSnapshot:
    source: str
    scope: str
    retrieved_at: datetime
    sha256: str
    satellite_count: int
    epoch_min: datetime
    epoch_max: datetime
    tles: tuple[TLE, ...]
    raw_artifact: Path
    normalized_artifact: Path
    manifest_path: Path

    def by_norad(self, norad_id: int) -> TLE:
        matches = [item for item in self.tles if item.norad_id == norad_id]
        if not matches:
            raise CatalogNotFound(f"NORAD {norad_id} is absent from catalog {self.sha256}")
        if len(matches) != 1:
            raise CatalogCorrupt(f"NORAD {norad_id} occurs more than once")
        return matches[0]

    def by_name(self, name: str) -> tuple[TLE, ...]:
        expected = name.casefold()
        return tuple(item for item in self.tles
                     if item.name is not None and item.name.casefold() == expected)

    def to_satrecs(self) -> tuple[Satrec, ...]:
        return tuple(Satrec.twoline2rv(item.line1, item.line2) for item in self.tles)


class CatalogStore:
    """Verified, network-free reader for a filesystem catalog archive."""

    def __init__(self, root: Path, *, verify: bool = True):
        self.root = Path(root).resolve()
        self.verify = verify

    @classmethod
    def open(cls, root: Path, *, verify: bool = True) -> "CatalogStore":
        return cls(root, verify=verify)

    def _index_path(self, source: str, scope: str) -> Path:
        return self.root / "indexes" / _identity(source, "source") / f"{_identity(scope, 'scope')}.json"

    def _entries(self, source: str, scope: str) -> list[dict]:
        path = self._index_path(source, scope)
        if not path.is_file():
            raise CatalogNotFound(f"no catalog history for {source}/{scope}")
        index = _json(path)
        if index.get("schema") != INDEX_SCHEMA:
            raise CatalogCorrupt(f"unsupported catalog index: {path}")
        entries = index.get("snapshots")
        if not isinstance(entries, list):
            raise CatalogCorrupt(f"catalog index has no snapshot list: {path}")
        return sorted(entries, key=lambda item: item["retrieved_at"])

    def _load(self, entry: dict) -> CatalogSnapshot:
        manifest_path = self.root / entry["snapshot"]
        manifest = _json(manifest_path)
        if manifest.get("schema") != SNAPSHOT_SCHEMA:
            raise CatalogCorrupt(f"unsupported snapshot: {manifest_path}")
        normalized = self.root / manifest["normalized_object"]
        try:
            stored_artifact = TLECatalogArtifact.read(normalized)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CatalogCorrupt(f"cannot read catalog object: {normalized}") from exc
        if stored_artifact.sha256 != manifest.get("catalog_sha256"):
            raise CatalogCorrupt(f"snapshot/object hash mismatch: {manifest_path}")
        # Content objects are deduplicated across retrievals and providers, but
        # provenance belongs to the retrieval manifest, never to whichever
        # source happened to publish the bytes first.
        artifact = TLECatalogArtifact(
            manifest["source_url"], parse_utc(manifest["retrieved_at"]),
            stored_artifact.content, stored_artifact.sha256)
        raw = self.root / manifest["raw_object"]
        if self.verify:
            try:
                raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
            except OSError as exc:
                raise CatalogCorrupt(f"missing raw catalog object: {raw}") from exc
            if raw_hash != manifest.get("raw_sha256"):
                raise CatalogCorrupt(f"raw catalog hash mismatch: {raw}")
        tles = tuple(parse_catalog(artifact.content, source=artifact.source_url,
                                   retrieved_at=artifact.retrieved_at))
        if len(tles) != int(manifest["satellite_count"]):
            raise CatalogCorrupt(f"catalog count mismatch: {manifest_path}")
        if len({item.norad_id for item in tles}) != len(tles):
            raise CatalogCorrupt(f"duplicate NORAD identifiers: {manifest_path}")
        return CatalogSnapshot(
            source=manifest["source"], scope=manifest["scope"],
            retrieved_at=parse_utc(manifest["retrieved_at"]),
            sha256=artifact.sha256, satellite_count=len(tles),
            epoch_min=min(item.epoch for item in tles),
            epoch_max=max(item.epoch for item in tles), tles=tles,
            raw_artifact=raw, normalized_artifact=normalized,
            manifest_path=manifest_path)

    def latest(self, *, source: str, scope: str,
               maximum_age_s: float | None = None,
               now: datetime | None = None) -> CatalogSnapshot:
        pointer = self.root / "latest" / _identity(source, "source") / f"{_identity(scope, 'scope')}.json"
        if not pointer.is_file():
            raise CatalogNotFound(f"no latest catalog for {source}/{scope}")
        snapshot = self._load(_json(pointer))
        if maximum_age_s is not None:
            moment = now or datetime.now(timezone.utc)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            age = (moment.astimezone(timezone.utc) - snapshot.retrieved_at).total_seconds()
            if age > maximum_age_s:
                raise CatalogStale(f"catalog is {age:.0f}s old; maximum is {maximum_age_s:.0f}s")
        return snapshot

    def history(self, *, source: str, scope: str,
                start: datetime | None = None,
                end: datetime | None = None) -> tuple[CatalogSnapshot, ...]:
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must be timezone-aware")
        if end is not None and (end.tzinfo is None or end.utcoffset() is None):
            raise ValueError("end must be timezone-aware")
        return tuple(self._load(item) for item in self._entries(source, scope)
                     if (start is None or parse_utc(item["retrieved_at"]) >= start) and
                        (end is None or parse_utc(item["retrieved_at"]) <= end))

    def at_time(self, moment: datetime, *, source: str, scope: str,
                knowledge: str = "available_then") -> CatalogSnapshot:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("catalog selection time must be timezone-aware")
        snapshots = self.history(source=source, scope=scope)
        if knowledge == "available_then":
            candidates = [item for item in snapshots if item.retrieved_at <= moment]
            if not candidates:
                raise CatalogNotFound(f"no {source}/{scope} catalog was available by {utc_iso(moment)}")
            return max(candidates, key=lambda item: item.retrieved_at)
        if knowledge == "best_ephemeris":
            if not snapshots:
                raise CatalogNotFound(f"no {source}/{scope} catalog exists")
            timestamp = moment.timestamp()
            return min(snapshots, key=lambda snapshot: min(
                abs(item.epoch.timestamp() - timestamp) for item in snapshot.tles))
        raise ValueError("knowledge must be available_then or best_ephemeris")

    def satellite_history(self, norad_id: int, *, source: str,
                          scope: str) -> tuple[TLE, ...]:
        result = []
        for snapshot in self.history(source=source, scope=scope):
            try:
                result.append(snapshot.by_norad(norad_id))
            except CatalogNotFound:
                pass
        return tuple(result)


def publish_catalog(root: Path, artifact: TLECatalogArtifact, *, source: str,
                    scope: str, raw: bytes | None = None,
                    media_type: str = "text/plain") -> dict:
    """Atomically publish one source-tagged retrieval and update its pointer."""
    root = Path(root).resolve(); source = _identity(source, "source"); scope = _identity(scope, "scope")
    raw = artifact.content.encode() if raw is None else raw
    raw_sha = hashlib.sha256(raw).hexdigest()
    tles = parse_catalog(artifact.content, source=artifact.source_url,
                         retrieved_at=artifact.retrieved_at)
    if len({item.norad_id for item in tles}) != len(tles):
        raise ValueError("catalog contains duplicate NORAD identifiers")
    lock_path = root / "staging" / "publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw_path = root / "raw" / source / f"{raw_sha}.tle"
        normalized = root / "objects" / f"{artifact.sha256}.json"
        if not raw_path.exists(): _atomic_bytes(raw_path, raw)
        if not normalized.exists(): _atomic_json(normalized, artifact.to_dict())
        stamp = artifact.retrieved_at.strftime("%Y%m%dT%H%M%SZ")
        relative = Path("snapshots") / source / scope / artifact.retrieved_at.strftime("%Y/%m/%d") / f"{stamp}-{artifact.sha256[:12]}.json"
        manifest = {"schema": SNAPSHOT_SCHEMA, "source": source, "scope": scope,
            "retrieved_at": utc_iso(artifact.retrieved_at),
            "source_url": artifact.source_url, "catalog_sha256": artifact.sha256,
            "raw_sha256": raw_sha, "raw_media_type": media_type,
            "raw_object": str(raw_path.relative_to(root)),
            "normalized_object": str(normalized.relative_to(root)),
            "satellite_count": len(tles),
            "tle_epoch_min": utc_iso(min(item.epoch for item in tles)),
            "tle_epoch_max": utc_iso(max(item.epoch for item in tles)),
            "snapshot": str(relative)}
        snapshot_path = root / relative
        if snapshot_path.exists() and _json(snapshot_path) != manifest:
            raise CatalogCorrupt(f"snapshot collision: {snapshot_path}")
        _atomic_json(snapshot_path, manifest)
        index_path = root / "indexes" / source / f"{scope}.json"
        if index_path.exists():
            index = _json(index_path)
            if index.get("schema") != INDEX_SCHEMA:
                raise CatalogCorrupt(f"unsupported catalog index: {index_path}")
        else:
            index = {"schema": INDEX_SCHEMA, "source": source, "scope": scope,
                     "snapshots": []}
        key = (manifest["retrieved_at"], manifest["catalog_sha256"])
        existing = {(item["retrieved_at"], item["catalog_sha256"])
                    for item in index["snapshots"]}
        if key not in existing: index["snapshots"].append(manifest)
        index["snapshots"].sort(key=lambda item: item["retrieved_at"])
        index["updated_at"] = utc_iso(datetime.now(timezone.utc))
        _atomic_json(index_path, index)
        latest = max(index["snapshots"], key=lambda item: item["retrieved_at"])
        _atomic_json(root / "latest" / source / f"{scope}.json", latest)
    return manifest
