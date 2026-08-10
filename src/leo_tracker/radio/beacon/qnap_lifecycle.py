"""Disabled-by-default lifecycle planning for complete raw IQ on QNAP."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time


PLAN_SCHEMA = "leo-tracker.qnap-lifecycle-plan/v1"
RECEIPT_SCHEMA = "leo-tracker.qnap-reclamation-receipt/v1"
CONFIRMATION = "DELETE-QNAP-RAW-IQ"
TIERS = {
    0: "strict_negative",
    1: "weak_candidate",
    2: "tracked_signal",
    3: "confirmed_beacon",
    4: "qualified_identity",
    5: "manual_pin",
}
DELETABLE_TIERS = {0, 1, 2, 3, 4}
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".next.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _capture_safe(shared_root: Path, capture: Path) -> bool:
    if capture.is_symlink():
        return False
    try:
        relative = capture.resolve(strict=True).relative_to(shared_root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return len(relative.parts) == 2 and relative.parts[0] == "captures"


def _source_bytes(manifest: dict) -> int:
    try:
        return sum(int(item.get("bytes", 0)) for item in manifest.get("chunks", []))
    except (TypeError, ValueError):
        return 0


def _worker_summary(path: Path) -> dict:
    result: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("{"):
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if "exact_candidate_count" in value:
                result["analysis"] = value
            if "confirmed" in value and "followup" in value:
                result["followup"] = value
            if "track_count" in value and "track" in value:
                result["track"] = value
            if "qualified_association_count" in value and "association" in value:
                result["association"] = value
    except OSError:
        return {}
    return result


def classify_recording(shared_root: Path, name: str) -> tuple[int | None, list[str], dict]:
    """Return the authoritative evidence tier recorded by the Kalman worker."""
    reports = shared_root / "reports"
    reasons: list[str] = []
    if (reports / "retention" / "pins" / f"{name}.json").is_file():
        return 5, ["manual_pin"], {}
    compact = _worker_summary(reports / f"{name}.worker.log")
    analysis_document = _json(reports / f"{name}.json")
    if compact.get("analysis"):
        summary = compact["analysis"]
    elif analysis_document.get("schema") == "leo-tracker.starlink-beacon-analysis/v1":
        summary = analysis_document.get("summary", {})
    else:
        return None, ["classification_summary_unavailable"], {}
    followup = compact.get("followup", {})
    if not followup:
        document = _json(reports / "followups" / f"{name}.json")
        if document.get("schema") == "leo-tracker.starlink-beacon-followup/v1":
            followup = {"confirmed": document.get("confirmation", {}).get(
                            "confirmed", False),
                        "trigger_count": document.get("trigger_count", 0)}
    track_summary = compact.get("track", {})
    if not track_summary:
        document = _json(reports / "tracks" / f"{name}.json")
        if document.get("schema") == "leo-tracker.starlink-continuous-track/v1":
            track_summary = document.get("summary", {})
    association_summary = compact.get("association", {})
    if not association_summary:
        document = _json(reports / "associations" / f"{name}.json")
        if document.get("schema") in {"leo-tracker.starlink-tle-association/v1",
                                       "leo-tracker.starlink-tle-association/v2"}:
            association_summary = document.get("summary", {})
    qualified = int(association_summary.get("qualified_association_count", 0) or 0)
    if qualified:
        return 4, ["qualified_tle_identity"], {"qualified_association_count": qualified}
    if followup.get("confirmed"):
        reasons.append("temporally_confirmed_beacon")
        return 3, reasons, {"trigger_count": followup.get("trigger_count", 0)}
    longest = float(track_summary.get("longest_dual_valid_duration_s",
                    track_summary.get("longest_valid_duration_s", 0)) or 0)
    if (summary.get("doppler_track_qualified") or longest >= 5 or
            int(track_summary.get("dual_valid_observation_count", 0) or 0) >= 20):
        reasons.append("tracked_doppler_signal")
        return 2, reasons, {"longest_track_duration_s": longest}
    candidate_count = (int(summary.get("exact_candidate_count", 0) or 0) +
                       int(summary.get("single_receiver_candidate_count", 0) or 0))
    trigger_count = int(followup.get("trigger_count",
                        summary.get("followup_trigger_count", 0)) or 0)
    if candidate_count or trigger_count:
        reasons.append("candidate_or_trigger_without_confirmation")
        return 1, reasons, {"candidate_count": candidate_count,
                            "trigger_count": trigger_count}
    reasons.append("no_signal_evidence_in_current_analysis")
    return 0, reasons, {"candidate_count": 0, "trigger_count": 0}


def _analysis_gate(shared_root: Path, name: str) -> tuple[bool, str]:
    receipt = _json(shared_root / "reports" / "receipts" / f"{name}.json")
    if (receipt.get("schema") != "leo-tracker.analysis-receipt/v1" or
            receipt.get("status") != "success" or receipt.get("job") != name):
        return False, "analysis_incomplete"
    return True, "eligible"


def _archive_gate(shared_root: Path, archive_root: Path, name: str,
                  manifest_sha: str) -> tuple[bool, str, dict]:
    receipt = _json(archive_root / "catalog" / "v2" / "receipts" / f"{name}.json")
    if (receipt.get("schema") != "leo-tracker.evidence-archive-receipt/v2" or
            receipt.get("status") != "verified" or not receipt.get("source_verified") or
            not receipt.get("required_event_replay_valid") or
            receipt.get("policy") != "tiered-v2"):
        return False, "evidence_v2_unverified", receipt
    if receipt.get("source_manifest_sha256") != manifest_sha:
        return False, "evidence_archive_stale", receipt
    bundle = archive_root / str(receipt.get("bundle", ""))
    manifest = bundle / "manifest.json"
    if (not bundle.is_dir() or not manifest.is_file() or
            _sha256(manifest) != receipt.get("bundle_manifest_sha256")):
        return False, "evidence_bundle_invalid", receipt
    return True, "eligible", receipt


def build_qnap_lifecycle_plan(shared_root: Path, archive_root: Path, *,
                              minimum_age_hours: float = 24,
                              maximum_tier: int = 0) -> dict:
    """Classify all complete QNAP raw recordings without removing anything."""
    if minimum_age_hours < 0 or maximum_tier not in DELETABLE_TIERS:
        raise ValueError("minimum age must be non-negative and maximum tier 0..4")
    shared_root = Path(shared_root).resolve(); archive_root = Path(archive_root).resolve()
    now = time.time(); entries = []
    for capture in sorted((shared_root / "captures").iterdir() if
                          (shared_root / "captures").is_dir() else []):
        if not capture.is_dir():
            continue
        name = capture.name; manifest_path = capture / "manifest.json"
        manifest = _json(manifest_path); status = "eligible"
        tier, reasons, evidence = classify_recording(shared_root, name)
        manifest_sha = _sha256(manifest_path) if manifest else None
        if not _NAME.fullmatch(name) or not _capture_safe(shared_root, capture):
            status = "unsafe_shared_path"
        elif manifest.get("state") not in ("complete", "interrupted") or not manifest.get("chunks"):
            status = "shared_capture_incomplete"
        elif now - manifest_path.stat().st_mtime < minimum_age_hours * 3600:
            status = "minimum_age_not_met"
        else:
            valid, status = _analysis_gate(shared_root, name)
            if valid:
                valid, status, archive = _archive_gate(
                    shared_root, archive_root, name, manifest_sha or "")
                evidence["archive_storage_fraction"] = archive.get(
                    "summary", {}).get("storage_fraction")
        if status == "eligible" and tier is None:
            status = "classification_unavailable"
        if status == "eligible" and tier is not None and tier > maximum_tier:
            status = "protected_by_tier"
        entries.append({"recording_id": name, "capture_path": str(capture),
            "source_manifest_sha256": manifest_sha,
            "source_bytes": _source_bytes(manifest), "tier": tier,
            "tier_name": TIERS.get(tier, "unclassified"),
            "classification_reasons": reasons,
            "evidence": evidence, "status": status})
    entries.sort(key=lambda item: (item["tier"] if item["tier"] is not None else 99,
                                  item["recording_id"]))
    counts: dict[str, int] = {}; tiers: dict[str, int] = {}
    for item in entries:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        tiers[item["tier_name"]] = tiers.get(item["tier_name"], 0) + 1
    return {"schema": PLAN_SCHEMA, "created_utc": _now(),
        "shared_root": str(shared_root), "archive_root": str(archive_root),
        "enabled": False,
        "configuration": {"minimum_age_hours": minimum_age_hours,
                          "maximum_tier": maximum_tier,
                          "maximum_tier_name": TIERS[maximum_tier]},
        "summary": {"recording_count": len(entries), "status_counts": counts,
            "tier_counts": tiers, "eligible_count": counts.get("eligible", 0),
            "eligible_bytes": sum(item["source_bytes"] for item in entries
                                  if item["status"] == "eligible")},
        "entries": entries}


def apply_qnap_lifecycle_plan(plan: dict, *, confirmation: str,
                              trigger_free_gb: float, target_free_gb: float,
                              limit: int | None = None,
                              pressure_required: bool = True) -> dict:
    """Remove planned QNAP raw only under an explicit pressure and token gate."""
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported QNAP lifecycle plan schema")
    if confirmation != CONFIRMATION:
        raise ValueError("QNAP deletion confirmation token is missing or incorrect")
    if trigger_free_gb < 0 or target_free_gb <= trigger_free_gb:
        raise ValueError("target free space must exceed the non-negative trigger")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    shared_root = Path(plan["shared_root"]).resolve()
    archive_root = Path(plan["archive_root"]).resolve()
    lock_path = shared_root / "reports" / "reclamation" / "qnap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError("another QNAP reclaimer is active") from exc
    free_bytes = shutil.disk_usage(shared_root).free
    trigger = int(trigger_free_gb * 1e9); target = int(target_free_gb * 1e9)
    if pressure_required and free_bytes >= trigger:
        return {"schema": "leo-tracker.qnap-reclamation-result/v1",
            "enabled": True, "pressure_triggered": False,
            "removed_count": 0, "removed_bytes": 0,
            "free_bytes_before": free_bytes, "free_bytes_after": free_bytes}
    removed = []; candidates = [item for item in plan.get("entries", [])
                                 if item.get("status") == "eligible"]
    for item in candidates:
        if ((pressure_required and free_bytes >= target) or
                (limit is not None and len(removed) >= limit)):
            break
        name = item["recording_id"]; capture = Path(item["capture_path"])
        if not capture.exists():
            continue
        if not _capture_safe(shared_root, capture):
            raise RuntimeError(f"unsafe QNAP capture path: {capture}")
        manifest = capture / "manifest.json"
        manifest_sha = _sha256(manifest)
        if manifest_sha != item.get("source_manifest_sha256"):
            continue
        valid, _ = _analysis_gate(shared_root, name)
        if valid:
            valid, _, _ = _archive_gate(shared_root, archive_root, name, manifest_sha)
        if not valid:
            continue
        tier, _, _ = classify_recording(shared_root, name)
        if tier != item.get("tier") or tier not in DELETABLE_TIERS:
            continue
        receipt_path = shared_root / "reports" / "reclamation" / "qnap" / f"{name}.json"
        prepared = {"schema": RECEIPT_SCHEMA, "recording_id": name,
            "status": "prepared", "prepared_utc": _now(), "tier": tier,
            "tier_name": TIERS[tier], "source_bytes": item["source_bytes"],
            "source_manifest_sha256": manifest_sha,
            "evidence_archive_preserved": True,
            "derived_reports_preserved": True}
        _atomic_json(receipt_path, prepared)
        shutil.rmtree(capture)
        completed = {**prepared, "status": "removed", "removed_utc": _now(),
                     "qnap_raw_absent_verified": not capture.exists()}
        _atomic_json(receipt_path, completed); removed.append(completed)
        free_bytes = shutil.disk_usage(shared_root).free
    return {"schema": "leo-tracker.qnap-reclamation-result/v1",
        "enabled": True, "pressure_triggered": free_bytes < trigger,
        "removed_count": len(removed),
        "removed_bytes": sum(item["source_bytes"] for item in removed),
        "free_bytes_after": free_bytes,
        "receipts": [str(shared_root / "reports" / "reclamation" / "qnap" /
                         f"{item['recording_id']}.json") for item in removed]}
