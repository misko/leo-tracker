"""Versioned, non-destructive replay of historical Doppler track assembly."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Callable

from leo_tracker.orbit import Observer
from leo_tracker.orbit.association import associate_tracks

from .continuous import track_capture


PLAN_SCHEMA = "leo-tracker.track-replay-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.track-replay-receipt/v1"
DEFAULT_REPLAY_ID = "continuity-gap15-reacq15k-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_id(value: str) -> str:
    if not value or any(character not in
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                        for character in value):
        raise ValueError(f"invalid replay identity: {value!r}")
    return value


def _artifact(path: Path) -> dict:
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _resolve_recorded_path(value: str | None, fallback: Path) -> Path:
    recorded = Path(value).resolve() if value else fallback.resolve()
    return recorded if recorded.exists() else fallback.resolve()


def _candidate(root: Path, old_track_path: Path, *, maximum_gap_s: float,
               maximum_reacquisition_span_hz: float,
               minimum_dual_observations: int,
               minimum_track_count: int,
               association_minimum_duration_s: float) -> tuple[dict | None, str]:
    name = old_track_path.stem
    old_track = _read_json(old_track_path)
    configuration = old_track.get("configuration", {})
    summary = old_track.get("summary", {})
    old_gap = float(configuration.get("maximum_gap_s", 5.0))
    old_span = float(configuration.get("maximum_reacquisition_span_hz", 5_000.0))
    if old_gap >= maximum_gap_s and old_span >= maximum_reacquisition_span_hz:
        return None, "already_current"
    dual = int(summary.get("dual_valid_observation_count") or 0)
    track_count = int(summary.get("track_count") or 0)
    longest = float(summary.get("longest_dual_valid_duration_s") or 0.0)
    if dual < minimum_dual_observations:
        return None, "insufficient_dual_observations"
    if track_count < minimum_track_count:
        return None, "not_fragmented"
    if longest >= association_minimum_duration_s:
        return None, "already_long_enough"

    reports = root / "reports"
    old_association_path = reports / "associations" / f"{name}.json"
    if not old_association_path.is_file():
        return None, "missing_old_association"
    old_association = _read_json(old_association_path)
    catalog_value = old_association.get("catalog", {})
    catalog = _resolve_recorded_path(
        catalog_value.get("resolved_object_path") or catalog_value.get("path"),
        root / "context" / "missing-tle-catalog.json")
    followup = _resolve_recorded_path(old_track.get("source_followup"),
                                      reports / "followups" / f"{name}.json")
    frame_track = _resolve_recorded_path(old_track.get("source_frame_track"),
                                         reports / "frame-tracks" / f"{name}.json")
    capture = _resolve_recorded_path(old_track.get("capture"), root / "captures" / name)
    manifest = capture / "manifest.json"
    required = {"followup": followup, "frame_track": frame_track,
                "tle_catalog": catalog,
                "old_track": old_track_path, "old_association": old_association_path}
    embedded_manifest = None
    if manifest.is_file():
        required["capture_manifest"] = manifest
    elif isinstance(old_track.get("capture_manifest"), dict):
        embedded_manifest = old_track["capture_manifest"]
    else:
        required["capture_manifest"] = manifest
    if frame_track.is_file():
        frame_report = _read_json(frame_track)
        samples_value = frame_report.get("samples", {}).get("path")
        samples = (Path(samples_value) if samples_value else
                   frame_track.with_suffix(".npz"))
        if not samples.is_absolute():
            samples = frame_track.parent / samples
        required["frame_samples"] = samples.resolve()
    missing = [key for key, path in required.items() if not path.is_file()]
    if missing:
        return None, "missing_" + "_and_".join(missing)
    return {
        "name": name,
        "capture": str(capture.resolve()),
        "inputs": {key: _artifact(path) for key, path in required.items()},
        "embedded_capture_manifest": embedded_manifest,
        "old_configuration": configuration,
        "old_summary": summary,
    }, "selected"


def create_replay_plan(root: Path, *, replay_id: str = DEFAULT_REPLAY_ID,
                       maximum_gap_s: float = 15.0,
                       maximum_reacquisition_span_hz: float = 15_000.0,
                       minimum_dual_observations: int = 45,
                       minimum_track_count: int = 2,
                       association_minimum_duration_s: float = 20.0) -> dict:
    """Inventory historical outputs and atomically publish an immutable replay plan."""
    root = Path(root).resolve(); replay_id = _safe_id(replay_id)
    replay_root = root / "reports" / "replays" / replay_id
    plan_path = replay_root / "plan.json"
    settings = {
        "maximum_gap_s": maximum_gap_s,
        "maximum_reacquisition_span_hz": maximum_reacquisition_span_hz,
        "minimum_dual_observations": minimum_dual_observations,
        "minimum_track_count": minimum_track_count,
        "association_minimum_duration_s": association_minimum_duration_s,
    }
    if plan_path.is_file():
        existing = _read_json(plan_path)
        if existing.get("schema") != PLAN_SCHEMA or existing.get("settings") != settings:
            raise ValueError(f"replay plan identity collision: {replay_id}")
        return existing

    exclusions: dict[str, int] = {}
    jobs = []
    track_root = root / "reports" / "tracks"
    for path in sorted(track_root.glob("*.json")):
        try:
            job, reason = _candidate(root, path, **settings)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            job, reason = None, "invalid_artifact"
        if job is None:
            exclusions[reason] = exclusions.get(reason, 0) + 1
        else:
            jobs.append(job)
    plan = {
        "schema": PLAN_SCHEMA,
        "replay_id": replay_id,
        "created_utc": _utc_now(),
        "root": str(root),
        "settings": settings,
        "job_count": len(jobs),
        "excluded": dict(sorted(exclusions.items())),
        "jobs": jobs,
    }
    _atomic_json(plan_path, plan)
    return plan


def _qualified_identities(report: dict) -> list[dict]:
    return [{"track_id": item.get("track_id"),
             "norad_id": item.get("best_norad_id"),
             "name": next((candidate.get("name") for candidate in
                           item.get("candidates", [])
                           if candidate.get("norad_id") == item.get("best_norad_id")), None),
             "held_out_rms_hz": item.get("best_holdout_residual_rms_hz"),
             "margin_hz": item.get("margin_to_second_hz")}
            for item in report.get("associations", []) if item.get("qualified")]


def _run_one(root_value: str, replay_id: str, name: str,
             observer_values: tuple[float, float, float]) -> dict:
    root = Path(root_value); replay_root = root / "reports" / "replays" / replay_id
    completion = replay_root / "jobs" / name / "completion.json"
    if completion.is_file():
        return {"name": name, "status": "already_complete"}
    locks = replay_root / "locks"; locks.mkdir(parents=True, exist_ok=True)
    lock = locks / f"{name}.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return {"name": name, "status": "already_running"}
    os.ftruncate(descriptor, 0)
    os.write(descriptor, json.dumps({"pid": os.getpid(), "started_utc": _utc_now()}).encode())
    staging = replay_root / "staging" / f"{name}.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        plan = _read_json(replay_root / "plan.json")
        job = next((item for item in plan["jobs"] if item["name"] == name), None)
        if job is None:
            raise ValueError(f"job is not in replay plan: {name}")
        for artifact in job["inputs"].values():
            path = Path(artifact["path"])
            if path.stat().st_size != artifact["bytes"] or _sha256(path) != artifact["sha256"]:
                raise ValueError(f"replay input changed after planning: {path}")
        staging.mkdir(parents=True)
        track_path = staging / "track.json"
        association_path = staging / "association.json"
        settings = plan["settings"]
        track = track_capture(
            Path(job["capture"]), Path(job["inputs"]["followup"]["path"]), track_path,
            measurement_source="conditioned_frames",
            frame_track_path=Path(job["inputs"]["frame_track"]["path"]),
            capture_manifest=job.get("embedded_capture_manifest"),
            maximum_gap_s=float(settings["maximum_gap_s"]),
            maximum_reacquisition_span_hz=float(
                settings["maximum_reacquisition_span_hz"]))
        observer = Observer(*observer_values)
        association = associate_tracks(
            track_path, Path(job["inputs"]["tle_catalog"]["path"]), association_path,
            observer=observer)
        final_dir = replay_root / "jobs" / name
        final_track = final_dir / "track.json"
        # The association records provenance; point it at the atomically published path.
        association["source_observations"] = str(final_track.resolve())
        _atomic_json(association_path, association)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "replay_id": replay_id,
            "name": name,
            "status": "success",
            "completed_utc": _utc_now(),
            "inputs": job["inputs"],
            "settings": settings,
            "old": {"configuration": job["old_configuration"],
                    "summary": job["old_summary"],
                    "qualified_identities": _qualified_identities(
                        _read_json(job["inputs"]["old_association"]["path"]))},
            "new": {"summary": track["summary"],
                    "association_summary": association["summary"],
                    "qualified_identities": _qualified_identities(association)},
            "outputs": {
                "track": {"path": str(final_track.resolve()),
                          "bytes": track_path.stat().st_size,
                          "sha256": _sha256(track_path)},
                "association": {"path": str((final_dir / "association.json").resolve()),
                                "bytes": association_path.stat().st_size,
                                "sha256": _sha256(association_path)},
            },
        }
        _atomic_json(staging / "completion.json", receipt)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(staging, final_dir)
        except FileExistsError:
            shutil.rmtree(staging, ignore_errors=True)
            if completion.is_file():
                return {"name": name, "status": "already_complete"}
            raise
        (replay_root / "failures" / f"{name}.json").unlink(missing_ok=True)
        return {"name": name, "status": "succeeded",
                "qualified": receipt["new"]["qualified_identities"]}
    except Exception as error:
        shutil.rmtree(staging, ignore_errors=True)
        _atomic_json(replay_root / "failures" / f"{name}.json", {
            "schema": RECEIPT_SCHEMA, "replay_id": replay_id, "name": name,
            "status": "failed", "failed_utc": _utc_now(),
            "error": f"{type(error).__name__}: {error}"})
        return {"name": name, "status": "failed", "error": str(error)}
    finally:
        os.ftruncate(descriptor, 0)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _lock_is_held(path: Path) -> bool:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def replay_status(root: Path, *, replay_id: str = DEFAULT_REPLAY_ID) -> dict:
    root = Path(root).resolve(); replay_id = _safe_id(replay_id)
    replay_root = root / "reports" / "replays" / replay_id
    plan = _read_json(replay_root / "plan.json")
    complete = list((replay_root / "jobs").glob("*/completion.json"))
    failures = [path for path in (replay_root / "failures").glob("*.json")
                if not (replay_root / "jobs" / path.stem / "completion.json").is_file()]
    running = [path for path in (replay_root / "locks").glob("*.lock")
               if _lock_is_held(path)]
    identities = []
    for path in complete:
        identities.extend(_read_json(path).get("new", {}).get("qualified_identities", []))
    total = int(plan["job_count"]); done = len(complete)
    return {"schema": RECEIPT_SCHEMA, "replay_id": replay_id,
            "job_count": total, "completed_count": done,
            "remaining_count": max(total - done, 0), "failed_count": len(failures),
            "running_count": len(running),
            "qualified_association_count": len(identities),
            "qualified_norad_ids": sorted({item["norad_id"] for item in identities
                                           if item.get("norad_id") is not None})}


def run_replay(root: Path, *, replay_id: str = DEFAULT_REPLAY_ID, workers: int = 1,
               observer: Observer = Observer(37.849165355010086,
                                             -122.48567658142287, 0.0),
               limit: int | None = None,
               names: list[str] | None = None,
               progress: Callable[[dict], None] | None = None) -> dict:
    """Resume an immutable replay plan with bounded parallelism."""
    if workers < 1 or limit is not None and limit < 1:
        raise ValueError("workers and limit must be positive")
    root = Path(root).resolve(); replay_id = _safe_id(replay_id)
    plan = _read_json(root / "reports" / "replays" / replay_id / "plan.json")
    replay_root = root / "reports" / "replays" / replay_id
    planned_names = {item["name"] for item in plan["jobs"]}
    if names is not None:
        unknown = sorted(set(names) - planned_names)
        if unknown:
            raise ValueError(f"recordings are not in replay plan: {', '.join(unknown)}")
        selected_names = list(dict.fromkeys(names))
    else:
        selected_names = [item["name"] for item in plan["jobs"]]
    names = [name for name in selected_names
             if not (replay_root / "jobs" / name / "completion.json").is_file()]
    if limit is not None:
        names = names[:limit]
    results = []
    observer_values = (observer.latitude_deg, observer.longitude_deg, observer.altitude_m)
    if workers == 1:
        for name in names:
            results.append(_run_one(str(root), replay_id, name, observer_values))
            if progress is not None:
                progress({"completed": len(results), "scheduled": len(names),
                          "result": results[-1]})
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(_run_one, str(root), replay_id, name,
                                   observer_values): name for name in names}
            for future in as_completed(pending):
                results.append(future.result())
                if progress is not None:
                    progress({"completed": len(results), "scheduled": len(names),
                              "result": results[-1]})
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return {"replay_id": replay_id, "attempted_count": len(results),
            "results": counts, "status": replay_status(root, replay_id=replay_id)}
