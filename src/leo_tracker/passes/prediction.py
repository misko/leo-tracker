"""Deterministic pass finding from SGP4 observer geometry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from leo_tracker.orbit.propagation import propagate_ecef
from leo_tracker.orbit.tle import TLE, _require_utc
from leo_tracker.orbit.topocentric import LookAngle, Observer, look_angle


@dataclass(frozen=True)
class PassSample:
    time: datetime
    look: LookAngle


@dataclass(frozen=True)
class Pass:
    rise: PassSample
    culmination: PassSample
    set: PassSample


def _sample(tle: TLE, observer: Observer, time: datetime) -> PassSample:
    return PassSample(time, look_angle(observer, propagate_ecef(tle, time)))


def sample_track(tle: TLE, observer: Observer, start: datetime, end: datetime, *,
                 step_seconds: float) -> list[PassSample]:
    """Sample the actual propagated topocentric trajectory, including both ends."""
    _require_utc(start); _require_utc(end)
    if end < start or step_seconds <= 0:
        raise ValueError("track end must not precede start and step_seconds must be positive")
    result = []; time = start
    while time < end:
        result.append(_sample(tle, observer, time))
        time = min(end, time+timedelta(seconds=step_seconds))
    result.append(_sample(tle, observer, end))
    return result


def _crossing(tle: TLE, observer: Observer, a: datetime, b: datetime, horizon: float) -> PassSample:
    ea = _sample(tle, observer, a).look.elevation_deg - horizon
    for _ in range(32):
        mid = a + (b-a)/2
        em = _sample(tle, observer, mid).look.elevation_deg - horizon
        if (ea >= 0) == (em >= 0):
            a, ea = mid, em
        else:
            b = mid
        if (b-a).total_seconds() < 0.01:
            break
    return _sample(tle, observer, a + (b-a)/2)


def _culmination(tle: TLE, observer: Observer, a: datetime, b: datetime) -> PassSample:
    """Maximize elevation in a bracket using deterministic ternary search."""
    for _ in range(40):
        left = a + (b-a)/3
        right = b - (b-a)/3
        if _sample(tle, observer, left).look.elevation_deg < _sample(tle, observer, right).look.elevation_deg:
            a = left
        else:
            b = right
        if (b-a).total_seconds() < 0.01:
            break
    return _sample(tle, observer, a + (b-a)/2)


def predict_passes(tle: TLE, observer: Observer, start: datetime, end: datetime, *, horizon_deg: float = 10.0, step_seconds: float = 30.0) -> list[Pass]:
    """Find passes, refining horizon crossings to better than 0.01 seconds."""
    _require_utc(start); _require_utc(end)
    if end <= start or step_seconds <= 0:
        raise ValueError("end must follow start and step_seconds must be positive")
    if not -90 <= horizon_deg < 90:
        raise ValueError("horizon_deg must be in [-90, 90)")
    result: list[Pass] = []
    previous = _sample(tle, observer, start)
    rise = previous if previous.look.elevation_deg >= horizon_deg else None
    best = previous if rise else None
    time = min(end, start + timedelta(seconds=step_seconds))
    while time <= end:
        current = _sample(tle, observer, time)
        was_up = previous.look.elevation_deg >= horizon_deg
        is_up = current.look.elevation_deg >= horizon_deg
        if not was_up and is_up:
            rise = _crossing(tle, observer, previous.time, current.time, horizon_deg)
            best = current
        if is_up and (best is None or current.look.elevation_deg > best.look.elevation_deg):
            best = current
        if was_up and not is_up and rise is not None and best is not None:
            setting = _crossing(tle, observer, previous.time, current.time, horizon_deg)
            bracket = timedelta(seconds=step_seconds)
            peak = _culmination(tle, observer, max(rise.time, best.time-bracket), min(setting.time, best.time+bracket))
            peak = max((rise, peak, setting), key=lambda sample: sample.look.elevation_deg)
            result.append(Pass(rise, peak, setting))
            rise = best = None
        if time == end:
            break
        previous = current
        time = min(end, time + timedelta(seconds=step_seconds))
    if rise is not None and best is not None:
        result.append(Pass(rise, best, _sample(tle, observer, end)))
    return result
