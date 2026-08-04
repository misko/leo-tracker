"""Stable, hardware-independent contracts joining orbit and radio pipelines."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def require_utc(value: datetime) -> datetime:
    """Reject ambiguous timestamps and normalize UTC-aware values to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class EphemerisProvenance:
    source: str
    retrieved_at: datetime
    element_epoch: datetime
    sha256: str
    frame: str = "TEME"
    propagator: str = "SGP4"

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at", require_utc(self.retrieved_at))
        object.__setattr__(self, "element_epoch", require_utc(self.element_epoch))
        if len(self.sha256) != 64:
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        try:
            int(self.sha256, 16)
        except ValueError as error:
            raise ValueError("sha256 must be hexadecimal") from error


@dataclass(frozen=True)
class PredictedObservation:
    timestamp: datetime
    norad_id: int
    azimuth_deg: float
    elevation_deg: float
    range_m: float
    range_rate_m_s: float
    carrier_hz: float
    doppler_hz: float
    ephemeris: EphemerisProvenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", require_utc(self.timestamp))


@dataclass(frozen=True)
class FrequencyObservation:
    timestamp: datetime
    frequency_hz: float
    uncertainty_hz: float
    snr_db: float | None = None
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", require_utc(self.timestamp))
        if self.uncertainty_hz < 0:
            raise ValueError("uncertainty_hz must be non-negative")


@dataclass(frozen=True)
class CaptureProvenance:
    capture_id: str
    started_at: datetime
    sample_rate_hz: float
    center_frequency_hz: float
    radio_serial: str | None
    firmware: str | None
    clock_source: str
    configuration: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", require_utc(self.started_at))
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")


def to_json_dict(value: Any) -> dict[str, Any]:
    """Convert a contract dataclass to JSON-compatible primitive values."""

    def convert(item: Any) -> Any:
        if isinstance(item, datetime):
            return require_utc(item).isoformat().replace("+00:00", "Z")
        if isinstance(item, tuple):
            return [convert(member) for member in item]
        if isinstance(item, dict):
            return {key: convert(member) for key, member in item.items()}
        return item

    return convert(asdict(value))
