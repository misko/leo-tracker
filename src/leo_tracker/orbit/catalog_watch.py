"""One durable scheduler for multiple orbit-catalog sources."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import time
from typing import Callable
from urllib.parse import urlsplit

from .artifacts import parse_catalog, parse_utc, utc_iso
from .catalog_sources import fetch_catalog
from .catalog_store import CatalogStore, publish_catalog


CONFIG_SCHEMA = "leo-tracker.catalog-watch-config/v1"
STATUS_SCHEMA = "leo-tracker.catalog-watch-status/v1"
PROFILE_STATUS_SCHEMA = "leo-tracker.catalog-profile-status/v1"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class CatalogProfile:
    id: str
    source: str
    scope: str
    url: str
    interval_s: int
    minimum_request_interval_s: int = 0
    scheduled_minute: int | None = None
    minimum_satellite_count: int = 1
    maximum_satellite_count: int = 100_000
    maximum_epoch_age_s: int | None = None

    def __post_init__(self) -> None:
        for field, value in (("profile id", self.id), ("source", self.source),
                             ("scope", self.scope)):
            if not SAFE_ID.fullmatch(value):
                raise ValueError(f"unsafe catalog {field}: {value!r}")
        if urlsplit(self.url).scheme != "https":
            raise ValueError("catalog URL must use HTTPS")
        if self.interval_s <= 0 or self.minimum_request_interval_s < 0:
            raise ValueError("catalog intervals must be positive")
        if self.scheduled_minute is not None and not 1 <= self.scheduled_minute <= 58:
            raise ValueError("scheduled_minute must be between 1 and 58")
        if not 0 < self.minimum_satellite_count <= self.maximum_satellite_count:
            raise ValueError("catalog satellite count bounds are invalid")
        if self.maximum_epoch_age_s is not None and self.maximum_epoch_age_s <= 0:
            raise ValueError("maximum_epoch_age_s must be positive")

    @classmethod
    def from_dict(cls, value: dict) -> "CatalogProfile":
        return cls(**value)


def load_config(path: Path) -> tuple[CatalogProfile, ...]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema") != CONFIG_SCHEMA or not isinstance(value.get("profiles"), list):
        raise ValueError("unsupported catalog watch configuration")
    profiles = tuple(CatalogProfile.from_dict(item) for item in value["profiles"])
    if not profiles or len({item.id for item in profiles}) != len(profiles):
        raise ValueError("catalog profile identifiers must be nonempty and unique")
    return profiles


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True); stream.write("\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_state(root: Path, profile: CatalogProfile) -> dict:
    path = root / "state" / f"{profile.id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot safely read scheduler state: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"scheduler state is not an object: {path}")
    return value


def _scheduled_after(moment: datetime, profile: CatalogProfile) -> datetime:
    moment = moment.astimezone(timezone.utc)
    if profile.scheduled_minute is None:
        return moment + timedelta(seconds=profile.interval_s)
    candidate = moment.replace(minute=profile.scheduled_minute, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(hours=1)
    minimum = moment + timedelta(seconds=profile.minimum_request_interval_s)
    while candidate < minimum:
        candidate += timedelta(hours=1)
    return candidate


def initialize_state(root: Path, profile: CatalogProfile, now: datetime) -> dict:
    state = _read_state(root, profile)
    if state.get("next_due_utc") is None:
        if profile.scheduled_minute is None:
            due = now
        else:
            due = now.astimezone(timezone.utc).replace(
                minute=profile.scheduled_minute, second=0, microsecond=0)
            if due < now:
                due += timedelta(hours=1)
        state.update({"schema": PROFILE_STATUS_SCHEMA, "profile": profile.id,
                      "source": profile.source, "scope": profile.scope,
                      "next_due_utc": utc_iso(due)})
        _atomic_json(root / "state" / f"{profile.id}.json", state)
    return state


def _emit(event: str, values: dict, logger: Callable[[str], None]) -> None:
    fields = " ".join(f"{key}={json.dumps(value, separators=(',', ':'))}"
                      for key, value in values.items())
    logger(f"{utc_iso(datetime.now(timezone.utc))} {event}" + (f" {fields}" if fields else ""))


def _validate(profile: CatalogProfile, artifact, now: datetime) -> list:
    tles = parse_catalog(artifact.content, source=artifact.source_url,
                         retrieved_at=artifact.retrieved_at)
    if not profile.minimum_satellite_count <= len(tles) <= profile.maximum_satellite_count:
        raise ValueError(f"catalog count {len(tles)} outside configured bounds")
    if profile.scope == "starlink" and any(
            item.name is None or "STARLINK" not in item.name.upper() for item in tles):
        raise ValueError("Starlink catalog contains an out-of-scope object")
    if len({item.norad_id for item in tles}) != len(tles):
        raise ValueError("catalog contains duplicate NORAD identifiers")
    if profile.maximum_epoch_age_s is not None:
        newest = max(item.epoch for item in tles)
        if (now - newest).total_seconds() > profile.maximum_epoch_age_s:
            raise ValueError("catalog newest element epoch is stale")
    return tles


def run_due_cycle(profiles: tuple[CatalogProfile, ...], root: Path, *,
                  now: datetime | None = None,
                  fetcher=fetch_catalog,
                  logger: Callable[[str], None] = print) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    root = Path(root).resolve(); results = []
    for profile in profiles:
        state = initialize_state(root, profile, now)
        due = parse_utc(state["next_due_utc"])
        if due > now:
            result = {"profile": profile.id, "status": "waiting",
                      "next_due_utc": utc_iso(due)}
            _atomic_json(root / "status" / f"{profile.id}.json", state)
            results.append(result)
            continue
        last_attempt = (parse_utc(state["last_attempt_utc"])
                        if state.get("last_attempt_utc") else None)
        if (last_attempt is not None and
                (now - last_attempt).total_seconds() < profile.minimum_request_interval_s):
            next_due = _scheduled_after(last_attempt, profile)
            state["next_due_utc"] = utc_iso(next_due)
            _atomic_json(root / "state" / f"{profile.id}.json", state)
            result = {"profile": profile.id, "status": "rate_limited",
                      "next_due_utc": utc_iso(next_due)}
            _atomic_json(root / "status" / f"{profile.id}.json", state)
            results.append(result)
            continue
        next_due = _scheduled_after(now, profile)
        state.update({"schema": PROFILE_STATUS_SCHEMA, "profile": profile.id,
                      "source": profile.source, "scope": profile.scope,
                      "last_attempt_utc": utc_iso(now),
                      "next_due_utc": utc_iso(next_due), "status": "fetching"})
        _atomic_json(root / "state" / f"{profile.id}.json", state)
        _emit("fetch_start", {"profile": profile.id, "source": profile.source,
                              "scope": profile.scope}, logger)
        started = time.monotonic()
        try:
            artifact, raw = fetcher(profile.url, now=now)
            tles = _validate(profile, artifact, now)
            manifest = publish_catalog(root, artifact, source=profile.source,
                                       scope=profile.scope, raw=raw)
            state.update({"status": "healthy", "last_success_utc": utc_iso(now),
                          "catalog_sha256": artifact.sha256,
                          "satellite_count": len(tles),
                          "tle_epoch_min": manifest["tle_epoch_min"],
                          "tle_epoch_max": manifest["tle_epoch_max"],
                          "last_error": None})
            result = {"profile": profile.id, "status": "published",
                      "catalog_sha256": artifact.sha256,
                      "satellite_count": len(tles),
                      "next_due_utc": utc_iso(next_due)}
            _emit("catalog_published", {**result,
                "elapsed_s": round(time.monotonic() - started, 3)}, logger)
        except Exception as exc:
            state.update({"status": "error", "last_error": type(exc).__name__,
                          "last_error_utc": utc_iso(now)})
            result = {"profile": profile.id, "status": "failed",
                      "error": type(exc).__name__, "next_due_utc": utc_iso(next_due)}
            _emit("fetch_failed", result, logger)
        _atomic_json(root / "state" / f"{profile.id}.json", state)
        _atomic_json(root / "status" / f"{profile.id}.json", state)
        results.append(result)
    watch = {"schema": STATUS_SCHEMA, "updated_utc": utc_iso(now),
             "profiles": results}
    _atomic_json(root / "status" / "watch.json", watch)
    return watch


def run_watch(config_path: Path, root: Path, *, once: bool = False,
              heartbeat_s: int = 900,
              logger: Callable[[str], None] = print) -> int:
    root = Path(root).resolve(); lock_path = root / "staging" / "watch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another catalog watcher owns {root}") from exc
        stopped = False
        reload_requested = False

        def stop(*_):
            nonlocal stopped; stopped = True

        def reload_config(*_):
            nonlocal reload_requested; reload_requested = True

        previous_term = signal.signal(signal.SIGTERM, stop)
        previous_int = signal.signal(signal.SIGINT, stop)
        previous_hup = signal.signal(signal.SIGHUP, reload_config)
        try:
            profiles = load_config(config_path)
            _emit("watch_start", {"root": str(root), "profiles": [p.id for p in profiles]}, logger)
            while not stopped:
                if reload_requested:
                    profiles = load_config(config_path); reload_requested = False
                    _emit("config_reloaded", {"profiles": [p.id for p in profiles]}, logger)
                run_due_cycle(profiles, root, logger=logger)
                if once: break
                now = datetime.now(timezone.utc)
                due = [parse_utc(initialize_state(root, item, now)["next_due_utc"])
                       for item in profiles]
                delay = min(heartbeat_s, max(1.0, (min(due) - now).total_seconds()))
                deadline = time.monotonic() + delay
                while not stopped and time.monotonic() < deadline:
                    time.sleep(min(1.0, deadline - time.monotonic()))
                if not stopped and delay >= heartbeat_s:
                    _emit("heartbeat", {"next_due_utc": utc_iso(min(due))}, logger)
            _emit("watch_stop", {"status": "clean"}, logger)
            return 0
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGHUP, previous_hup)


def store_status(root: Path) -> dict:
    root = Path(root).resolve(); profiles = []
    for path in sorted((root / "status").glob("*.json")):
        if path.name == "watch.json": continue
        try: profiles.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError): pass
    watch_path = root / "status" / "watch.json"
    try:
        watch = json.loads(watch_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        watch = None
    return {"schema": STATUS_SCHEMA, "root": str(root), "watch": watch,
            "profiles": profiles}
