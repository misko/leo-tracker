"""Independent command line interface for frozen orbit predictions."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys

import numpy as np
from sgp4.api import Satrec, SatrecArray, jday

from .artifacts import PASSES_SCHEMA, TLECatalogArtifact, parse_catalog, parse_utc, utc_iso
from .archive import archive_catalog
from .association import associate_tracks
from .catalog_sources import _space_track_bytes, fetch_bytes
from .catalog_store import CatalogStore
from .catalog_watch import load_config, run_watch, store_status
from .source_comparison import compare_source_associations
from .doppler import predicted_doppler_hz
from .fragment_diagnostics import diagnose_fragments
from .topocentric import Observer
from leo_tracker.passes import predict_passes, sample_track

DEFAULT_STARLINK_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=tle"


def _visibility_candidates(tles, observer: Observer, start: datetime, end: datetime,
                           horizon_deg: float, *, coarse_seconds: float = 120.0,
                           limit: int | None = 256) -> list:
    """Vectorized coarse screen before precise scalar pass boundary solving."""
    count = max(2, math.ceil((end - start).total_seconds() / coarse_seconds) + 1)
    offsets = np.linspace(0.0, (end - start).total_seconds(), count)
    timestamps = np.array([start.timestamp() + offset for offset in offsets])
    moments = [datetime.fromtimestamp(value, timezone.utc) for value in timestamps]
    jd_parts = [jday(t.year, t.month, t.day, t.hour, t.minute,
                     t.second + t.microsecond / 1e6) for t in moments]
    jd = np.array([item[0] for item in jd_parts])
    fraction = np.array([item[1] for item in jd_parts])
    observer_ecef = np.asarray(observer.ecef_km())
    lat, lon = np.radians([observer.latitude_deg, observer.longitude_deg])
    up_vector = np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])
    centuries = (jd + fraction - 2451545.0) / 36525.0
    gmst_deg = (280.46061837 + 360.98564736629 * (jd + fraction - 2451545.0)
                + 0.000387933 * centuries**2 - centuries**3 / 38710000.0) % 360.0
    theta = np.radians(gmst_deg)

    candidates = []
    # Bound memory while still using the accelerated SGP4 array implementation.
    for first in range(0, len(tles), 512):
        chunk = tles[first:first + 512]
        satellites = SatrecArray([Satrec.twoline2rv(tle.line1, tle.line2) for tle in chunk])
        errors, positions, _ = satellites.sgp4(jd, fraction)
        valid = errors == 0
        c, s = np.cos(theta)[None, :], np.sin(theta)[None, :]
        x = c * positions[:, :, 0] + s * positions[:, :, 1]
        y = -s * positions[:, :, 0] + c * positions[:, :, 1]
        z = positions[:, :, 2]
        delta = np.stack((x - observer_ecef[0], y - observer_ecef[1],
                          z - observer_ecef[2]), axis=-1)
        distance = np.linalg.norm(delta, axis=-1)
        up = delta @ up_vector
        elevation = np.degrees(np.arcsin(np.clip(up / distance, -1.0, 1.0)))
        maximum = np.max(np.where(valid, elevation, -90.0), axis=1)
        candidates.extend((float(peak), tle) for tle, peak in zip(chunk, maximum, strict=True)
                          if peak >= horizon_deg - 1.0)
    candidates.sort(key=lambda item: item[0], reverse=True)
    if limit is not None:
        candidates = candidates[:limit]
    return [tle for _, tle in candidates]


def fetch_catalog(url: str, *, now: datetime | None = None) -> TLECatalogArtifact:
    return TLECatalogArtifact.create(url, now or datetime.now(timezone.utc), fetch_bytes(url))


def _sample_json(sample, carrier_hz: float) -> dict:
    look = sample.look
    return {"time": utc_iso(sample.time), "azimuth_deg": look.azimuth_deg,
            "elevation_deg": look.elevation_deg, "range_km": look.range_km,
            "range_rate_km_s": look.range_rate_km_s,
            "expected_doppler_hz": predicted_doppler_hz(carrier_hz, look.range_rate_km_s)}


def build_predictions(catalog: TLECatalogArtifact, observer: Observer, start: datetime,
                      end: datetime, horizon_deg: float, carrier_hz: float,
                      step_seconds: float, candidate_limit: int | None = 256) -> dict:
    satellites = []
    all_tles = parse_catalog(catalog.content, source=catalog.source_url,
                             retrieved_at=catalog.retrieved_at)
    candidates = _visibility_candidates(all_tles, observer, start, end, horizon_deg,
                                        limit=candidate_limit)
    for tle in candidates:
        passes = predict_passes(tle, observer, start, end, horizon_deg=horizon_deg,
                                step_seconds=step_seconds)
        if passes:
            satellites.append({
                "norad_id": tle.norad_id, "name": tle.name, "tle_epoch": utc_iso(tle.epoch),
                "tle_sha256": tle.sha256,
                "passes": [{"rise": _sample_json(item.rise, carrier_hz),
                            "culmination": _sample_json(item.culmination, carrier_hz),
                            "set": _sample_json(item.set, carrier_hz),
                            "track": [_sample_json(sample, carrier_hz) for sample in sample_track(
                                tle, observer, item.rise.time, item.set.time,
                                step_seconds=step_seconds)]} for item in passes],
            })
    return {
        "schema": PASSES_SCHEMA,
        "generated_at": utc_iso(datetime.now(timezone.utc)),
        "source": {"url": catalog.source_url, "retrieved_at": utc_iso(catalog.retrieved_at),
                   "catalog_sha256": catalog.sha256},
        "observer": {"latitude_deg": observer.latitude_deg, "longitude_deg": observer.longitude_deg,
                     "altitude_m": observer.altitude_m},
        "window": {"start": utc_iso(start), "end": utc_iso(end), "horizon_deg": horizon_deg,
                   "sample_step_seconds": step_seconds,
                   "candidate_limit": candidate_limit,
                   "catalog_satellites": len(all_tles),
                   "screened_satellites": len(candidates)},
        "carrier_hz": carrier_hz,
        "doppler_convention": "received_minus_transmitted; approaching is positive",
        "frames": {"propagation": "TEME", "observer_geometry": "ITRF_APPROX"},
        "satellites": satellites,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="leo-orbit")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="freeze the current CelesTrak Starlink 3LE catalog")
    fetch.add_argument("--url", default=DEFAULT_STARLINK_URL)
    fetch.add_argument("--output", type=Path, required=True)
    archive = commands.add_parser("archive", help="store an immutable catalog for retrospective use")
    source = archive.add_mutually_exclusive_group(required=True)
    source.add_argument("--catalog", type=Path, help="archive an existing validated catalog")
    source.add_argument("--url", help="fetch and archive a new catalog")
    archive.add_argument("--archive-dir", type=Path, required=True)
    archive.add_argument("--label", default="starlink")
    watch = commands.add_parser(
        "catalog-watch", help="run the durable multi-source catalog scheduler")
    watch.add_argument("--config", type=Path, required=True)
    watch.add_argument("--root", type=Path, required=True)
    watch.add_argument("--once", action="store_true",
                       help="process currently due profiles and exit")
    watch.add_argument("--check", action="store_true",
                       help="validate configuration without accessing the network")
    watch.add_argument("--heartbeat-s", type=int, default=900)
    status = commands.add_parser(
        "catalog-status", help="show catalog scheduler state as JSON")
    status.add_argument("--root", type=Path, required=True)
    latest = commands.add_parser(
        "catalog-latest", help="resolve a verified latest catalog from disk")
    latest.add_argument("--root", type=Path, required=True)
    latest.add_argument("--source", required=True)
    latest.add_argument("--scope", required=True)
    latest.add_argument("--maximum-age-s", type=float)
    history = commands.add_parser(
        "catalog-history", help="list immutable catalog history from disk")
    history.add_argument("--root", type=Path, required=True)
    history.add_argument("--source", required=True)
    history.add_argument("--scope", required=True)
    compare = commands.add_parser("compare-sources",
        help="pair two catalog providers on identical tracks and score the difference")
    compare.add_argument("--reports", type=Path, required=True,
                         help="analysis reports root holding associations/<source>/")
    compare.add_argument("--sources", nargs=2, required=True,
                         metavar=("SOURCE", "SOURCE"))
    compare.add_argument("--output", type=Path)
    associate = commands.add_parser("associate",
        help="rank archived TLEs against a continuous 10 Hz Starlink Doppler track")
    associate.add_argument("--observations", type=Path, required=True)
    associate.add_argument("--catalog", type=Path, required=True)
    associate.add_argument("--lat", type=float, required=True)
    associate.add_argument("--lon", type=float, required=True)
    associate.add_argument("--alt-m", type=float, default=0.0)
    associate.add_argument("--horizon-deg", type=float, default=5.0)
    associate.add_argument("--candidate-limit", type=int, default=256)
    associate.add_argument("--minimum-duration-s", type=float, default=20.0)
    associate.add_argument("--minimum-dual-epochs", type=int, default=45)
    associate.add_argument("--minimum-coverage-fraction", type=float, default=.18)
    associate.add_argument("--epoch-search-s", type=float, default=2.5,
        help=("bounded along-track time search; Kassas et al. report small "
              "epoch corrections, and broad searches admit unrelated Starlink arcs"))
    associate.add_argument("--epoch-step-s", type=float, default=.05)
    associate.add_argument("--epoch-coarse-step-s", type=float, default=.5,
        help="coarse TLE epoch grid before fine local refinement")
    associate.add_argument("--prediction-step-s", type=float, default=.1)
    associate.add_argument("--maximum-nuisance-drift-hz-s", type=float, default=200.0,
        help="physical bound preventing LNB drift from absorbing orbital Doppler slope")
    associate.add_argument("--maximum-holdout-rms-hz", type=float, default=500.0)
    associate.add_argument("--minimum-margin-hz", type=float, default=100.0)
    associate.add_argument("--output", type=Path, required=True)
    fragment = commands.add_parser("diagnose-fragments",
        help="compare same-satellite and satellite-switch explanations in shadow mode")
    fragment.add_argument("--tracks", type=Path, required=True)
    fragment.add_argument("--links", type=Path, required=True)
    fragment.add_argument("--joint-association", type=Path, required=True)
    fragment.add_argument("--fragment-association", type=Path, required=True)
    fragment.add_argument("--high-separation-ratio", type=float, default=20.0)
    fragment.add_argument("--maximum-fragment-rms-hz", type=float, default=500.0)
    fragment.add_argument("--maximum-differential-fraction", type=float, default=.15)
    fragment.add_argument("--output", type=Path, required=True)
    sky = commands.add_parser("sky-map",
        help="render qualified satellite tracks and a daily beacon/identity census")
    sky.add_argument("--beacon-root", type=Path, required=True)
    sky.add_argument("--output", type=Path, required=True)
    sky.add_argument("--latest-output", type=Path)
    sky.add_argument("--daily-output", type=Path)
    sky.add_argument("--timezone", default="America/Los_Angeles")
    sky.add_argument("--high-separation-ratio", type=float, default=20.0)
    sky.add_argument("--workers", type=int, default=16)
    for name in ("passes", "schedule"):
        command = commands.add_parser(name, help="predict visible passes" if name == "passes" else "create a recording schedule")
        command.add_argument("--catalog", type=Path, required=True)
        command.add_argument("--lat", type=float, required=True)
        command.add_argument("--lon", type=float, required=True)
        command.add_argument("--alt-m", type=float, default=0.0)
        command.add_argument("--start", type=parse_utc, required=True)
        command.add_argument("--end", type=parse_utc, required=True)
        command.add_argument("--horizon-deg", type=float, default=10.0)
        command.add_argument("--carrier-hz", type=float, required=True)
        command.add_argument("--step-seconds", type=float, default=30.0)
        command.add_argument("--candidate-limit", type=int, default=256,
            help=("precisely solve this many strongest coarse-screen candidates; "
                  "use 0 to solve every satellite that crosses the coarse horizon"))
        if name == "schedule":
            command.add_argument("--padding-seconds", type=float, default=300.0)
        command.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "fetch":
        artifact = fetch_catalog(args.url)
        artifact.write(args.output)
        return 0
    if args.command == "archive":
        artifact = (TLECatalogArtifact.read(args.catalog) if args.catalog else
                    fetch_catalog(args.url))
        result = archive_catalog(artifact, args.archive_dir, label=args.label)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "catalog-watch":
        profiles = load_config(args.config)
        if args.check:
            print(json.dumps({"config": str(args.config), "profiles": [
                {"id": item.id, "source": item.source, "scope": item.scope}
                for item in profiles]}, sort_keys=True))
            return 0
        if args.heartbeat_s <= 0:
            raise ValueError("heartbeat must be positive")
        return run_watch(args.config, args.root, once=args.once,
                         heartbeat_s=args.heartbeat_s)
    if args.command == "catalog-status":
        print(json.dumps(store_status(args.root), sort_keys=True))
        return 0
    if args.command in {"catalog-latest", "catalog-history"}:
        store = CatalogStore.open(args.root)
        snapshots = ([store.latest(source=args.source, scope=args.scope,
                                   maximum_age_s=args.maximum_age_s)]
                     if args.command == "catalog-latest" else
                     store.history(source=args.source, scope=args.scope))
        print(json.dumps([{
            "source": item.source, "scope": item.scope,
            "retrieved_at": utc_iso(item.retrieved_at), "sha256": item.sha256,
            "satellite_count": item.satellite_count,
            "tle_epoch_min": utc_iso(item.epoch_min),
            "tle_epoch_max": utc_iso(item.epoch_max),
            "catalog": str(item.normalized_artifact),
        } for item in snapshots], sort_keys=True))
        return 0
    if args.command == "compare-sources":
        result = compare_source_associations(
            args.reports, sources=tuple(args.sources), output=args.output)
        print(json.dumps({k: v for k, v in result.items()
                          if k != "disagreements"}, sort_keys=True))
        return 0
    if args.command == "associate":
        if args.candidate_limit < 0:
            raise ValueError("candidate limit cannot be negative")
        result = associate_tracks(
            args.observations, args.catalog, args.output,
            observer=Observer(args.lat, args.lon, args.alt_m),
            horizon_deg=args.horizon_deg, candidate_limit=args.candidate_limit,
            minimum_duration_s=args.minimum_duration_s,
            minimum_dual_epochs=args.minimum_dual_epochs,
            minimum_coverage_fraction=args.minimum_coverage_fraction,
            epoch_search_s=args.epoch_search_s, epoch_step_s=args.epoch_step_s,
            epoch_coarse_step_s=args.epoch_coarse_step_s,
            prediction_step_s=args.prediction_step_s,
            maximum_nuisance_drift_hz_s=args.maximum_nuisance_drift_hz_s,
            maximum_holdout_rms_hz=args.maximum_holdout_rms_hz,
            minimum_margin_hz=args.minimum_margin_hz)
        print(json.dumps({"association": str(args.output), **result["summary"]},
                         sort_keys=True))
        return 0
    if args.command == "diagnose-fragments":
        result = diagnose_fragments(args.tracks, args.links,
            args.joint_association, args.fragment_association, args.output,
            high_separation_ratio=args.high_separation_ratio,
            maximum_fragment_rms_hz=args.maximum_fragment_rms_hz,
            maximum_differential_fraction=args.maximum_differential_fraction)
        print(json.dumps({"fragment_diagnostic": str(args.output),
            **result["summary"]}, sort_keys=True))
        return 0
    if args.command == "sky-map":
        from .sky_map import render_sky_map
        result = render_sky_map(args.beacon_root, args.output,
            latest_output=args.latest_output, daily_output=args.daily_output,
            timezone_name=args.timezone,
            high_separation_ratio=args.high_separation_ratio, workers=args.workers)
        print(json.dumps(result, sort_keys=True))
        return 0
    catalog = TLECatalogArtifact.read(args.catalog)
    if args.candidate_limit < 0:
        raise ValueError("candidate limit cannot be negative")
    candidate_limit = None if args.candidate_limit == 0 else args.candidate_limit
    predictions = build_predictions(catalog, Observer(args.lat, args.lon, args.alt_m),
                                    args.start, args.end, args.horizon_deg,
                                    args.carrier_hz, args.step_seconds,
                                    candidate_limit)
    if args.command == "schedule":
        entries = []
        padding = timedelta(seconds=args.padding_seconds)
        for satellite in predictions["satellites"]:
            for predicted_pass in satellite["passes"]:
                entries.append({
                    "norad_id": satellite["norad_id"], "name": satellite["name"],
                    "record_start": utc_iso(parse_utc(predicted_pass["rise"]["time"]) - padding),
                    "record_end": utc_iso(parse_utc(predicted_pass["set"]["time"]) + padding),
                    "culmination": predicted_pass["culmination"],
                })
        predictions["schema"] = "leo-tracker.recording-schedule/v1"
        predictions["padding_seconds"] = args.padding_seconds
        predictions["entries"] = sorted(entries, key=lambda item: item["record_start"])
    args.output.write_text(json.dumps(predictions, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
