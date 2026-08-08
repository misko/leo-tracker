import json
import fcntl
from datetime import datetime, timedelta, timezone

import pytest

from leo_tracker.orbit.artifacts import TLECatalogArtifact, parse_utc
from leo_tracker.orbit.catalog_sources import _is_space_track_host, fetch_bytes
from leo_tracker.orbit.catalog_store import CatalogStore
from leo_tracker.orbit.catalog_watch import (
    CONFIG_SCHEMA, CatalogProfile, load_config, run_due_cycle, run_watch,
)


CATALOG = b"""STARLINK TEST
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 331.5174 1849677 331.7664  19.3264 10.82419157413661
"""


def profile(**changes):
    values = dict(id="space-track-starlink", source="space-track",
                  scope="starlink", url="https://example.test/tle",
                  interval_s=3600, minimum_request_interval_s=3600,
                  minimum_satellite_count=1, maximum_satellite_count=10)
    values.update(changes)
    return CatalogProfile(**values)


def test_due_cycle_publishes_and_rate_guard_prevents_second_request(tmp_path):
    moment = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
    calls = []

    def fetcher(url, *, now):
        calls.append(url)
        return TLECatalogArtifact.create(url, now, CATALOG), CATALOG

    first = run_due_cycle((profile(),), tmp_path, now=moment, fetcher=fetcher,
                          logger=lambda _: None)
    second = run_due_cycle((profile(),), tmp_path, now=moment + timedelta(minutes=1),
                           fetcher=fetcher, logger=lambda _: None)
    assert first["profiles"][0]["status"] == "published"
    assert second["profiles"][0]["status"] == "waiting"
    assert len(calls) == 1
    assert CatalogStore.open(tmp_path).latest(
        source="space-track", scope="starlink").satellite_count == 1


def test_fixed_minute_schedule_avoids_hour_boundary(tmp_path):
    moment = datetime(2026, 8, 8, 1, 20, tzinfo=timezone.utc)
    result = run_due_cycle((profile(scheduled_minute=37),), tmp_path, now=moment,
                           fetcher=lambda *_args, **_kwargs: pytest.fail("not due"),
                           logger=lambda _: None)
    assert result["profiles"][0]["status"] == "waiting"
    assert parse_utc(result["profiles"][0]["next_due_utc"]) == moment.replace(minute=37)
    persisted = json.loads((tmp_path / "status" / "space-track-starlink.json").read_text())
    assert persisted["next_due_utc"] == result["profiles"][0]["next_due_utc"]


def test_one_source_failure_does_not_block_another(tmp_path):
    moment = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)

    def fetcher(url, *, now):
        if "broken" in url:
            raise OSError("credential-secret-must-not-be-logged")
        return TLECatalogArtifact.create(url, now, CATALOG), CATALOG

    logs = []
    profiles = (profile(url="https://broken.test"),
                profile(id="mirror", source="huggingface",
                        url="https://good.test", minimum_request_interval_s=0))
    result = run_due_cycle(profiles, tmp_path, now=moment, fetcher=fetcher,
                           logger=logs.append)
    assert [item["status"] for item in result["profiles"]] == ["failed", "published"]
    assert "credential-secret" not in "\n".join(logs)


def test_config_validation_and_check_cli(tmp_path, capsys):
    from leo_tracker.orbit.cli import main
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"schema": CONFIG_SCHEMA, "profiles": [
        {"id": "mirror", "source": "huggingface", "scope": "starlink",
         "url": "https://example.test/tle", "interval_s": 21600}
    ]}))
    assert load_config(config)[0].id == "mirror"
    assert main(["catalog-watch", "--config", str(config), "--root",
                 str(tmp_path / "store"), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["profiles"][0]["id"] == "mirror"


def test_config_rejects_path_escape_and_insecure_transport(tmp_path):
    for bad_profile in (
        {"id": "../escape", "source": "mirror", "scope": "starlink",
         "url": "https://example.test/tle", "interval_s": 60},
        {"id": "mirror", "source": "mirror", "scope": "starlink",
         "url": "http://example.test/tle", "interval_s": 60},
    ):
        config = tmp_path / "bad.json"
        config.write_text(json.dumps({"schema": CONFIG_SCHEMA,
                                      "profiles": [bad_profile]}))
        with pytest.raises(ValueError):
            load_config(config)


def test_space_track_hostname_matching_cannot_leak_credentials(monkeypatch):
    assert _is_space_track_host("www.space-track.org")
    assert not _is_space_track_host("evilspace-track.org")
    assert not _is_space_track_host("space-track.org.example.test")
    monkeypatch.delenv("LEO_SPACETRACK_IDENTITY", raising=False)
    monkeypatch.delenv("LEO_SPACETRACK_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="exact HTTPS host"):
        fetch_bytes("http://www.space-track.org/query")


def test_watch_refuses_a_second_daemon(tmp_path):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"schema": CONFIG_SCHEMA, "profiles": [
        {"id": "mirror", "source": "huggingface", "scope": "starlink",
         "url": "https://example.test/tle", "interval_s": 21600}
    ]}))
    lock_path = tmp_path / "store" / "staging" / "watch.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another catalog watcher"):
            run_watch(config, tmp_path / "store", once=True,
                      logger=lambda _: None)


def test_corrupt_rate_state_fails_closed_without_fetching(tmp_path):
    state = tmp_path / "state" / "space-track-starlink.json"
    state.parent.mkdir(parents=True)
    state.write_text("truncated")
    called = False

    def fetcher(*_args, **_kwargs):
        nonlocal called
        called = True

    with pytest.raises(RuntimeError, match="cannot safely read scheduler state"):
        run_due_cycle((profile(),), tmp_path,
                      now=datetime(2026, 8, 8, 1, tzinfo=timezone.utc),
                      fetcher=fetcher, logger=lambda _: None)
    assert not called
