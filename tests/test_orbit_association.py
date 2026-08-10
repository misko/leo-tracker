from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest

from leo_tracker.orbit import Observer, look_angle, parse_tle, propagate_ecef
from leo_tracker.orbit.artifacts import TLECatalogArtifact
from leo_tracker.orbit.association import (
    ASSOCIATION_SCHEMA, _groupwise_temporal_split, _observation_rows,
    _ranking_gate, associate_tracks,
    catalog_doppler, rank_doppler_track)
from leo_tracker.orbit.doppler import predicted_doppler_hz


VANGUARD = """VANGUARD 1
1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753
2 00005  34.2682 331.5174 1849677 331.7664  19.3264 10.82419157413661"""


def test_association_uses_calibrated_consensus_once_per_dual_epoch():
    track = {"observations": [{"utc": "2026-08-06T08:00:00Z",
        "receivers": [
            {"receiver": 0, "valid": True, "frequency_offset_hz": 10_000,
             "formal_sigma_hz": 50},
            {"receiver": 1, "valid": True, "frequency_offset_hz": -90_000,
             "formal_sigma_hz": 50}],
        "consensus": {"valid": True, "receiver_referenced_cfo_hz": 10_123,
                      "frequency_sigma_hz": 275}}]}

    rows = _observation_rows(track)

    assert rows["times"].size == 1
    assert rows["receiver"].tolist() == [0]
    assert rows["observed"].tolist() == [10_123]
    assert rows["sigma"].tolist() == [275]
    assert rows["common_drift"] is True


def test_consensus_channel_groups_fit_static_offsets_and_one_common_drift():
    tle = parse_tle(VANGUARD)
    grid = np.arange(-1, 41, .05)
    truth = 24_000 * np.sin((grid + 2) / 24)
    decoy = 24_000 * np.sin((grid + 8) / 34)
    times = np.arange(0, 40, .1)
    groups = np.where(times < 20, 1, 4)
    observed = np.interp(times + .25, grid, truth)
    observed += np.where(groups == 1, 75_000, -55_000) + 7 * (times - 20)
    rows = {"times": times, "receiver": groups, "observed": observed,
            "sigma": np.full(times.size, 10.0), "common_drift": True}
    other = replace(tle, name="COMMON DRIFT DECOY", norad_id=9, sha256="9" * 64)

    ranked = rank_doppler_track(rows, [tle, other], grid,
        np.vstack((truth, decoy)), epoch_search_s=.5, epoch_step_s=.05)

    assert ranked[0]["norad_id"] == tle.norad_id
    assert ranked[0]["epoch_adjustment_s"] == pytest.approx(.25, abs=.051)
    assert ranked[0]["nuisance_model"] == "per_group_offset_common_drift"
    assert [item["frequency_drift_hz_s"] for item in
            ranked[0]["receiver_nuisance"]] == pytest.approx([7.0, 7.0], abs=1e-6)


def test_groupwise_holdout_represents_nonoverlapping_retuned_channels():
    times = np.asarray([0, .1, .2, .3, 20, 20.1, 20.2, 20.3])
    groups = np.asarray([3, 3, 3, 3, 4, 4, 4, 4])

    train, holdout = _groupwise_temporal_split(times, groups, .6)

    assert set(groups[train]) == {3, 4}
    assert set(groups[holdout]) == {3, 4}
    assert np.max(times[train & (groups == 3)]) < np.min(
        times[holdout & (groups == 3)])
    assert np.max(times[train & (groups == 4)]) < np.min(
        times[holdout & (groups == 4)])


def test_nonoverlapping_channel_groups_rank_without_unfitted_offset():
    tle = parse_tle(VANGUARD)
    grid = np.arange(-1, 24, .05)
    truth = 20_000 * np.sin((grid + 2) / 19)
    decoy = 20_000 * np.sin((grid + 7) / 27)
    times = np.r_[np.arange(0, 1, .1), np.arange(20, 22, .1)]
    groups = np.where(times < 10, 3, 4)
    observed = np.interp(times + .2, grid, truth)
    observed += np.where(groups == 3, 80_000, -60_000) + 5 * times
    rows = {"times": times, "receiver": groups, "observed": observed,
            "sigma": np.full(times.size, 10.0), "common_drift": True}
    other = replace(tle, name="NONOVERLAP DECOY", norad_id=10, sha256="a" * 64)

    ranked = rank_doppler_track(rows, [tle, other], grid,
        np.vstack((truth, decoy)), epoch_search_s=.5, epoch_step_s=.05)

    assert ranked[0]["norad_id"] == tle.norad_id
    assert ranked[0]["epoch_adjustment_s"] == pytest.approx(.2, abs=.051)
    assert ranked[0]["holdout_residual_rms_hz"] < 1


def test_ranking_gate_requires_residual_margin_and_interior_epoch():
    rows = [{"norad_id": 1, "name": "one", "holdout_residual_rms_hz": 300,
             "epoch_adjustment_s": 1, "epoch_at_search_boundary": False},
            {"norad_id": 2, "name": "two", "holdout_residual_rms_hz": 550,
             "epoch_adjustment_s": 0, "epoch_at_search_boundary": False}]
    assert _ranking_gate(rows, maximum_holdout_rms_hz=500,
                         minimum_margin_hz=100)["passed"]
    rows[1]["holdout_residual_rms_hz"] = 350
    assert not _ranking_gate(rows, maximum_holdout_rms_hz=500,
                             minimum_margin_hz=100)["passed"]
    rows[1]["holdout_residual_rms_hz"] = 550
    rows[0]["epoch_at_search_boundary"] = True
    assert not _ranking_gate(rows, maximum_holdout_rms_hz=500,
                             minimum_margin_hz=100)["passed"]


def test_default_nuisance_drift_bound_does_not_absorb_large_orbital_slope():
    import inspect

    rank_default = inspect.signature(rank_doppler_track).parameters[
        "maximum_nuisance_drift_hz_s"].default
    associate_default = inspect.signature(associate_tracks).parameters[
        "maximum_nuisance_drift_hz_s"].default
    epoch_default = inspect.signature(associate_tracks).parameters[
        "epoch_search_s"].default

    assert rank_default == 200.0
    assert associate_default == 200.0
    assert epoch_default == 2.5


def test_vectorized_catalog_doppler_matches_scalar_orbit_geometry():
    tle = parse_tle(VANGUARD)
    observer = Observer(37.4, -122.1, 20)
    moments = [datetime(2000, 6, 27, 19, minute, tzinfo=timezone.utc)
               for minute in (0, 1, 2)]
    times = np.asarray([item.timestamp() for item in moments])
    carrier = 11_459_687_500.0
    doppler, elevation, errors = catalog_doppler([tle], observer, times, carrier)

    expected = [predicted_doppler_hz(
        carrier, look_angle(observer, propagate_ecef(tle, moment)).range_rate_km_s)
        for moment in moments]
    expected_elevation = [look_angle(
        observer, propagate_ecef(tle, moment)).elevation_deg for moment in moments]
    assert errors.tolist() == [[0, 0, 0]]
    assert doppler[0] == pytest.approx(expected, abs=1e-6)
    assert elevation[0] == pytest.approx(expected_elevation, abs=1e-8)
    per_epoch_carrier = carrier * np.asarray([.9, 1.0, 1.1])
    variable, _, _ = catalog_doppler([tle], observer, times, per_epoch_carrier)
    assert variable[0] == pytest.approx(
        np.asarray(expected) * per_epoch_carrier / carrier, abs=1e-6)


def test_long_track_ranking_recovers_curve_epoch_and_receiver_nuisance():
    tle = parse_tle(VANGUARD)
    decoy = replace(tle, name="CURVATURE DECOY", norad_id=6, sha256="d" * 64)
    grid = np.arange(-3.0, 64.0, .05)
    true_curve = 30_000 * np.sin((grid + 4) / 38) + 80 * grid
    decoy_curve = 30_000 * np.sin((grid + 9) / 48) + 80 * grid
    sample_times = np.arange(0.0, 60.0, .1)
    receivers = np.tile((0, 1), sample_times.size)
    times = np.repeat(sample_times, 2)
    geometric = np.interp(times + .35, grid, true_curve)
    offsets = np.where(receivers == 0, 120_000.0, -85_000.0)
    drifts = np.where(receivers == 0, 12.0, -7.0)
    rng = np.random.default_rng(22)
    observed = geometric + offsets + drifts * (times - 30) + rng.normal(0, 8, times.size)
    rows = {"times": times, "receiver": receivers, "observed": observed,
            "sigma": np.full(times.size, 10.0)}

    ranked = rank_doppler_track(
        rows, [tle, decoy], grid, np.vstack((true_curve, decoy_curve)),
        epoch_search_s=1, epoch_step_s=.05)

    assert ranked[0]["norad_id"] == tle.norad_id
    assert ranked[0]["epoch_adjustment_s"] == pytest.approx(.35, abs=.051)
    assert ranked[0]["holdout_residual_rms_hz"] < 20
    assert ranked[1]["holdout_residual_rms_hz"] > 5 * ranked[0]["holdout_residual_rms_hz"]
    nuisance = {item["receiver"]: item for item in ranked[0]["receiver_nuisance"]}
    assert nuisance[0]["frequency_drift_hz_s"] == pytest.approx(12, abs=1)
    assert nuisance[1]["frequency_drift_hz_s"] == pytest.approx(-7, abs=1)


def test_coarse_to_fine_epoch_search_recovers_large_interior_tle_time_error():
    tle = parse_tle(VANGUARD)
    decoy = replace(tle, name="WIDE SEARCH DECOY", norad_id=8, sha256="f" * 64)
    grid = np.arange(-65.0, 126.0, .05)
    truth = 45_000 * np.sin((grid + 3) / 31) + 20 * grid
    other = 45_000 * np.sin((grid - 15) / 44) + 20 * grid
    times = np.arange(0.0, 60.0, .1)
    observed = np.interp(times + 37.25, grid, truth) + 80_000 + 7 * times
    rows = {"times": times, "receiver": np.zeros(times.size, int),
            "observed": observed, "sigma": np.full(times.size, 10.0)}

    ranked = rank_doppler_track(rows, [tle, decoy], grid,
        np.vstack((truth, other)), epoch_search_s=60, epoch_step_s=.05,
        epoch_coarse_step_s=.5)

    assert ranked[0]["norad_id"] == tle.norad_id
    assert ranked[0]["epoch_adjustment_s"] == pytest.approx(37.25, abs=.051)
    assert not ranked[0]["epoch_at_search_boundary"]
    assert ranked[0]["holdout_residual_rms_hz"] < 1


def test_ranking_scales_fractional_prediction_by_each_observed_channel_carrier():
    tle = parse_tle(VANGUARD)
    grid = np.arange(-1, 31, .05)
    fractional = 3e-6 * np.sin(grid / 11)
    decoy = 3e-6 * np.sin((grid + 4) / 15)
    times = np.arange(0, 30, .1)
    carriers = np.where(times < 15, 10_709_687_500.0, 11_459_687_500.0)
    groups = np.where(times < 15, 1, 4)
    observed = np.interp(times + .2, grid, fractional) * carriers
    observed += np.where(groups == 1, 80_000 + 4 * times, -60_000 - 3 * times)
    rows = {"times": times, "receiver": groups, "observed": observed,
            "sigma": np.full(times.size, 10.0), "carrier": carriers}
    other = replace(tle, name="CHANNEL DECOY", norad_id=7, sha256="e" * 64)

    ranked = rank_doppler_track(rows, [tle, other], grid,
        np.vstack((fractional, decoy)), epoch_search_s=.5, epoch_step_s=.05,
        predicted_is_fractional=True)

    assert ranked[0]["norad_id"] == tle.norad_id
    assert ranked[0]["epoch_adjustment_s"] == pytest.approx(.2, abs=.051)
    assert ranked[0]["holdout_residual_rms_hz"] < 1
    assert ranked[1]["holdout_residual_rms_hz"] > 100


def test_association_records_short_track_as_unqualified_without_overclaiming(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    TLECatalogArtifact.create("fixture", datetime(2000, 6, 27, tzinfo=timezone.utc),
                              (VANGUARD + "\n").encode()).write(catalog_path)
    observations = tmp_path / "tracks.json"
    rows = []
    for index in range(6):
        rows.append({"utc": f"2000-06-27T19:00:0{index}.000000Z",
            "receivers": [
                {"receiver": 0, "valid": True,
                 "frequency_offset_hz": 1_000 + index, "formal_sigma_hz": 100},
                {"receiver": 1, "valid": True,
                 "frequency_offset_hz": -2_000 + index, "formal_sigma_hz": 100}]})
    observations.write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "signal": {"nominal_rf_hz": 11_459_687_500},
        "tracks": [{"track_id": "track-000", "observations": rows}]}))
    output = tmp_path / "association.json"

    report = associate_tracks(observations, catalog_path, output,
                              observer=Observer(37.4, -122.1, 20),
                              minimum_duration_s=20)

    assert report["schema"] == ASSOCIATION_SCHEMA
    assert report["summary"]["qualified_association_count"] == 0
    assert "below" in report["associations"][0]["reason"]
    assert json.loads(output.read_text())["schema"] == ASSOCIATION_SCHEMA


def test_association_rejects_single_receiver_track_before_tle_ranking(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    TLECatalogArtifact.create("fixture", datetime(2000, 6, 27, tzinfo=timezone.utc),
                              (VANGUARD + "\n").encode()).write(catalog_path)
    observations = tmp_path / "tracks.json"
    observations.write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "signal": {"nominal_rf_hz": 11_459_687_500},
        "tracks": [{"track_id": "track-000", "observations": [
            {"utc": datetime.fromtimestamp(962_133_600 + index, timezone.utc)
                    .isoformat().replace("+00:00", "Z"),
             "receivers": [{"receiver": 0, "valid": True,
                            "frequency_offset_hz": 1_000 + index,
                            "formal_sigma_hz": 100}]}
            for index in range(60)]}]}))

    report = associate_tracks(observations, catalog_path, tmp_path / "association.json",
                              observer=Observer(37.4, -122.1, 20))

    assert not report["associations"][0]["qualified"]
    assert "dual-receiver" in report["associations"][0]["reason"]


def test_association_records_unusable_temporal_holdout_as_unqualified(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    TLECatalogArtifact.create("fixture", datetime(2000, 6, 27, tzinfo=timezone.utc),
                              (VANGUARD + "\n").encode()).write(catalog_path)
    start = datetime(2000, 6, 27, 19, tzinfo=timezone.utc)
    offsets = [index / 10 for index in range(44)] + [20.1]
    observations = []
    for index, offset in enumerate(offsets):
        observations.append({
            "utc": (start + timedelta(seconds=offset)).isoformat(),
            "receivers": [
                {"receiver": 0, "valid": True, "frequency_offset_hz": index,
                 "formal_sigma_hz": 50},
                {"receiver": 1, "valid": True, "frequency_offset_hz": index,
                 "formal_sigma_hz": 50}],
            "consensus": {"valid": True, "receiver_referenced_cfo_hz": index,
                          "frequency_sigma_hz": 50}})
    tracks = tmp_path / "tracks.json"
    tracks.write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "signal": {"nominal_rf_hz": 11_459_687_500.0},
        "configuration": {"output_rate_hz": 10},
        "tracks": [{"track_id": "track-000", "observations": observations}]}))

    report = associate_tracks(tracks, catalog_path, tmp_path / "association.json",
                              observer=Observer(37.8, -122.4), horizon_deg=-90)

    result = report["associations"][0]
    assert not result["qualified"]
    assert result["reason"].startswith("held-out validation unavailable")
    assert result["dual_epoch_count"] == 45


def test_association_rejects_sparse_epochs_across_long_wall_clock_span(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    TLECatalogArtifact.create("fixture", datetime(2000, 6, 27, tzinfo=timezone.utc),
                              (VANGUARD + "\n").encode()).write(catalog_path)
    observations = tmp_path / "tracks.json"
    rows = []
    for index in range(6):
        moment = datetime.fromtimestamp(962_133_600 + 5 * index, timezone.utc)
        rows.append({"utc": moment.isoformat().replace("+00:00", "Z"),
            "receivers": [{"receiver": receiver, "valid": True,
                "frequency_offset_hz": 1_000 * receiver + index,
                "formal_sigma_hz": 100} for receiver in range(2)]})
    observations.write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "signal": {"nominal_rf_hz": 11_459_687_500},
        "configuration": {"output_rate_hz": 10},
        "tracks": [{"track_id": "track-000", "observations": rows}]}))

    report = associate_tracks(observations, catalog_path, tmp_path / "association.json",
                              observer=Observer(37.4, -122.1, 20))

    result = report["associations"][0]
    assert not result["qualified"]
    assert result["dual_epoch_count"] == 6
    assert result["coverage_fraction"] < .03
    assert "measured dual-receiver epochs" in result["reason"]


def test_long_track_artifact_to_sgp4_association_e2e(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    retrieved = datetime(2000, 6, 27, tzinfo=timezone.utc)
    TLECatalogArtifact.create("fixture", retrieved, (VANGUARD + "\n").encode()).write(
        catalog_path)
    tle = parse_tle(VANGUARD)
    observer = Observer(37.4, -122.1, 20)
    carrier = 11_459_687_500.0
    start = datetime(2000, 6, 27, 19, tzinfo=timezone.utc).timestamp()
    relative = np.arange(0, 60, .1)
    times = start + relative
    truth, _, _ = catalog_doppler([tle], observer, times + .3, carrier)
    observations = []
    for index, (time_s, doppler) in enumerate(zip(times, truth[0], strict=True)):
        observations.append({
            "utc": datetime.fromtimestamp(time_s, timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            "receivers": [
                {"receiver": 0, "valid": True,
                 "frequency_offset_hz": float(doppler + 90_000 + 4 * relative[index]),
                 "formal_sigma_hz": 20},
                {"receiver": 1, "valid": True,
                 "frequency_offset_hz": float(doppler - 40_000 - 3 * relative[index]),
                 "formal_sigma_hz": 20}]})
    tracks_path = tmp_path / "tracks.json"
    tracks_path.write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "signal": {"nominal_rf_hz": carrier},
        "tracks": [{"track_id": "track-000", "observations": observations}]}))
    output = tmp_path / "association.json"

    report = associate_tracks(
        tracks_path, catalog_path, output, observer=observer, horizon_deg=-90,
        candidate_limit=8, minimum_duration_s=20, epoch_search_s=1,
        epoch_step_s=.05, maximum_holdout_rms_hz=50)

    result = report["associations"][0]
    assert report["configuration"]["stability_train_fractions"] == [.5, .6, .7]
    assert report["configuration"]["sensitivity_drift_fraction"] == .8
    assert report["configuration"]["maximum_holdout_rms_hz"] == 50
    assert result["qualified"]
    assert result["best_norad_id"] == tle.norad_id
    assert result["best_holdout_residual_rms_hz"] < 1
    assert result["candidates"][0]["epoch_adjustment_s"] == pytest.approx(.3, abs=.051)


def _catalog_store(tmp_path, *, source: str, scope: str = "starlink"):
    """Build the minimal catalog-store layout the association must resolve."""
    from leo_tracker.orbit.catalog_store import SNAPSHOT_SCHEMA as STORE_SCHEMA
    retrieved = datetime(2026, 8, 10, 19, 37, tzinfo=timezone.utc)
    artifact = TLECatalogArtifact.create(
        f"https://{source}.example/query", retrieved, VANGUARD.encode())
    objects = tmp_path / "objects"
    objects.mkdir(exist_ok=True)
    object_path = objects / f"{artifact.sha256}.json"
    artifact.write(object_path)
    manifest = tmp_path / "latest" / source / f"{scope}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "schema": STORE_SCHEMA, "source": source, "scope": scope,
        "retrieved_at": retrieved.isoformat().replace("+00:00", "Z"),
        "catalog_sha256": artifact.sha256,
        "normalized_object": f"objects/{artifact.sha256}.json"}))
    return manifest, object_path, artifact


def test_association_resolves_a_catalog_store_snapshot_and_names_its_source(tmp_path):
    """The store publishes its own snapshot schema, addressed relative to its root.

    Association previously understood only the archive schema, so every
    catalog-store snapshot fell through to the raw-artifact path and failed.
    That left a fully populated Space-Track branch unreadable.
    """
    from leo_tracker.orbit.association import _read_catalog_or_snapshot

    manifest, object_path, artifact = _catalog_store(tmp_path, source="space-track")

    resolved, resolved_path, catalog_manifest = _read_catalog_or_snapshot(manifest)

    assert resolved.sha256 == artifact.sha256
    assert resolved_path.resolve() == object_path.resolve()
    assert catalog_manifest["source"] == "space-track"
    assert catalog_manifest["scope"] == "starlink"


def test_association_resolves_a_store_snapshot_from_the_dated_layout(tmp_path):
    """`snapshots/<source>/<scope>/YYYY/MM/DD/` sits deeper than `latest/`."""
    from leo_tracker.orbit.catalog_store import SNAPSHOT_SCHEMA as STORE_SCHEMA
    from leo_tracker.orbit.association import _read_catalog_or_snapshot

    _, object_path, artifact = _catalog_store(tmp_path, source="huggingface")
    dated = tmp_path / "snapshots/huggingface/starlink/2026/08/10/retrieval.json"
    dated.parent.mkdir(parents=True)
    dated.write_text(json.dumps({
        "schema": STORE_SCHEMA, "source": "huggingface", "scope": "starlink",
        "normalized_object": f"objects/{artifact.sha256}.json"}))

    resolved, resolved_path, catalog_manifest = _read_catalog_or_snapshot(dated)

    assert resolved.sha256 == artifact.sha256
    assert resolved_path.resolve() == object_path.resolve()
    assert catalog_manifest["source"] == "huggingface"


def test_association_still_reads_a_plain_catalog_artifact(tmp_path):
    from leo_tracker.orbit.association import _read_catalog_or_snapshot

    artifact = TLECatalogArtifact.create(
        "https://example.test/tle", datetime(2026, 8, 10, tzinfo=timezone.utc),
        VANGUARD.encode())
    path = tmp_path / "catalog.json"
    artifact.write(path)

    resolved, resolved_path, catalog_manifest = _read_catalog_or_snapshot(path)

    assert resolved.sha256 == artifact.sha256
    assert resolved_path.resolve() == path.resolve()
    # A bare artifact names no provider, and must not invent one.
    assert catalog_manifest == {}


def test_association_still_reads_an_archive_snapshot(tmp_path):
    """The original archive schema addresses its object differently; keep it working."""
    from leo_tracker.orbit.archive import SNAPSHOT_SCHEMA as ARCHIVE_SCHEMA
    from leo_tracker.orbit.association import _read_catalog_or_snapshot

    artifact = TLECatalogArtifact.create(
        "https://example.test/tle", datetime(2026, 8, 10, tzinfo=timezone.utc),
        VANGUARD.encode())
    objects = tmp_path / "objects"; objects.mkdir()
    object_path = objects / f"{artifact.sha256}.json"
    artifact.write(object_path)
    manifest = tmp_path / "latest.json"
    manifest.write_text(json.dumps({
        "schema": ARCHIVE_SCHEMA, "object": f"objects/{artifact.sha256}.json"}))

    resolved, resolved_path, catalog_manifest = _read_catalog_or_snapshot(manifest)

    assert resolved.sha256 == artifact.sha256
    assert resolved_path.resolve() == object_path.resolve()
    assert catalog_manifest["schema"] == ARCHIVE_SCHEMA
