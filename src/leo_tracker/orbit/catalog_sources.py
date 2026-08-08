"""Authenticated and public orbit-catalog fetchers."""
from __future__ import annotations

from datetime import datetime, timezone
from http.cookiejar import CookieJar
import os
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from .artifacts import TLECatalogArtifact


USER_AGENT = "leo-tracker/0.1"
SPACE_TRACK_HOST = "space-track.org"
MAXIMUM_CATALOG_BYTES = 64 * 1024 * 1024


def _is_space_track_host(hostname: str) -> bool:
    hostname = hostname.rstrip(".").lower()
    return hostname == SPACE_TRACK_HOST or hostname.endswith("." + SPACE_TRACK_HOST)


def _read_bounded(response, maximum_bytes: int = MAXIMUM_CATALOG_BYTES) -> bytes:
    content = response.read(maximum_bytes + 1)
    if len(content) > maximum_bytes:
        raise ValueError(f"catalog response exceeds {maximum_bytes} bytes")
    return content


def _space_track_bytes(url: str, identity: str, password: str) -> bytes:
    """Authenticate, run exactly one query, then release the session."""
    origin = urlsplit(url)
    base = f"{origin.scheme}://{origin.netloc}"
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    login = Request(f"{base}/ajaxauth/login",
                    data=urlencode({"identity": identity,
                                    "password": password}).encode(),
                    headers={"User-Agent": USER_AGENT})
    with opener.open(login, timeout=60) as response:
        _read_bounded(response, 1024 * 1024)
    try:
        query = Request(url, headers={"User-Agent": USER_AGENT})
        with opener.open(query, timeout=180) as response:
            return _read_bounded(response)
    finally:
        try:
            with opener.open(Request(f"{base}/ajaxauth/logout",
                                     headers={"User-Agent": USER_AGENT}),
                             timeout=30) as response:
                _read_bounded(response, 1024 * 1024)
        except OSError:
            pass


def fetch_bytes(url: str) -> bytes:
    origin = urlsplit(url)
    hostname = origin.hostname or ""
    if _is_space_track_host(hostname):
        if origin.scheme != "https" or hostname.rstrip(".").lower() != "www.space-track.org":
            raise RuntimeError("Space-Track queries require exact HTTPS host www.space-track.org")
        identity = os.environ.get("LEO_SPACETRACK_IDENTITY", "")
        password = os.environ.get("LEO_SPACETRACK_PASSWORD", "")
        if not identity or not password:
            raise RuntimeError(
                "space-track needs LEO_SPACETRACK_IDENTITY and "
                "LEO_SPACETRACK_PASSWORD in the environment")
        return _space_track_bytes(url, identity, password)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return _read_bounded(response)


def fetch_catalog(url: str, *, now: datetime | None = None) -> tuple[TLECatalogArtifact, bytes]:
    raw = fetch_bytes(url)
    artifact = TLECatalogArtifact.create(
        url, now or datetime.now(timezone.utc), raw)
    return artifact, raw
