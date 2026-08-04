from datetime import datetime, timezone
import hashlib
import json

import pytest

from leo_tracker.orbit import cli
from leo_tracker.orbit.artifacts import TLECatalogArtifact


CATALOG = b"""VANGUARD 1
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 331.5174 1849677 331.7664  19.3264 10.82419157413661
"""


def test_fetch_freezes_validated_content_with_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "fetch_bytes", lambda url: CATALOG)
    output = tmp_path / "starlink.json"
    assert cli.main(["fetch", "--url", "https://example.test/starlink", "--output", str(output)]) == 0
    value = json.loads(output.read_text())
    assert value["schema"] == "leo-tracker.tle-catalog/v1"
    assert value["source_url"] == "https://example.test/starlink"
    assert value["retrieved_at"].endswith("Z")
    assert value["sha256"] == hashlib.sha256(CATALOG).hexdigest()


def test_artifact_detects_tampering():
    artifact = TLECatalogArtifact.create("fixture", datetime(2026, 1, 1, tzinfo=timezone.utc), CATALOG)
    value = artifact.to_dict()
    value["content"] += " "
    with pytest.raises(ValueError, match="SHA-256"):
        TLECatalogArtifact.from_dict(value)


def test_passes_is_offline_and_emits_doppler_contract(tmp_path):
    catalog = tmp_path / "catalog.json"
    TLECatalogArtifact.create("fixture:vanguard", datetime(2000, 6, 27, tzinfo=timezone.utc), CATALOG).write(catalog)
    output = tmp_path / "passes.json"
    assert cli.main(["passes", "--catalog", str(catalog), "--lat", "37.4", "--lon", "-122.1",
                     "--alt-m", "20", "--start", "2000-06-27T00:00:00Z",
                     "--end", "2000-06-28T00:00:00Z", "--horizon-deg", "10",
                     "--carrier-hz", "12000000000", "--step-seconds", "60",
                     "--output", str(output)]) == 0
    value = json.loads(output.read_text())
    assert value["schema"] == "leo-tracker.predicted-passes/v1"
    assert value["source"]["catalog_sha256"]
    assert value["frames"] == {"observer_geometry": "ITRF_APPROX", "propagation": "TEME"}
    sample = value["satellites"][0]["passes"][0]["culmination"]
    assert set(("time", "azimuth_deg", "elevation_deg", "range_km", "range_rate_km_s", "expected_doppler_hz")) <= sample.keys()
    assert sample["time"].endswith("Z")
    track = value["satellites"][0]["passes"][0]["track"]
    assert len(track) >= 2
    assert track[0]["time"] == value["satellites"][0]["passes"][0]["rise"]["time"]
    assert track[-1]["time"] == value["satellites"][0]["passes"][0]["set"]["time"]


def test_zero_candidate_limit_means_complete_coarse_screen(tmp_path):
    catalog = tmp_path / "catalog.json"
    output = tmp_path / "passes.json"
    TLECatalogArtifact.create("fixture:vanguard", datetime(2000, 6, 27,
        tzinfo=timezone.utc), CATALOG).write(catalog)
    assert cli.main(["passes", "--catalog", str(catalog), "--lat", "37.4",
        "--lon", "-122.1", "--start", "2000-06-27T00:00:00Z",
        "--end", "2000-06-28T00:00:00Z", "--horizon-deg", "10",
        "--carrier-hz", "12000000000", "--step-seconds", "60",
        "--candidate-limit", "0", "--output", str(output)]) == 0
    value = json.loads(output.read_text())
    assert value["window"]["candidate_limit"] is None
    assert value["window"]["screened_satellites"] == 1


def test_cli_rejects_naive_time(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["passes", "--catalog", str(tmp_path/"x"), "--lat", "0", "--lon", "0",
                  "--start", "2026-01-01T00:00:00", "--end", "2026-01-02T00:00:00Z",
                  "--carrier-hz", "1", "--output", str(tmp_path/"o")])


def test_schedule_adds_sorted_padded_recording_windows(tmp_path):
    catalog, output = tmp_path / "catalog.json", tmp_path / "schedule.json"
    TLECatalogArtifact.create("fixture", datetime(2000, 6, 27, tzinfo=timezone.utc), CATALOG).write(catalog)
    cli.main(["schedule", "--catalog", str(catalog), "--lat", "37.4", "--lon", "-122.1",
              "--start", "2000-06-27T00:00:00Z", "--end", "2000-06-28T00:00:00Z",
              "--carrier-hz", "12000000000", "--padding-seconds", "120", "--output", str(output)])
    value = json.loads(output.read_text())
    assert value["schema"] == "leo-tracker.recording-schedule/v1"
    assert value["padding_seconds"] == 120
    assert value["entries"]
    assert [item["record_start"] for item in value["entries"]] == sorted(item["record_start"] for item in value["entries"])
