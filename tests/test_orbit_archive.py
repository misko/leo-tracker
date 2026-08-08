import json
from datetime import datetime, timedelta, timezone

from leo_tracker.orbit.archive import archive_catalog
from leo_tracker.orbit.artifacts import TLECatalogArtifact


CATALOG = b"""VANGUARD 1
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 331.5174 1849677 331.7664  19.3264 10.82419157413661
"""


def test_archive_deduplicates_content_but_labels_each_retrieval(tmp_path):
    first_time = datetime(2000, 6, 27, tzinfo=timezone.utc)
    first = TLECatalogArtifact.create("fixture", first_time, CATALOG)
    second = TLECatalogArtifact.create("fixture", first_time+timedelta(hours=6), CATALOG)
    one = archive_catalog(first, tmp_path, label="starlink")
    two = archive_catalog(second, tmp_path, label="starlink")
    assert one["catalog_sha256"] == two["catalog_sha256"]
    assert len(list((tmp_path/"objects").glob("*.json"))) == 1
    assert len(list((tmp_path/"snapshots").glob("*.json"))) == 2
    index = json.loads((tmp_path/"index.json").read_text())
    assert len(index["snapshots"]) == 2
    assert index["snapshots"][0]["satellite_count"] == 1
    assert json.loads((tmp_path/"latest.json").read_text())["retrieved_at"] == two["retrieved_at"]


def test_space_track_authenticates_queries_once_and_always_logs_out(tmp_path, monkeypatch):
    """Space-Track bans for abuse, so one login, one query, and always a logout.

    Celestrak already blocks this host for hammering; the same mistake against
    the authoritative source would cost far more.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def _reply(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "chocolatechip=session; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            seen.append(f"POST {self.path} {body}")
            self._reply(b"")

        def do_GET(self):
            seen.append(f"GET {self.path} cookie={self.headers.get('Cookie')}")
            self._reply(CATALOG if "query" in self.path else b"")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from leo_tracker.orbit.cli import _space_track_bytes
        url = f"http://127.0.0.1:{server.server_port}/basicspacedata/query/class/gp"
        content = _space_track_bytes(url, "pilot@example.test", "hunter2")
    finally:
        server.shutdown(); thread.join(timeout=5)

    assert content == CATALOG
    assert seen[0].startswith("POST /ajaxauth/login")
    assert "identity=pilot%40example.test" in seen[0] and "hunter2" in seen[0]
    # The query must carry the session, and the session must be released.
    assert "GET /basicspacedata/query" in seen[1] and "chocolatechip" in seen[1]
    assert seen[-1].startswith("GET /ajaxauth/logout")
    assert sum(1 for s in seen if "basicspacedata" in s) == 1


def test_space_track_url_requires_credentials_in_the_environment(monkeypatch):
    import pytest
    from leo_tracker.orbit.cli import fetch_bytes
    monkeypatch.delenv("LEO_SPACETRACK_IDENTITY", raising=False)
    monkeypatch.delenv("LEO_SPACETRACK_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="LEO_SPACETRACK_IDENTITY"):
        fetch_bytes("https://www.space-track.org/basicspacedata/query/class/gp")
