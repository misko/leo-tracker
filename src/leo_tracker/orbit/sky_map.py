"""Reproducible sky map and daily census for qualified radio observations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
import numpy as np

from .artifacts import TLECatalogArtifact, parse_catalog, parse_utc
from .propagation import propagate_ecef
from .topocentric import Observer, look_angle

CAPTURE_TIME = re.compile(r"(\d{8}T\d{6}Z)")


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _capture_time(name: str) -> datetime | None:
    match = CAPTURE_TIME.search(name)
    return (datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc) if match else None)


def collect_confirmed_beacons(root: Path, *, workers: int = 16) -> list[datetime]:
    """Return one UTC epoch for each temporally confirmed capture window."""
    paths = list((Path(root) / "reports" / "followups").glob("*.json"))

    def inspect(path: Path) -> datetime | None:
        value = _json(path)
        if not value.get("confirmation", {}).get("confirmed"):
            return None
        return _capture_time(path.stem)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        return [value for value in executor.map(inspect, paths) if value is not None]


def collect_qualified_associations(root: Path, *, workers: int = 16) -> list[dict]:
    """Collect production-qualified track identities from regular associations."""
    root = Path(root).resolve()
    paths = list((root / "reports" / "associations").glob("*.json"))

    def inspect(path: Path) -> list[dict]:
        value = _json(path)
        observation_path = Path(value.get("source_observations", ""))
        if not observation_path.is_file():
            observation_path = root / "reports" / "tracks" / path.name
        tracks = {item.get("track_id"): item for item in _json(
            observation_path).get("tracks", [])}
        result = []
        for association in value.get("associations", []):
            candidates = association.get("candidates", [])
            if not association.get("qualified") or not candidates:
                continue
            observations = tracks.get(association.get("track_id"), {}).get(
                "observations", [])
            times = [parse_utc(item["utc"]) for item in observations
                     if item.get("utc") and item.get("lock_valid", True)]
            if not times:
                continue
            best = candidates[0]
            runner_up = candidates[1] if len(candidates) > 1 else None
            best_rms = float(best.get("holdout_residual_rms_hz", float("nan")))
            second_rms = (float(runner_up.get("holdout_residual_rms_hz", float("nan")))
                          if runner_up else float("inf"))
            result.append({
                "capture": path.stem, "track_id": association.get("track_id"),
                "name": best.get("name"), "norad_id": int(best["norad_id"]),
                "tle_sha256": best.get("tle_sha256"), "times": times,
                "observer": value.get("observer", {}), "catalog": value.get("catalog", {}),
                "holdout_rms_hz": best_rms,
                "separation_ratio": second_rms / best_rms if best_rms > 0 else float("inf"),
            })
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        return [item for group in executor.map(inspect, paths) for item in group]


def summarize_daily(beacons: list[datetime], associations: list[dict], *,
                    timezone_name: str = "America/Los_Angeles") -> list[dict]:
    """Count confirmed windows, qualified tracks and unique satellite identities by day."""
    zone = ZoneInfo(timezone_name)
    days: dict[str, dict] = {}

    def row(moment: datetime) -> dict:
        day = moment.astimezone(zone).date().isoformat()
        return days.setdefault(day, {"date": day, "confirmed_beacon_windows": 0,
            "qualified_satellite_tracks": 0, "unique_norad_ids": set()})

    for moment in beacons:
        row(moment)["confirmed_beacon_windows"] += 1
    for item in associations:
        target = row(min(item["times"]))
        target["qualified_satellite_tracks"] += 1
        target["unique_norad_ids"].add(int(item["norad_id"]))
    return [{**{key: value for key, value in item.items()
                if key != "unique_norad_ids"},
             "unique_satellites": len(item["unique_norad_ids"]),
             "norad_ids": sorted(item["unique_norad_ids"])}
            for _, item in sorted(days.items())]


def render_sky_map(root: Path, output: Path, *, latest_output: Path | None = None,
                   daily_output: Path | None = None,
                   timezone_name: str = "America/Los_Angeles",
                   high_separation_ratio: float = 20.0, workers: int = 16) -> dict:
    """Render all qualified TLE-associated streaks and publish the daily census."""
    root, output = Path(root).resolve(), Path(output)
    associations = collect_qualified_associations(root, workers=workers)
    beacons = collect_confirmed_beacons(root, workers=workers)
    if not associations:
        raise ValueError("no qualified satellite associations found")
    catalog_cache = {}
    plotted = []
    for item in associations:
        catalog_value = item["catalog"]
        catalog_path = Path(catalog_value.get("resolved_object_path") or
                            catalog_value.get("path", ""))
        try:
            if catalog_path not in catalog_cache:
                artifact = TLECatalogArtifact.read(catalog_path)
                catalog_cache[catalog_path] = parse_catalog(
                    artifact.content, source=artifact.source_url,
                    retrieved_at=artifact.retrieved_at)
            tle = next(value for value in catalog_cache[catalog_path]
                       if value.norad_id == item["norad_id"] and
                       (not item["tle_sha256"] or value.sha256 == item["tle_sha256"]))
            observer = Observer(float(item["observer"]["latitude_deg"]),
                                float(item["observer"]["longitude_deg"]),
                                float(item["observer"].get("altitude_m", 0)))
            looks = [look_angle(observer, propagate_ecef(tle, moment))
                     for moment in item["times"]]
        except (OSError, ValueError, KeyError, StopIteration):
            continue
        plotted.append({**item, "azimuth_deg": [x.azimuth_deg for x in looks],
                        "elevation_deg": [x.elevation_deg for x in looks]})
    if not plotted:
        raise ValueError("qualified associations could not be propagated")

    all_times = [moment for item in plotted for moment in item["times"]]
    first, last = min(all_times), max(all_times)
    norm = Normalize(first.timestamp(), max(first.timestamp() + 1, last.timestamp()))
    cmap = plt.get_cmap("turbo")
    plt.style.use("dark_background")
    fig = plt.figure(figsize=(13, 11), constrained_layout=True)
    axis = fig.add_subplot(111, projection="polar")
    axis.set_theta_zero_location("N"); axis.set_theta_direction(-1)
    axis.set_ylim(0, 90); axis.set_yticks([15, 30, 45, 60, 75, 90])
    axis.set_yticklabels(["75°", "60°", "45°", "30°", "15°", "Horizon"])
    axis.grid(color="#355064", alpha=.55)
    for item in sorted(plotted, key=lambda value: min(value["times"])):
        color = cmap(norm(min(item["times"]).timestamp()))
        theta = np.radians(item["azimuth_deg"]); radius = 90-np.asarray(item["elevation_deg"])
        strong = item["separation_ratio"] >= high_separation_ratio
        axis.plot(theta, radius, color=color, linewidth=2.4 if strong else .8,
                  alpha=.9 if strong else .45)
        axis.scatter(theta[:1], radius[:1], s=20, color=color, edgecolor="white", linewidth=.3)
        axis.scatter(theta[-1:], radius[-1:], s=28, color=color, marker=">",
                     edgecolor="white", linewidth=.3)
        if strong:
            axis.annotate(str(item["name"]).replace("STARLINK-", "SL-"),
                          (theta[len(theta)//2], radius[len(radius)//2]), fontsize=5,
                          color="#d8e5ed", alpha=.8)
    high = sum(item["separation_ratio"] >= high_separation_ratio for item in plotted)
    zone = ZoneInfo(timezone_name); through = last.astimezone(zone)
    axis.set_title(f"Qualified Starlink Sky Tracks — through {through:%Y-%m-%d %H:%M %Z}\n"
                   f"{len(plotted)} qualified observations · {high} at ≥{high_separation_ratio:g}:1 separation · start ○  end ▷",
                   pad=22, fontsize=15)
    observer = plotted[0]["observer"]
    fig.text(.5, .015, f"Observer: {float(observer['latitude_deg']):.6f}° N, "
             f"{abs(float(observer['longitude_deg'])):.6f}° W · radius is zenith angle · "
             "thin tracks are lower-separation matches", ha="center", fontsize=9,
             color="#a9bfcc")
    scalar = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    bar = fig.colorbar(scalar, ax=axis, shrink=.62, pad=.1)
    ticks = np.linspace(first.timestamp(), last.timestamp(), 6)
    bar.set_ticks(ticks)
    bar.set_ticklabels([datetime.fromtimestamp(value, zone).strftime("%b %d\n%H:%M")
                        for value in ticks])
    bar.set_label(f"Observation time ({through.tzname()})")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=170, facecolor="#07131c"); plt.close(fig)
    if latest_output is not None:
        Path(latest_output).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output, latest_output)
    daily = summarize_daily(beacons, associations, timezone_name=timezone_name)
    report = {"schema": "leo-tracker.sky-observation-summary/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "definitions": {"confirmed_beacon_windows": "capture windows with temporally confirmed follow-up",
            "qualified_satellite_tracks": "production-qualified held-out TLE associations",
            "unique_satellites": "distinct NORAD identities among qualified tracks"},
        "qualified_associations_found": len(associations), "tracks_plotted": len(plotted),
        "high_separation_tracks": high, "daily": daily, "plot": str(output)}
    if daily_output is not None:
        destination = Path(daily_output); destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".next")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        temporary.replace(destination)
    return report
