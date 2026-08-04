"""Small, dependency-free NMEA snapshot reader for capture provenance."""
from __future__ import annotations

from datetime import datetime, timezone
import os
import select
import termios
import time
from pathlib import Path


def _field_coordinate(value: str, hemisphere: str) -> float:
    if not value or hemisphere not in ("N", "S", "E", "W"):
        raise ValueError("invalid NMEA coordinate")
    degrees_width = 2 if hemisphere in ("N", "S") else 3
    degrees = int(value[:degrees_width])
    minutes = float(value[degrees_width:])
    result = degrees + minutes/60
    return -result if hemisphere in ("S", "W") else result


def _sentence(line: str) -> list[str] | None:
    value = line.strip()
    if not value.startswith("$") or "*" not in value:
        return None
    body, checksum = value[1:].rsplit("*", 1)
    expected = 0
    for character in body:
        expected ^= ord(character)
    try:
        if expected != int(checksum[:2], 16):
            return None
    except ValueError:
        return None
    return body.split(",")


def parse_nmea_snapshot(lines: list[str], *, host_received: datetime | None = None) -> dict:
    """Combine the latest valid RMC time/position and GGA fix-quality records."""
    rmc = gga = None
    for line in lines:
        fields = _sentence(line)
        if not fields:
            continue
        kind = fields[0][-3:]
        if kind == "RMC" and len(fields) >= 10 and fields[2] == "A":
            rmc = fields
        elif kind == "GGA" and len(fields) >= 10 and fields[6] not in ("", "0"):
            gga = fields
    if rmc is None:
        raise ValueError("no valid NMEA RMC fix")
    raw_time, raw_date = rmc[1], rmc[9]
    if len(raw_time) < 6 or len(raw_date) != 6:
        raise ValueError("NMEA fix has no complete UTC date/time")
    whole, _, fractional = raw_time.partition(".")
    gps_utc = datetime.strptime(raw_date+whole[:6], "%d%m%y%H%M%S").replace(
        tzinfo=timezone.utc, microsecond=int((fractional+"000000")[:6]))
    host = (host_received or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = {
        "source": "NMEA", "gps_utc": gps_utc.isoformat().replace("+00:00", "Z"),
        "host_received_utc": host.isoformat().replace("+00:00", "Z"),
        "host_minus_gps_s": (host-gps_utc).total_seconds(),
        "latitude_deg": _field_coordinate(rmc[3], rmc[4]),
        "longitude_deg": _field_coordinate(rmc[5], rmc[6]),
        "status": rmc[2], "mode": rmc[12] if len(rmc) > 12 else None,
    }
    if gga is not None:
        result.update({"fix_quality": int(gga[6]), "satellites": int(gga[7] or 0),
                       "hdop": float(gga[8]), "altitude_m": float(gga[9])})
    return result


def read_nmea_snapshot(device: Path, *, timeout_s: float = 3.0) -> dict:
    """Read enough serial data for a valid fix and restore the original TTY state."""
    if timeout_s <= 0:
        raise ValueError("GPS timeout must be positive")
    fd = os.open(Path(device), os.O_RDONLY | os.O_NONBLOCK)
    original = termios.tcgetattr(fd)
    configured = list(original)
    configured[4] = configured[5] = termios.B9600
    termios.tcsetattr(fd, termios.TCSANOW, configured)
    data = bytearray(); deadline = time.monotonic()+timeout_s
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], min(.5, deadline-time.monotonic()))
            if readable:
                try:
                    data.extend(os.read(fd, 4096))
                except BlockingIOError:
                    pass
                lines = data.decode("ascii", "ignore").splitlines()
                if any("RMC" in line for line in lines) and any("GGA" in line for line in lines):
                    try:
                        return parse_nmea_snapshot(lines)
                    except ValueError:
                        pass
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)
        os.close(fd)
    raise TimeoutError(f"no valid GPS fix from {device} within {timeout_s:g} s")
