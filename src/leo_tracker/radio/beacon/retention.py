"""Evidence-aware retention for high-rate beacon captures."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from .artifact import SCHEMA


PIN_SCHEMA = "leo-tracker.qualified-capture-pins/v1"


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _artifact_capture_sources(path: Path, roots: tuple[Path, ...],
                              seen: set[Path] | None = None) -> set[Path]:
    """Follow a track or channel-link artifact back to its raw IQ captures."""
    path = path.resolve()
    seen = set() if seen is None else seen
    if path in seen:
        return set()
    seen.add(path)
    artifact = _read_json(path)
    if artifact is None:
        return set()
    captures: set[Path] = set()
    capture = artifact.get("capture")
    if isinstance(capture, str):
        candidate = Path(capture).resolve()
        if _inside(candidate, roots):
            captures.add(candidate)
    references: set[Path] = set()
    source_observations = artifact.get("source_observations")
    if isinstance(source_observations, str):
        references.add(Path(source_observations))
    for reference in artifact.get("source_track_artifacts", []):
        if isinstance(reference, str):
            references.add(Path(reference))
    for track in artifact.get("tracks", []):
        if not isinstance(track, dict):
            continue
        for observation in track.get("observations", []):
            if not isinstance(observation, dict):
                continue
            source_track = observation.get("source_track")
            if isinstance(source_track, dict) and isinstance(source_track.get("report"), str):
                references.add(Path(source_track["report"]))
    for reference in references:
        captures.update(_artifact_capture_sources(reference, roots, seen))
    return captures


def _qualified_capture_pins(root: Path, *, dry_run: bool) -> tuple[set[Path], Path, list[str]]:
    """Discover qualified evidence and durably remember every source capture."""
    reports_root = root / "reports"
    captures_root = root / "captures"
    hops_root = root / "hop-sessions"
    roots = (captures_root.resolve(), hops_root.resolve())
    ledger_path = reports_root / "retention" / "qualified-capture-pins.json"
    ledger = _read_json(ledger_path) or {}
    entries: dict[str, dict] = {}
    for item in ledger.get("captures", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = Path(item["path"]).resolve()
        if _inside(path, roots):
            entries[str(path)] = dict(item)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    discovered: list[str] = []
    for association_path in sorted((reports_root / "associations").glob("*.json")):
        association = _read_json(association_path)
        if association is None or not any(
                isinstance(item, dict) and item.get("qualified") is True
                for item in association.get("associations", [])):
            continue
        source = association.get("source_observations")
        if not isinstance(source, str):
            continue
        for capture in _artifact_capture_sources(Path(source), roots):
            key = str(capture)
            source_name = str(association_path.resolve())
            if key not in entries:
                entries[key] = {"path": key, "first_seen_utc": now,
                                "source_associations": [source_name]}
                discovered.append(key)
            else:
                sources = set(entries[key].get("source_associations", []))
                sources.add(source_name)
                entries[key]["source_associations"] = sorted(sources)

    if not dry_run:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": PIN_SCHEMA, "updated_utc": now,
                   "captures": [entries[key] for key in sorted(entries)]}
        temporary = ledger_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary.replace(ledger_path)
    return {Path(key) for key in entries}, ledger_path, discovered


def _capture_mode(manifest: dict, capture: Path) -> str:
    metadata = manifest.get("metadata", {})
    if isinstance(metadata, dict) and isinstance(metadata.get("observation_mode"), str):
        return metadata["observation_mode"]
    for mode in ("oversample", "narrow", "wide"):
        if f"-{mode}-" in capture.name:
            return mode
    return "other"


def _active_analysis_jobs(root: Path) -> set[str]:
    """Return captures which a local or remote analysis worker may still read."""
    queue = root / "staging" / "analysis-queue"
    active: set[str] = set()
    for pattern in ("*.job", "*.running.*"):
        for marker in queue.glob(pattern):
            try:
                name = marker.read_text().split("\t", 1)[0].strip()
            except OSError:
                continue
            if name:
                active.add(name)
    return active


def _fully_derived(root: Path, capture: Path, mode: str) -> tuple[bool, bool]:
    reports = root / "reports"
    report = _read_json(reports / f"{capture.name}.json")
    followup = _read_json(reports / "followups" / f"{capture.name}.json")
    if report is None or followup is None:
        return False, False
    confirmed = bool(followup.get("confirmation", {}).get("confirmed"))
    # Narrow and oversampled confirmed observations enter the decoder. Wide
    # acquisitions deliberately do not, and a missing continuous track can be
    # the valid result of an insufficiently coherent seed.
    if confirmed and mode in {"narrow", "oversample", "other"} and not (
            reports / "decoded" / f"{capture.name}.json").is_file():
        return False, confirmed
    return True, confirmed


def _remove_directories(paths: list[Path], root: Path, *, dry_run: bool) -> list[str]:
    removed: list[str] = []
    for path in paths:
        if path.parent.resolve() != root.resolve():
            raise ValueError(f"refusing to remove capture outside storage root: {path}")
        removed.append(str(path))
        if not dry_run:
            shutil.rmtree(path)
    return removed


def apply_retention(root: Path, *, keep_negative: int = 6,
                    keep_confirmed: int = 8, keep_wide: int = 2,
                    keep_oversample: int = 4, keep_hop_sessions: int = 6,
                    dry_run: bool = False) -> dict:
    limits = (keep_negative, keep_confirmed, keep_wide, keep_oversample,
              keep_hop_sessions)
    if any(value < 0 for value in limits):
        raise ValueError("retention counts cannot be negative")
    root = Path(root).resolve()
    captures_root = (root / "captures").resolve()
    reports_root = (root / "reports").resolve()
    if captures_root.parent != root or reports_root.parent != root:
        raise ValueError("invalid beacon storage root")
    quarantine_root = (root / "quarantine").resolve()
    pins, pin_ledger, newly_pinned = _qualified_capture_pins(root, dry_run=dry_run)
    active_jobs = _active_analysis_jobs(root)

    # Captures used by a learned template are scientific fixtures independent
    # of the ordinary rolling rings.
    learned_sources: set[Path] = set()
    for learned in (reports_root / "learned-beacons").glob("*.json"):
        value = _read_json(learned)
        if value is not None and isinstance(value.get("capture"), str):
            learned_sources.add(Path(value["capture"]).resolve())

    negative: list[tuple[int, Path]] = []
    confirmed: list[tuple[int, Path]] = []
    bounded_modes: dict[str, list[tuple[int, Path]]] = {"wide": [], "oversample": []}
    protected: list[str] = []
    incomplete: list[str] = []
    interrupted: list[Path] = []
    for capture in sorted(captures_root.iterdir() if captures_root.exists() else []):
        if not capture.is_dir():
            continue
        # A parallel server can have written enough derivatives for a capture
        # to look eligible while a worker still needs its IQ for a later stage.
        if capture.name in active_jobs:
            protected.append(str(capture)); continue
        manifest = _read_json(capture / "manifest.json")
        if manifest is None:
            incomplete.append(str(capture)); continue
        if manifest.get("schema") == SCHEMA and manifest.get("state") == "interrupted":
            interrupted.append(capture); continue
        if manifest.get("schema") != SCHEMA or manifest.get("state") != "complete":
            incomplete.append(str(capture)); continue
        if capture.resolve() in pins or capture.resolve() in learned_sources:
            protected.append(str(capture)); continue
        mode = _capture_mode(manifest, capture)
        derived, is_confirmed = _fully_derived(root, capture, mode)
        if not derived:
            protected.append(str(capture)); continue
        item = (int(manifest.get("created_utc_ns", 0)), capture)
        if mode in bounded_modes:
            bounded_modes[mode].append(item)
        elif is_confirmed:
            confirmed.append(item)
        else:
            negative.append(item)

    negative.sort(); confirmed.sort()
    for values in bounded_modes.values():
        values.sort()
    removable_negative = negative[:-keep_negative] if keep_negative else negative
    removable_confirmed = confirmed[:-keep_confirmed] if keep_confirmed else confirmed
    removable_wide = (bounded_modes["wide"][:-keep_wide]
                       if keep_wide else bounded_modes["wide"])
    removable_oversample = (bounded_modes["oversample"][:-keep_oversample]
                             if keep_oversample else bounded_modes["oversample"])
    removed_negative = _remove_directories(
        [item[1] for item in removable_negative], captures_root, dry_run=dry_run)
    removed_confirmed = _remove_directories(
        [item[1] for item in removable_confirmed], captures_root, dry_run=dry_run)
    removed_wide = _remove_directories(
        [item[1] for item in removable_wide], captures_root, dry_run=dry_run)
    removed_oversample = _remove_directories(
        [item[1] for item in removable_oversample], captures_root, dry_run=dry_run)

    # Hop sessions are atomic: retaining the whole session preserves its tuning
    # manifest and every child. A session remains pending until every child has
    # both ordinary and dense follow-up reports. Any pinned child pins the
    # complete parent session permanently.
    hop_root = root / "hop-sessions"
    hop_eligible: list[tuple[int, Path]] = []
    protected_hop_sessions: list[str] = []
    for session in sorted(hop_root.iterdir() if hop_root.exists() else []):
        if not session.is_dir():
            continue
        if any(pin == session.resolve() or session.resolve() in pin.parents for pin in pins):
            protected_hop_sessions.append(str(session)); continue
        children = sorted(path for path in session.iterdir() if path.is_dir())
        complete = bool(children)
        created = 0
        for child in children:
            manifest = _read_json(child / "manifest.json")
            if manifest is None or manifest.get("state") != "complete":
                complete = False; break
            created = max(created, int(manifest.get("created_utc_ns", 0)))
            report_name = f"{session.name}-{child.name}"
            if (_read_json(reports_root / f"{report_name}.json") is None or
                    _read_json(reports_root / "followups" / f"{report_name}.json") is None):
                complete = False; break
        if complete:
            hop_eligible.append((created, session))
        else:
            protected_hop_sessions.append(str(session))
    hop_eligible.sort()
    removable_hops = (hop_eligible[:-keep_hop_sessions]
                       if keep_hop_sessions else hop_eligible)
    removed_hop_sessions = _remove_directories(
        [item[1] for item in removable_hops], hop_root, dry_run=dry_run)

    # Interrupted captures are quarantined, never destroyed automatically. A
    # partial recording may still contain unique RF evidence and needs an
    # explicit review path before bounded quarantine cleanup is safe.
    quarantined = []
    for capture in interrupted:
        target = quarantine_root / capture.name
        quarantined.append(str(target))
        if not dry_run:
            quarantine_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(target)
            shutil.move(str(capture), str(target))

    removed = (removed_negative + removed_confirmed + removed_wide +
               removed_oversample + removed_hop_sessions)
    return {"schema": "leo-tracker.beacon-retention/v2", "root": str(root),
            "keep_negative": keep_negative, "keep_confirmed": keep_confirmed,
            "keep_wide": keep_wide, "keep_oversample": keep_oversample,
            "keep_hop_sessions": keep_hop_sessions, "dry_run": dry_run,
            "qualified_pin_ledger": str(pin_ledger),
            "qualified_capture_pins": sorted(str(path) for path in pins),
            "newly_pinned": sorted(newly_pinned),
            "negative_capture_count": len(negative),
            "eligible_confirmed_capture_count": len(confirmed),
            "eligible_wide_capture_count": len(bounded_modes["wide"]),
            "eligible_oversample_capture_count": len(bounded_modes["oversample"]),
            "eligible_hop_session_count": len(hop_eligible),
            "protected": protected,
            "protected_hop_sessions": protected_hop_sessions,
            "incomplete": incomplete, "removed": removed,
            "removed_negative": removed_negative,
            "removed_confirmed": removed_confirmed,
            "removed_wide": removed_wide,
            "removed_oversample": removed_oversample,
            "removed_hop_sessions": removed_hop_sessions,
            "quarantined": quarantined}
