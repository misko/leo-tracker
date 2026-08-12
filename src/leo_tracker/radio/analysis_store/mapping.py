"""Build authenticated input manifests and map them into relational rows."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from ..beacon.dashboard_index import capture_dashboard_record, confirmed_beacon_events
from ..beacon.dashboard_shards import listing_row
from .identity import INPUT_SCHEMA, canonical_json, run_id_for_manifest, sha256_json

DOCUMENT_PATHS = {
    "analysis": lambda reports, name: reports / f"{name}.json",
    "followup": lambda reports, name: reports / "followups" / f"{name}.json",
    "frame_track": lambda reports, name: reports / "frame-tracks" / f"{name}.json",
    "track": lambda reports, name: reports / "tracks" / f"{name}.json",
    "decoded": lambda reports, name: reports / "decoded" / f"{name}.json",
    "association": lambda reports, name: reports / "associations" / f"{name}.json",
    "channel_link": lambda reports, name: reports / "channel-links" / f"{name}.json",
    "linked_association": lambda reports, name: (
        reports / "associations" / f"{name}-channel-link.json"),
    "fragment_association": lambda reports, name: (
        reports / "fragment-associations" / f"{name}.json"),
    "fragment_diagnostic": lambda reports, name: (
        reports / "fragment-diagnostics" / f"{name}.json"),
    "fingerprint": lambda reports, name: reports / "fingerprints" / f"{name}.json",
}

ARTIFACT_PATHS = {
    "analysis_plot": (lambda reports, name: reports / "plots" / f"{name}.png",
                      "image/png"),
    "frame_samples": (lambda reports, name: reports / "frame-tracks" / f"{name}.npz",
                      "application/x-npz"),
    "decode_symbols": (lambda reports, name: reports / "decoded" / f"{name}.npz",
                       "application/x-npz"),
    "decode_plot": (lambda reports, name: reports / "decoded" / f"{name}.png",
                    "image/png"),
    "fingerprint_plot": (lambda reports, name: reports / "fingerprints" / f"{name}.png",
                         "image/png"),
    "worker_log": (lambda reports, name: reports / f"{name}.worker.log",
                   "text/plain"),
}

DEFAULT_ASSOCIATION_SOURCES = ("space-track", "huggingface")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read analysis-store JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _safe_file(root: Path, path: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"analysis-store input escapes shared root: {path}")
    if path.is_symlink() or not resolved.is_file():
        raise ValueError(f"analysis-store input is not an ordinary file: {path}")
    return resolved


def _entry(root: Path, path: Path, *, schema: str | None = None,
           media_type: str | None = None) -> dict:
    resolved = _safe_file(root, path)
    result = {"path": str(resolved), "bytes": resolved.stat().st_size,
              "sha256": _sha256(resolved)}
    if schema is not None:
        result["schema"] = schema
    if media_type is not None:
        result["media_type"] = media_type
    return result


def _context_entry(root: Path, value) -> dict | None:
    if value is None:
        return None
    path = Path(str(value))
    resolved = path.resolve()
    if (not resolved.is_relative_to(root.resolve()) or path.is_symlink() or
            not resolved.is_dir()):
        raise ValueError(f"invalid analysis context bundle: {path}")
    manifest_path = _safe_file(root, resolved / "manifest.json")
    payload = _json(manifest_path)
    bundle_id = payload.get("bundle_id")
    if (payload.get("schema") != "leo-tracker.analysis-offload/v2" or
            not isinstance(bundle_id, str) or resolved.name != bundle_id):
        raise ValueError(f"invalid analysis context manifest: {manifest_path}")
    return {"path": str(resolved), "bundle_id": bundle_id,
            "manifest": _entry(root, manifest_path, schema=payload.get("schema"))}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def build_input_manifest(shared_root: Path, recording_id: str, pipeline_id: str,
                         *, association_sources=DEFAULT_ASSOCIATION_SOURCES) -> dict:
    """Inventory one completed run through explicit contract paths.

    This deliberately never globs report directories. Optional outputs are
    admitted only when their named contract path exists.
    """
    if not recording_id or Path(recording_id).name != recording_id:
        raise ValueError("unsafe recording ID")
    if not pipeline_id or Path(pipeline_id).name != pipeline_id:
        raise ValueError("unsafe pipeline ID")
    root = Path(shared_root).resolve()
    reports = root / "reports"
    completion_path = reports / "runs" / pipeline_id / recording_id / "completion.json"
    completion = _json(_safe_file(root, completion_path))
    if (completion.get("schema") != "leo-tracker.analysis-receipt/v1" or
            completion.get("job") != recording_id or
            completion.get("pipeline_id") != pipeline_id or
            completion.get("status") != "success"):
        raise ValueError(f"invalid completion receipt: {completion_path}")

    documents = {}
    for kind, resolver in DOCUMENT_PATHS.items():
        path = resolver(reports, recording_id)
        if not path.is_file():
            continue
        payload = _json(path)
        documents[kind] = _entry(root, path, schema=payload.get("schema"))
    for source in association_sources:
        if not source or Path(source).name != source:
            raise ValueError(f"unsafe association source: {source!r}")
        path = reports / "associations" / source / f"{recording_id}.json"
        if path.is_file():
            payload = _json(path)
            documents[f"association:{source}"] = _entry(
                root, path, schema=payload.get("schema"))
    if "analysis" not in documents or "followup" not in documents:
        raise ValueError(f"completed run lacks analysis or follow-up: {recording_id}")

    receipt_outputs = completion.get("outputs") or {}
    for kind, claimed in receipt_outputs.items():
        actual = documents.get(kind)
        if actual is None:
            raise ValueError(f"completion output is absent from store contract: {kind}")
        if (int(claimed.get("bytes", -1)) != actual["bytes"] or
                claimed.get("sha256") != actual["sha256"]):
            raise ValueError(f"completion output changed before store ingest: {kind}")

    artifacts = {}
    for kind, (resolver, media_type) in ARTIFACT_PATHS.items():
        path = resolver(reports, recording_id)
        if path.is_file():
            artifacts[kind] = _entry(root, path, media_type=media_type)

    analysis = _json(Path(documents["analysis"]["path"]))
    capture_manifest = analysis.get("capture_manifest")
    if not isinstance(capture_manifest, dict):
        raise ValueError(f"analysis lacks capture manifest: {recording_id}")
    manifest = {
        "schema": INPUT_SCHEMA,
        "recording_id": recording_id,
        "pipeline_id": pipeline_id,
        "mode": completion.get("mode"),
        "context": _context_entry(root, completion.get("context")),
        "capture_manifest_sha256": sha256_json(capture_manifest),
        "completion": _entry(root, completion_path,
                             schema=completion.get("schema")),
        "documents": documents,
        "artifacts": artifacts,
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest["run_id"] = run_id_for_manifest(manifest)
    return manifest


def enqueue_input(shared_root: Path, store_root: Path, recording_id: str,
                  pipeline_id: str) -> tuple[Path, dict]:
    manifest = build_input_manifest(shared_root, recording_id, pipeline_id)
    return enqueue_manifest(store_root, manifest), manifest


def enqueue_manifest(store_root: Path, manifest: dict) -> Path:
    if manifest.get("run_id") != run_id_for_manifest(manifest):
        raise ValueError("refusing to enqueue an invalid run identity")
    target = Path(store_root).resolve() / "inbox" / f"{manifest['run_id']}.json"
    if not target.exists():
        _atomic_json(target, manifest)
    return target


def validate_input_manifest(path: Path, shared_root: Path) -> tuple[dict, dict, dict]:
    """Authenticate a queued manifest file and return it, completion and documents."""
    manifest = _json(Path(path))
    if Path(path).stem != run_id_for_manifest(manifest):
        raise ValueError("analysis-store run identity mismatch")
    return load_authenticated(manifest, shared_root)


def load_authenticated(manifest: dict, shared_root: Path) -> tuple[dict, dict, dict]:
    """Authenticate an in-memory manifest against the sources it names.

    The same checks as :func:`validate_input_manifest` minus the queue file, so
    a projection builder can go from receipt to rows without a round trip
    through a manifest on disk.
    """
    expected = run_id_for_manifest(manifest)
    if manifest.get("run_id") != expected:
        raise ValueError("analysis-store run identity mismatch")
    root = Path(shared_root).resolve()
    completion_entry = manifest.get("completion") or {}
    completion_path = _safe_file(root, Path(completion_entry.get("path", "")))
    if (completion_path.stat().st_size != int(completion_entry.get("bytes", -1)) or
            _sha256(completion_path) != completion_entry.get("sha256")):
        raise ValueError("completion receipt changed after enqueue")
    completion = _json(completion_path)
    if (completion.get("job") != manifest.get("recording_id") or
            completion.get("pipeline_id") != manifest.get("pipeline_id") or
            completion.get("status") != "success"):
        raise ValueError("completion receipt identity mismatch")

    context = manifest.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise ValueError("analysis context identity is malformed")
        context_path = Path(context.get("path", ""))
        resolved_context = context_path.resolve()
        if (not resolved_context.is_relative_to(root) or context_path.is_symlink() or
                not resolved_context.is_dir() or
                resolved_context.name != context.get("bundle_id")):
            raise ValueError("analysis context bundle changed after enqueue")
        entry = context.get("manifest") or {}
        source = _safe_file(root, Path(entry.get("path", "")))
        if (source.parent != resolved_context or
                source.stat().st_size != int(entry.get("bytes", -1)) or
                _sha256(source) != entry.get("sha256")):
            raise ValueError("analysis context manifest changed after enqueue")
        payload = _json(source)
        if (payload.get("schema") != entry.get("schema") or
                payload.get("bundle_id") != context.get("bundle_id")):
            raise ValueError("analysis context identity changed after enqueue")

    values = {}
    for collection in ("documents", "artifacts"):
        for kind, entry in (manifest.get(collection) or {}).items():
            source = _safe_file(root, Path(entry.get("path", "")))
            if (source.stat().st_size != int(entry.get("bytes", -1)) or
                    _sha256(source) != entry.get("sha256")):
                raise ValueError(f"{collection} input changed after enqueue: {kind}")
            if collection == "documents":
                value = _json(source)
                if value.get("schema") != entry.get("schema"):
                    raise ValueError(f"document schema changed after enqueue: {kind}")
                values[kind] = value
    analysis = values.get("analysis") or {}
    if sha256_json(analysis.get("capture_manifest")) != manifest.get(
            "capture_manifest_sha256"):
        raise ValueError("capture manifest changed after enqueue")
    return manifest, completion, values


def _at(values, index, default=None):
    return values[index] if isinstance(values, list) and index < len(values) else default


def relational_rows(manifest: dict, completion: dict, documents: dict,
                    shared_root: Path) -> dict:
    """Map an authenticated input into rows without touching the database."""
    analysis = documents["analysis"]
    capture = analysis["capture_manifest"]
    identity = capture.get("identity") or {}
    metadata = capture.get("metadata") or {}
    labels = identity.get("receiver_labels") or []
    receiver_count = int(capture.get("receiver_count") or len(labels))
    if receiver_count < 1:
        raise ValueError("capture has no receivers")
    if labels and len(labels) != receiver_count:
        raise ValueError("receiver labels do not match receiver count")
    sample_stats = ((capture.get("sample_statistics") or {}).get("receivers") or [])
    created_ns = int(capture.get("created_utc_ns", 0) or 0)
    created_utc = (datetime.fromtimestamp(created_ns / 1e9, timezone.utc)
                   if created_ns else None)
    run_id = manifest["run_id"]
    checks = analysis.get("exact_checks") or []

    probe_rows, receiver_rows = [], []
    for check_index, check in enumerate(checks):
        probe_rows.append((run_id, check_index, check.get("start_s"),
                           check.get("duration_s"), check.get("candidate"),
                           check.get("qualified"), check.get("followup_trigger"),
                           check.get("cfo_difference_hz"),
                           check.get("epoch_difference_samples"), canonical_json(check)))
        receivers = check.get("receivers") or []
        candidates = check.get("receiver_candidates") or []
        qualified = check.get("receiver_qualified") or []
        if len(receivers) != receiver_count:
            raise ValueError(f"probe {check_index} receiver count mismatch")
        for receiver_index, receiver in enumerate(receivers):
            acquisition = receiver.get("acquisition") or {}
            exact = acquisition.get("exact_match") or {}
            stats = _at(sample_stats, receiver_index, {}) or {}
            receiver_rows.append((
                run_id, check_index, receiver_index,
                _at(labels, receiver_index), bool(_at(candidates, receiver_index, False)),
                bool(_at(qualified, receiver_index, False)),
                exact.get("frequency_offset_hz"), exact.get("epoch_sample"),
                exact.get("score"), acquisition.get("match_score_margin"),
                stats.get("rms_magnitude"), stats.get("near_full_scale_fraction"),
                canonical_json(receiver)))

    windows = [(run_id, index, value.get("start_s"), value.get("duration_s"),
                value.get("qualified"), canonical_json(value))
               for index, value in enumerate(analysis.get("windows") or [])]
    followup = documents.get("followup") or {}
    followup_rows = [(run_id, index, value.get("start_s"), value.get("duration_s"),
                      value.get("candidate"), value.get("qualified"),
                      canonical_json(value))
                     for index, value in enumerate(followup.get("checks") or [])]
    events = [(run_id, index, event.get("start_s"), event.get("stop_s"),
               int(event.get("link_count", 0)))
              for index, event in enumerate(confirmed_beacon_events(
                  followup.get("confirmation") or {}))]
    track_rows, track_point_rows = [], []
    track_document = documents.get("track") or {}
    for track_index, track in enumerate(track_document.get("tracks") or []):
        track_summary = track.get("summary") or {}
        consensus = track.get("consensus") or {}
        observations = track.get("observations") or []
        track_rows.append((
            run_id, "continuous", track_index, track.get("track_id"),
            bool(track.get("qualified", consensus.get("qualified", False))),
            len(observations), track_summary.get("valid_duration_s"),
            canonical_json(track)))
        for point_index, point in enumerate(observations):
            track_point_rows.append((
                run_id, "continuous", track_index, point_index,
                point.get("time_s"), point.get("utc"), point.get("lock_valid"),
                canonical_json(point)))

    decode = documents.get("decoded")
    decode_row = None
    if decode is not None:
        combined = decode.get("combined") or {}
        pilot = (combined.get("soft_dual_rx") or {}).get("pilot") or {}
        decode_row = (run_id, combined.get("minimum_frame_count"),
                      pilot.get("hard_symbol_accuracy",
                                combined.get("minimum_pilot_accuracy")),
                      combined.get("minimum_sss_accuracy"),
                      pilot.get("soft_mean_confidence"), pilot.get("rms_evm"),
                      canonical_json(decode))

    association_rows, candidate_rows = [], []
    for kind, document in sorted(documents.items()):
        if kind != "association" and not kind.startswith("association:") and \
                kind != "linked_association":
            continue
        for association_index, association in enumerate(
                document.get("associations") or []):
            candidates = association.get("candidates") or []
            best = min(candidates,
                       key=lambda item: int(item.get("rank", 1 << 30)), default={})
            association_rows.append((
                run_id, kind, association_index, association.get("track_id"),
                bool(association.get("qualified")),
                association.get("best_norad_id", best.get("norad_id")),
                association.get("best_name", best.get("name")),
                association.get("holdout_residual_rms_hz",
                                best.get("holdout_residual_rms_hz")),
                canonical_json(association)))
            for candidate_index, candidate in enumerate(candidates):
                candidate_rows.append((
                    run_id, kind, association_index, candidate_index,
                    candidate.get("rank"), candidate.get("norad_id"),
                    candidate.get("name"), candidate.get("train_residual_rms_hz"),
                    candidate.get("holdout_residual_rms_hz"),
                    canonical_json(candidate)))
    detail = capture_dashboard_record(Path(shared_root), manifest["recording_id"])
    if detail is None:
        raise ValueError(f"cannot construct dashboard record: {manifest['recording_id']}")
    listing = listing_row(detail)
    summary = analysis.get("summary") or {}
    configured_gain = capture.get("configured_gain_db")
    recording = (
        manifest["recording_id"], manifest["capture_manifest_sha256"], created_utc,
        identity.get("radio_id"), identity.get("serial"), canonical_json(labels),
        receiver_count, metadata.get("channel_number"), metadata.get("region"),
        metadata.get("observation_mode") or manifest.get("mode"), capture.get("gain_mode"),
        configured_gain, capture.get("center_frequency_hz"), capture.get("rf_center_hz"),
        capture.get("sample_rate_hz"), capture.get("bandwidth_hz"),
        capture.get("requested_duration_s"), canonical_json(capture))
    return {
        "recording": recording,
        "parameters": canonical_json(analysis.get("analysis") or {}),
        "summary": (
            run_id, summary.get("window_count"), summary.get("qualified_window_count"),
            summary.get("exact_check_count"), summary.get("exact_candidate_count"),
            summary.get("exact_qualified_count"),
            summary.get("single_receiver_candidate_count"),
            summary.get("single_receiver_qualified_count"),
            summary.get("followup_trigger_count"), summary.get("exact_sampled_time_s"),
            summary.get("exact_temporal_coverage_fraction"), canonical_json(summary)),
        "windows": windows, "probes": probe_rows, "receiver_probes": receiver_rows,
        "followups": followup_rows, "events": events,
        "tracks": track_rows, "track_points": track_point_rows,
        "decode": decode_row, "associations": association_rows,
        "association_candidates": candidate_rows,
        "documents": [(run_id, kind, value.get("schema"), canonical_json(value))
                      for kind, value in sorted(documents.items())],
        "sources": [(run_id, kind, entry.get("schema"), entry["path"],
                     int(entry["bytes"]), entry["sha256"])
                    for kind, entry in sorted(manifest["documents"].items())],
        "artifacts": [(run_id, kind, entry["path"], entry.get("media_type"),
                       int(entry["bytes"]), entry["sha256"])
                      for kind, entry in sorted(manifest.get("artifacts", {}).items())],
        "dashboard": (run_id, manifest["recording_id"], detail.get("start_utc"),
                      bool(detail.get("confirmed")), bool(detail.get("decoded")),
                      bool(detail.get("qualified_tle_association_count")),
                      canonical_json(listing), canonical_json(detail)),
    }
