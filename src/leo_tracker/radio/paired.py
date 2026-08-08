"""Synchronous two-channel acquisition and jointly published artifacts."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Protocol
import uuid

import numpy as np

from .artifact import CaptureArtifact, _sha256
from .schema import CAPTURE_SCHEMA_VERSION
from .source import RadioConfig


@dataclass(frozen=True)
class PairedSampleBlock:
    rx0: np.ndarray
    rx1: np.ndarray
    sample_index: int
    utc_ns: int
    dropped_samples: int = 0
    read_duration_ns: int | None = None
    gain_db: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        a, b = np.asarray(self.rx0), np.asarray(self.rx1)
        if a.ndim != 1 or b.ndim != 1 or a.size != b.size:
            raise ValueError("paired blocks require equal one-dimensional channels")
        if not np.iscomplexobj(a) or not np.iscomplexobj(b):
            raise ValueError("paired channel samples must be complex")


@dataclass(frozen=True)
class PairedCI16Block:
    """Native dual-RX I/Q components without a complex64 round trip.

    Components are ordered I0, Q0, I1, Q1 and retain the libiio-owned buffers
    until the consumer has copied them into the durable CI16 layout.
    """
    components: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]
    sample_index: int
    utc_ns: int
    dropped_samples: int = 0
    read_duration_ns: int | None = None
    gain_db: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        values = tuple(np.asarray(item) for item in self.components)
        if len(values) != 4 or any(item.ndim != 1 for item in values):
            raise ValueError("native paired blocks require four one-dimensional components")
        if len({item.size for item in values}) != 1:
            raise ValueError("native paired block components differ in length")
        if any(item.dtype.kind != "i" or item.dtype.itemsize != 2 for item in values):
            raise ValueError("native paired block components must be signed int16")
        object.__setattr__(self, "components", values)

    @property
    def sample_count(self) -> int:
        return int(self.components[0].size)


def paired_sample_count(block: PairedSampleBlock | PairedCI16Block) -> int:
    return (block.sample_count if isinstance(block, PairedCI16Block)
            else int(block.rx0.size))


class PairedRadioSource(Protocol):
    @property
    def configs(self) -> tuple[RadioConfig, RadioConfig]: ...
    @property
    def identity(self) -> Mapping[str, object]: ...
    def blocks(self) -> Iterator[PairedSampleBlock]: ...
    def close(self) -> None: ...


class FakePairedSource:
    def __init__(self, rx0, rx1, config: RadioConfig, *, block_size: int = 4096,
                 start_utc_ns: int = 0):
        self.values = (np.asarray(rx0, np.complex64), np.asarray(rx1, np.complex64))
        if self.values[0].shape != self.values[1].shape: raise ValueError("fake channels differ in shape")
        self._configs = (replace(config, channel=0), replace(config, channel=1))
        self.block_size, self.start_utc_ns, self.closed = block_size, start_utc_ns, False
        self.retune_history: list[float] = []

    @property
    def configs(self): return self._configs
    @property
    def identity(self): return {"kind": "fake-paired"}
    def blocks(self):
        for start in range(0, self.values[0].size, self.block_size):
            utc = self.start_utc_ns + round(start * 1e9 / self._configs[0].sample_rate_hz)
            yield PairedSampleBlock(self.values[0][start:start+self.block_size],
                                    self.values[1][start:start+self.block_size], start, utc)
    def close(self): self.closed = True
    def retune(self, center_frequency_hz: float):
        if center_frequency_hz <= 0:
            raise ValueError("center frequency must be positive")
        self.retune_history.append(float(center_frequency_hz))


def capture_pair_to_artifacts(source: PairedRadioSource, destination: str | Path, *,
                              sample_count: int | None = None, metadata: dict | None = None
                              ) -> tuple[CaptureArtifact, CaptureArtifact]:
    """Publish a session directory containing synchronized ``rx0``/``rx1`` artifacts."""
    destination = Path(destination)
    if destination.exists(): raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.incomplete-", dir=destination.parent))
    session_id = str(uuid.uuid4()); configs = source.configs
    for name in ("rx0", "rx1"): (temporary / name).mkdir()
    paths = (temporary / "rx0" / "iq.c64", temporary / "rx1" / "iq.c64")
    total = expected = drops = 0; first_utc_ns = None
    try:
        with paths[0].open("xb") as file0, paths[1].open("xb") as file1:
            for block in source.blocks():
                if block.sample_index != expected:
                    raise ValueError(f"non-contiguous paired block: expected {expected}, got {block.sample_index}")
                count = block.rx0.size
                if sample_count is not None: count = min(count, sample_count - total)
                if count <= 0: break
                file0.write(np.asarray(block.rx0[:count], dtype="<c8").tobytes())
                file1.write(np.asarray(block.rx1[:count], dtype="<c8").tobytes())
                if first_utc_ns is None: first_utc_ns = block.utc_ns
                total += count; expected += block.rx0.size; drops += block.dropped_samples
                if sample_count is not None and total == sample_count: break
            if sample_count is not None and total != sample_count:
                raise RuntimeError(f"paired source ended with {sample_count-total} requested samples missing")
            for stream in (file0, file1): stream.flush(); os.fsync(stream.fileno())
        manifests = []
        created = datetime.now(timezone.utc).isoformat()
        for channel, (config, path) in enumerate(zip(configs, paths)):
            capture_id = str(uuid.uuid4())
            pair_metadata = dict(metadata or {})
            pair_metadata.update({"pair_session_id": session_id, "paired_channel": channel,
                                  "paired_capture_id": None, "synchronized_first_buffer_utc_ns": first_utc_ns})
            manifest = {"schema_version": CAPTURE_SCHEMA_VERSION, "capture_id": capture_id,
                "created_utc": created, "sample_dtype": "complex64-le", "sample_count": total,
                "start_utc_ns": first_utc_ns, "dropped_samples": drops,
                "radio_config": asdict(config), "radio_identity": dict(source.identity),
                "metadata": pair_metadata,
                "files": {"iq.c64": {"bytes": path.stat().st_size, "sha256": _sha256(path)}}}
            manifests.append(manifest)
        manifests[0]["metadata"]["paired_capture_id"] = manifests[1]["capture_id"]
        manifests[1]["metadata"]["paired_capture_id"] = manifests[0]["capture_id"]
        for channel, (path, manifest) in enumerate(zip(paths, manifests)):
            manifest_path = temporary / f"rx{channel}" / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            with manifest_path.open("rb") as stream: os.fsync(stream.fileno())
            os.chmod(path, 0o444); os.chmod(manifest_path, 0o444)
        session_manifest = temporary / "pair.json"
        session_manifest.write_text(json.dumps({"schema_version": 1, "pair_session_id": session_id,
            "first_buffer_utc_ns": first_utc_ns, "sample_count_per_channel": total,
            "captures": {"rx0": manifests[0]["capture_id"], "rx1": manifests[1]["capture_id"]}},
            indent=2, sort_keys=True) + "\n")
        os.chmod(session_manifest, 0o444)
        os.replace(temporary, destination)
        return (CaptureArtifact(destination / "rx0", manifests[0]),
                CaptureArtifact(destination / "rx1", manifests[1]))
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True); raise
    finally:
        source.close()
