from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class RadioConfig:
    center_frequency_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    gain_db: float | None = None
    channel: int = 0
    gain_mode: str | None = None

    def __post_init__(self) -> None:
        if self.center_frequency_hz <= 0 or self.sample_rate_hz <= 0:
            raise ValueError("center frequency and sample rate must be positive")
        if not 0 < self.bandwidth_hz <= self.sample_rate_hz:
            raise ValueError("bandwidth must be positive and no greater than sample rate")
        if self.gain_mode not in (None, "manual", "slow_attack", "fast_attack"):
            raise ValueError("gain mode must be manual, slow_attack, or fast_attack")
        if self.gain_db is not None and self.gain_mode not in (None, "manual"):
            raise ValueError("a fixed gain is only valid in manual mode")


@dataclass(frozen=True)
class SampleBlock:
    samples: npt.NDArray[np.complex64]
    sample_index: int
    utc_ns: int | None = None
    dropped_samples: int = 0

    def __post_init__(self) -> None:
        array = np.asarray(self.samples)
        if array.ndim != 1 or not np.issubdtype(array.dtype, np.complexfloating):
            raise ValueError("samples must be a one-dimensional complex array")
        if self.sample_index < 0 or self.dropped_samples < 0:
            raise ValueError("sample indexes and drop counts cannot be negative")


@runtime_checkable
class RadioSource(Protocol):
    @property
    def config(self) -> RadioConfig: ...
    @property
    def identity(self) -> Mapping[str, object]: ...
    def blocks(self) -> Iterator[SampleBlock]: ...
    def close(self) -> None: ...


class FakeSource:
    """Deterministic source used by tests and simulated experiments."""

    def __init__(self, samples: npt.ArrayLike, config: RadioConfig, block_size: int = 4096,
                 *, start_utc_ns: int | None = None, identity: Mapping[str, object] | None = None):
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        self._samples = np.asarray(samples, dtype=np.complex64)
        if self._samples.ndim != 1:
            raise ValueError("samples must be one-dimensional")
        self._config, self._block_size, self._start = config, block_size, start_utc_ns
        self._identity = dict(identity or {"kind": "fake"})
        self.closed = False

    @property
    def config(self) -> RadioConfig: return self._config
    @property
    def identity(self) -> Mapping[str, object]: return dict(self._identity)

    def blocks(self) -> Iterator[SampleBlock]:
        if self.closed:
            raise RuntimeError("source is closed")
        for start in range(0, self._samples.size, self._block_size):
            utc = None if self._start is None else self._start + round(start * 1e9 / self.config.sample_rate_hz)
            yield SampleBlock(self._samples[start:start + self._block_size].copy(), start, utc)

    def close(self) -> None: self.closed = True


class ReplaySource(FakeSource):
    """Replay a verified capture through the same source interface."""

    def __init__(self, path: str | Path, block_size: int = 4096):
        from .artifact import CaptureArtifact
        artifact = CaptureArtifact.open(path, verify=True)
        manifest = artifact.manifest
        config = RadioConfig(**manifest["radio_config"])
        samples = artifact.load_samples()
        super().__init__(samples, config, block_size, start_utc_ns=manifest.get("start_utc_ns"),
                         identity={"kind": "replay", "capture_id": manifest["capture_id"]})
