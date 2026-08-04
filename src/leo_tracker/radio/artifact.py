from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

import numpy as np

from .source import RadioSource
from .schema import CAPTURE_SCHEMA_VERSION, validate_capture_manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CaptureArtifact:
    path: Path
    manifest: dict

    @classmethod
    def open(cls, path: str | Path, *, verify: bool = True) -> "CaptureArtifact":
        path = Path(path)
        manifest = validate_capture_manifest(json.loads((path / "manifest.json").read_text()))
        obj = cls(path, manifest)
        if verify:
            expected = manifest["files"]["iq.c64"]["sha256"]
            if _sha256(path / "iq.c64") != expected:
                raise ValueError("capture IQ checksum mismatch")
        return obj

    def load_samples(self, *, mmap: bool = False) -> np.ndarray:
        """Read IQ, optionally as a read-only memory map for field-sized captures."""
        if mmap:
            return np.memmap(self.path / "iq.c64", dtype="<c8", mode="r")
        return np.fromfile(self.path / "iq.c64", dtype="<c8")


def capture_to_artifact(source: RadioSource, destination: str | Path, *,
                        metadata: dict | None = None) -> CaptureArtifact:
    """Drain *source* and atomically publish an immutable capture directory."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.incomplete-", dir=destination.parent))
    iq_path = temporary / "iq.c64"
    count, expected_index, start_utc_ns, drops = 0, 0, None, 0
    try:
        with iq_path.open("xb") as stream:
            for block in source.blocks():
                if block.sample_index != expected_index:
                    raise ValueError(f"non-contiguous block: expected {expected_index}, got {block.sample_index}")
                values = np.asarray(block.samples, dtype="<c8")
                stream.write(values.tobytes())
                if start_utc_ns is None: start_utc_ns = block.utc_ns
                count += values.size
                expected_index += values.size
                drops += block.dropped_samples
            stream.flush()
            os.fsync(stream.fileno())
        capture_id = str(uuid.uuid4())
        manifest = {
            "schema_version": CAPTURE_SCHEMA_VERSION, "capture_id": capture_id,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "sample_dtype": "complex64-le", "sample_count": count,
            "start_utc_ns": start_utc_ns, "dropped_samples": drops,
            "radio_config": asdict(source.config), "radio_identity": dict(source.identity),
            "metadata": metadata or {},
            "files": {"iq.c64": {"bytes": iq_path.stat().st_size, "sha256": _sha256(iq_path)}},
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        with manifest_path.open("rb") as stream: os.fsync(stream.fileno())
        os.chmod(iq_path, 0o444)
        os.chmod(manifest_path, 0o444)
        os.replace(temporary, destination)
        return CaptureArtifact(destination, manifest)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        source.close()
