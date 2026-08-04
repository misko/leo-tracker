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
