"""Content identities for immutable analysis-store runs."""
from __future__ import annotations

import hashlib
import json

INPUT_SCHEMA = "leo-tracker.analysis-store-input/v1"


def canonical_json(value) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def sha256_json(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def run_identity_payload(manifest: dict) -> dict:
    completion = manifest.get("completion") or {}
    analysis = (manifest.get("documents") or {}).get("analysis") or {}
    return {
        "recording_id": manifest.get("recording_id"),
        "pipeline_id": manifest.get("pipeline_id"),
        "completion_sha256": completion.get("sha256"),
        "capture_manifest_sha256": manifest.get("capture_manifest_sha256"),
        "context": manifest.get("context"),
        "documents": [[kind, item.get("bytes"), item.get("sha256")]
                      for kind, item in sorted((manifest.get("documents") or {}).items())],
        "artifacts": [[kind, item.get("bytes"), item.get("sha256")]
                     for kind, item in sorted((manifest.get("artifacts") or {}).items())],
        "analysis_schema": analysis.get("schema"),
    }


def run_id_for_manifest(manifest: dict) -> str:
    if manifest.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"unexpected analysis-store input schema: {manifest.get('schema')!r}")
    return hashlib.sha256(canonical_json(run_identity_payload(manifest)).encode()).hexdigest()
