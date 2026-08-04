"""Minimal, strict NMEA-0183 reader for USB GNSS receivers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import os
import select
import termios
from time import monotonic


@dataclass(frozen=True)
class GPSFix:
    timestamp: datetime
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    quality: int
    satellites: int
    hdop: float
    device: str | None = None


def _checksum(sentence: str) -> str:
    value = 0
    for character in sentence:
        value ^= ord(character)
    return f"{value:02X}"


def _coordinate(value: str, hemisphere: str) -> float:
    if not value or hemisphere not in {"N", "S", "E", "W"}:
        raise ValueError("missing NMEA coordinate")
    degree_digits = 2 if hemisphere in {"N", "S"} else 3
    result = float(value[:degree_digits]) + float(value[degree_digits:]) / 60.0
    return -result if hemisphere in {"S", "W"} else result


def _utc_time(value: str) -> time:
    if len(value) < 6:
        raise ValueError("missing NMEA UTC time")
    seconds = float(value[4:])
    whole = int(seconds)
    micros = round((seconds - whole) * 1_000_000)
    return time(int(value[:2]), int(value[2:4]), whole, micros, tzinfo=timezone.utc)


def parse_sentence(line: str, *, day: date | None = None) -> GPSFix | date | None:
    """Parse checksummed GGA fixes and RMC dates; ignore other sentence types."""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        raise ValueError("not a checksummed NMEA sentence")
    body, supplied = line[1:].rsplit("*", 1)
    if _checksum(body) != supplied.upper():
        raise ValueError("NMEA checksum mismatch")
    fields = body.split(",")
    kind = fields[0][-3:]
    if kind == "RMC":
        if len(fields) > 9 and len(fields[9]) == 6:
            return date(2000 + int(fields[9][4:6]), int(fields[9][2:4]), int(fields[9][:2]))
        return None
    if kind != "GGA":
        return None
    quality = int(fields[6] or 0)
    if quality <= 0:
        return None
    if day is None:
        day = datetime.now(timezone.utc).date()
    timestamp = datetime.combine(day, _utc_time(fields[1]))
    return GPSFix(
        timestamp=timestamp,
        latitude_deg=_coordinate(fields[2], fields[3]),
        longitude_deg=_coordinate(fields[4], fields[5]),
        altitude_m=float(fields[9]),
        quality=quality,
        satellites=int(fields[7]),
        hdop=float(fields[8]),
    )


def acquire_fix(device: str, timeout_seconds: float = 120.0, *, min_satellites: int = 4,
                max_hdop: float = 10.0) -> GPSFix:
    """Wait for a trustworthy GGA fix, rejecting stale, weak, or malformed data."""
    descriptor = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(descriptor)
        attrs[4] = attrs[5] = termios.B9600
        attrs[3] = 0
        termios.tcsetattr(descriptor, termios.TCSANOW, attrs)
        deadline = monotonic() + timeout_seconds
        buffered = b""
        current_day: date | None = None
        last_reason = "no valid NMEA fix received"
        while monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], min(1.0, deadline - monotonic()))
            if not ready:
                continue
            buffered += os.read(descriptor, 4096)
            while b"\n" in buffered:
                raw, buffered = buffered.split(b"\n", 1)
                try:
                    parsed = parse_sentence(raw.decode("ascii", errors="strict"), day=current_day)
                except (UnicodeError, ValueError, IndexError):
                    continue
                if isinstance(parsed, date) and not isinstance(parsed, datetime):
                    current_day = parsed
                elif isinstance(parsed, GPSFix):
                    if parsed.satellites < min_satellites:
                        last_reason = f"only {parsed.satellites} satellites"
                    elif parsed.hdop > max_hdop:
                        last_reason = f"HDOP {parsed.hdop} exceeds {max_hdop}"
                    else:
                        return GPSFix(**{**parsed.__dict__, "device": device})
        raise TimeoutError(f"GPS fix timeout after {timeout_seconds:g}s: {last_reason}")
    finally:
        os.close(descriptor)
