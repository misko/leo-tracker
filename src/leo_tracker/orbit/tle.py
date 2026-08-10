"""Strict two-line element parsing with source provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib


@dataclass(frozen=True)
class TLEProvenance:
    source: str
    retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.retrieved_at is not None:
            _require_utc(self.retrieved_at)


@dataclass(frozen=True)
class TLE:
    name: str | None
    line1: str
    line2: str
    norad_id: int
    epoch: datetime
    provenance: TLEProvenance
    sha256: str


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be timezone-aware UTC")


# Space-Track Alpha-5 extends the five-character catalog field past 99999 by
# replacing the leading digit with a letter worth ten more than its position.
# "I" and "O" are omitted so they cannot be misread as "1" and "0".
ALPHA5_ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _catalog_number(field: str) -> int:
    """Decode a catalog field, accepting Alpha-5 (A0001 is 100001)."""
    value = field.strip()
    if value.isdigit():
        return int(value)
    if len(value) == 5 and value[1:].isdigit():
        leading = ALPHA5_ALPHABET.find(value[0])
        if leading >= 10:
            return leading * 10000 + int(value[1:])
    raise ValueError(f"invalid catalog number: {field!r}")


def _checksum_valid(line: str) -> bool:
    if len(line) != 69 or not line[68].isdigit():
        return False
    total = sum(int(c) if c.isdigit() else 1 if c == "-" else 0 for c in line[:68])
    return total % 10 == int(line[68])


def parse_tle(
    text: str,
    *,
    source: str = "unknown",
    retrieved_at: datetime | None = None,
    validate_checksum: bool = True,
) -> TLE:
    """Parse a named or unnamed TLE without silently repairing corrupt input."""
    rows = [row.rstrip("\r") for row in text.strip().splitlines() if row.strip()]
    if len(rows) == 2:
        name, line1, line2 = None, *rows
    elif len(rows) == 3:
        name, line1, line2 = rows
        if name.startswith("0 "):
            name = name[2:].strip()
    else:
        raise ValueError("TLE must contain two element lines and optional name")
    if not line1.startswith("1 ") or not line2.startswith("2 "):
        raise ValueError("invalid TLE line numbers")
    if len(line1) != 69 or len(line2) != 69:
        raise ValueError("TLE element lines must be exactly 69 characters")
    if validate_checksum and (not _checksum_valid(line1) or not _checksum_valid(line2)):
        raise ValueError("invalid TLE checksum")
    try:
        sat1, sat2 = _catalog_number(line1[2:7]), _catalog_number(line2[2:7])
        yy, day = int(line1[18:20]), float(line1[20:32])
    except ValueError as exc:
        raise ValueError("invalid TLE numeric field") from exc
    if sat1 != sat2:
        raise ValueError("NORAD identifiers differ between element lines")
    year = 1900 + yy if yy >= 57 else 2000 + yy
    epoch = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1.0)
    canonical = f"{line1}\n{line2}\n".encode()
    return TLE(name, line1, line2, sat1, epoch, TLEProvenance(source, retrieved_at), hashlib.sha256(canonical).hexdigest())
