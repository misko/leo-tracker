import json
from datetime import datetime, timedelta, timezone

import pytest

from leo_tracker.orbit import CatalogCorrupt, CatalogNotFound, CatalogStale, CatalogStore
from leo_tracker.orbit.artifacts import TLECatalogArtifact
from leo_tracker.orbit.catalog_store import publish_catalog


CATALOG = b"""STARLINK TEST
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 331.5174 1849677 331.7664  19.3264 10.82419157413661
"""


def artifact(moment, source="fixture"):
    return TLECatalogArtifact.create(source, moment, CATALOG)


def test_store_keeps_sources_separate_and_deduplicates_objects(tmp_path):
    first = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
    publish_catalog(tmp_path, artifact(first), source="space-track", scope="starlink")
    publish_catalog(tmp_path, artifact(first + timedelta(hours=1)),
                    source="space-track", scope="starlink")
    publish_catalog(tmp_path, artifact(first + timedelta(hours=2), "mirror"),
                    source="huggingface", scope="starlink")

    store = CatalogStore.open(tmp_path)
    assert len(store.history(source="space-track", scope="starlink")) == 2
    assert len(store.history(source="huggingface", scope="starlink")) == 1
    assert store.latest(source="space-track", scope="starlink").retrieved_at == first + timedelta(hours=1)
    assert store.latest(source="space-track", scope="starlink").tles[0].provenance.retrieved_at == first + timedelta(hours=1)
    assert store.latest(source="huggingface", scope="starlink").tles[0].provenance.source == "mirror"
    assert len(list((tmp_path / "raw" / "space-track").glob("*.tle"))) == 1
    assert len(list((tmp_path / "objects").glob("*.json"))) == 1


def test_temporal_queries_never_leak_future_information(tmp_path):
    first = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
    for offset in (0, 2, 4):
        publish_catalog(tmp_path, artifact(first + timedelta(hours=offset)),
                        source="space-track", scope="starlink")
    store = CatalogStore.open(tmp_path)
    chosen = store.at_time(first + timedelta(hours=3), source="space-track",
                           scope="starlink", knowledge="available_then")
    assert chosen.retrieved_at == first + timedelta(hours=2)
    assert chosen.by_norad(5).name == "STARLINK TEST"
    assert chosen.by_name("starlink test")[0].norad_id == 5
    assert len(chosen.to_satrecs()) == 1
    assert len(store.satellite_history(5, source="space-track", scope="starlink")) == 3
    with pytest.raises(CatalogNotFound):
        store.at_time(first - timedelta(seconds=1), source="space-track",
                      scope="starlink")


def test_store_enforces_freshness_and_raw_integrity(tmp_path):
    moment = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
    manifest = publish_catalog(tmp_path, artifact(moment), source="space-track",
                               scope="starlink")
    store = CatalogStore.open(tmp_path)
    with pytest.raises(CatalogStale):
        store.latest(source="space-track", scope="starlink", maximum_age_s=10,
                     now=moment + timedelta(seconds=11))
    (tmp_path / manifest["raw_object"]).write_bytes(b"tampered")
    with pytest.raises(CatalogCorrupt, match="raw catalog hash"):
        store.latest(source="space-track", scope="starlink")


def test_store_wraps_corrupt_normalized_object(tmp_path):
    moment = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
    manifest = publish_catalog(tmp_path, artifact(moment), source="space-track",
                               scope="starlink")
    (tmp_path / manifest["normalized_object"]).write_text("not json")
    with pytest.raises(CatalogCorrupt, match="cannot read catalog object"):
        CatalogStore.open(tmp_path).latest(source="space-track", scope="starlink")


def test_store_rejects_unsafe_identifiers(tmp_path):
    with pytest.raises(ValueError, match="unsafe source"):
        CatalogStore.open(tmp_path).latest(source="../secret", scope="starlink")


def test_cli_lists_latest_catalog(tmp_path, capsys):
    from leo_tracker.orbit.cli import main
    moment = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
    publish_catalog(tmp_path, artifact(moment), source="space-track", scope="starlink")
    assert main(["catalog-latest", "--root", str(tmp_path), "--source",
                 "space-track", "--scope", "starlink"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value[0]["satellite_count"] == 1
    assert value[0]["source"] == "space-track"
