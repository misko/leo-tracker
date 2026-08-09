"""Lossless, provenance-preserving evidence clips from immutable beacon IQ."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable

from .artifact import BeaconCapture, SCHEMA as CAPTURE_SCHEMA
from .continuous import _sample_utc_ns
from .qnap_lifecycle import classify_recording


PLAN_SCHEMA = "leo-tracker.evidence-plan/v1"
BUNDLE_SCHEMA = "leo-tracker.evidence-bundle/v1"
VERIFICATION_SCHEMA = "leo-tracker.evidence-verification/v1"
AUDIT_SCHEMA = "leo-tracker.evidence-audit/v1"
SHADOW_SCHEMA = "leo-tracker.evidence-v2-shadow/v1"
BYTES_PER_PAIRED_SAMPLE = 8  # ci16 I/Q x two receivers
EVIDENCE_POLICIES = ("conservative-v1", "tiered-v2")
TIER_NAMES = {
    0: "strict_negative", 1: "weak_candidate", 2: "tracked_signal",
    3: "confirmed_beacon", 4: "qualified_identity", 5: "manual_pin",
}


def _source_root(capture_path: Path, reports_root: Path | None = None) -> Path | None:
    if capture_path.parent.name != "captures":
        return None
    candidate = capture_path.parent.parent.resolve()
    if reports_root is not None and reports_root.resolve() != candidate / "reports":
        return None
    return candidate


def _inside(path: Path, root: Path) -> bool:
    path = path.resolve(); root = root.resolve()
    return path == root or root in path.parents


def _json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".next.{os.getpid()}")
    with temporary.open("wb") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _artifact(path: Path, root: Path) -> dict | None:
    value = _json(path)
    if value is None:
        return None
    return {"path": str(path.relative_to(root)), "schema": value.get("schema"),
            "sha256": _sha256_path(path), "value": value}


def _artifact_file(path: Path, root: Path) -> dict | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return {"path": str(path.relative_to(root)), "schema": None,
            "sha256": _sha256_path(path), "bytes": size}


def _interesting_check(check: dict) -> bool:
    return bool(check.get("candidate") or check.get("qualified") or
                check.get("followup_trigger") or
                any(check.get("receiver_candidates", [])) or
                any(check.get("receiver_qualified", [])))


def _merge_intervals(intervals: Iterable[dict], total_samples: int) -> list[dict]:
    ordered = sorted(intervals, key=lambda item: (item["first_sample"], item["stop_sample"]))
    merged: list[dict] = []
    for item in ordered:
        first = max(0, min(total_samples, int(item["first_sample"])))
        stop = max(first, min(total_samples, int(item["stop_sample"])))
        if stop <= first:
            continue
        reasons = sorted(set(item.get("reasons", [])))
        if merged and first <= merged[-1]["stop_sample"]:
            merged[-1]["stop_sample"] = max(merged[-1]["stop_sample"], stop)
            merged[-1]["reasons"] = sorted(set(merged[-1]["reasons"] + reasons))
        else:
            merged.append({"first_sample": first, "stop_sample": stop,
                           "reasons": reasons})
    return merged


def plan_evidence(capture_path: Path, reports_root: Path, output: Path | None = None,
                  *, guard_s: float | None = None,
                  control_duration_s: float | None = None,
                  control_count: int | None = None,
                  policy: str = "conservative-v1") -> dict:
    """Plan conservative signal and control intervals without reading IQ payloads."""
    if policy not in EVIDENCE_POLICIES:
        raise ValueError(f"unsupported evidence policy: {policy}")
    capture_path = Path(capture_path).resolve(); reports_root = Path(reports_root).resolve()
    storage_root = _source_root(capture_path, reports_root)
    if output is not None and storage_root is not None and _inside(Path(output), storage_root):
        raise ValueError("evidence plan output must not modify the source storage root")
    capture = BeaconCapture.open(capture_path)
    manifest = capture.manifest
    if manifest.get("state") != "complete":
        raise ValueError("evidence requires a complete capture")
    rate = float(manifest["sample_rate_hz"])
    total = int(manifest["captured_samples_per_receiver"])
    duration = total / rate
    name = capture_path.name
    artifacts: list[dict] = []
    spans: list[tuple[float, float, str, bool]] = []

    def add_span(start: float, stop: float, reason: str, *, required: bool = True) -> None:
        spans.append((start, stop, reason, required))

    def load(relative: str) -> dict | None:
        item = _artifact(reports_root / relative, reports_root)
        if item is not None:
            artifacts.append({key: item[key] for key in ("path", "schema", "sha256")})
            return item["value"]
        return None

    analysis = load(f"{name}.json")
    if analysis:
        for check in analysis.get("exact_checks", []):
            if isinstance(check, dict) and _interesting_check(check):
                start = float(check.get("start_s", 0)); width = float(check.get("duration_s", 0.01))
                add_span(start, start + width, "exact_candidate")
        for window in analysis.get("windows", []):
            if isinstance(window, dict) and any(window.get(key) for key in (
                    "candidate", "qualified", "detected", "doppler_like")):
                start = float(window.get("start_s", 0)); width = float(window.get("duration_s", 1))
                add_span(start, start + width, "broadband_or_window_candidate")

    followup = load(f"followups/{name}.json")
    confirmed = bool((followup or {}).get("confirmation", {}).get("confirmed"))
    if followup:
        checks = [item for item in followup.get("checks", []) if isinstance(item, dict)]
        selected = (checks if confirmed and policy == "conservative-v1" else
                    [item for item in checks if _interesting_check(item)])
        for check in selected:
            start = float(check.get("start_s", 0))
            width = float(check.get("duration_s", followup.get("settings", {}).get("window_s", .01)))
            interesting = _interesting_check(check)
            add_span(start, start + width,
                     ("confirmed_dense_followup" if policy == "conservative-v1" else
                      "confirmed_followup_candidate" if interesting else
                      "confirmed_dense_followup_control"),
                     required=interesting)

    tracks = load(f"tracks/{name}.json")
    if tracks:
        for track in tracks.get("tracks", []):
            observations = track.get("observations", []) if isinstance(track, dict) else []
            positions = [int(item["start_sample"]) for item in observations
                         if isinstance(item, dict) and item.get("start_sample") is not None]
            if positions:
                add_span(min(positions) / rate,
                         (max(positions) + max(1, round(.1 * rate))) / rate,
                         "continuous_doppler_track")

    decoded = load(f"decoded/{name}.json")
    if decoded and isinstance(decoded.get("selected_observation"), dict):
        selected = decoded["selected_observation"]
        start = float(selected.get("start_s", 0)); width = float(selected.get("duration_s", .1))
        add_span(start, start + width, "decoded_symbols")

    # These are scientific provenance even though they do not add intervals.
    load(f"frame-tracks/{name}.json")
    association = load(f"associations/{name}.json")
    load(f"channel-links/{name}.json")
    for relative in (f"plots/{name}.png", f"decoded/{name}.png",
                     f"decoded/{name}.npz", f"frame-tracks/{name}.npz"):
        item = _artifact_file(reports_root / relative, reports_root)
        if item is not None:
            artifacts.append(item)

    identity = bool(association and any(item.get("qualified") for item in
                    association.get("associations", []) if isinstance(item, dict)))
    pinned = bool(storage_root and
                  (storage_root / "reports" / "retention" / "pins" /
                   f"{name}.json").is_file())
    has_track = bool(tracks and any(item.get("observations") for item in
                     tracks.get("tracks", []) if isinstance(item, dict)))
    has_candidate = any(required for _, _, _, required in spans)
    inferred_tier = (5 if pinned else 4 if identity else 3 if confirmed else
                     2 if has_track else 1 if has_candidate else 0)
    authoritative_tier = classify_recording(storage_root, name)[0] if storage_root else None
    # Prefer the worker's final summary, which includes linked TLE association
    # stages not otherwise required by the evidence planner. Fall back to the
    # artifacts when planning isolated tests or legacy captures without a log.
    tier = authoritative_tier if authoritative_tier is not None else inferred_tier
    if policy == "conservative-v1":
        defaults = (10.0, 1.0, 3)
    else:
        defaults = {
            0: (0.0, .1, 1), 1: (1.0, .25, 1), 2: (2.0, .5, 2),
            3: (2.0, .5, 2), 4: (2.0, .5, 2), 5: (2.0, .5, 2),
        }[tier]
    guard_s = defaults[0] if guard_s is None else guard_s
    control_duration_s = defaults[1] if control_duration_s is None else control_duration_s
    control_count = defaults[2] if control_count is None else control_count
    if guard_s < 0 or control_duration_s <= 0 or control_count < 0:
        raise ValueError("invalid evidence planning durations or control count")

    required_events = []
    for start, stop, reason, required in spans:
        if not required:
            continue
        first = max(0, min(total, round(start * rate)))
        event_stop = max(first, min(total, round(stop * rate)))
        if event_stop > first:
            required_events.append({"event_id": f"event-{len(required_events):03d}",
                                    "first_sample": first, "stop_sample": event_stop,
                                    "reason": reason})

    raw: list[dict] = []
    guard_samples = round(guard_s * rate)
    for start, stop, reason, _ in spans:
        raw.append({"first_sample": round(start * rate) - guard_samples,
                    "stop_sample": round(stop * rate) + guard_samples,
                    "reasons": [reason]})
    # Controls protect future calibration and false-positive work even when the
    # current detector sees nothing. They intentionally do not receive guards.
    control_samples = max(1, round(control_duration_s * rate))
    if control_count:
        fractions = [(index + 1) / (control_count + 1) for index in range(control_count)]
        for fraction in fractions:
            center = round(fraction * total)
            raw.append({"first_sample": center - control_samples // 2,
                        "stop_sample": center - control_samples // 2 + control_samples,
                        "reasons": ["deterministic_control"]})
    intervals = _merge_intervals(raw, total)
    for index, interval in enumerate(intervals):
        interval["interval_id"] = f"clip-{index:03d}"
        interval["sample_count"] = interval["stop_sample"] - interval["first_sample"]
        interval["start_s"] = interval["first_sample"] / rate
        interval["stop_s"] = interval["stop_sample"] / rate
        first_utc, method, uncertainty = _sample_utc_ns(manifest, interval["first_sample"])
        stop_utc, _, stop_uncertainty = _sample_utc_ns(manifest, interval["stop_sample"])
        interval.update({"first_utc_ns": first_utc, "stop_utc_ns": stop_utc,
                         "utc_mapping_method": method,
                         "utc_uncertainty_s": max(value or 0 for value in
                                                   (uncertainty, stop_uncertainty))})
    manifest_path = capture_path / "manifest.json"
    plan = {"schema": PLAN_SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
            "recording_id": name, "source_manifest_sha256": _sha256_path(manifest_path),
            "source_capture_schema": manifest.get("schema"),
            "source_total_samples_per_receiver": total, "sample_rate_hz": rate,
            "capture_duration_s": duration,
            "radio_parameters": {key: manifest.get(key) for key in (
                "center_frequency_hz", "rf_center_hz", "lnb_lo_hz", "bandwidth_hz",
                "gain_mode", "configured_gain_db", "receiver_count", "dtype", "layout")},
            "evidence_tier": tier, "evidence_tier_name": TIER_NAMES[tier],
            "policy": {"name": policy,
                       "guard_s": guard_s, "control_duration_s": control_duration_s,
                       "control_count": control_count,
                       "confirmed_followup": confirmed,
                       "selection": ("all dense confirmed checks plus known signal classes"
                                     if policy == "conservative-v1" else
                                     "interesting event spans plus tier-sized controls")},
            "source_artifacts": artifacts, "required_events": required_events,
            "intervals": intervals,
            "summary": {"interval_count": len(intervals),
                        "signal_reason_count": len({reason for item in intervals
                            for reason in item["reasons"] if reason != "deterministic_control"}),
                        "selected_samples_per_receiver": sum(item["sample_count"] for item in intervals),
                        "coverage_fraction": (sum(item["sample_count"] for item in intervals) / total
                                              if total else 0)}}
    if output is not None:
        _atomic_json(Path(output), plan)
    return plan


def compare_evidence_plan_coverage(reference: dict, candidate: dict) -> dict:
    """Prove that a candidate covers every detector event required by a plan."""
    if reference.get("schema") != PLAN_SCHEMA or candidate.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported evidence plan")
    if reference.get("recording_id") != candidate.get("recording_id"):
        raise ValueError("evidence plans belong to different recordings")
    if "required_events" not in reference:
        raise ValueError("reference plan predates required-event replay metadata")
    intervals = candidate.get("intervals", [])
    missing = []
    for event in reference.get("required_events", []):
        covered = any(int(interval["first_sample"]) <= int(event["first_sample"]) and
                      int(interval["stop_sample"]) >= int(event["stop_sample"])
                      for interval in intervals)
        if not covered:
            missing.append(event)
    return {"valid": not missing,
            "reference_required_event_count": len(reference.get("required_events", [])),
            "missing_required_event_count": len(missing), "missing_required_events": missing,
            "reference_coverage_fraction": reference.get("summary", {}).get("coverage_fraction"),
            "candidate_coverage_fraction": candidate.get("summary", {}).get("coverage_fraction")}


def build_evidence_v2_shadow(shared_root: Path, archive_root: Path, *,
                             output: Path | None = None,
                             limit: int | None = None) -> dict:
    """Build replay-gated v2 plans and storage projections without extracting IQ."""
    shared_root = Path(shared_root).resolve(); archive_root = Path(archive_root).resolve()
    if _inside(archive_root, shared_root):
        raise ValueError("shadow archive root must not be inside source storage")
    if limit is not None and limit < 1:
        raise ValueError("shadow limit must be positive")
    shadow = archive_root / "catalog" / "v2-shadow"
    entries = []; failures = []
    captures = sorted(path for path in (shared_root / "captures").iterdir()
                      if path.is_dir()) if (shared_root / "captures").is_dir() else []
    for capture_path in captures:
        if limit is not None and len(entries) >= limit:
            break
        name = capture_path.name
        if not (archive_root / "catalog" / "receipts" / f"{name}.json").is_file():
            continue
        try:
            reference_path = shadow / "references" / f"{name}.json"
            candidate_path = shadow / "plans" / f"{name}.json"
            comparison_path = shadow / "comparisons" / f"{name}.json"
            reference = plan_evidence(capture_path, shared_root / "reports",
                                      reference_path, policy="conservative-v1")
            candidate = plan_evidence(capture_path, shared_root / "reports",
                                      candidate_path, policy="tiered-v2")
            comparison = compare_evidence_plan_coverage(reference, candidate)
            _atomic_json(comparison_path, comparison)
            if not comparison["valid"]:
                raise ValueError("candidate failed required-event replay gate")
            receipt = _json(archive_root / "catalog" / "receipts" / f"{name}.json") or {}
            v1_bytes = int(receipt.get("summary", {}).get("stored_bytes", 0) or 0)
            v2_bytes = int(candidate["summary"]["selected_samples_per_receiver"]) * \
                BYTES_PER_PAIRED_SAMPLE
            entries.append({"recording_id": name,
                            "evidence_tier": candidate["evidence_tier"],
                            "evidence_tier_name": candidate["evidence_tier_name"],
                            "required_event_count": len(candidate["required_events"]),
                            "v1_stored_bytes": v1_bytes,
                            "v2_projected_bytes": v2_bytes,
                            "projected_savings_bytes": max(0, v1_bytes - v2_bytes),
                            "v1_coverage_fraction": reference["summary"]["coverage_fraction"],
                            "v2_coverage_fraction": candidate["summary"]["coverage_fraction"],
                            "replay_gate_valid": True})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append({"recording_id": name, "error": str(exc)})
    tiers: dict[str, dict] = {}
    for entry in entries:
        tier = tiers.setdefault(entry["evidence_tier_name"], {
            "recording_count": 0, "v1_stored_bytes": 0,
            "v2_projected_bytes": 0, "projected_savings_bytes": 0})
        tier["recording_count"] += 1
        for key in ("v1_stored_bytes", "v2_projected_bytes", "projected_savings_bytes"):
            tier[key] += entry[key]
    result = {"schema": SHADOW_SCHEMA,
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "shared_root": str(shared_root), "archive_root": str(archive_root),
              "summary": {"recording_count": len(entries),
                          "failure_count": len(failures),
                          "v1_stored_bytes": sum(item["v1_stored_bytes"] for item in entries),
                          "v2_projected_bytes": sum(item["v2_projected_bytes"] for item in entries),
                          "projected_savings_bytes": sum(item["projected_savings_bytes"]
                                                         for item in entries),
                          "tiers": tiers},
              "entries": entries, "failures": failures}
    _atomic_json(output or shadow / "summary.json", result)
    return result


def _copy_interval(capture: BeaconCapture, first: int, stop: int, output: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); written = 0
    with output.open("wb", buffering=0) as target:
        for item in capture.manifest["chunks"]:
            chunk_first = int(item["first_sample_index"])
            chunk_stop = chunk_first + int(item["sample_count"])
            if chunk_stop <= first or chunk_first >= stop:
                continue
            local_first = max(first, chunk_first) - chunk_first
            count = min(stop, chunk_stop) - max(first, chunk_first)
            with (capture.root / item["path"]).open("rb") as source:
                source.seek(local_first * BYTES_PER_PAIRED_SAMPLE)
                remaining = count * BYTES_PER_PAIRED_SAMPLE
                while remaining:
                    block = source.read(min(8 * 1024 * 1024, remaining))
                    if not block:
                        raise ValueError("source capture ended inside evidence interval")
                    target.write(block); digest.update(block); written += len(block)
                    remaining -= len(block)
        target.flush(); os.fsync(target.fileno())
    expected = (stop - first) * BYTES_PER_PAIRED_SAMPLE
    if written != expected:
        raise ValueError("evidence interval byte count is incomplete")
    return digest.hexdigest(), written


def extract_evidence(capture_path: Path, plan_path: Path, destination: Path) -> dict:
    """Extract exact paired ci16 intervals and atomically publish one bundle."""
    capture_path = Path(capture_path).resolve(); destination = Path(destination).resolve()
    storage_root = _source_root(capture_path)
    if ((storage_root is not None and _inside(destination, storage_root)) or
            destination == capture_path or capture_path in destination.parents):
        raise ValueError("evidence destination must not modify the source storage root")
    plan = _json(Path(plan_path))
    if plan is None or plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported evidence plan")
    if plan.get("recording_id") != capture_path.name:
        raise ValueError("plan recording does not match source capture")
    capture = BeaconCapture.open(capture_path, verify=True)
    source_manifest = capture_path / "manifest.json"
    if _sha256_path(source_manifest) != plan.get("source_manifest_sha256"):
        raise ValueError("source manifest changed after evidence planning")
    plan_sha = _sha256_path(Path(plan_path))
    if destination.exists():
        report = verify_evidence(destination, capture_path=capture_path, write=False)
        if report["valid"] and report["plan_sha256"] == plan_sha:
            return _json(destination / "manifest.json") or {}
        raise FileExistsError(destination)
    partial = destination.with_name(destination.name + ".partial")
    partial.mkdir(parents=True, exist_ok=True)
    progress_path = partial / "manifest.json"
    bundle = _json(progress_path)
    if bundle is None:
        bundle = {"schema": BUNDLE_SCHEMA, "state": "extracting",
                  "recording_id": capture_path.name, "plan_sha256": plan_sha,
                  "source_manifest_sha256": plan["source_manifest_sha256"],
                  "source": {"recording_id": capture_path.name,
                             "manifest_schema": capture.manifest.get("schema")},
                  "sample_rate_hz": capture.manifest["sample_rate_hz"],
                  "radio_parameters": plan["radio_parameters"], "clips": []}
        _atomic_json(progress_path, bundle)
    if (bundle.get("plan_sha256") != plan_sha or
            bundle.get("source_manifest_sha256") != plan["source_manifest_sha256"]):
        raise ValueError("partial evidence bundle belongs to a different source or plan")
    completed = {item["interval_id"]: item for item in bundle.get("clips", [])}
    source_manifest_copy = partial / "source-manifest.json"
    if not source_manifest_copy.exists():
        temporary_manifest = partial / "source-manifest.json.next"
        with source_manifest.open("rb") as source, temporary_manifest.open("wb") as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                target.write(block)
            target.flush(); os.fsync(target.fileno())
        if _sha256_path(temporary_manifest) != plan["source_manifest_sha256"]:
            raise ValueError("copied source manifest failed verification")
        os.replace(temporary_manifest, source_manifest_copy)
    for interval in plan["intervals"]:
        interval_id = interval["interval_id"]; filename = f"{interval_id}.ci16"
        final = partial / filename
        previous = completed.get(interval_id)
        if previous and final.is_file() and final.stat().st_size == previous["bytes"] and \
                _sha256_path(final) == previous["sha256"]:
            continue
        temporary = partial / (filename + ".next")
        digest, size = _copy_interval(capture, int(interval["first_sample"]),
                                      int(interval["stop_sample"]), temporary)
        os.replace(temporary, final)
        clip = {**interval, "path": filename, "bytes": size, "sha256": digest,
                "dtype": "ci16_le", "layout": capture.manifest.get("layout"),
                "receiver_count": 2}
        completed[interval_id] = clip
        bundle["clips"] = [completed[key] for key in sorted(completed)]
        _atomic_json(progress_path, bundle)
    bundle["state"] = "complete"
    bundle["created_utc"] = datetime.now(timezone.utc).isoformat()
    bundle["summary"] = {"clip_count": len(bundle["clips"]),
                         "stored_bytes": sum(item["bytes"] for item in bundle["clips"]),
                         "source_bytes": int(capture.manifest.get("stored_bytes", 0)),
                         "storage_fraction": (sum(item["bytes"] for item in bundle["clips"]) /
                                              int(capture.manifest.get("stored_bytes", 1) or 1))}
    _atomic_json(progress_path, bundle)
    os.replace(partial, destination)
    return bundle


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if _sha256_path(destination) == expected_sha256:
            return
        raise ValueError(f"derived artifact collision: {destination}")
    temporary = destination.with_suffix(destination.suffix + f".next.{os.getpid()}")
    with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
        for block in iter(lambda: input_stream.read(8 * 1024 * 1024), b""):
            output_stream.write(block)
        output_stream.flush(); os.fsync(output_stream.fileno())
    if _sha256_path(temporary) != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"derived artifact copy failed verification: {source}")
    os.replace(temporary, destination)


def archive_evidence(capture_path: Path, reports_root: Path, qnap_root: Path, *,
                     guard_s: float = 10.0, control_duration_s: float = 1.0,
                     control_count: int = 3) -> dict:
    """Publish one complete, source-verified evidence record without source writes."""
    capture_path = Path(capture_path).resolve(); reports_root = Path(reports_root).resolve()
    qnap_root = Path(qnap_root).resolve(); name = capture_path.name
    storage_root = _source_root(capture_path, reports_root)
    if storage_root is not None and _inside(qnap_root, storage_root):
        raise ValueError("QNAP archive root must not be inside source storage")
    plans = qnap_root / "catalog" / "plans"; receipts = qnap_root / "catalog" / "receipts"
    plan_path = plans / f"{name}.json"; bundle_path = qnap_root / "evidence" / name
    if not plan_path.exists():
        plan = plan_evidence(capture_path, reports_root, plan_path, guard_s=guard_s,
                             control_duration_s=control_duration_s,
                             control_count=control_count)
    else:
        plan = _json(plan_path)
        if plan is None or plan.get("schema") != PLAN_SCHEMA:
            raise ValueError("existing archive plan is invalid")
        if _sha256_path(capture_path / "manifest.json") != plan.get("source_manifest_sha256"):
            raise ValueError("existing archive plan belongs to a changed source")
    bundle = extract_evidence(capture_path, plan_path, bundle_path)
    verification = verify_evidence(bundle_path, capture_path=capture_path)
    if not verification["valid"]:
        raise ValueError("evidence bundle failed source verification")
    copied = []
    for artifact in plan.get("source_artifacts", []):
        relative = Path(artifact["path"])
        source = reports_root / relative; destination = qnap_root / "derived" / relative
        _copy_verified(source, destination, artifact["sha256"])
        copied.append({"path": str(Path("derived") / relative),
                       "sha256": artifact["sha256"], "bytes": destination.stat().st_size})
    receipt = {"schema": "leo-tracker.evidence-archive-receipt/v1",
               "created_utc": datetime.now(timezone.utc).isoformat(),
               "recording_id": name, "status": "verified",
               "source_manifest_sha256": plan["source_manifest_sha256"],
               "plan": str(Path("catalog/plans") / plan_path.name),
               "plan_sha256": _sha256_path(plan_path),
               "bundle": str(Path("evidence") / name),
               "bundle_manifest_sha256": verification["bundle_manifest_sha256"],
               "source_verified": True, "derived_artifacts": copied,
               "summary": bundle["summary"]}
    _atomic_json(receipts / f"{name}.json", receipt)
    return receipt


def materialize_evidence_clip(bundle_path: Path, interval_id: str,
                              destination: Path) -> dict:
    """Create a standard BeaconCapture view of one archived evidence clip."""
    bundle_path = Path(bundle_path).resolve(); destination = Path(destination).resolve()
    bundle = _json(bundle_path / "manifest.json")
    source_manifest = _json(bundle_path / "source-manifest.json")
    if (bundle is None or bundle.get("schema") != BUNDLE_SCHEMA or
            source_manifest is None or source_manifest.get("schema") != CAPTURE_SCHEMA):
        raise ValueError("invalid evidence bundle or source manifest")
    clip = next((item for item in bundle.get("clips", [])
                 if item.get("interval_id") == interval_id), None)
    if clip is None:
        raise ValueError(f"unknown evidence interval: {interval_id}")
    if destination.exists():
        existing = BeaconCapture.open(destination, verify=True)
        if existing.manifest.get("metadata", {}).get("evidence", {}).get(
                "bundle_manifest_sha256") == _sha256_path(bundle_path / "manifest.json"):
            return existing.manifest
        raise FileExistsError(destination)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise FileExistsError(partial)
    partial.mkdir(parents=True)
    iq_source = bundle_path / clip["path"]; iq_target = partial / "chunk-000000.ci16"
    try:
        os.link(iq_source, iq_target)
    except OSError:
        _copy_verified(iq_source, iq_target, clip["sha256"])
    manifest = dict(source_manifest)
    manifest["state"] = "complete"
    manifest["created_utc_ns"] = int(clip["first_utc_ns"])
    manifest["completed_utc_ns"] = int(clip["stop_utc_ns"])
    manifest["requested_duration_s"] = clip["sample_count"] / float(bundle["sample_rate_hz"])
    manifest["requested_samples_per_receiver"] = int(clip["sample_count"])
    manifest["captured_samples_per_receiver"] = int(clip["sample_count"])
    manifest["chunk_samples"] = int(clip["sample_count"])
    manifest["stored_bytes"] = int(clip["bytes"])
    manifest["chunks"] = [{"path": iq_target.name, "first_sample_index": 0,
                            "sample_count": int(clip["sample_count"]),
                            "first_utc_ns": int(clip["first_utc_ns"]),
                            "last_utc_ns": int(clip["stop_utc_ns"]),
                            "read_count": 1, "sha256": clip["sha256"],
                            "bytes": int(clip["bytes"])}]
    metadata = dict(source_manifest.get("metadata", {}))
    metadata["evidence"] = {"schema": BUNDLE_SCHEMA,
        "source_recording_id": bundle["recording_id"], "interval_id": interval_id,
        "source_first_sample": int(clip["first_sample"]),
        "source_stop_sample": int(clip["stop_sample"]), "reasons": clip["reasons"],
        "bundle_manifest_sha256": _sha256_path(bundle_path / "manifest.json")}
    manifest["metadata"] = metadata
    manifest["stream_timing"] = {"read_count": 1,
        "first_read_start_utc_ns": int(clip["first_utc_ns"]),
        "last_read_stop_utc_ns": int(clip["stop_utc_ns"]),
        "wall_span_s": (int(clip["stop_utc_ns"]) - int(clip["first_utc_ns"])) / 1e9,
        "sample_time_s": clip["sample_count"] / float(bundle["sample_rate_hz"]),
        "clock_samples": [],
        "note": "materialized exact evidence clip; see metadata.evidence for source indexes"}
    manifest.pop("sample_statistics", None)
    gain = dict(source_manifest.get("gain_telemetry", {})); entries = []
    for item in gain.get("entries", []):
        sample = int(item.get("sample_index", -1))
        if int(clip["first_sample"]) <= sample < int(clip["stop_sample"]):
            entries.append({**item, "sample_index": sample - int(clip["first_sample"])})
    gain["entries"] = entries; manifest["gain_telemetry"] = gain
    _atomic_json(partial / "manifest.json", manifest)
    BeaconCapture.open(partial, verify=True)
    os.replace(partial, destination)
    return manifest


def _source_interval_sha(capture: BeaconCapture, first: int, stop: int) -> str:
    digest = hashlib.sha256()
    for item in capture.manifest["chunks"]:
        chunk_first = int(item["first_sample_index"]); chunk_stop = chunk_first + int(item["sample_count"])
        if chunk_stop <= first or chunk_first >= stop:
            continue
        local_first = max(first, chunk_first) - chunk_first
        remaining = (min(stop, chunk_stop) - max(first, chunk_first)) * BYTES_PER_PAIRED_SAMPLE
        with (capture.root / item["path"]).open("rb") as stream:
            stream.seek(local_first * BYTES_PER_PAIRED_SAMPLE)
            while remaining:
                block = stream.read(min(8 * 1024 * 1024, remaining))
                if not block:
                    raise ValueError("source interval is incomplete")
                digest.update(block); remaining -= len(block)
    return digest.hexdigest()


def verify_evidence(bundle_path: Path, *, capture_path: Path | None = None,
                    write: bool = True) -> dict:
    """Read back every clip and optionally prove byte identity with its source."""
    bundle_path = Path(bundle_path).resolve(); manifest = _json(bundle_path / "manifest.json")
    if manifest is None or manifest.get("schema") != BUNDLE_SCHEMA or manifest.get("state") != "complete":
        raise ValueError("evidence bundle is not complete")
    source_manifest_copy = bundle_path / "source-manifest.json"
    if (not source_manifest_copy.is_file() or
            _sha256_path(source_manifest_copy) != manifest.get("source_manifest_sha256")):
        raise ValueError("evidence source manifest copy is missing or corrupt")
    capture = None
    if capture_path is not None:
        capture = BeaconCapture.open(Path(capture_path).resolve(), verify=True)
        if capture.root.name != manifest.get("recording_id"):
            raise ValueError("verification source does not match evidence bundle")
        if _sha256_path(capture.root / "manifest.json") != manifest["source_manifest_sha256"]:
            raise ValueError("verification source manifest differs from evidence source")
    checks = []
    for clip in manifest.get("clips", []):
        path = bundle_path / clip["path"]
        actual_size = path.stat().st_size; actual_sha = _sha256_path(path)
        valid = actual_size == clip["bytes"] and actual_sha == clip["sha256"]
        source_equal = None
        if capture is not None:
            source_equal = (_source_interval_sha(capture, int(clip["first_sample"]),
                                                 int(clip["stop_sample"])) == actual_sha)
            valid = valid and source_equal
        checks.append({"interval_id": clip["interval_id"], "valid": valid,
                       "source_equal": source_equal, "bytes": actual_size,
                       "sha256": actual_sha})
    report = {"schema": VERIFICATION_SCHEMA,
              "verified_utc": datetime.now(timezone.utc).isoformat(),
              "recording_id": manifest["recording_id"],
              "bundle_manifest_sha256": _sha256_path(bundle_path / "manifest.json"),
              "plan_sha256": manifest["plan_sha256"],
              "source_verified": capture is not None,
              "valid": bool(checks) and all(item["valid"] for item in checks),
              "checks": checks}
    if write:
        _atomic_json(bundle_path / "verification.json", report)
    return report


def audit_evidence(source_root: Path, evidence_root: Path, output: Path | None = None) -> dict:
    """Inventory complete local captures and independently verified QNAP bundles."""
    source_root = Path(source_root).resolve(); evidence_root = Path(evidence_root).resolve()
    sources = {path.name: path for path in (source_root / "captures").glob("*")
               if path.is_dir() and (_json(path / "manifest.json") or {}).get("state") == "complete"}
    bundles = {path.name: path for path in evidence_root.glob("*") if path.is_dir() and
               (_json(path / "manifest.json") or {}).get("state") == "complete"}
    valid, invalid = [], []
    for name, path in sorted(bundles.items()):
        try:
            result = verify_evidence(path, capture_path=sources.get(name), write=False)
            (valid if result["valid"] else invalid).append(name)
        except (OSError, ValueError, KeyError) as exc:
            invalid.append({"recording_id": name, "error": str(exc)})
    report = {"schema": AUDIT_SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
              "source_root": str(source_root), "evidence_root": str(evidence_root),
              "source_capture_count": len(sources), "bundle_count": len(bundles),
              "verified_bundle_count": len(valid), "invalid": invalid,
              "missing_recordings": sorted(set(sources) - set(bundles)),
              "orphan_bundles": sorted(set(bundles) - set(sources))}
    if output is not None:
        _atomic_json(Path(output), report)
    return report
