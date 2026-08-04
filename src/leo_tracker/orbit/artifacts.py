"""Versioned, JSON-serializable artifacts for the independent orbit pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .tle import TLE, parse_tle

CATALOG_SCHEMA = "leo-tracker.tle-catalog/v1"
PASSES_SCHEMA = "leo-tracker.predicted-passes/v1"


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time must include a UTC offset")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class TLECatalogArtifact:
    source_url: str
    retrieved_at: datetime
    content: str
    sha256: str

    @classmethod
    def create(cls, source_url: str, retrieved_at: datetime, content: bytes) -> "TLECatalogArtifact":
        text = content.decode("utf-8")
        # Parse eagerly: never freeze a corrupt response or HTML error page.
        parse_catalog(text, source=source_url, retrieved_at=retrieved_at)
        return cls(source_url, retrieved_at, text, hashlib.sha256(content).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return {"schema": CATALOG_SCHEMA, "source_url": self.source_url,
                "retrieved_at": utc_iso(self.retrieved_at), "sha256": self.sha256,
                "content_encoding": "utf-8", "content": self.content}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TLECatalogArtifact":
        if value.get("schema") != CATALOG_SCHEMA:
            raise ValueError("unsupported TLE catalog schema")
        artifact = cls(value["source_url"], parse_utc(value["retrieved_at"]), value["content"], value["sha256"])
        if hashlib.sha256(artifact.content.encode()).hexdigest() != artifact.sha256:
            raise ValueError("TLE catalog SHA-256 mismatch")
        parse_catalog(artifact.content, source=artifact.source_url, retrieved_at=artifact.retrieved_at)
        return artifact

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "TLECatalogArtifact":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def parse_catalog(text: str, *, source: str, retrieved_at: datetime) -> list[TLE]:
    rows = [row.rstrip("\r") for row in text.splitlines() if row.strip()]
    result: list[TLE] = []
    index = 0
    while index < len(rows):
        width = 2 if rows[index].startswith("1 ") else 3
        if index + width > len(rows):
            raise ValueError("incomplete TLE catalog entry")
        result.append(parse_tle("\n".join(rows[index:index+width]), source=source,
                                retrieved_at=retrieved_at))
        index += width
    if not result:
        raise ValueError("TLE catalog is empty")
    return result
