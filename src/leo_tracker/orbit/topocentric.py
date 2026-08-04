"""WGS-84 observer geometry in the rotating Earth frame."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .propagation import ECEFState


@dataclass(frozen=True)
class Observer:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float = 0.0

    def __post_init__(self) -> None:
        if not -90 <= self.latitude_deg <= 90:
            raise ValueError("latitude must be in [-90, 90] degrees")
        if not -180 <= self.longitude_deg <= 180:
            raise ValueError("longitude must be in [-180, 180] degrees")

    def ecef_km(self) -> tuple[float, float, float]:
        lat, lon = math.radians(self.latitude_deg), math.radians(self.longitude_deg)
        a, e2 = 6378.137, 6.69437999014e-3
        n = a / math.sqrt(1.0 - e2 * math.sin(lat)**2)
        h = self.altitude_m / 1000.0
        return ((n+h)*math.cos(lat)*math.cos(lon), (n+h)*math.cos(lat)*math.sin(lon), (n*(1-e2)+h)*math.sin(lat))


@dataclass(frozen=True)
class LookAngle:
    azimuth_deg: float
    elevation_deg: float
    range_km: float
    range_rate_km_s: float


def look_angle(observer: Observer, state: ECEFState) -> LookAngle:
    ox, oy, oz = observer.ecef_km()
    dx, dy, dz = (state.position_km[0]-ox, state.position_km[1]-oy, state.position_km[2]-oz)
    lat, lon = math.radians(observer.latitude_deg), math.radians(observer.longitude_deg)
    east = -math.sin(lon)*dx + math.cos(lon)*dy
    north = -math.sin(lat)*math.cos(lon)*dx - math.sin(lat)*math.sin(lon)*dy + math.cos(lat)*dz
    up = math.cos(lat)*math.cos(lon)*dx + math.cos(lat)*math.sin(lon)*dy + math.sin(lat)*dz
    distance = math.sqrt(dx*dx + dy*dy + dz*dz)
    azimuth = math.degrees(math.atan2(east, north)) % 360.0
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, up/distance))))
    rate = (dx*state.velocity_km_s[0] + dy*state.velocity_km_s[1] + dz*state.velocity_km_s[2]) / distance
    return LookAngle(azimuth, elevation, distance, rate)
