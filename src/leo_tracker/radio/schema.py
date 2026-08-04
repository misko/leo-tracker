"""Versioned persisted radio artifact contracts."""
from __future__ import annotations

CAPTURE_SCHEMA_VERSION = 1
RIDGE_SCHEMA_VERSION = 1

CAPTURE_REQUIRED_FIELDS = frozenset({
    "schema_version", "capture_id", "created_utc", "sample_dtype", "sample_count",
    "radio_config", "radio_identity", "files",
})


def validate_capture_manifest(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("capture manifest must be a JSON object")
    missing = CAPTURE_REQUIRED_FIELDS - value.keys()
    if missing:
        raise ValueError(f"capture manifest missing fields: {', '.join(sorted(missing))}")
    if value["schema_version"] != CAPTURE_SCHEMA_VERSION:
        raise ValueError(f"unsupported capture schema version: {value['schema_version']}")
    if value["sample_dtype"] != "complex64-le" or not isinstance(value["sample_count"], int):
        raise ValueError("unsupported sample representation")
    files = value.get("files")
    if not isinstance(files, dict) or "iq.c64" not in files or "sha256" not in files["iq.c64"]:
        raise ValueError("capture manifest lacks IQ checksum")
    return value
