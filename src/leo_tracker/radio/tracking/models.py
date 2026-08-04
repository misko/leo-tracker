from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TrackCandidate:
    """One receiver-local frequency trajectory produced by any tracker."""

    tracker: str
    receiver: int
    start_time_s: float
    stop_time_s: float
    time_s: tuple[float, ...]
    frequency_hz: tuple[float, ...]
    drift_hz_s: float
    curvature_hz_s2: float | None = None
    frequency_low_hz: float = 0.0
    frequency_high_hz: float = 0.0
    supporting_features: int = 1
    signal_score: float = 0.0
    false_alarm_probability: float | None = None
    qualified: bool = False
    warnings: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JointTrack:
    tracker: str
    member_indexes: tuple[int, int]
    receiver_path_correlation: float
    receiver_frequency_offset_hz: float
    drift_difference_hz_s: float
    confidence: float
    qualified: bool
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TrackerReport:
    schema: str
    source: str
    configuration: dict[str, Any]
    candidates: tuple[TrackCandidate, ...]
    joint_tracks: tuple[JointTrack, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    identifications: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "source": self.source,
                "configuration": self.configuration,
                "candidates": [item.to_dict() for item in self.candidates],
                "joint_tracks": [item.to_dict() for item in self.joint_tracks],
                "metrics": self.metrics,
                "identifications": list(self.identifications)}
