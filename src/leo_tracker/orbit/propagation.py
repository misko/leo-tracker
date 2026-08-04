"""SGP4 propagation in TEME and a documented TEME-to-ECEF conversion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from .tle import TLE, _require_utc


@dataclass(frozen=True)
class TEMEState:
    time: datetime
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]
    frame: str = "TEME"


@dataclass(frozen=True)
class ECEFState:
    time: datetime
    position_km: tuple[float, float, float]
    velocity_km_s: tuple[float, float, float]
    frame: str = "ITRF_APPROX"


def propagate_teme(tle: TLE, time: datetime) -> TEMEState:
    """Propagate with the Vallado SGP4 implementation supplied by ``sgp4``."""
    _require_utc(time)
    try:
        from sgp4.api import Satrec, jday, SGP4_ERRORS
    except ImportError as exc:  # pragma: no cover - actionable packaging failure
        raise RuntimeError("SGP4 propagation requires the 'sgp4' package") from exc
    sat = Satrec.twoline2rv(tle.line1, tle.line2)
    second = time.second + time.microsecond / 1e6
    jd, fraction = jday(time.year, time.month, time.day, time.hour, time.minute, second)
    error, position, velocity = sat.sgp4(jd, fraction)
    if error:
        raise ValueError(f"SGP4 error {error}: {SGP4_ERRORS.get(error, 'unknown error')}")
    return TEMEState(time, tuple(position), tuple(velocity))


def _julian_date(time: datetime) -> float:
    # Unix epoch is exactly JD 2440587.5 UTC.
    return 2440587.5 + time.timestamp() / 86400.0


def _gmst_radians(time: datetime) -> float:
    """IAU-82 GMST; UT1 is approximated by UTC (documented frame limitation)."""
    jd = _julian_date(time)
    t = (jd - 2451545.0) / 36525.0
    degrees = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933*t*t - t*t*t/38710000.0
    return math.radians(degrees % 360.0)


def teme_to_ecef(state: TEMEState) -> ECEFState:
    """Rotate TEME to an approximate ITRF/ECEF frame.

    This uses GMST with UT1=UTC and omits polar motion, hence ``ITRF_APPROX``;
    callers needing centimetre precision must use Earth orientation parameters.
    """
    theta = _gmst_radians(state.time)
    c, s = math.cos(theta), math.sin(theta)
    x, y, z = state.position_km
    vx, vy, vz = state.velocity_km_s
    r = (c*x + s*y, -s*x + c*y, z)
    rotated_v = (c*vx + s*vy, -s*vx + c*vy, vz)
    omega = 7.29211514670698e-5
    v = (rotated_v[0] + omega*r[1], rotated_v[1] - omega*r[0], rotated_v[2])
    return ECEFState(state.time, r, v)


def propagate_ecef(tle: TLE, time: datetime) -> ECEFState:
    return teme_to_ecef(propagate_teme(tle, time))
