"""Read-only web dashboard for a running Starlink waterfall observation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote, urlparse

import numpy as np

from .physics import doppler_radial_acceleration_m_s2
from .tuning_dither import dither_phase_locked
from .beacon.dashboard_shards import activity_summary, read_listing
from .beacon.fingerprint import render_fingerprint_svg
from .beacon.dashboard_index import (capture_dashboard_record,
                                     capture_radio_parameters,
                                     confirmed_beacon_events)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso_from_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")


#: Rows to read from the day shards.  The table itself renders at most a couple
#: of hundred, but the activity panel aggregates a full day, and both radios
#: together have exceeded 5,000 recordings in 24 h.  Sizing this to the widest
#: activity window rather than to the table keeps the headline counts honest;
#: reading fewer would silently under-report duty and beacon totals.
LISTING_READ_LIMIT = 12_000


def _row_identity(item: dict) -> dict:
    """Radio and LNB identity for a persisted row, wherever it was recorded.

    Older rows carry it only inside the saved radio parameters; newer rows
    promote it to the top level.
    """
    radio = (item.get("_statistics", {}) or {}).get("radio_parameters", {}) or {}
    return {"radio_id": item.get("radio_id") or (
                radio.get("hardware", {}) or {}).get("radio_id"),
            "receiver_labels": item.get("receiver_labels") or (
                radio.get("receivers", {}) or {}).get("receiver_labels")}


def _pass_rows(catalog: dict) -> list[dict]:
    rows = []
    for satellite in catalog.get("satellites", []):
        for item in satellite.get("passes", []):
            rise, culmination, setting = item["rise"], item["culmination"], item["set"]
            rise_time, set_time = _utc(rise["time"]), _utc(setting["time"])
            duration_s = max((set_time - rise_time).total_seconds(), 1e-9)
            rows.append({
                "name": satellite["name"].strip(), "norad_id": satellite["norad_id"],
                "rise_utc": rise_time.isoformat().replace("+00:00", "Z"),
                "culmination_utc": culmination["time"],
                "set_utc": set_time.isoformat().replace("+00:00", "Z"),
                "max_elevation_deg": culmination["elevation_deg"],
                "predicted_drift_hz_s": (
                    setting["expected_doppler_hz"] - rise["expected_doppler_hz"]) / duration_s,
                "rise_range_rate_km_s": rise["range_rate_km_s"],
                "culmination_range_rate_km_s": culmination["range_rate_km_s"],
                "set_range_rate_km_s": setting["range_rate_km_s"],
            })
    return sorted(rows, key=lambda row: row["rise_utc"])


class DashboardModel:
    """Build small JSON snapshots from append-only watcher artifacts."""

    def __init__(self, observation_dir: Path, passes_path: Path | None = None, *,
                 beacon_root: Path | None = None,
                 samples_per_snapshot: int = 262_144,
                 sample_rate_hz: float = 30_720_000,
                 snapshots_per_chunk: int = 4096):
        self.root = Path(observation_dir).resolve()
        self.passes_path = (Path(passes_path).resolve() if passes_path else self.root / "passes.json")
        self.beacon_root = None if beacon_root is None else Path(beacon_root).resolve()
        self.samples_per_snapshot = int(samples_per_snapshot)
        self.sample_rate_hz = float(sample_rate_hz)
        self.snapshots_per_chunk = int(snapshots_per_chunk)
        self._measurement_cache_signature = None
        self._measurement_cache: list[dict] | None = None
        self._measurement_cache_by_path: dict[Path, dict] = {}
        self._measurement_path_signatures: dict[Path, tuple[int, ...]] = {}
        self._measurement_cache_lock = threading.RLock()
        self._catalog_cache_signature: tuple[int, int] | None = None
        self._catalog_cache: tuple[dict, list[dict]] = ({}, [])
        self._beacon_cache_signature: tuple | None = None
        self._beacon_cache: dict | None = None
        self._beacon_cache_lock = threading.RLock()
        self._recording_index_signature: tuple[int, int] | None = None
        self._recording_index_cache: dict = {}
        self._snapshot_cache_lock = threading.Lock()
        self._snapshot_cache_created = 0.0
        self._snapshot_cache: dict | None = None
        # The web page renders at most twelve waterfalls. Keep a small margin
        # for summary calculations; longer-term statistics belong to the
        # periodic review artifacts rather than a cold HTTP request.
        self._dashboard_history_limit = 16
        if self.samples_per_snapshot <= 0 or self.sample_rate_hz <= 0 or self.snapshots_per_chunk <= 0:
            raise ValueError("dashboard sampling parameters must be positive")

    @staticmethod
    def _json(path: Path, default):
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    @staticmethod
    def _newest_existing_paths(paths, *, limit: int | None = None) -> list[Path]:
        """Sort a changing artifact set without failing when cleanup removes a path."""
        scored = []
        for path in paths:
            try:
                scored.append((path.stat().st_mtime_ns, path))
            except (FileNotFoundError, OSError):
                continue
        scored.sort(key=lambda item: item[0], reverse=True)
        ordered = [path for _, path in scored]
        return ordered if limit is None else ordered[:limit]

    @staticmethod
    def _recent_capture_manifests(captures_root: Path, *, limit: int = 20) -> list[Path]:
        """Find timestamp-named captures without an NFS stat call for every directory."""
        try:
            directories = list(captures_root.iterdir())
        except (FileNotFoundError, OSError):
            return []

        def capture_timestamp(path: Path) -> str:
            match = re.search(r"(\d{8}T\d{6}Z)$", path.name)
            return match.group(1) if match else ""

        directories.sort(key=lambda path: (capture_timestamp(path), path.name), reverse=True)
        return [path / "manifest.json" for path in directories[:max(0, limit)]]

    def _plot_url(self, plot_name: str) -> str:
        """Version immutable plot URLs so regenerated diagnostics refresh in browsers."""
        try:
            version = (self.root / "plots" / plot_name).stat().st_mtime_ns
        except OSError:
            version = 0
        return f"/plots/{plot_name}?v={version}"

    def _beacon_recording_index(self) -> dict:
        if self.beacon_root is None:
            return {}
        reports = self.beacon_root / "reports"
        shards = sorted((reports / "dashboard-index").glob("2*.json"))
        path = reports / "dashboard-index.json"
        # Day shards are written by whichever analysis host finishes a
        # recording, so they lead the monolithic index whenever that index is
        # not being rebuilt.  Prefer whichever source is actually newer rather
        # than pinning to one, so neither a stalled compactor nor a stalled
        # index rebuild can silently hide recent recordings.
        try:
            index_stat = path.stat()
            index_mtime, signature = index_stat.st_mtime_ns, (
                index_stat.st_mtime_ns, index_stat.st_size)
        except OSError:
            index_mtime, signature = -1, None
        shard_mtime = max((item.stat().st_mtime_ns for item in shards),
                          default=-1)
        if shard_mtime < 0 and signature is None:
            return {}
        if shard_mtime > index_mtime:
            signature = ("shards", shard_mtime, len(shards))
        with self._beacon_cache_lock:
            if signature == self._recording_index_signature:
                return self._recording_index_cache
            if shard_mtime > index_mtime:
                value = {"schema": "leo-tracker.beacon-dashboard-index/v3",
                         "recordings": read_listing(
                             self.beacon_root, limit=LISTING_READ_LIMIT),
                         "summary": self._json(
                             reports / "dashboard-index" / "summary.json", {})}
            else:
                value = self._json(path, {})
            self._recording_index_signature = signature
            self._recording_index_cache = value
            return value

    def records(self) -> list[dict]:
        legacy = self.root / "index.jsonl"
        if not legacy.is_file():
            # The watch is append-only and can contain thousands of large NPZ
            # files. Rebuilding every record whenever one new analysis lands
            # eventually makes the API slower than the browser timeout. Keep a
            # bounded display history and refresh only new/changed records.
            analysis_paths = sorted((self.root / "analysis").glob("*.json"))[
                -self._dashboard_history_limit:]
            with self._measurement_cache_lock:
                active = set(analysis_paths)
                for stale in set(self._measurement_cache_by_path)-active:
                    self._measurement_cache_by_path.pop(stale, None)
                    self._measurement_path_signatures.pop(stale, None)
                for analysis_path in analysis_paths:
                    wide_path = self.root / "wide" / analysis_path.name
                    observation_path = self.root / "observations" / analysis_path.name
                    tracker_path = self.root / "tracker-ensemble" / analysis_path.name
                    coherent_path = self.root / "coherent" / analysis_path.name
                    tracker_plot_path = (self.root / "plots" /
                                         f"{analysis_path.stem}-trackers.png")
                    try:
                        signature = (analysis_path.stat().st_mtime_ns,
                                     wide_path.stat().st_mtime_ns if wide_path.is_file() else 0,
                                     observation_path.stat().st_mtime_ns
                                     if observation_path.is_file() else 0,
                                     tracker_path.stat().st_mtime_ns
                                     if tracker_path.is_file() else 0,
                                     coherent_path.stat().st_mtime_ns
                                     if coherent_path.is_file() else 0,
                                     tracker_plot_path.stat().st_mtime_ns
                                     if tracker_plot_path.is_file() else 0)
                    except OSError:
                        continue
                    if self._measurement_path_signatures.get(analysis_path) == signature:
                        continue
                    parsed = self._measurement_records([analysis_path])
                    if parsed:
                        self._measurement_cache_by_path[analysis_path] = parsed[0]
                        self._measurement_path_signatures[analysis_path] = signature
                return sorted(self._measurement_cache_by_path.values(),
                              key=lambda item: item["chunk"])
        try:
            lines = legacy.read_text().splitlines()
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _measurement_records(self, analysis_paths=None) -> list[dict]:
        """Normalize v2 measurement-watch artifacts into dashboard records."""
        records = []
        paths = (sorted((self.root / "analysis").glob("*.json"))
                 if analysis_paths is None else analysis_paths)
        for analysis_path in paths:
            analysis = self._json(analysis_path, None)
            if not isinstance(analysis, dict):
                continue
            match = re.match(r"(?:chunk|dwell)-(\d+)", analysis_path.stem)
            measurement_path = self.root / "chunks" / f"{analysis_path.stem}.npz"
            if match is None or not measurement_path.is_file():
                continue
            try:
                with np.load(measurement_path, allow_pickle=False) as measurement:
                    utc_ns = measurement["utc_ns"]
                    if utc_ns.size == 0:
                        continue
                    first_utc_ns = int(utc_ns[0])
                    last_utc_ns = int(utc_ns[-1])
                    center_hz = float(measurement["center_frequency_hz"])
                    lnb_lo_hz = float(measurement["lnb_lo_hz"])
                    sample_rate_hz = float(measurement["sample_rate_hz"])
                    bandwidth_hz = (float(measurement["bandwidth_hz"])
                                    if "bandwidth_hz" in measurement else None)
                    gain_mode = (str(measurement["gain_mode"])
                                 if "gain_mode" in measurement else None)
                    configured_gain_db = (float(measurement["configured_gain_db"])
                        if "configured_gain_db" in measurement else None)
                    if configured_gain_db is not None and not math.isfinite(configured_gain_db):
                        configured_gain_db = None
                    frequency_bins = (int(np.asarray(measurement["frequency_offsets_hz"]).size)
                                      if "frequency_offsets_hz" in measurement else int(
                                          (analysis.get("measurement") or {}).get(
                                              "artifact_frequency_bins", 0)))
                    frequency_bin_width_hz = (None if not frequency_bins else
                                              sample_rate_hz/frequency_bins)
                    psd_quantization_db = (None if "psd_db_quantization_db" not in measurement
                                           else float(measurement["psd_db_quantization_db"]))
                    samples_per_snapshot = int(measurement["samples_per_snapshot"])
                    snapshots = int(utc_ns.size)
            except (OSError, KeyError, ValueError):
                continue

            joint = analysis.get("joint_events") or []
            qualified = [item for item in joint
                         if (item.get("qualification") or {}).get("qualified")]
            doppler_observations = [item["doppler_observation"] for item in joint
                if (item.get("doppler_observation") or {}).get("qualified")]
            sky_fixed_observations = [item for item in doppler_observations
                if item.get("sky_fixed_by_dither") and
                item.get("retune_transient_confounded") is False]
            phase_locked, _ = dither_phase_locked([float(item[
                "nearest_transition_to_start_s"]) for item in sky_fixed_observations
                if item.get("nearest_transition_to_start_s") is not None])
            if phase_locked:
                sky_fixed_observations = []
            associations = [item.get("association", {}) for item in qualified]
            tracks = []
            for association in associations:
                for receiver in (0, 1):
                    drift = association.get(f"rx{receiver}_drift_hz_s")
                    if drift is not None and math.isfinite(float(drift)):
                        tracks.append({"receiver": receiver,
                                       "fitted_drift_hz_s": float(drift)})
            best = max(associations,
                       key=lambda item: float(item.get("association_score", 0)),
                       default={})
            measurement_summary = analysis.get("measurement") or {}
            capture_identity = analysis.get("capture_identity") or {}
            observation_mode = capture_identity.get("observation_mode") or (
                "retune-validation" if analysis.get("interleaved_dither") else "fixed")
            tle_guided = analysis.get("tle_guided_search") or {}
            all_tle_candidates = tle_guided.get("candidates") or []
            tle_guided_candidates = [item for item in tle_guided.get("candidates", [])
                                     if item.get("qualified")]
            blind = analysis.get("blind_comb_search") or {}
            all_blind_candidates = blind.get("candidates") or []
            blind_candidates = [item for item in all_blind_candidates if item.get("qualified")]
            carrier = analysis.get("blind_carrier_search") or {}
            all_carrier_candidates = carrier.get("candidates") or []
            carrier_candidates = [item for item in all_carrier_candidates if item.get("qualified")]
            hopping = analysis.get("tle_carrier_hopping_search") or {}
            all_hopping_candidates = hopping.get("candidates") or []
            wide = self._json(self.root / "wide" / analysis_path.name, {})
            structured = self._json(self.root / "observations" / analysis_path.name, {})
            tracker_ensemble = self._json(
                self.root / "tracker-ensemble" / analysis_path.name, {})
            tracker_metrics = (tracker_ensemble.get("metrics") or {}
                if isinstance(tracker_ensemble, dict) else {})
            tracker_joint = (tracker_ensemble.get("joint_tracks") or []
                if isinstance(tracker_ensemble, dict) else [])
            tracker_qualified_joint = [item for item in tracker_joint
                                       if item.get("qualified")]
            tracker_identifications = (tracker_ensemble.get("identifications") or []
                if isinstance(tracker_ensemble, dict) else [])
            tracker_qualified_identifications = [item for item in tracker_identifications
                                                 if item.get("qualified")]
            tracker_compatible_identifications = [item for item in tracker_identifications
                                                  if item.get("compatible")]
            coherent = self._json(self.root / "coherent" / analysis_path.name, {})
            coherent_blocks = (coherent.get("blocks") or []
                if isinstance(coherent, dict) else [])
            coherent_joint = (coherent.get("joint_track")
                if isinstance(coherent, dict) else None)
            structured_summary = (structured.get("summary") or {}
                                  if isinstance(structured, dict) else {})
            wide_candidates = wide.get("candidates", []) if isinstance(wide, dict) else []
            wide_moving = [item for item in wide_candidates if item.get(
                "moving_rf_qualified", item.get("leo_like_qualified", False))]
            wide_leo_rate = [item for item in wide_candidates if item.get("leo_like_qualified")]
            wide_orbital = [item for item in wide_candidates if item.get(
                "orbital_shape_qualified", False)]
            best_wide = None
            if wide_candidates:
                source_wide = wide_candidates[0]
                best_wide = {key: value for key, value in source_wide.items()
                             if key not in ("time_s", "mean_relative_path_hz", "receivers")}
                # The UI displays only the best comparison. Full sampled
                # paths and the complete TLE ranking remain in the source
                # report; sending them every five seconds can exceed 1 MB.
                best_wide["tle_comparisons"] = source_wide.get(
                    "tle_comparisons", [])[:3]
                best_wide["receivers"] = [
                    {key: value for key, value in receiver.items() if key not in (
                        "path_rf_hz", "internal_translation_path_rf_hz")}
                    for receiver in source_wide.get("receivers", [])]
            event_groups = analysis.get("events") or []
            event_counts = [len(group) for group in event_groups]
            records.append({
                "schema": analysis.get("schema"),
                "capture_identity": capture_identity,
                "observation_mode": observation_mode,
                "observer_validation": analysis.get("observer_validation"),
                "chunk": int(match.group(1)),
                "capture_id": analysis_path.stem,
                "detected": bool(qualified or tle_guided_candidates or blind_candidates
                                 or carrier_candidates or wide_moving or tracker_qualified_joint),
                "joint_event_count": len(joint),
                "qualified_event_count": len(qualified),
                "doppler_observation_count": len(doppler_observations),
                "doppler_observations": doppler_observations,
                "sky_fixed_doppler_count": len(sky_fixed_observations),
                "interleaved_dither": analysis.get("interleaved_dither"),
                "structured_observation_summary": structured_summary,
                "structured_observation_name": (analysis_path.name
                    if structured_summary else None),
                "tle_guided_candidate_count": int(tle_guided.get("qualified_count", 0)),
                "tle_guided_candidates": tle_guided_candidates,
                "best_tle_candidate": all_tle_candidates[0] if all_tle_candidates else None,
                "blind_comb_candidate_count": int(blind.get("qualified_count", 0)),
                "best_blind_comb_candidate": all_blind_candidates[0] if all_blind_candidates else None,
                "blind_spacing_scan": (blind.get("exploratory_spacing_scan") or {}).get("best", []),
                "blind_carrier_candidate_count": int(carrier.get("qualified_count", 0)),
                "best_blind_carrier_candidate": (all_carrier_candidates[0]
                                                  if all_carrier_candidates else None),
                "tle_hopping_candidate_count": int(hopping.get("qualified_count", 0)),
                "best_tle_hopping_candidate": (all_hopping_candidates[0]
                                                if all_hopping_candidates else None),
                "wide_feature_candidate_count": len(wide_candidates),
                # Retain the old field for API compatibility.  It denotes the
                # coarse TLE/rate heuristic, not proof of orbital curvature.
                "wide_feature_qualified_count": len(wide_leo_rate),
                "wide_feature_moving_rf_count": len(wide_moving),
                "wide_feature_leo_rate_count": len(wide_leo_rate),
                "wide_feature_orbital_shape_count": len(wide_orbital),
                "best_wide_feature_candidate": best_wide,
                "wide_analysis_available": bool(wide),
                "tracker_ensemble_available": bool(tracker_ensemble),
                "tracker_candidate_count_by_tracker": tracker_metrics.get(
                    "candidate_count_by_tracker", {}),
                "tracker_qualified_count_by_tracker": tracker_metrics.get(
                    "qualified_count_by_tracker", {}),
                "tracker_joint_count": len(tracker_joint),
                "tracker_qualified_joint_count": len(tracker_qualified_joint),
                "tracker_tle_identification_count": len(tracker_identifications),
                "tracker_tle_compatible_count": len(tracker_compatible_identifications),
                "tracker_qualified_tle_identification_count": len(
                    tracker_qualified_identifications),
                "tracker_best_tle_identification": (tracker_identifications[0]
                    if tracker_identifications else None),
                "tracker_runtime_s_by_tracker": tracker_metrics.get(
                    "runtime_s_by_tracker", {}),
                "coherent_analysis_available": bool(coherent),
                "coherent_block_count": len(coherent_blocks),
                "coherent_joint_track": coherent_joint,
                "frequency_bins": frequency_bins,
                "sample_rate_hz": sample_rate_hz,
                "bandwidth_hz": bandwidth_hz,
                "gain_mode": gain_mode,
                "configured_gain_db": configured_gain_db,
                "frequency_bin_width_hz": frequency_bin_width_hz,
                "psd_quantization_db": psd_quantization_db,
                "event_counts": event_counts,
                "first_utc_ns": first_utc_ns,
                "last_utc_ns": last_utc_ns,
                "wall_duration_s": float(measurement_summary.get(
                    "observation_span_s", (last_utc_ns - first_utc_ns) / 1e9)),
                "retained_sample_time_s": float(measurement_summary.get(
                    "retained_sample_time_s",
                    snapshots * samples_per_snapshot / sample_rate_hz)),
                "tracks": tracks,
                "receiver_agreement": ({
                    "correlation": best.get("centered_path_correlation"),
                    "drift_difference_hz_s": best.get("drift_difference_hz_s"),
                    "association_score": best.get("association_score"),
                } if best else None),
                "rejection_reasons": ([] if joint else ["no joint dual-receiver event"]),
                "carrier_hz": float(analysis.get("carrier_hz") or center_hz + lnb_lo_hz),
                "channel": {"if_center_hz": center_hz,
                            "rf_center_hz": center_hz + lnb_lo_hz,
                            "lnb_lo_hz": lnb_lo_hz},
                "plot_name": f"{analysis_path.stem}.png",
                "carrier_zoom_name": (f"{analysis_path.stem}-carrier-zoom.png"
                    if (self.root / "plots" / f"{analysis_path.stem}-carrier-zoom.png").is_file()
                    else None),
                "wide_plot_name": (f"{analysis_path.stem}-wide.png"
                    if (self.root / "plots" / f"{analysis_path.stem}-wide.png").is_file()
                    else None),
                "tracker_plot_name": (f"{analysis_path.stem}-trackers.png"
                    if (self.root / "plots" / f"{analysis_path.stem}-trackers.png").is_file()
                    else None),
                # Named for the capture rather than the analysis, because it
                # is written before the analysis exists and outlives the
                # recording it describes.
                "survey_plot_name": (f"{analysis_path.stem}-survey.png"
                    if (self.root / "plots" / f"{analysis_path.stem}-survey.png").is_file()
                    else None),
            })
        return records

    def _catalog(self) -> tuple[dict, list[dict]]:
        try:
            stat = self.passes_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        with self._measurement_cache_lock:
            if signature == self._catalog_cache_signature:
                return self._catalog_cache
            catalog = self._json(self.passes_path, {})
            self._catalog_cache = (catalog, _pass_rows(catalog))
            self._catalog_cache_signature = signature
            return self._catalog_cache

    def status(self, now: datetime | None = None) -> dict:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        summary = self._json(self.root / "summary.json", {})
        records = self.records()
        watcher = self._json(self.root / "status.json", {})
        is_measurement = not (self.root / "index.jsonl").is_file()
        if is_measurement:
            summary = {
                "state": watcher.get("stage", "running"),
                "started_utc": (_iso_from_ns(records[0]["first_utc_ns"])
                                if records else None),
                "updated_utc": watcher.get("updated_utc"),
                "requested_hours": 0,
                "channel": records[-1].get("channel") if records else None,
            }
        started = _utc(summary["started_utc"]) if summary.get("started_utc") else None
        hours = float(summary.get("requested_hours", 0))
        deadline = started + timedelta(hours=hours) if started and hours > 0 else None
        elapsed = (now - started).total_seconds() if started else 0
        coverage_s = sum(float(row.get("wall_duration_s", 0)) for row in records)
        retained_s = sum(float(row.get("retained_sample_time_s",
            self.snapshots_per_chunk * self.samples_per_snapshot / self.sample_rate_hz))
            for row in records)
        updated = _utc(summary["updated_utc"]) if summary.get("updated_utc") else None
        latest = records[-1] if records else {}
        latest_channel = latest.get("channel") or {}
        latest_bins = int(latest.get("frequency_bins", 0))
        latest_channel_number = (3 if abs(float(latest_channel.get(
            "rf_center_hz", 0))-11_325_117_187.5) < 100_000_000 else 4)
        baseline_path = self.root / "baseline" / (
            f"channel-{latest_channel_number}-{latest_bins}bins.npz")
        # A resolution transition intentionally leaves historical generations
        # on their own baselines. Only the active generation can be pending.
        pending_wide = sum(row.get("frequency_bins") == latest_bins and
                           not row.get("wide_analysis_available", False) for row in records)
        return {
            "state": summary.get("state", "not-started"),
            "started_utc": None if started is None else started.isoformat().replace("+00:00", "Z"),
            "deadline_utc": None if deadline is None else deadline.isoformat().replace("+00:00", "Z"),
            "updated_utc": None if updated is None else updated.isoformat().replace("+00:00", "Z"),
            "update_age_s": None if updated is None else max(0, (now - updated).total_seconds()),
            "requested_hours": hours, "analyzed_span_hours": coverage_s / 3600,
            "coverage_hours": coverage_s / 3600,
            "retained_sample_hours": retained_s / 3600,
            "progress_fraction": 0 if not hours else min(1, max(0, elapsed / (hours * 3600))),
            "coverage_fraction": 0 if not hours else min(1, coverage_s / (hours * 3600)),
            "retained_sample_fraction": 0 if not hours else min(1, retained_s / (hours * 3600)),
            "completed_chunks": len(records),
            "frequency_bins": latest_bins or None,
            "frequency_bin_width_hz": latest.get("frequency_bin_width_hz"),
            "psd_quantization_db": latest.get("psd_quantization_db"),
            "resolution_baseline_ready": baseline_path.is_file() if latest_bins else False,
            "pending_wide_analysis_chunks": pending_wide,
            "detection_count": sum(bool(row.get("detected")) for row in records),
            "joint_event_count": sum(int(row.get("joint_event_count", 0)) for row in records),
            "qualified_event_count": sum(int(row.get("qualified_event_count", 0)) for row in records),
            "tle_guided_candidate_count": sum(int(row.get("tle_guided_candidate_count", 0)) for row in records),
            "channel": summary.get("channel"), "identity": summary.get("identity"),
        }

    def expected_passes(self, now: datetime | None = None, limit: int = 16) -> dict:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        catalog, passes = self._catalog()
        current_or_future = [row for row in passes if _utc(row["set_utc"]) >= now]
        return {"generated_at": catalog.get("generated_at"), "carrier_hz": catalog.get("carrier_hz"),
                "observer": catalog.get("observer"), "total_passes": len(passes),
                "passes": current_or_future[:limit]}

    def detections(self) -> dict:
        catalog, passes = self._catalog()
        records = self.records()
        carrier_hz = float(catalog.get("carrier_hz") or 11_575_117_187.5)
        candidates = []
        for record in records:
            if not record.get("detected"):
                continue
            start = datetime.fromtimestamp(record["first_utc_ns"] / 1e9, timezone.utc)
            stop = datetime.fromtimestamp(record["last_utc_ns"] / 1e9, timezone.utc)
            duration_s = max((stop - start).total_seconds(), 1e-9)
            drifts = [float(track["fitted_drift_hz_s"]) for track in record.get("tracks", [])]
            wide_candidate = record.get("best_wide_feature_candidate")
            if (not drifts and wide_candidate and wide_candidate.get(
                    "moving_rf_qualified", wide_candidate.get("leo_like_qualified", False))):
                drifts = [float(item["linear_drift_hz_s"])
                          for item in wide_candidate.get("receivers", [])]
            mean_drift = sum(drifts) / len(drifts) if drifts else None
            record_carrier_hz = float(record.get("carrier_hz", carrier_hz))
            acceleration = (doppler_radial_acceleration_m_s2(mean_drift, record_carrier_hz)
                            if drifts else None)
            overlapping = [item for item in passes
                           if _utc(item["rise_utc"]) <= stop and _utc(item["set_utc"]) >= start]
            if drifts:
                overlapping.sort(key=lambda item: (
                    abs(item["predicted_drift_hz_s"] - mean_drift), -item["max_elevation_deg"]))
            else:
                overlapping.sort(key=lambda item: -item["max_elevation_deg"])
            tle_paths = record.get("tle_guided_candidates") or []
            best_path = tle_paths[0] if tle_paths else None
            best = (next((item for item in overlapping
                          if best_path and item["norad_id"] == best_path.get("norad_id")), None)
                    or (overlapping[0] if overlapping else None))
            if (wide_candidate and wide_candidate.get("leo_like_qualified")
                    and not wide_candidate.get("specific_tle_identifiable")):
                best = None
            plot_name = record.get("plot_name") or f"chunk-{int(record['chunk']):05d}.png"
            candidates.append({
                "chunk": record["chunk"], "start_utc": _iso_from_ns(record["first_utc_ns"]),
                "stop_utc": _iso_from_ns(record["last_utc_ns"]), "duration_s": duration_s,
                "mean_drift_hz_s": mean_drift, "receiver_drifts_hz_s": drifts,
                "radial_acceleration_m_s2": acceleration,
                "equivalent_radial_velocity_change_m_s": (
                    None if acceleration is None else acceleration * duration_s),
                "receiver_agreement": record.get("receiver_agreement"),
                "joint_event_count": int(record.get("joint_event_count", 0)),
                "qualified_event_count": int(record.get("qualified_event_count", 0)),
                "doppler_observation_count": int(record.get("doppler_observation_count", 0)),
                "doppler_observations": record.get("doppler_observations", [])[:20],
                "sky_fixed_doppler_count": int(record.get("sky_fixed_doppler_count", 0)),
                "interleaved_dither": record.get("interleaved_dither"),
                "structured_observation_summary": record.get(
                    "structured_observation_summary") or {},
                "structured_observation_url": (None if not record.get(
                    "structured_observation_name") else "/observations/"+record[
                        "structured_observation_name"]),
                "tle_guided_candidate_count": int(record.get("tle_guided_candidate_count", 0)),
                "best_tle_candidate": record.get("best_tle_candidate"),
                "blind_comb_candidate_count": int(record.get("blind_comb_candidate_count", 0)),
                "best_blind_comb_candidate": record.get("best_blind_comb_candidate"),
                "blind_spacing_scan": record.get("blind_spacing_scan", []),
                "blind_carrier_candidate_count": int(record.get("blind_carrier_candidate_count", 0)),
                "best_blind_carrier_candidate": record.get("best_blind_carrier_candidate"),
                "tle_hopping_candidate_count": int(record.get("tle_hopping_candidate_count", 0)),
                "best_tle_hopping_candidate": record.get("best_tle_hopping_candidate"),
                "wide_feature_candidate_count": int(record.get("wide_feature_candidate_count", 0)),
                "wide_feature_qualified_count": int(record.get("wide_feature_qualified_count", 0)),
                "wide_feature_moving_rf_count": int(record.get(
                    "wide_feature_moving_rf_count", record.get("wide_feature_qualified_count", 0))),
                "wide_feature_leo_rate_count": int(record.get(
                    "wide_feature_leo_rate_count", record.get("wide_feature_qualified_count", 0))),
                "wide_feature_orbital_shape_count": int(record.get(
                    "wide_feature_orbital_shape_count", 0)),
                "best_wide_feature_candidate": record.get("best_wide_feature_candidate"),
                "tracker_ensemble_available": bool(record.get("tracker_ensemble_available")),
                "tracker_candidate_count_by_tracker": record.get(
                    "tracker_candidate_count_by_tracker", {}),
                "tracker_qualified_count_by_tracker": record.get(
                    "tracker_qualified_count_by_tracker", {}),
                "tracker_joint_count": int(record.get("tracker_joint_count", 0)),
                "tracker_qualified_joint_count": int(record.get(
                    "tracker_qualified_joint_count", 0)),
                "tracker_runtime_s_by_tracker": record.get(
                    "tracker_runtime_s_by_tracker", {}),
                "tracker_tle_identification_count": int(record.get(
                    "tracker_tle_identification_count", 0)),
                "tracker_tle_compatible_count": int(record.get(
                    "tracker_tle_compatible_count", 0)),
                "tracker_qualified_tle_identification_count": int(record.get(
                    "tracker_qualified_tle_identification_count", 0)),
                "tracker_best_tle_identification": record.get(
                    "tracker_best_tle_identification"),
                "coherent_analysis_available": bool(record.get(
                    "coherent_analysis_available")),
                "coherent_block_count": int(record.get("coherent_block_count", 0)),
                "coherent_joint_track": record.get("coherent_joint_track"),
                "observer_validation": record.get("observer_validation"),
                "tle_guided_candidate": best_path,
                "event_counts": record.get("event_counts"),
                "false_alarm_probability": max((item.get("false_alarm_probability", 1)
                    for item in record.get("significance", [])), default=None),
                "overlapping_expected_passes": len(overlapping), "best_tle_match": best,
                "classification": (
                    "orbital-shape candidate; specific TLE unresolved"
                    if wide_candidate and wide_candidate.get("orbital_shape_qualified") else
                    "coherent moving RF; LEO-rate compatible; orbital curvature unproven"
                    if wide_candidate and wide_candidate.get("leo_like_qualified") else
                    "coherent moving RF; orbital interpretation unproven"
                    if wide_candidate and wide_candidate.get(
                        "moving_rf_qualified", wide_candidate.get("leo_like_qualified", False)) else
                    "event-qualified + TLE-integrated Doppler candidate"
                    if drifts and best_path else
                    "event-qualified Doppler candidate" if drifts else
                    "TLE-integrated Doppler-path candidate"),
                "plot_url": self._plot_url(plot_name),
                "carrier_zoom_url": (None if not record.get("carrier_zoom_name") else
                                     self._plot_url(record["carrier_zoom_name"])),
                "wide_plot_url": (None if not record.get("wide_plot_name") else
                                  self._plot_url(record["wide_plot_name"])),
                "tracker_plot_url": (None if not record.get("tracker_plot_name") else
                                     self._plot_url(record["tracker_plot_name"])),
                "survey_plot_url": (None if not record.get("survey_plot_name") else
                                    self._plot_url(record["survey_plot_name"])),
            })
        return {"carrier_hz": carrier_hz,
                "speed_note": ("Doppler slope measures radial acceleration. Absolute measured speed "
                               "requires a known transmit carrier; TLE range rates are predictions."),
                "detections": list(reversed(candidates))}

    def logs(self, limit: int = 40) -> dict:
        rows = self.records()[-max(1, min(limit, 200)):]
        events = [{"chunk": row.get("chunk"), "time_utc": _iso_from_ns(row["last_utc_ns"]),
                   "detected": bool(row.get("detected")),
                   "joint_event_count": int(row.get("joint_event_count", 0)),
                   "qualified_event_count": int(row.get("qualified_event_count", 0)),
                   "tle_guided_candidate_count": int(row.get("tle_guided_candidate_count", 0)),
                   "event_counts": row.get("event_counts"),
                   "drift_hz_s": [track.get("fitted_drift_hz_s") for track in row.get("tracks", [])],
                   "rejection_reasons": row.get("rejection_reasons", [])} for row in reversed(rows)]
        return {"events": events}

    def waterfalls(self, limit: int = 12) -> dict:
        rows = []
        for record in reversed(self.records()[-max(1, min(limit, 100)):]):
            plot_name = record.get("plot_name") or f"chunk-{int(record['chunk']):05d}.png"
            rows.append({
                "chunk": record["chunk"],
                "capture_id": record.get("capture_id"),
                "start_utc": _iso_from_ns(record["first_utc_ns"]),
                "stop_utc": _iso_from_ns(record["last_utc_ns"]),
                "wall_duration_s": float(record.get("wall_duration_s", 0)),
                "detected": bool(record.get("detected")),
                "joint_event_count": int(record.get("joint_event_count", 0)),
                "qualified_event_count": int(record.get("qualified_event_count", 0)),
                "doppler_observation_count": int(record.get("doppler_observation_count", 0)),
                "doppler_observations": record.get("doppler_observations", [])[:20],
                "sky_fixed_doppler_count": int(record.get("sky_fixed_doppler_count", 0)),
                "interleaved_dither": record.get("interleaved_dither"),
                "structured_observation_summary": record.get(
                    "structured_observation_summary") or {},
                "structured_observation_url": (None if not record.get(
                    "structured_observation_name") else "/observations/"+record[
                        "structured_observation_name"]),
                "tle_guided_candidate_count": int(record.get("tle_guided_candidate_count", 0)),
                "best_tle_candidate": record.get("best_tle_candidate"),
                "blind_comb_candidate_count": int(record.get("blind_comb_candidate_count", 0)),
                "best_blind_comb_candidate": record.get("best_blind_comb_candidate"),
                "blind_spacing_scan": record.get("blind_spacing_scan", []),
                "blind_carrier_candidate_count": int(record.get("blind_carrier_candidate_count", 0)),
                "best_blind_carrier_candidate": record.get("best_blind_carrier_candidate"),
                "tle_hopping_candidate_count": int(record.get("tle_hopping_candidate_count", 0)),
                "best_tle_hopping_candidate": record.get("best_tle_hopping_candidate"),
                "wide_feature_candidate_count": int(record.get("wide_feature_candidate_count", 0)),
                "wide_feature_moving_rf_count": int(record.get(
                    "wide_feature_moving_rf_count", record.get("wide_feature_qualified_count", 0))),
                "wide_feature_leo_rate_count": int(record.get(
                    "wide_feature_leo_rate_count", record.get("wide_feature_qualified_count", 0))),
                "wide_feature_orbital_shape_count": int(record.get(
                    "wide_feature_orbital_shape_count", 0)),
                "best_wide_feature_candidate": record.get("best_wide_feature_candidate"),
                "tracker_ensemble_available": bool(record.get("tracker_ensemble_available")),
                "tracker_candidate_count_by_tracker": record.get(
                    "tracker_candidate_count_by_tracker", {}),
                "tracker_qualified_count_by_tracker": record.get(
                    "tracker_qualified_count_by_tracker", {}),
                "tracker_joint_count": int(record.get("tracker_joint_count", 0)),
                "tracker_qualified_joint_count": int(record.get(
                    "tracker_qualified_joint_count", 0)),
                "tracker_runtime_s_by_tracker": record.get(
                    "tracker_runtime_s_by_tracker", {}),
                "tracker_tle_identification_count": int(record.get(
                    "tracker_tle_identification_count", 0)),
                "tracker_tle_compatible_count": int(record.get(
                    "tracker_tle_compatible_count", 0)),
                "tracker_qualified_tle_identification_count": int(record.get(
                    "tracker_qualified_tle_identification_count", 0)),
                "tracker_best_tle_identification": record.get(
                    "tracker_best_tle_identification"),
                "coherent_analysis_available": bool(record.get(
                    "coherent_analysis_available")),
                "coherent_block_count": int(record.get("coherent_block_count", 0)),
                "coherent_joint_track": record.get("coherent_joint_track"),
                "observer_validation": record.get("observer_validation"),
                "event_counts": record.get("event_counts"),
                "sample_rate_hz": record.get("sample_rate_hz"),
                "bandwidth_hz": record.get("bandwidth_hz"),
                "gain_mode": record.get("gain_mode"),
                "configured_gain_db": record.get("configured_gain_db"),
                "observation_mode": record.get("observation_mode", "fixed"),
                "channel": record.get("channel"),
                "plot_url": self._plot_url(plot_name),
                "carrier_zoom_url": (None if not record.get("carrier_zoom_name") else
                                     self._plot_url(record["carrier_zoom_name"])),
                "wide_plot_url": (None if not record.get("wide_plot_name") else
                                  self._plot_url(record["wide_plot_name"])),
                "tracker_plot_url": (None if not record.get("tracker_plot_name") else
                                     self._plot_url(record["tracker_plot_name"])),
                "survey_plot_url": (None if not record.get("survey_plot_name") else
                                    self._plot_url(record["survey_plot_name"])),
            })
        return {"waterfalls": rows}

    def dither_comparisons(self, limit: int = 12) -> dict:
        rows = []
        for path in sorted((self.root / "dither").glob("*.json"), reverse=True)[:limit]:
            report = self._json(path, None)
            if not isinstance(report, dict):
                continue
            match = re.match(r"chunk-(\d+)", path.stem)
            rows.append({"chunk": None if match is None else int(match.group(1)),
                "classification": report.get("classification"),
                "tuning_dither_hz": report.get("tuning_dither_hz"),
                "receivers_agree": report.get("receivers_agree"),
                "receivers": report.get("receivers", []),
                "first": report.get("first"), "second": report.get("second")})
        return {"comparisons": rows}

    def beacon_fingerprint_plot(self) -> bytes:
        index = ({} if self.beacon_root is None else self._json(
            self.beacon_root / "reports" / "fingerprints" / "index.json", {}))
        return render_fingerprint_svg(index)

    def beacon(self, limit: int = 12) -> dict:
        if self.beacon_root is None:
            return {"enabled": False, "captures": [], "candidate_count": 0,
                    "qualified_count": 0, "active": None, "calibration": {}}
        captures_root, reports_root = self.beacon_root / "captures", self.beacon_root / "reports"
        report_paths = sorted(reports_root.glob("*.json"),
                              key=lambda path: path.stat().st_mtime_ns, reverse=True)
        manifest_paths = self._newest_existing_paths(
            captures_root.glob("*/manifest.json"))
        followup_paths = list((reports_root / "followups").glob("*.json"))
        decode_paths = list((reports_root / "decoded").glob("*.json"))
        fingerprint_paths = list((reports_root / "fingerprints").glob("*.json"))
        fingerprint_index_path = reports_root / "fingerprints" / "index.json"
        gain_summary_path = reports_root / "gain-experiment" / "summary.json"
        calibration_path = reports_root / "calibration" / "calibration.json"
        signature_paths = (manifest_paths[:2] +
            report_paths[:max(1, min(limit, 100))] + followup_paths + decode_paths +
            fingerprint_paths)
        if calibration_path.is_file():
            signature_paths.append(calibration_path)
        if gain_summary_path.is_file():
            signature_paths.append(gain_summary_path)
        try:
            signature = tuple(sorted((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                                     for path in signature_paths))
        except OSError:
            signature = ()
        with self._beacon_cache_lock:
            if signature and signature == self._beacon_cache_signature:
                return self._beacon_cache
        fingerprint_index = self._json(fingerprint_index_path, {})
        fingerprint_membership = fingerprint_index.get("membership", {})
        fingerprint_cluster_sizes = {cluster.get("cluster_id"): cluster.get("member_count", 0)
            for cluster in fingerprint_index.get("clusters", [])}
        active = None
        for manifest_path in manifest_paths:
            manifest = self._json(manifest_path, {})
            report_exists = (reports_root / f"{manifest_path.parent.name}.json").is_file()
            if manifest.get("state") == "capturing" or (
                    manifest.get("state") == "complete" and not report_exists):
                active = {"name": manifest_path.parent.name,
                          "stage": "capturing" if manifest.get("state") == "capturing" else "analyzing",
                          "created_utc": _iso_from_ns(manifest.get("created_utc_ns", 0)),
                          "channel_number": manifest.get("metadata", {}).get("channel_number"),
                          "region": manifest.get("metadata", {}).get("region"),
                          "observation_mode": manifest.get("metadata", {}).get(
                              "observation_mode", "narrow"),
                          "if_center_hz": manifest.get("center_frequency_hz"),
                          "rf_center_hz": manifest.get("rf_center_hz")}
                break
        rows = []
        recent_paths = report_paths[:max(1, min(limit, 100))]
        confirmed_names = set()
        for followup_path in followup_paths:
            followup = self._json(followup_path, {})
            if followup.get("confirmation", {}).get("confirmed"):
                confirmed_names.add(followup_path.name)
        selected_paths = list(recent_paths)
        selected_names = {path.name for path in selected_paths}
        for name in confirmed_names - selected_names:
            path = reports_root / name
            if path.is_file():
                selected_paths.append(path)
        # Confirmed events are durable evidence, not ephemeral recent status.
        # Pin them first so a high capture cadence cannot hide them from the UI.
        selected_paths.sort(key=lambda path: (
            path.name in confirmed_names, path.stat().st_mtime_ns), reverse=True)
        for report_path in selected_paths:
            report = self._json(report_path, {})
            if report.get("schema") != "leo-tracker.starlink-beacon-analysis/v1":
                continue
            manifest, summary = report.get("capture_manifest", {}), report.get("summary", {})
            analysis = report.get("analysis", {})
            exact = report.get("exact_checks", [])
            followup_path = reports_root / "followups" / report_path.name
            followup = self._json(followup_path, {})
            decode_path = reports_root / "decoded" / report_path.name
            decode = self._json(decode_path, {})
            decode_plot = reports_root / "decoded" / f"{report_path.stem}.png"
            fingerprint_path = reports_root / "fingerprints" / report_path.name
            fingerprint = self._json(fingerprint_path, {})
            fingerprint_cluster = fingerprint_membership.get(report_path.stem)
            fingerprint_matches = fingerprint_index.get("nearest_matches", {}).get(
                report_path.stem, [])
            confirmation = followup.get("confirmation", {})
            confirmation_links = (confirmation.get("cross_receiver_links", []) +
                confirmation.get("dual_receiver_links", []) + [
                link for receiver in confirmation.get("receivers", [])
                for link in receiver.get("links", [])])
            strongest_link = max(confirmation_links,
                key=lambda item: abs(float(item.get("drift_hz_s", 0))), default=None)
            overlapping_passes = followup.get("overlapping_passes", [])
            rows.append({"name": report_path.stem,
                "plot_url": (f"/beacon-plots/{report_path.stem}.png"
                             if (reports_root / "plots" / f"{report_path.stem}.png").is_file()
                             else None),
                "created_utc": _iso_from_ns(manifest.get("created_utc_ns", 0)),
                "channel_number": manifest.get("metadata", {}).get("channel_number"),
                "region": manifest.get("metadata", {}).get("region"),
                "observation_mode": manifest.get("metadata", {}).get(
                    "observation_mode", "narrow"),
                "if_center_hz": manifest.get("center_frequency_hz"),
                "rf_center_hz": manifest.get("rf_center_hz"),
                "sample_rate_hz": manifest.get("sample_rate_hz"),
                "bandwidth_hz": manifest.get("bandwidth_hz"),
                "gain_mode": manifest.get("gain_mode"),
                "configured_gain_db": manifest.get("configured_gain_db"),
                "duration_s": manifest.get("requested_duration_s"),
                "exact_acquisition_method": analysis.get(
                    "exact_acquisition_method", "coherent_grid_v1"),
                "acquisition_span_hz": analysis.get("acquisition_span_hz", 0),
                "acquisition_step_hz": analysis.get("acquisition_step_hz"),
                "stream_timing": manifest.get("stream_timing", {}),
                "candidate_count": summary.get("exact_candidate_count", 0),
                "qualified_count": summary.get("exact_qualified_count", 0),
                "single_receiver_candidate_count": summary.get("single_receiver_candidate_count", 0),
                "single_receiver_qualified_count": summary.get("single_receiver_qualified_count", 0),
                "followup_trigger_count": summary.get("followup_trigger_count", 0),
                "exact_sampled_time_s": summary.get("exact_sampled_time_s"),
                "exact_temporal_coverage_fraction": summary.get(
                    "exact_temporal_coverage_fraction"),
                "followup_check_count": len(followup.get("checks", [])),
                "beacon_detected_count": len(confirmed_beacon_events(confirmation)),
                "followup_url": (f"/beacon-followups/{report_path.name}"
                                 if followup_path.is_file() else None),
                "decode_url": (f"/beacon-decodes/{report_path.name}"
                               if decode.get("schema") ==
                               "leo-tracker.starlink-edge-decode/v1" else None),
                "decode_plot_url": (f"/beacon-decode-plots/{report_path.stem}.png"
                                    if decode_plot.is_file() else None),
                "decode": decode.get("combined"),
                "fingerprint_url": (f"/beacon-fingerprints/{report_path.name}"
                    if fingerprint.get("schema") ==
                    "leo-tracker.starlink-waveform-fingerprint/v1" else None),
                "fingerprint_cluster_id": fingerprint_cluster,
                "fingerprint_cluster_size": fingerprint_cluster_sizes.get(
                    fingerprint_cluster, 0),
                "fingerprint_nearest_matches": fingerprint_matches,
                "followup_confirmed": confirmation.get("confirmed", False),
                "same_receiver_confirmed": confirmation.get("same_receiver_confirmed", False),
                "cross_receiver_confirmed": confirmation.get("cross_receiver_confirmed", False),
                "dual_receiver_confirmed": confirmation.get("dual_receiver_confirmed", False),
                "confirmed_link_count": len(confirmation_links),
                "strongest_confirmed_link": strongest_link,
                "overlapping_pass_count": len(overlapping_passes),
                "overlapping_passes": [{
                    "name": item.get("name"), "norad_id": item.get("norad_id"),
                    "observation_utc": item.get("observation_utc"),
                    "culmination_elevation_deg": item.get("culmination_elevation_deg"),
                    "nearest_prediction": item.get("nearest_prediction")}
                    for item in overlapping_passes[:5]],
                "structural_qualified_fraction": summary.get("qualified_fraction", 0),
                "exact_checks": [{"candidate": item.get("candidate"),
                    "qualified": item.get("qualified"),
                    "epoch_difference_samples": item.get("epoch_difference_samples"),
                    "cfo_difference_hz": item.get("cfo_difference_hz"),
                    "pss_ratios": [receiver.get("pss", {}).get("peak_to_median")
                                   for receiver in item.get("receivers", [])],
                    "pilot_margins": [receiver.get("pilot", {}).get("score_margin")
                                      for receiver in item.get("receivers", [])],
                    "matched_margins": [receiver.get("acquisition", {}).get("match_score_margin")
                                        for receiver in item.get("receivers", [])],
                    "selected_subband_offsets_hz": [
                        receiver.get("acquisition", {}).get("selected_center_offset_hz")
                        for receiver in item.get("receivers", [])]}
                    for item in exact]})
        calibration = self._json(calibration_path, {})
        gain_experiment = self._json(gain_summary_path, {})
        result = {"enabled": True, "root": str(self.beacon_root), "active": active,
                "calibration": calibration,
                "gain_experiment": gain_experiment,
                "inventory": {"analyzed_capture_count": len(report_paths),
                    "retained_iq_capture_count": len(manifest_paths),
                    "followup_capture_count": len(followup_paths),
                    "temporally_confirmed_capture_count": len(confirmed_names),
                    "decoded_capture_count": len(decode_paths)},
                "fingerprints": {"count": fingerprint_index.get("fingerprint_count", 0),
                    "cluster_count": len(fingerprint_index.get("clusters", [])),
                    "linked_count": fingerprint_index.get("linked_fingerprint_count", 0),
                    "unresolved_count": fingerprint_index.get(
                        "unresolved_singleton_count", 0),
                    "largest_cluster_size": fingerprint_index.get(
                        "largest_cluster_size", 0),
                    "index_url": ("/beacon-fingerprints/index.json"
                                  if fingerprint_index_path.is_file() else None)},
                "active_acquisition_method": (rows[0]["exact_acquisition_method"]
                                               if rows else None),
                "captures": rows, "candidate_count": sum(row["candidate_count"] for row in rows),
                "qualified_count": sum(row["qualified_count"] for row in rows)}
        with self._beacon_cache_lock:
            self._beacon_cache_signature = signature
            self._beacon_cache = result
        return result

    def recordings(self, limit: int = 100) -> dict:
        """Return a lightweight, image-free index of recent recording artifacts."""
        limit = max(1, min(int(limit), 200))
        rows = []
        activity: dict = {}
        persisted = self._beacon_recording_index()
        if persisted.get("schema") in {
                "leo-tracker.beacon-dashboard-index/v2",
                "leo-tracker.beacon-dashboard-index/v3"}:
            persisted_rows = persisted.get("recordings", [])
            # Aggregated over every persisted row rather than the rendered page,
            # so the headline windows are not truncated by the table limit.
            activity = activity_summary([{
                "start_utc": item.get("start_utc"),
                "duration_s": item.get("duration_s"),
                "confirmed": item.get("confirmed"),
                "beacon_detected_count": item.get("beacon_detected_count"),
                "qualified_tle_association_count": item.get(
                    "qualified_tle_association_count"),
                "satellite_norad_id": item.get("satellite_norad_id"),
                "channel": item.get("channel"), "region": item.get("region"),
                "receiver_probe_count": item.get("receiver_probe_count"),
                "receiver_candidate_counts": item.get("receiver_candidate_counts"),
                "receiver_qualified_counts": item.get("receiver_qualified_counts"),
                "receiver_rms_magnitude": item.get("receiver_rms_magnitude"),
                "receiver_near_full_scale": item.get("receiver_near_full_scale"),
                "receiver_gain_db": item.get("receiver_gain_db"),
                **_row_identity(item)} for item in persisted_rows])
            for item in persisted_rows[:limit]:
                public = {key: value for key, value in item.items()
                          if not key.startswith("_")}
                radio = item.get("_statistics", {}).get(
                    "radio_parameters", {})
                hardware = radio.get("hardware", {}) or {}
                receivers = radio.get("receivers", {}) or {}
                public.setdefault("radio_id", hardware.get("radio_id"))
                public.setdefault("radio_serial", hardware.get("serial"))
                public.setdefault("receiver_labels", receivers.get("receiver_labels"))
                rows.append(public)
            beacon = {"inventory": persisted.get("summary", {}),
                "fingerprints": {"count": persisted.get("summary", {}).get(
                    "fingerprint_count", 0)}, "captures": [], "active": None,
                "active_captures": []}
            persisted_recording_ids = {
                item.get("recording_id") for item in persisted_rows
            }
            manifest_paths = self._recent_capture_manifests(
                self.beacon_root / "captures", limit=20)
            for manifest_path in manifest_paths:
                manifest = self._json(manifest_path, {})
                name = manifest_path.parent.name
                # A committed snapshot is authoritative for historical state.
                # Once source-sidecar retirement is enabled, the absence of a
                # report JSON must not turn an already stored run back into an
                # apparently active analysis.
                if name in persisted_recording_ids:
                    continue
                report_exists = (self.beacon_root / "reports" / f"{name}.json").is_file()
                if manifest.get("state") == "capturing" or (
                        manifest.get("state") == "complete" and not report_exists):
                    metadata = manifest.get("metadata", {})
                    identity = manifest.get("identity", {}) or {}
                    created_ns = int(manifest.get("created_utc_ns", 0) or 0)
                    pending = {"name": name,
                        "stage": "capturing" if manifest.get("state") == "capturing" else "analyzing",
                        "created_utc": _iso_from_ns(created_ns) if created_ns else None,
                        "radio_id": identity.get("radio_id"),
                        "radio_serial": identity.get("serial"),
                        "receiver_labels": identity.get("receiver_labels"),
                        "channel_number": metadata.get("channel_number"),
                        "region": metadata.get("region"),
                        "observation_mode": metadata.get("observation_mode", "narrow"),
                        "if_center_hz": manifest.get("center_frequency_hz"),
                        "rf_center_hz": manifest.get("rf_center_hz"),
                        "sample_rate_hz": manifest.get("sample_rate_hz"),
                        "bandwidth_hz": manifest.get("bandwidth_hz"),
                        "gain_mode": manifest.get("gain_mode"),
                        "configured_gain_db": manifest.get("configured_gain_db")}
                    beacon["active_captures"].append(pending)
            beacon["active"] = (beacon["active_captures"][0]
                                if beacon["active_captures"] else None)
        else:
            beacon = self.beacon(limit=min(limit, 100))
        active_captures = beacon.get("active_captures") or (
            [beacon["active"]] if beacon.get("active") else [])
        persisted_ids = {row.get("recording_id") for row in rows}
        for active in active_captures:
            if active.get("name") in persisted_ids:
                continue
            rows.append({"kind": "beacon", "recording_id": active.get("name"),
                "start_utc": active.get("created_utc"), "status": active.get("stage", "active"),
                "radio_id": active.get("radio_id"),
                "radio_serial": active.get("radio_serial"),
                "receiver_labels": active.get("receiver_labels"),
                "mode": active.get("observation_mode"),
                "channel": active.get("channel_number"), "region": active.get("region"),
                "if_center_hz": active.get("if_center_hz"),
                "rf_center_hz": active.get("rf_center_hz"),
                "sample_rate_hz": active.get("sample_rate_hz"),
                "bandwidth_hz": active.get("bandwidth_hz"), "duration_s": None,
                "gain": (active.get("gain_mode") or "unknown") + (
                    f" {float(active['configured_gain_db']):g} dB"
                    if active.get("configured_gain_db") is not None else ""),
                "candidate_count": None, "confirmed": None, "decoded": None,
                "beacon_detected_count": None,
                "pilot_accuracy": None,
                "detail_url": f"/recordings/beacon/{active.get('name')}"})
        for item in beacon.get("captures", []):
            identity = (item.get("capture_manifest") or {}).get("identity", {}) or {}
            decode = item.get("decode") or {}
            pilot = (decode.get("soft_dual_rx") or {}).get("pilot") or {}
            pilot_accuracy = pilot.get("hard_symbol_accuracy",
                decode.get("minimum_pilot_accuracy"))
            strongest_link = item.get("strongest_confirmed_link") or {}
            status = ("confirmed" if item.get("followup_confirmed") else
                      "qualified" if item.get("qualified_count") else
                      "candidate" if (item.get("candidate_count") or
                                      item.get("single_receiver_candidate_count")) else
                      "analyzed")
            gain = item.get("gain_mode") or "unknown"
            if item.get("configured_gain_db") is not None:
                gain += f" {float(item['configured_gain_db']):g} dB"
            rows.append({"kind": "beacon", "recording_id": item.get("name"),
                "start_utc": item.get("created_utc"), "status": status,
                "radio_id": identity.get("radio_id"),
                "radio_serial": identity.get("serial"),
                "receiver_labels": identity.get("receiver_labels"),
                "mode": item.get("observation_mode"), "channel": item.get("channel_number"),
                "region": item.get("region"), "if_center_hz": item.get("if_center_hz"),
                "rf_center_hz": item.get("rf_center_hz"),
                "sample_rate_hz": item.get("sample_rate_hz"),
                "bandwidth_hz": item.get("bandwidth_hz"),
                "duration_s": item.get("duration_s"), "gain": gain,
                "candidate_count": item.get("candidate_count", 0) +
                    item.get("single_receiver_candidate_count", 0),
                "dual_candidate_count": item.get("candidate_count", 0),
                "single_receiver_candidate_count": item.get(
                    "single_receiver_candidate_count", 0),
                "confirmed": bool(item.get("followup_confirmed")),
                "beacon_detected_count": item.get("beacon_detected_count", 0),
                "decoded": bool(item.get("decode_url")), "pilot_accuracy": pilot_accuracy,
                "pilot_confidence": pilot.get("soft_mean_confidence"),
                "pilot_evm": pilot.get("rms_evm"),
                "decode_frame_count": decode.get("minimum_frame_count"),
                "strongest_drift_hz_s": strongest_link.get("drift_hz_s"),
                "tle_overlap_count": item.get("overlapping_pass_count", 0),
                "fingerprint_family": item.get("fingerprint_cluster_id"),
                "fingerprint_family_size": item.get("fingerprint_cluster_size", 0),
                "exact_coverage_fraction": item.get("exact_temporal_coverage_fraction"),
                "detail_url": f"/recordings/beacon/{item.get('name')}"})
        remaining = max(0, limit - len(rows))
        for item in (self.waterfalls(limit=min(remaining, 100)).get("waterfalls", [])
                     if remaining else []):
            recording_id = item.get("capture_id") or f"chunk-{int(item['chunk']):05d}"
            gain = item.get("gain_mode") or "unknown"
            if item.get("configured_gain_db") is not None:
                gain += f" {float(item['configured_gain_db']):g} dB"
            rows.append({"kind": "sweep", "recording_id": recording_id,
                "start_utc": item.get("start_utc"), "status": (
                    "doppler" if item.get("doppler_observation_count") else
                    "candidate" if item.get("detected") else "analyzed"),
                "mode": item.get("observation_mode"),
                "channel": (item.get("channel") or {}).get("channel_number"),
                "region": (item.get("channel") or {}).get("region"),
                "if_center_hz": (item.get("channel") or {}).get("if_center_hz"),
                "rf_center_hz": (item.get("channel") or {}).get("rf_center_hz"),
                "sample_rate_hz": item.get("sample_rate_hz"),
                "bandwidth_hz": item.get("bandwidth_hz"),
                "duration_s": item.get("wall_duration_s"), "gain": gain,
                "candidate_count": item.get("joint_event_count", 0),
                "dual_candidate_count": item.get("joint_event_count", 0),
                "single_receiver_candidate_count": None,
                "confirmed": bool(item.get("qualified_event_count")), "decoded": False,
                "beacon_detected_count": None,
                "pilot_accuracy": None, "pilot_confidence": None, "pilot_evm": None,
                "decode_frame_count": None,
                "strongest_drift_hz_s": ((item.get("coherent_joint_track") or {}).get(
                    "mean_drift_hz_s")),
                "tle_overlap_count": item.get("tle_guided_candidate_count", 0),
                "fingerprint_family": None, "fingerprint_family_size": None,
                "exact_coverage_fraction": None,
                "detail_url": f"/recordings/sweep/{recording_id}"})
        rows.sort(key=lambda item: item.get("start_utc") or "", reverse=True)
        return {"schema": "leo-tracker.dashboard-recording-index/v1",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "summary": {**beacon.get("inventory", {}),
                    "fingerprint_count": beacon.get("fingerprints", {}).get("count", 0)},
                "activity": activity,
                "recordings": rows[:limit]}

    def recording_detail(self, kind: str, recording_id: str) -> dict | None:
        """Return one recording's statistics and plot links on demand."""
        if kind == "beacon":
            persisted = self._beacon_recording_index()
            if persisted.get("schema") in {
                    "leo-tracker.beacon-dashboard-index/v2",
                    "leo-tracker.beacon-dashboard-index/v3"}:
                row = next((item for item in persisted.get("recordings", [])
                            if item.get("recording_id") == recording_id), None)
                if row is not None:
                    complete = (row if row.get("_statistics") else
                                capture_dashboard_record(self.beacon_root,
                                                         recording_id))
                    if complete is None:
                        return None
                    return {"kind": kind, "recording_id": recording_id, "active": False,
                            "statistics": complete.get("_statistics", {}),
                            "plots": complete.get("_plots", []),
                            "artifacts": complete.get("_artifacts", [])}
            manifest_path = (self.beacon_root / "captures" / recording_id /
                             "manifest.json")
            analysis_path = self.beacon_root / "reports" / f"{recording_id}.json"
            if manifest_path.is_file() and not analysis_path.is_file():
                manifest = self._json(manifest_path, {})
                metadata = manifest.get("metadata", {}) or {}
                identity = manifest.get("identity", {}) or {}
                created_ns = int(manifest.get("created_utc_ns", 0) or 0)
                return {"kind": kind, "recording_id": recording_id, "active": True,
                        "statistics": {
                            "name": recording_id,
                            "stage": ("capturing" if manifest.get("state") == "capturing"
                                      else "analyzing"),
                            "created_utc": (_iso_from_ns(created_ns)
                                            if created_ns else None),
                            "radio_id": identity.get("radio_id"),
                            "radio_serial": identity.get("serial"),
                            "receiver_labels": identity.get("receiver_labels"),
                            "channel_number": metadata.get("channel_number"),
                            "region": metadata.get("region"),
                            "observation_mode": metadata.get(
                                "observation_mode", "narrow"),
                            "if_center_hz": manifest.get("center_frequency_hz"),
                            "rf_center_hz": manifest.get("rf_center_hz"),
                            "sample_rate_hz": manifest.get("sample_rate_hz"),
                            "bandwidth_hz": manifest.get("bandwidth_hz"),
                            "gain_mode": manifest.get("gain_mode"),
                            "configured_gain_db": manifest.get("configured_gain_db"),
                            "capture_manifest": manifest,
                            "radio_parameters": capture_radio_parameters(manifest)},
                        "plots": [], "artifacts": []}
            report = self.beacon(limit=100)
            if (report.get("active") or {}).get("name") == recording_id:
                manifest = self._json(self.beacon_root / "captures" / recording_id /
                                      "manifest.json", {})
                return {"kind": kind, "recording_id": recording_id,
                        "active": True, "statistics": {**report["active"],
                            "capture_manifest": manifest,
                            "radio_parameters": capture_radio_parameters(manifest)}, "plots": [],
                        "artifacts": []}
            item = next((row for row in report.get("captures", [])
                         if row.get("name") == recording_id), None)
            if item is None:
                return None
            plots = [value for value in (item.get("plot_url"), item.get("decode_plot_url"))
                     if value]
            artifacts = [{"label": label, "url": item.get(key)} for label, key in (
                ("Follow-up JSON", "followup_url"), ("Decode JSON", "decode_url"),
                ("Fingerprint JSON", "fingerprint_url")) if item.get(key)]
            manifest = item.get("capture_manifest") or self._json(
                self.beacon_root / "captures" / recording_id / "manifest.json", {})
            fingerprint_plot = (self.beacon_root / "reports" / "fingerprints" /
                                f"{recording_id}.png")
            return {"kind": kind, "recording_id": recording_id, "active": False,
                    "statistics": {**item,
                        "fingerprint_plot_url": (f"/beacon-fingerprint-plots/"
                            f"{recording_id}.png" if fingerprint_plot.is_file() else None),
                        "radio_parameters": capture_radio_parameters(manifest)},
                    "plots": plots, "artifacts": artifacts}
        if kind == "sweep":
            item = next((row for row in self.waterfalls(limit=100).get("waterfalls", [])
                if (row.get("capture_id") or f"chunk-{int(row['chunk']):05d}") == recording_id),
                None)
            if item is None:
                return None
            plots = [value for value in (item.get("plot_url"), item.get("tracker_plot_url"),
                item.get("wide_plot_url"), item.get("carrier_zoom_url"),
                item.get("survey_plot_url")) if value]
            artifacts = ([{"label": "Structured observation JSON",
                           "url": item["structured_observation_url"]}]
                         if item.get("structured_observation_url") else [])
            channel = item.get("channel") or {}
            radio_parameters = {"tuning": {
                    "if_center_hz": channel.get("if_center_hz"),
                    "lnb_lo_hz": channel.get("lnb_lo_hz"),
                    "rf_center_hz": channel.get("rf_center_hz"),
                    "sample_rate_hz": item.get("sample_rate_hz"),
                    "rf_bandwidth_hz": item.get("bandwidth_hz")},
                "receivers": {"gain_mode_requested": item.get("gain_mode"),
                    "configured_manual_gain_db": item.get("configured_gain_db")}}
            return {"kind": kind, "recording_id": recording_id, "active": False,
                    "statistics": {**item, "radio_parameters": radio_parameters},
                    "plots": plots, "artifacts": artifacts}
        return None

    def snapshot(self, now: datetime | None = None) -> dict:
        # A browser tab can issue another refresh before a cold snapshot has
        # finished. Coalesce those requests so they cannot start parallel scans
        # of the same on-disk history and consume a CPU core indefinitely.
        if now is not None:
            return {"status": self.status(now), "expected": self.expected_passes(now),
                    "detected": self.detections(), "logs": self.logs(),
                    "waterfalls": self.waterfalls(), "dither": self.dither_comparisons(),
                    "beacon": self.beacon()}
        with self._snapshot_cache_lock:
            monotonic = time.monotonic()
            if (self._snapshot_cache is not None and
                    monotonic - self._snapshot_cache_created < 4.0):
                return self._snapshot_cache
            result = {"status": self.status(), "expected": self.expected_passes(),
                      "detected": self.detections(), "logs": self.logs(),
                      "waterfalls": self.waterfalls(), "dither": self.dither_comparisons(),
                      "beacon": self.beacon()}
            self._snapshot_cache = result
            self._snapshot_cache_created = time.monotonic()
            return result


LEGACY_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LEO Tracker</title><style>
:root{color-scheme:dark;--bg:#071018;--panel:#0d1a24;--line:#223646;--text:#dcebf4;--muted:#8ba4b4;--cyan:#3ed5d7;--amber:#ffbf69;--red:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#102b39 0,transparent 33%),var(--bg);color:var(--text);font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:1500px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:end;gap:16px}h1{font:600 30px/1.1 system-ui;margin:0}h2{font:600 17px system-ui;margin:0 0 14px}.muted{color:var(--muted)}.live{color:var(--cyan)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.card,.panel{background:linear-gradient(145deg,#10202b,#0a151e);border:1px solid var(--line);border-radius:12px;padding:16px}.metric{font-size:27px;margin-top:8px}.bar{height:8px;background:#182b38;border-radius:9px;overflow:hidden;margin-top:10px}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),#5b8cff)}
.columns{display:grid;grid-template-columns:1.35fr .65fr;gap:12px}.detections{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.candidate img{width:100%;border-radius:7px;border:1px solid var(--line);margin-top:10px}.candidate strong{color:var(--amber)}
.waterfalls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.waterfall img{width:100%;border-radius:7px;border:1px solid var(--line);margin-top:10px}.waterfall strong{color:var(--cyan)}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}th{color:var(--muted)}.log{padding:7px 0;border-bottom:1px solid var(--line);font-size:12px}.yes{color:var(--amber)}
.fingerprint-map{display:block;width:100%;min-height:420px;margin-top:10px;border:1px solid var(--line);border-radius:10px;background:#091722}
@media(max-width:950px){.grid{grid-template-columns:repeat(2,1fr)}.columns{grid-template-columns:1fr}.detections,.waterfalls{grid-template-columns:1fr}}@media(max-width:560px){main{padding:14px}.grid{grid-template-columns:1fr 1fr}}
</style></head><body><main>
<div class="top"><div><h1>LEO / Starlink tracker</h1><div class="muted">Dual-LNB Ku-band observation console</div></div><div id="stamp" class="muted">loading…</div></div>
<section class="grid"><div class="card"><div class="muted">WATCHER</div><div id="state" class="metric">—</div><div id="age" class="muted"></div></div>
<div class="card"><div class="muted">WALL WINDOW</div><div id="progress" class="metric">—</div><div class="bar"><i id="progressbar"></i></div></div>
<div class="card"><div class="muted">RETAINED IQ</div><div id="coverage" class="metric">—</div><div class="bar"><i id="coveragebar"></i></div></div>
<div class="card"><div class="muted">CANDIDATES</div><div id="count" class="metric">—</div><div id="chunks" class="muted"></div></div></section>
<section class="panel" style="margin-bottom:12px"><h2>Exact Starlink beacon receiver</h2><div class="muted">Published PSS + 2026 edge-pilot codes · dual-RX controls · TLE-blind acquisition</div><div id="beacon"></div></section>
<section class="panel" style="margin-bottom:12px"><h2>Randomized gain experiment</h2><div class="muted">Independent 50/50 assignment per capture: manual 50 dB control versus slow-attack AGC. Wait for balanced analyzed groups before choosing a winner.</div><div id="gainexperiment"></div></section>
<section class="panel" style="margin-bottom:12px"><h2>Waveform fingerprint clusters</h2><div id="fingerprintsummary" class="muted">Loading fingerprint index…</div><object id="fingerprintplot" class="fingerprint-map" type="image/svg+xml" data="/beacon-fingerprint-map.svg">Fingerprint visualization unavailable.</object></section>
<section class="panel" style="margin-bottom:12px"><h2>Tuning-dither validation</h2><div class="muted">Sky-fixed features move in baseband when the Pluto tuning center changes; receiver spurs do not.</div><div id="dither"></div></section>
<section class="panel" style="margin-bottom:12px"><h2>Recent time waterfalls</h2><div class="muted">All completed chunks, newest first. Time is vertical; frequency is horizontal.</div><div id="waterfalls" class="waterfalls"></div></section>
<div class="columns"><section class="panel"><h2>Detected Doppler candidates</h2><div class="muted" id="speednote"></div><div id="detections" class="detections"></div></section>
<aside><section class="panel"><h2>Expected passes</h2><div id="passes"></div></section><section class="panel" style="margin-top:12px"><h2>Recent analysis log</h2><div id="logs"></div></section></aside></div>
</main><script>
const f=(x,n=2)=>Number.isFinite(x)?x.toFixed(n):'—'; const when=s=>s?new Date(s).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
let waterfallSignature='',refreshing=false;
async function refresh(){if(refreshing)return;refreshing=true;try{const d=await fetch('/api/snapshot',{cache:'no-store'}).then(r=>r.json()),s=d.status;
document.querySelector('#stamp').textContent='updated '+new Date().toLocaleTimeString();document.querySelector('#state').innerHTML='<span class="live">'+s.state.toUpperCase()+'</span>';document.querySelector('#age').textContent=f(s.update_age_s,0)+' s since completed chunk';
document.querySelector('#progress').textContent=f(100*s.progress_fraction,1)+'%';document.querySelector('#progressbar').style.width=(100*s.progress_fraction)+'%';document.querySelector('#coverage').textContent=f(s.retained_sample_hours*60,1)+' min';document.querySelector('#coveragebar').style.width=(100*s.retained_sample_fraction)+'%';document.querySelector('#count').textContent=s.detection_count;document.querySelector('#chunks').textContent=s.completed_chunks+' chunks · '+f(s.analyzed_span_hours,2)+' h wall span · '+f(s.frequency_bin_width_hz/1000,2)+' kHz/bin · baseline '+(s.resolution_baseline_ready?'ready':'warming')+(s.pending_wide_analysis_chunks?' · '+s.pending_wide_analysis_chunks+' pending wide':'');
const bcn=d.beacon||{},cal=bcn.calibration||{},cm=bcn.active_acquisition_method||'coherent_grid_v1',cms=(cal.acquisition_methods||{})[cm]||cal.modes||{},cn=cms.narrow||{},cw=cms.wide||{},fps=bcn.fingerprints||{};document.querySelector('#beacon').innerHTML=!bcn.enabled?'<span class="muted">Beacon storage is not configured.</span>':`${bcn.active?`<div class="log"><span class="live">${(bcn.active.stage||'active').toUpperCase()}</span> ${bcn.active.name} · IF ${f(bcn.active.if_center_hz/1e6,3)} MHz · Ku ${f(bcn.active.rf_center_hz/1e9,6)} GHz</div>`:''}<div class="log">Detector ${cm} · recent exact candidates ${bcn.candidate_count} · qualified ${bcn.qualified_count}${Number.isFinite(cn.check_count)?' · method-specific null narrow/wide '+cn.check_count+'/'+cw.check_count+' checks · p99 '+f(cn.match_margin_quantiles.p99,4)+'/'+f(cw.match_margin_quantiles.p99,4):''} · waveform fingerprints ${fps.count||0} · linked ${fps.linked_count||0} · largest family ${fps.largest_cluster_size||0} · unresolved low-confidence ${fps.unresolved_count||0}${fps.index_url?' · <a href="'+fps.index_url+'" target="_blank">index JSON</a>':''}</div>`+bcn.captures.slice(0,8).map(x=>{const e=(x.exact_checks||[])[0]||{},t=x.stream_timing||{},single=x.single_receiver_candidate_count||0,label=x.followup_confirmed?'TEMPORALLY CONFIRMED':x.qualified_count?'QUALIFIED':x.candidate_count?'DUAL CANDIDATE':single?'single-RX follow-up':'control rejected',obs=Number.isFinite(t.sample_time_s)&&Number.isFinite(t.wall_span_s)?100*t.sample_time_s/t.wall_span_s:null,wide=(x.acquisition_span_hz||0)>0,link=x.strongest_confirmed_link||{},passes=x.overlapping_passes||[],dec=x.decode||{},soft=((dec.soft_dual_rx||{}).pilot||{}),mode=x.observation_mode||'narrow',match=(x.fingerprint_nearest_matches||[])[0]||{};return `<div class="log"><strong>${x.name}</strong> · <span class="${x.candidate_count||single||x.followup_confirmed?'yes':'muted'}">${label}</span><br>ch ${x.channel_number} ${x.region} · ${mode==='oversample'?'5 MS/s oversample':wide?'wide acquisition':'narrow lock'} · detector ${x.exact_acquisition_method||'coherent_grid_v1'} · IF ${f(x.if_center_hz/1e6,3)} MHz · ${f(x.sample_rate_hz/1e6,2)} MS/s · RF BW ${f(x.bandwidth_hz/1e6,2)} MHz · ${x.gain_mode}${Number.isFinite(x.configured_gain_db)?' '+f(x.configured_gain_db,1)+' dB':''}${Number.isFinite(obs)?' · observed/wall '+f(obs,1)+'%':''}${Number.isFinite(x.exact_temporal_coverage_fraction)?' · exact replay '+f(100*x.exact_temporal_coverage_fraction,2)+'%':''}${wide?' · search ±'+f(x.acquisition_span_hz/1e6,2)+' MHz':''}${x.followup_trigger_count?' · dense replay '+x.followup_check_count+' checks':''}${x.followup_confirmed?' · '+(x.dual_receiver_confirmed?'dual-RX Doppler':x.cross_receiver_confirmed?'cross-RX':'same-RX')+' confirmation · '+x.confirmed_link_count+' link(s) · strongest drift '+f(link.drift_hz_s/1000,2)+' kHz/s · '+x.overlapping_pass_count+' overlapping Starlink passes':''}${x.followup_url?` · <a href="${x.followup_url}" target="_blank">follow-up JSON</a>`:''}${x.decode_url?`<br><span class="yes">DECODED</span> · held-out pilots ${f(100*dec.minimum_pilot_accuracy,1)}%${Number.isFinite(soft.hard_symbol_accuracy)?' · soft dual-RX '+f(100*soft.hard_symbol_accuracy,1)+'% / confidence '+f(100*soft.soft_mean_confidence,1)+'%':''} · narrow SSS ${f(100*dec.minimum_sss_accuracy,1)}% · ${dec.minimum_frame_count} frames · <a href="${x.decode_url}" target="_blank">decode JSON</a>`:''}${x.fingerprint_url?`<br><span class="live">FINGERPRINT</span> · family ${x.fingerprint_cluster_id} (${x.fingerprint_cluster_size})${match.capture_name?' · nearest '+match.capture_name+' · waveform '+f(100*match.waveform_family_similarity,1)+'% · conditional channel '+f(100*match.conditional_channel_similarity,1)+'%':''} · <a href="${x.fingerprint_url}" target="_blank">signature JSON</a>`:''}${passes.length?'<br><span class="muted">top pass compatibility:</span> '+passes.map(p=>p.name+' ('+f(p.culmination_elevation_deg,1)+'°)').join(', '):''}<br>joint match margin ${(e.matched_margins||[]).map(v=>f(v,4)).join(' / ')} · symbolwise margin ${(e.pilot_margins||[]).map(v=>f(v,4)).join(' / ')} · PSS ${(e.pss_ratios||[]).map(v=>f(v,2)).join(' / ')} · epoch Δ ${f(e.epoch_difference_samples,1)} samples · CFO Δ ${f(e.cfo_difference_hz/1000,1)} kHz${(e.selected_subband_offsets_hz||[]).some(Number.isFinite)?' · selected bank '+e.selected_subband_offsets_hz.map(v=>f(v/1e6,2)).join(' / ')+' MHz':''}${x.plot_url?`<a href="${x.plot_url}" target="_blank"><img loading="lazy" style="max-width:100%;margin-top:8px" src="${x.plot_url}"></a>`:''}${x.decode_plot_url?`<a href="${x.decode_plot_url}" target="_blank"><img loading="lazy" style="max-width:100%;margin-top:8px" src="${x.decode_plot_url}"></a>`:''}</div>`}).join('');
const ge=bcn.gain_experiment||{},groups=ge.groups||{},gainRow=(label,g)=>{g=g||{};const hg=g.median_hardware_gain_db||[],rms=g.median_rms_magnitude||[],clip=g.maximum_near_full_scale_fraction||[];return `<tr><td>${label}</td><td>${g.capture_count||0} / ${g.analyzed_count||0}</td><td>${g.confirmed_count||0} (${Number.isFinite(g.confirmation_rate)?f(100*g.confirmation_rate,1)+'%':'—'})</td><td>${Number.isFinite(g.median_pilot_accuracy)?f(100*g.median_pilot_accuracy,1)+'%':'—'}</td><td>${Number.isFinite(g.median_pilot_confidence)?f(100*g.median_pilot_confidence,1)+'%':'—'}</td><td>${f(g.median_pilot_evm,3)}</td><td>${f(hg[0],1)} / ${f(hg[1],1)} dB</td><td>${f(rms[0],1)} / ${f(rms[1],1)}</td><td>${f(100*Math.max(clip[0]||0,clip[1]||0),4)}%</td></tr>`};document.querySelector('#gainexperiment').innerHTML=!ge.schema?'<p class="muted">Waiting for the first randomized capture.</p>':`<div class="log">Experiment ${ge.experiment_ids.join(', ')} · ${ge.randomized_capture_count} randomized captures · decision ${ge.decision_guidance.ready?'<span class="yes">sample-size ready</span>':'waiting for ≥30 analyzed captures per group'}</div><table><thead><tr><th>Mode</th><th>captures / analyzed</th><th>confirmed</th><th>pilot accuracy</th><th>confidence</th><th>EVM ↓</th><th>gain RX0/RX1</th><th>RMS RX0/RX1</th><th>max near-FS</th></tr></thead><tbody>${gainRow('manual 50 dB',groups.manual)}${gainRow('slow-attack AGC',groups.slow_attack)}</tbody></table>`;
const fpSignature=[fps.count,fps.linked_count,fps.largest_cluster_size,fps.unresolved_count].join('-'),fpPlot=document.querySelector('#fingerprintplot');document.querySelector('#fingerprintsummary').innerHTML=`Nearest-neighbor evidence: ${fps.count||0} fingerprints · ${fps.linked_count||0} linked · largest repeated family ${fps.largest_cluster_size||0} · ${fps.unresolved_count||0} unresolved. X measures pilot/SSS repetition; Y includes the fixed LNB/RX path. This is not satellite identity.${fps.index_url?' <a href="'+fps.index_url+'" target="_blank">Open comparison index</a>':''}`;if(fpPlot.dataset.signature!==fpSignature){fpPlot.dataset.signature=fpSignature;fpPlot.data='/beacon-fingerprint-map.svg?v='+encodeURIComponent(fpSignature)}
document.querySelector('#dither').innerHTML=(d.dither.comparisons||[]).slice(0,6).map(x=>`<div class="log"><strong>Chunk ${x.chunk}</strong> · ${x.classification} · ${f(x.tuning_dither_hz/1e6,3)} MHz dither · receivers ${x.receivers_agree?'agree':'disagree'}<br>${x.receivers.map(r=>'RX'+r.receiver+' sky/baseband '+f(r.sky_fixed_correlation,3)+' / '+f(r.baseband_fixed_correlation,3)).join(' · ')}</div>`).join('')||'<span class="muted">Waiting for the first nominal/dither pair.</span>';
const nextWaterfallSignature=d.waterfalls.waterfalls.map(x=>x.plot_url+':'+x.qualified_event_count+':'+x.tle_guided_candidate_count+':'+x.blind_comb_candidate_count+':'+x.blind_carrier_candidate_count+':'+x.wide_feature_candidate_count).join('|');
if(nextWaterfallSignature!==waterfallSignature){waterfallSignature=nextWaterfallSignature;document.querySelector('#waterfalls').innerHTML=d.waterfalls.waterfalls.map(x=>{
const b=x.best_tle_candidate,q=x.best_blind_comb_candidate,c=x.best_blind_carrier_candidate,w=x.best_wide_feature_candidate,sp=(x.blind_spacing_scan||[])[0];
const best=b?`<br><span class="muted">best TLE</span> ${b.name} · ${b.signal_model||'single-tone'} · <span class="muted">stationary margin</span> ${f(b.stationary_improvement_db,3)} dB · <span class="muted">RX corr</span> ${f(b.trace_correlation,2)}<br><span class="muted">rejected:</span> ${(b.rejection_reasons||[]).join('; ')||'none'}`:'';
const blind=q?`<br><span class="muted">RF-blind comb</span> ${q.qualified?'qualified':'rejected'} · ${f(q.receivers[0].fitted_drift_hz_s/1000,2)} / ${f(q.receivers[1].fitted_drift_hz_s/1000,2)} kHz/s · <span class="muted">path corr</span> ${f(q.path_correlation,2)}${sp?' · best exploratory spacing '+f(sp.spacing_hz/1000,1)+' kHz':''}<br><span class="muted">rejected:</span> ${(q.rejection_reasons||[]).join('; ')||'none'}`:'';
const carrier=c?`<br><span class="muted">RF-blind carrier</span> ${c.qualified?'qualified':'rejected'} · ${f(c.receivers[0].fitted_drift_hz_s/1000,2)} / ${f(c.receivers[1].fitted_drift_hz_s/1000,2)} kHz/s · <span class="muted">path corr</span> ${f(c.path_correlation,2)} · <span class="muted">FAP</span> ${f(c.empirical_false_alarm_probability,3)}<br><span class="muted">rejected:</span> ${(c.rejection_reasons||[]).join('; ')||'none'}`:'';
const wide=w?(()=>{const t=(w.tle_comparisons||[])[0],label=w.orbital_shape_qualified?'orbital shape qualified':w.leo_like_qualified?'moving RF · LEO-rate compatible · curvature unproven':w.doppler_candidate_qualified?'dual-RX Doppler candidate · orbital interpretation unproven':(w.moving_rf_qualified??w.leo_like_qualified)?'centroid motion only · internal translation unconfirmed':'rejected',tr=w.receivers.map(x=>x.internal_translation_drift_hz_s/1000);return `<br><span class="muted">wide feature</span> ${label} · ${w.polarity} · ${f(w.duration_s,1)} s × ${f(w.bounding_width_hz/1000,0)} kHz · <span class="muted">RX path corr</span> ${f(w.receiver_path_correlation,2)} · <span class="muted">centroid drift</span> ${f(w.receivers[0].linear_drift_hz_s/1000,2)} / ${f(w.receivers[1].linear_drift_hz_s/1000,2)} kHz/s${tr.every(Number.isFinite)?` · <span class="muted">internal-pattern drift</span> ${f(tr[0],2)} / ${f(tr[1],2)} kHz/s · corr ${f(w.internal_translation_path_correlation,2)} · ${w.internal_translation_consistent?'consistent':'not consistent'}`:''} · <span class="muted">TLEs within 1 bin</span> ${w.tles_within_one_bin_of_best||0}${w.specific_tle_identifiable?' · specific TLE':' · no unique TLE'}${t?` · <span class="muted">best TLE / affine RMS</span> ${f(t.rms_error_hz/1000,2)} / ${f(t.affine_drift_rms_error_hz/1000,2)} kHz · <span class="muted">predicted curvature</span> ${f(w.best_tle_curvature_resolution_bins,2)} bins`:''}${(w.measurement_warnings||[]).length?`<br><span class="muted">measurement warnings:</span> ${w.measurement_warnings.join('; ')}`:''}<br><span class="muted">orbital-shape rejected:</span> ${(w.orbital_shape_rejection_reasons||w.rejection_reasons||[]).join('; ')||'none'}`})():'';
const wideImage=x.wide_plot_url?`<a href="${x.wide_plot_url}" target="_blank"><img loading="lazy" src="${x.wide_plot_url}"></a>`:'';
const trackerImage=x.tracker_plot_url?`<a href="${x.tracker_plot_url}" target="_blank"><img loading="lazy" src="${x.tracker_plot_url}"></a>`:'';
const surveyImage=x.survey_plot_url?`<figure class="survey"><figcaption class="muted">pre-dwell survey \u00b7 all eight low-band edge tunings, both receivers</figcaption><a href="${x.survey_plot_url}" target="_blank"><img loading="lazy" src="${x.survey_plot_url}"></a></figure>`:'';
const dither=x.interleaved_dither?` · <span class="muted">rapid dither</span> ${x.interleaved_dither.classification||'active'}`:'';
const so=x.structured_observation_summary||{},structured=x.structured_observation_url?`<br><a href="${x.structured_observation_url}" target="_blank">structured observation JSON</a> · validated ${so.accepted_track_count||0}/${so.track_count||0} · processed as Doppler ${so.processed_as_doppler_count||0} · boundary sky/baseband/ambiguous ${so.sky_fixed_count||0}/${so.baseband_fixed_count||0}/${so.ambiguous_boundary_count||0}`:'';
const tq=x.tracker_qualified_count_by_tracker||{},tracker=x.tracker_ensemble_available?`<br><span class="muted">tracker ensemble</span> ${Object.entries(tq).map(([k,v])=>k.replace('/v1','')+': '+v).join(' · ')} · paired ${x.tracker_qualified_joint_count}/${x.tracker_joint_count} · TLE compatible ${x.tracker_tle_compatible_count} · specific IDs ${x.tracker_qualified_tle_identification_count}${x.coherent_analysis_available?' · coherent IQ '+x.coherent_block_count+' blocks'+(x.coherent_joint_track?.qualified?' · paired '+f(x.coherent_joint_track.mean_drift_hz_s/1000,2)+' kHz/s':''):''}`:'';
return `<article class="card waterfall"><strong>${x.capture_id||('Chunk '+x.chunk)}</strong> · ${when(x.start_utc)} · <span class="muted">mode</span> ${x.observation_mode||'fixed'}<br><span class="muted">IF</span> ${x.channel?f(x.channel.if_center_hz/1e6,3)+' MHz':'—'} · <span class="muted">gain control</span> ${x.gain_mode||'—'}${Number.isFinite(x.configured_gain_db)?' '+f(x.configured_gain_db,1)+' dB':''} · <span class="muted">sample rate</span> ${Number.isFinite(x.sample_rate_hz)?f(x.sample_rate_hz/1e6,2)+' MS/s':'—'} · <span class="muted">RF bandwidth</span> ${Number.isFinite(x.bandwidth_hz)?f(x.bandwidth_hz/1e6,2)+' MHz':'—'} · <span class="muted">events RX0/RX1</span> ${x.event_counts?x.event_counts.join(' / '):'—'} · <span class="muted">raw joint</span> ${x.joint_event_count} · <span class="muted">Doppler observations</span> ${x.doppler_observation_count} · <span class="muted">sky-fixed by dither</span> ${x.sky_fixed_doppler_count||0} · <span class="muted">identified</span> ${x.qualified_event_count} · <span class="muted">TLE-integrated</span> ${x.tle_guided_candidate_count}${dither} · <span class="muted">comb/carrier/wide</span> ${x.blind_comb_candidate_count}/${x.blind_carrier_candidate_count}/${x.wide_feature_candidate_count}${carrier}${blind}${best}${wide}${structured}${tracker}<a href="${x.plot_url}" target="_blank"><img loading="lazy" src="${x.plot_url}"></a>${trackerImage}${wideImage}${surveyImage}</article>`}).join('')||'<p class="muted">No completed waterfalls yet.</p>'}
document.querySelector('#speednote').textContent=d.detected.speed_note;document.querySelector('#detections').innerHTML=d.detected.detections.slice(0,12).map(x=>{const p=x.tle_guided_candidate;const measurement=x.mean_drift_hz_s===null?(p?`<span class="muted">TLE path</span> ${p.name} · ${p.signal_model||'single-tone'} · <span class="muted">predicted span</span> ${f(p.predicted_doppler_span_hz/1000,1)} kHz<br><span class="muted">score / stationary gain</span> ${f(p.joint_score_db,3)} / ${f(p.stationary_improvement_db,3)} dB`:'measured drift unavailable'):`<span class="muted">drift</span> ${f(x.mean_drift_hz_s/1000,2)} kHz/s · <span class="muted">radial a</span> ${f(x.radial_acceleration_m_s2,1)} m/s²<br><span class="muted">Δ radial v</span> ${f(x.equivalent_radial_velocity_change_m_s/1000,2)} km/s`;return `<article class="card candidate"><strong>Chunk ${x.chunk}</strong> · ${when(x.start_utc)}<br>${measurement} · ${x.overlapping_expected_passes} TLE overlaps<br>${x.best_tle_match?'<span class="muted">matched pass</span> '+x.best_tle_match.name+' ('+f(x.best_tle_match.max_elevation_deg,1)+'°)':''}<a href="${x.plot_url}" target="_blank"><img loading="lazy" src="${x.plot_url}"></a></article>`}).join('')||'<p class="muted">No promoted candidates yet.</p>';
document.querySelector('#passes').innerHTML='<table><tr><th>Satellite</th><th>Rise</th><th>El.</th><th>LOS speed</th></tr>'+d.expected.passes.slice(0,12).map(p=>`<tr><td>${p.name}</td><td>${when(p.rise_utc)}</td><td>${f(p.max_elevation_deg,0)}°</td><td>${f(Math.max(Math.abs(p.rise_range_rate_km_s),Math.abs(p.set_range_rate_km_s)),2)} km/s</td></tr>`).join('')+'</table>';
document.querySelector('#logs').innerHTML=d.logs.events.slice(0,18).map(e=>`<div class="log"><span class="muted">${when(e.time_utc)}</span> chunk ${e.chunk} <span class="${e.detected?'yes':''}">${e.detected?'candidate':'rejected'}</span><br>${e.drift_hz_s.map(x=>f(x/1000,2)).join(' / ')} kHz/s</div>`).join('');
}catch(e){document.querySelector('#state').textContent='OFFLINE';console.error(e)}finally{refreshing=false}}refresh();setInterval(refresh,5000);
</script></body></html>'''


INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LEO Tracker Recordings</title><style>
:root{color-scheme:dark;--bg:#071018;--panel:#0d1a24;--line:#223646;--text:#dcebf4;--muted:#8ba4b4;--cyan:#3ed5d7;--amber:#ffbf69}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:1900px;margin:auto;padding:20px}.top{display:flex;align-items:end;justify-content:space-between;gap:12px;margin-bottom:16px}h1{font:600 27px/1.1 system-ui;margin:0}.muted{color:var(--muted)}.live{color:var(--cyan)}.yes{color:var(--amber)}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}table{width:100%;border-collapse:collapse;white-space:nowrap}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}th{position:sticky;top:0;background:#10202b;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}tbody tr{cursor:pointer}tbody tr:hover{background:#142936}tbody tr:last-child td{border-bottom:0}a{color:var(--cyan);text-decoration:none}.empty{text-align:center;color:var(--muted);padding:30px}
@media(max-width:700px){main{padding:12px}.top{align-items:start;flex-direction:column}h1{font-size:23px}}
</style></head><body><main>
<div class="top"><div><h1>LEO / Starlink recordings</h1><div class="muted">Click any recording for statistics, artifacts, and plots. “Beacons detected” counts distinct confirmed time intervals, not unique satellite identities.</div><div id="totals" class="muted">Counting beacon observations…</div></div><div id="stamp" class="muted">loading…</div></div>
<section class="panel"><h2>LNB health</h2><div class="muted" style="margin-bottom:10px">Each port is scored on what it independently saw, over probes rather than recordings. A dual-receiver confirmation needs both ports, so one degraded chain suppresses the pair even when its twin is healthy. Input level is referred back through the gain, because AGC regulates output and would otherwise hide a hot or dead front end.</div><div class="table-wrap"><table id="lnb"><thead><tr><th>Position / label</th><th>Radio</th><th>Probes 24 h</th><th>Detections</th><th>Detection rate</th><th>Qualified</th><th>Referred input</th><th>Max near-full-scale</th></tr></thead><tbody><tr><td colspan="8" class="empty">Loading…</td></tr></tbody></table></div></section><section class="panel"><h2>Beacons by channel</h2><div class="muted" style="margin-bottom:10px">Each channel carries two edge pilots 230.625 MHz apart; a 2.5 MHz capture reaches only one, so they are surveyed separately.</div><div class="table-wrap"><table id="tuning"><thead><tr><th rowspan="2">Channel / edge</th><th rowspan="2">IF</th><th colspan="4">past 6 h</th><th colspan="4">past 12 h</th><th colspan="4">past 24 h</th></tr><tr><th>Duty</th><th>Recordings</th><th>Beacons</th><th>Satellites</th><th>Duty</th><th>Recordings</th><th>Beacons</th><th>Satellites</th><th>Duty</th><th>Recordings</th><th>Beacons</th><th>Satellites</th></tr></thead><tbody><tr><td colspan="14" class="empty">Loading…</td></tr></tbody></table></div></section><section class="panel"><h2>Recent activity</h2><div class="muted" style="margin-bottom:10px">Duty is recorded RF time over wall-clock time. A dual-receiver capture occupies both of its LNB ports, so it counts in full against each.</div><div class="table-wrap"><table id="activity"><thead><tr><th rowspan="2">Scope</th><th colspan="4">past 6 h</th><th colspan="4">past 12 h</th><th colspan="4">past 24 h</th></tr><tr><th>Duty</th><th>Recordings</th><th>Beacons</th><th>Satellites</th><th>Duty</th><th>Recordings</th><th>Beacons</th><th>Satellites</th><th>Duty</th><th>Recordings</th><th>Beacons</th><th>Satellites</th></tr></thead><tbody><tr><td colspan="13" class="empty">Loading…</td></tr></tbody></table></div></section><div class="table-wrap"><table id="recordings"><thead><tr><th>Recorded</th><th>Radio</th><th>Status</th><th>Mode</th><th>Channel</th><th>IF</th><th>Sample rate</th><th>RF BW</th><th>Gain</th><th>Duration</th><th>Dual / single candidates</th><th>Beacons detected</th><th>Strongest drift</th><th>TLE overlaps</th><th>Satellite</th><th>Fit ST / HF</th><th>Confirmed</th><th>Decoded</th><th>Frames</th><th>Pilot accuracy</th><th>Confidence</th><th>EVM</th><th>Fingerprint family</th></tr></thead><tbody><tr><td colspan="23" class="empty">Loading recordings…</td></tr></tbody></table></div>
</main><script>
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const f=(v,n=2)=>Number.isFinite(v)?Number(v).toFixed(n):'—';
const when=v=>v?new Date(v).toLocaleString([],{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
let refreshing=false;
async function refresh(){if(refreshing)return;refreshing=true;try{const d=await fetch('/api/recordings',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error(r.status);return r.json()});const rows=d.recordings||[];
document.querySelector('#stamp').textContent='updated '+new Date().toLocaleTimeString()+' · '+rows.length+' recordings';
const s=d.summary||{};document.querySelector('#totals').textContent=`${s.analyzed_capture_count||0} capture windows analyzed · ${s.temporally_confirmed_capture_count||0} temporally confirmed beacon observations · ${s.decoded_capture_count||0} decoded · ${s.retained_iq_capture_count||0} IQ captures retained · ${s.fingerprint_count||0} fingerprints`;
const act=d.activity||{},pct=v=>Number.isFinite(v)?f(100*v,1)+'%':'\u2014',cells=w=>{const g=w||{};return `<td>${pct(g.duty_fraction)}</td><td>${g.recordings??0}</td><td>${g.beacons_detected??0}</td><td>${g.satellites_tracked??(g.qualified_associations??0)}</td>`},actRow=(label,g,cls)=>`<tr class="${cls||''}"><td>${esc(label)}</td>${cells((g||{})['6h'])}${cells((g||{})['12h'])}${cells((g||{})['24h'])}</tr>`;const IFMHZ={'ch1-lower':959.6875,'ch1-upper':1190.3125,'ch2-lower':1209.6875,'ch2-upper':1440.3125,'ch3-lower':1459.6875,'ch3-upper':1690.3125,'ch4-lower':1709.6875,'ch4-upper':1940.3125};const bln=act.by_lnb||{},lrow=(k,g)=>{const w=(g||{})['24h']||{},dr=w.detection_rate,ri=w.referred_input_dbfs,nf=w.maximum_near_full_scale;return `<tr><td><strong>${esc(w.position||'\u2014')}</strong> <span class="muted">${esc(k)}</span></td><td>${esc(w.radio_id||(rowsRadio[k]||'\u2014'))}</td><td>${w.probe_count??0}</td><td>${w.receiver_candidate_count??0}</td><td class="${Number.isFinite(dr)&&dr>0.04?'yes':''}">${Number.isFinite(dr)?f(100*dr,2)+'%':'\u2014'}</td><td>${w.receiver_qualified_count??0}</td><td>${Number.isFinite(ri)?f(ri,1)+' dBFS':'\u2014'}</td><td>${Number.isFinite(nf)?f(100*nf,3)+'%':'\u2014'}</td></tr>`};const rowsRadio={};(d.recordings||[]).forEach(x=>(x.receiver_labels||[]).forEach(l=>{if(x.radio_id)rowsRadio[l]=x.radio_id}));document.querySelector('#lnb tbody').innerHTML=Object.keys(bln).length?Object.entries(bln).map(([k,g])=>lrow(k,g)).join(''):'<tr><td colspan="8" class="empty">No LNB activity yet.</td></tr>';
const bt=act.by_tuning||{};document.querySelector('#tuning tbody').innerHTML=Object.keys(bt).length?Object.entries(bt).map(([k,g])=>`<tr><td>${esc(k)}</td><td>${IFMHZ[k]!==undefined?f(IFMHZ[k],4)+' MHz':'\u2014'}</td>${cells((g||{})['6h'])}${cells((g||{})['12h'])}${cells((g||{})['24h'])}</tr>`).join(''):'<tr><td colspan="14" class="empty">No channel activity yet.</td></tr>';
if(act.overall){document.querySelector('#activity tbody').innerHTML=actRow('All receivers',{'6h':act.overall['6h'],'12h':act.overall['12h'],'24h':act.overall['24h']},'live')+Object.entries(act.by_radio||{}).map(([k,v])=>actRow(k,v)).join('')+Object.entries(act.by_lnb||{}).map(([k,v])=>actRow('\u2003'+k,v)).join('')}
document.querySelector('#recordings tbody').innerHTML=rows.length?rows.map(x=>`<tr data-href="${esc(x.detail_url)}"><td>${esc(when(x.start_utc))}</td><td>${esc(x.radio_id||'—')}</td><td class="${x.status==='capturing'||x.status==='analyzing'?'live':x.confirmed?'yes':''}">${esc(x.status)}</td><td>${esc(x.mode)}</td><td>${esc(x.channel??'—')}${x.region?' '+esc(x.region):''}</td><td>${Number.isFinite(x.if_center_hz)?f(x.if_center_hz/1e6,3)+' MHz':'—'}</td><td>${Number.isFinite(x.sample_rate_hz)?f(x.sample_rate_hz/1e6,2)+' MS/s':'—'}</td><td>${Number.isFinite(x.bandwidth_hz)?f(x.bandwidth_hz/1e6,2)+' MHz':'—'}</td><td>${esc(x.gain)}</td><td>${Number.isFinite(x.duration_s)?f(x.duration_s,1)+' s':'—'}</td><td>${esc(x.dual_candidate_count)} / ${esc(x.single_receiver_candidate_count)}</td><td class="${x.beacon_detected_count?'yes':''}">${x.beacon_detected_count===null||x.beacon_detected_count===undefined?'—':esc(x.beacon_detected_count)}</td><td>${Number.isFinite(x.strongest_drift_hz_s)?f(x.strongest_drift_hz_s/1000,2)+' kHz/s':'—'}</td><td>${x.satellite_name?esc(x.satellite_name)+' ('+esc(x.satellite_norad_id)+')'+(x.source_identity_agreement===false?' <span class="muted">\u26a0 sources differ</span>':''):'\u2014'}</td><td>${(()=>{const s=x.source_fit_hz||{},a=s['space-track'],b=s['huggingface'];return (a===undefined&&b===undefined)?'\u2014':(a===undefined?'\u2014':f(a,1))+' / '+(b===undefined?'\u2014':f(b,1))+' Hz'})()}</td><td>${esc(x.tle_overlap_count)}</td><td>${x.confirmed===null?'—':x.confirmed?'yes':'no'}</td><td>${x.decoded===null?'—':x.decoded?'yes':'no'}</td><td>${esc(x.decode_frame_count)}</td><td>${Number.isFinite(x.pilot_accuracy)?f(100*x.pilot_accuracy,1)+'%':'—'}</td><td>${Number.isFinite(x.pilot_confidence)?f(100*x.pilot_confidence,1)+'%':'—'}</td><td>${Number.isFinite(x.pilot_evm)?f(x.pilot_evm,3):'—'}</td><td>${x.fingerprint_family?esc(x.fingerprint_family)+' ('+esc(x.fingerprint_family_size)+')':'—'}</td></tr>`).join(''):'<tr><td colspan="23" class="empty">No recordings available.</td></tr>';
document.querySelectorAll('tr[data-href]').forEach(row=>row.addEventListener('click',event=>{if(event.target.closest('a'))return;location.href=row.dataset.href}));
}catch(error){document.querySelector('#stamp').textContent='offline';console.error(error)}finally{refreshing=false}}
refresh();setInterval(refresh,10000);
</script></body></html>'''


DETAIL_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recording statistics</title><style>
:root{color-scheme:dark;--bg:#071018;--panel:#0d1a24;--line:#223646;--text:#dcebf4;--muted:#8ba4b4;--cyan:#3ed5d7;--amber:#ffbf69}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}main{max-width:1350px;margin:auto;padding:20px}a{color:var(--cyan);text-decoration:none}h1{font:600 26px/1.15 system-ui;margin:14px 0 4px}h2{font:600 17px system-ui;margin:0 0 12px}h3{font:600 14px system-ui;margin:0 0 8px;color:var(--cyan)}.muted{color:var(--muted)}.panel{margin-top:14px;padding:16px;background:var(--panel);border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}th{width:240px;color:var(--muted)}.radio-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.radio-card{padding:12px;border:1px solid var(--line);border-radius:9px;background:#091722}.radio-card th{width:48%}.plots{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.plots a{display:block}.plots img{display:block;width:100%;border:1px solid var(--line);border-radius:9px;background:#091722}pre{white-space:pre-wrap;word-break:break-word;max-height:520px;overflow:auto;color:#b9ceda}.artifact{display:inline-block;margin:4px 14px 4px 0}.error{color:var(--amber)}
@media(max-width:800px){main{padding:12px}.plots,.radio-grid{grid-template-columns:1fr}th{width:130px}}
</style></head><body><main><a href="/">← All recordings</a><h1 id="title">Recording statistics</h1><div id="subtitle" class="muted">loading…</div><section class="panel"><h2>Capture summary</h2><table><tbody id="summary"></tbody></table></section><section id="radiopanel" class="panel"><h2>Radio parameters</h2><div class="muted" style="margin-bottom:12px">Configured settings and hardware readback saved with this capture.</div><div id="radio" class="radio-grid"></div></section><section id="dopplerpanel" class="panel"><h2>Doppler and predicted-pass matching</h2><div id="doppler"></div></section><section id="fragmentpanel" class="panel"><h2>Fragment identity evidence</h2><div class="muted" style="margin-bottom:12px">Shadow diagnostic only: it compares same-satellite, discontinuity, and satellite-switch explanations without changing production qualification.</div><div id="fragment"></div></section><section id="detectorpanel" class="panel"><h2>Dual-receiver detector and decode evidence</h2><div id="detector"></div></section><section id="fingerprintpanel" class="panel"><h2>Temporal QPSK fingerprint</h2><div class="muted" style="margin-bottom:12px">Frame-by-frame soft probabilities for the known pilot and SSS states. This compares waveform evidence and confidence; it is not decoded user payload or a satellite identity by itself.</div><div id="fingerprintplot" class="plots"></div></section><section id="artifactpanel" class="panel"><h2>Structured artifacts</h2><div id="artifacts"></div></section><section id="plotpanel" class="panel"><h2>Raw signal and analysis plots</h2><div id="plots" class="plots"></div></section><details class="panel"><summary>Complete statistics JSON</summary><pre id="raw"></pre></details></main><script>
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl=v=>typeof v==='string'&&v.startsWith('/')?v:'#';const f=(v,n=2)=>Number.isFinite(v)?Number(v).toFixed(n):'—';
const parts=location.pathname.split('/').filter(Boolean),api='/api/recordings/'+encodeURIComponent(parts[1]||'')+'/'+encodeURIComponent(parts.slice(2).join('/')||'');
function value(v){if(v===null||v===undefined)return '—';if(typeof v==='boolean')return v?'yes':'no';if(typeof v==='object')return Array.isArray(v)?v.length+' item(s)':Object.keys(v).length+' field(s)';return String(v)}
const hz=(v,scale,suffix,n=3)=>Number.isFinite(v)?f(v/scale,n)+' '+suffix:null;
const bytes=v=>Number.isFinite(v)?f(v/1e9,3)+' GB':null;
function radioCard(title,rows){const present=rows.filter(([,v])=>v!==null&&v!==undefined);if(!present.length)return '';return `<section class="radio-card"><h3>${esc(title)}</h3><table><tbody>${present.map(([k,v])=>`<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join('')}</tbody></table></section>`}
function gainReadback(rows){return (rows||[]).map(x=>`RX${x.receiver}: ${Number.isFinite(x.minimum_db)?f(x.minimum_db,1):'—'} / ${Number.isFinite(x.median_db)?f(x.median_db,1):'—'} / ${Number.isFinite(x.maximum_db)?f(x.maximum_db,1):'—'} dB (min / median / max; n=${x.sample_count||0})`).join(' · ')||null}
function signalReadback(rows){return (rows||[]).map(x=>`RX${x.receiver}: RMS ${f(x.rms_magnitude,2)} · peak ${f(x.peak_abs_component,0)} · near-full-scale ${Number.isFinite(x.near_full_scale_fraction)?f(100*x.near_full_scale_fraction,6)+'%':'—'}`).join(' · ')||null}
async function load(){try{const d=await fetch(api,{cache:'no-store'}).then(r=>{if(!r.ok)throw Error(r.status);return r.json()}),s=d.statistics||{};document.title=d.recording_id+' · LEO Tracker';document.querySelector('#title').textContent=d.recording_id;document.querySelector('#subtitle').textContent=d.kind+' recording · '+(d.active?'capture in progress':'completed analysis');
const preferred=[['Recorded',s.created_utc||s.start_utc],['Radio',s.radio_id||s.radio_parameters?.hardware?.radio_id],['Radio serial',s.radio_serial||s.radio_parameters?.hardware?.serial],['Receiver / LNB mapping',(s.receiver_labels||s.radio_parameters?.receivers?.receiver_labels||[]).map((x,i)=>'RX'+i+': '+x).join(' · ')||null],['Status',s.stage||(d.active?'active':s.followup_confirmed?'confirmed':s.qualified_count?'qualified':s.candidate_count?'candidate':'analyzed')],['Mode',s.observation_mode],['Channel',s.channel_number],['Region',s.region],['IF center',Number.isFinite(s.if_center_hz)?f(s.if_center_hz/1e6,3)+' MHz':Number.isFinite(s.channel?.if_center_hz)?f(s.channel.if_center_hz/1e6,3)+' MHz':null],['Ku RF center',Number.isFinite(s.rf_center_hz)?f(s.rf_center_hz/1e9,6)+' GHz':null],['Sample rate',Number.isFinite(s.sample_rate_hz)?f(s.sample_rate_hz/1e6,2)+' MS/s':null],['RF bandwidth',Number.isFinite(s.bandwidth_hz)?f(s.bandwidth_hz/1e6,2)+' MHz':null],['Gain control',s.gain_mode],['Configured gain',Number.isFinite(s.configured_gain_db)?f(s.configured_gain_db,1)+' dB':null],['Duration',Number.isFinite(s.duration_s)?f(s.duration_s,1)+' s':Number.isFinite(s.wall_duration_s)?f(s.wall_duration_s,1)+' s':null],['Exact candidates',s.candidate_count],['Single-RX candidates',s.single_receiver_candidate_count],['Temporally confirmed',s.followup_confirmed],['Doppler observations',s.doppler_observation_count],['Fingerprint family',s.fingerprint_cluster_id],['Fingerprint family size',s.fingerprint_cluster_size]];
document.querySelector('#summary').innerHTML=preferred.filter(([,v])=>v!==null&&v!==undefined).map(([k,v])=>`<tr><th>${esc(k)}</th><td>${esc(value(v))}</td></tr>`).join('');
const rp=s.radio_parameters||{},t=rp.tuning||{},r=rp.receivers||{},g=rp.gain_experiment||{},h=rp.hardware||{},q=rp.signal||{},st=rp.stream||{},c=rp.capture||{};const cards=[radioCard('Tuning and bandwidth',[['IF / Pluto LO',hz(t.if_center_hz,1e6,'MHz')],['LNB local oscillator',hz(t.lnb_lo_hz,1e9,'GHz',6)],['Ku-band RF center',hz(t.rf_center_hz,1e9,'GHz',6)],['Complex sample rate',hz(t.sample_rate_hz,1e6,'MS/s',3)],['RF analog bandwidth',hz(t.rf_bandwidth_hz,1e6,'MHz',3)],['Sampled baseband span',Number.isFinite(t.sample_rate_hz)?'±'+f(t.sample_rate_hz/2e6,3)+' MHz':null],['Tuning basis',t.tuning_basis]]),radioCard('Receivers and gain',[['Receiver count',r.receiver_count],['Enabled channels',Array.isArray(r.enabled_channels)?r.enabled_channels.map(x=>'RX'+x).join(', '):null],['Receiver / LNB labels',Array.isArray(r.receiver_labels)?r.receiver_labels.map((x,i)=>'RX'+i+': '+x).join(' · '):null],['Requested gain mode',r.gain_mode_requested],['Gain-mode readback',Array.isArray(r.gain_mode_readback)?r.gain_mode_readback.map((x,i)=>'RX'+i+': '+x).join(' · '):null],['Configured manual gain',Number.isFinite(r.configured_manual_gain_db)?f(r.configured_manual_gain_db,1)+' dB':null],['Hardware gain readback',gainReadback(r.gain_readback_by_receiver)],['Gain telemetry cadence',Number.isFinite(r.gain_telemetry_interval_s)?f(r.gain_telemetry_interval_s,2)+' s':null],['Sample layout',r.layout]]),radioCard('Randomized gain experiment',[['Experiment',g.experiment_id],['Assigned mode',g.assigned_gain_mode],['AGC assignment probability',Number.isFinite(g.agc_assignment_probability)?f(100*g.agc_assignment_probability,1)+'%':null],['Random draw (uint32)',g.random_draw_u32],['AGC settling delay',Number.isFinite(g.agc_settle_s)?f(g.agc_settle_s,2)+' s':null]]),radioCard('Hardware and temperatures',[['Operator radio ID',h.radio_id],['Radio',h.kind],['Hardware model',h.hardware_model],['Firmware',h.firmware_version],['Radio kernel',h.kernel_version],['AD9361 model',h.ad9361_model],['XO correction',Number.isFinite(h.xo_correction_hz)?hz(h.xo_correction_hz,1e6,'MHz',6):h.xo_correction_hz],['Driver',h.implementation],['Transport',h.transport],['Configured URI',h.uri],['Resolved IIO URI',h.context_uri],['Serial',h.serial],['Pi temperature',Number.isFinite(h.host_temperature_c)?f(h.host_temperature_c,1)+' °C':null],['Pluto temperature',Number.isFinite(h.radio_temperature_c)?f(h.radio_temperature_c,1)+' °C':null]]),radioCard('Signal levels',[['ADC nominal full scale',q.adc_nominal_full_scale],['Near-full-scale threshold',q.near_full_scale_threshold],['Per-receiver measurements',signalReadback(q.receivers)],['Interpretation',q.note]]),radioCard('Stream continuity',[['RF sample time',Number.isFinite(st.sample_time_s)?f(st.sample_time_s,3)+' s':null],['Host wall span',Number.isFinite(st.wall_span_s)?f(st.wall_span_s,3)+' s':null],['Host read duty',Number.isFinite(st.host_read_duty_fraction)?f(100*st.host_read_duty_fraction,2)+'%':null],['IIO read count',st.read_count],['Total / maximum read time',Number.isFinite(st.total_read_duration_s)?f(st.total_read_duration_s,3)+' / '+f(st.maximum_read_duration_s,3)+' s':null],['Total / maximum host gap',Number.isFinite(st.total_positive_host_gap_s)?f(st.total_positive_host_gap_s,3)+' / '+f(st.maximum_positive_host_gap_s,3)+' s':null],['Timing note',st.note]]),radioCard('Capture storage',[['Capture state',c.state],['Observation',c.observation_mode],['Channel / region',[c.channel_number,c.region].filter(x=>x!==null&&x!==undefined).join(' · ')||null],['Requested duration',Number.isFinite(c.requested_duration_s)?f(c.requested_duration_s,1)+' s':null],['Requested samples / RX',c.requested_samples_per_receiver?.toLocaleString()],['Captured samples / RX',c.captured_samples_per_receiver?.toLocaleString()],['I/Q format',c.dtype],['Samples / storage chunk',c.chunk_samples?.toLocaleString()],['Storage chunks / IIO reads',Number.isFinite(c.chunk_count)?c.chunk_count+' / '+(c.read_count??'—'):null],['Stored size',bytes(c.stored_bytes)],['Timestamp semantics',c.timestamp_semantics]])].filter(Boolean);document.querySelector('#radiopanel').hidden=!cards.length;document.querySelector('#radio').innerHTML=cards.join('');
const confirmation=s.confirmation||{},links=[...(confirmation.cross_receiver_links||[]),...(confirmation.dual_receiver_links||[]),...(confirmation.receivers||[]).flatMap(x=>x.links||[])],passes=[...(s.overlapping_passes||[])],ft=s.frame_tracking||{},fs=ft.summary||{},ct=s.continuous_tracking||{},tc=ct.configuration||{},cl=s.continuous_linking||{},associations=[...((s.tle_association||{}).associations||[]),...((s.linked_tle_association||{}).associations||[])];if(s.coherent_joint_track?.qualified)links.push({drift_hz_s:s.coherent_joint_track.mean_drift_hz_s,kind:'coherent dual-receiver track'});if(s.best_tle_candidate)passes.push({name:s.best_tle_candidate.name,norad_id:s.best_tle_candidate.norad_id,nearest_prediction:{expected_doppler_hz:s.best_tle_candidate.expected_doppler_hz}});const trackRows=[...(ct.tracks||[]).map(x=>[x,tc.measurement_source||'direct']),...(cl.tracks||[]).filter(x=>(x.summary?.source_segment_count||0)>1).map(x=>[x,'gapped hypothesis'])].map(([x,source])=>{const q=x.summary||{};return `<tr><td>${esc(x.track_id)}</td><td>${esc(source)}</td><td>${esc(q.dual_valid_observation_count??0)}</td><td>${Number.isFinite(q.dual_valid_duration_s)?f(q.dual_valid_duration_s,2)+' s':'—'}</td><td>${Number.isFinite(x.relative_receiver_calibration?.residual_rms_hz)?f(x.relative_receiver_calibration.residual_rms_hz,1)+' Hz':'—'}</td></tr>`}).join('');const associationRows=associations.map(x=>{const b=(x.candidates||[])[0]||{},st=x.stability||{};return `<tr><td>${esc(x.track_id)}</td><td>${esc(b.name||'—')} ${Number.isFinite(x.best_norad_id)?'('+esc(x.best_norad_id)+')':''}</td><td>${Number.isFinite(x.best_holdout_residual_rms_hz)?f(x.best_holdout_residual_rms_hz,1)+' Hz':'—'}</td><td>${Number.isFinite(x.margin_to_second_hz)?f(x.margin_to_second_hz,1)+' Hz':'—'}</td><td class="${x.qualified?'yes':''}">${x.qualified?'qualified':st.passed===false?'unstable / rejected':'not qualified'}</td></tr>`}).join('');document.querySelector('#dopplerpanel').hidden=!fs.frame_observation_count&&!links.length&&!passes.length&&!trackRows&&!associationRows;document.querySelector('#doppler').innerHTML=`${fs.frame_observation_count?'<h3>Conditioned 750 Hz frames</h3><table><tbody><tr><th>Observed frames</th><td>'+esc(fs.frame_observation_count)+'</td></tr><tr><th>Dual-valid frames</th><td>'+esc(fs.dual_valid_frame_count??0)+'</td></tr><tr><th>Dual-valid fraction</th><td>'+esc(Number.isFinite(fs.dual_valid_fraction)?f(100*fs.dual_valid_fraction,1)+'%':'—')+'</td></tr><tr><th>Measured span</th><td>'+esc(Number.isFinite(fs.measured_span_s)?f(fs.measured_span_s,2)+' s':'—')+'</td></tr><tr><th>Propagated windows</th><td>'+esc((fs.extension_accepted_window_count??0)+' / '+(fs.extension_attempted_window_count??0)+' accepted / attempted')+'</td></tr></tbody></table>':''}${trackRows?'<h3 style="margin-top:18px">Calibrated 10 Hz tracks and gapped hypotheses</h3><table><thead><tr><th>Track</th><th>Epoch source</th><th>Dual epochs</th><th>Measured span</th><th>RX calibration RMS</th></tr></thead><tbody>'+trackRows+'</tbody></table>':''}${associationRows?'<h3 style="margin-top:18px">Held-out TLE association</h3><table><thead><tr><th>Track</th><th>Best candidate</th><th>Held-out RMS</th><th>Margin</th><th>Stability gate</th></tr></thead><tbody>'+associationRows+'</tbody></table>':''}${links.length?'<h3 style="margin-top:18px">Temporal Doppler links</h3><table><thead><tr><th>Start–stop</th><th>Drift</th><th>Evidence</th></tr></thead><tbody>'+links.map(x=>`<tr><td>${Number.isFinite(x.start_s)?f(x.start_s,2):'—'}–${Number.isFinite(x.stop_s)?f(x.stop_s,2):'—'} s</td><td>${Number.isFinite(x.drift_hz_s)?f(x.drift_hz_s/1000,3)+' kHz/s':'—'}</td><td>${esc(x.kind||x.classification||'dual-receiver temporal link')}</td></tr>`).join('')+'</tbody></table>':''}${passes.length?'<h3 style="margin-top:18px">Overlapping Starlink predictions</h3><table><thead><tr><th>Satellite</th><th>NORAD</th><th>Maximum elevation</th><th>Predicted Doppler</th></tr></thead><tbody>'+passes.map(p=>`<tr><td>${esc(p.name)}</td><td>${esc(p.norad_id)}</td><td>${Number.isFinite(p.culmination_elevation_deg)?f(p.culmination_elevation_deg,1)+'°':'—'}</td><td>${Number.isFinite(p.nearest_prediction?.expected_doppler_hz)?f(p.nearest_prediction.expected_doppler_hz/1000,1)+' kHz':'—'}</td></tr>`).join('')+'</tbody></table>':''}`;
const fd=s.fragment_diagnostic||{},fragmentHypotheses=fd.hypotheses||[];document.querySelector('#fragmentpanel').hidden=!fragmentHypotheses.length;document.querySelector('#fragment').innerHTML=fragmentHypotheses.map(x=>{const m=x.model_comparison||{},j=x.joint_association||{},gap=(x.gaps||[])[0]||{};return `<h3>${esc(x.track_id)}</h3><table><tbody><tr><th>Shadow classification</th><td>${esc(x.shadow_classification)}</td></tr><tr><th>Fragment top identities agree</th><td>${x.fragment_top_identity_agreement?'yes':'no'} (${esc((x.fragment_top_norad_ids||[]).join(', '))})</td></tr><tr><th>Dual-RX gap is common-mode</th><td>${x.receiver_common_mode_consistent?'yes':'no'}</td></tr><tr><th>Common / differential gap step</th><td>${Number.isFinite(gap.common_step_hz)?f(gap.common_step_hz,1)+' / '+f(gap.differential_step_hz,1)+' Hz':'—'}</td></tr><tr><th>Continuous / piecewise / independent RMS</th><td>${[m.same_continuous_holdout_rms_hz,m.same_identity_piecewise_holdout_rms_hz,m.independent_identities_holdout_rms_hz].map(v=>Number.isFinite(v)?f(v,1)+' Hz':'—').join(' / ')}</td></tr><tr><th>Joint best NORAD / separation ratio</th><td>${esc(j.best_norad_id)} / ${Number.isFinite(j.separation?.runner_up_over_winner_rms)?f(j.separation.runner_up_over_winner_rms,2)+':1':'—'}</td></tr></tbody></table>`}).join('');
const checks=s.exact_checks||[],candidates=checks.filter(x=>x.candidate),decode=s.decode||{},soft=((decode.soft_dual_rx||{}).pilot||{});document.querySelector('#detector').innerHTML=`<table><tbody><tr><th>Exact checks / candidates / qualified</th><td>${checks.length} / ${candidates.length} / ${checks.filter(x=>x.qualified).length}</td></tr><tr><th>Exact temporal coverage</th><td>${Number.isFinite(s.exact_coverage_fraction)?f(100*s.exact_coverage_fraction,3)+'%':Number.isFinite(s.summary?.exact_temporal_coverage_fraction)?f(100*s.summary.exact_temporal_coverage_fraction,3)+'%':'—'}</td></tr><tr><th>Soft dual-RX pilot accuracy</th><td>${Number.isFinite(soft.hard_symbol_accuracy)?f(100*soft.hard_symbol_accuracy,2)+'%':'—'}</td></tr><tr><th>Soft confidence / EVM</th><td>${Number.isFinite(soft.soft_mean_confidence)?f(100*soft.soft_mean_confidence,2)+'%':'—'} / ${f(soft.rms_evm,3)}</td></tr><tr><th>Decoded frames / SSS accuracy</th><td>${esc(decode.minimum_frame_count)} / ${Number.isFinite(decode.minimum_sss_accuracy)?f(100*decode.minimum_sss_accuracy,2)+'%':'—'}</td></tr></tbody></table>${candidates.length?'<h2 style="margin-top:18px">Candidate checks</h2><table><thead><tr><th>Time</th><th>Qualified</th><th>CFO Δ</th><th>RX pilot CFO</th><th>Match margins</th></tr></thead><tbody>'+candidates.map(x=>`<tr><td>${f(x.start_s,2)} s</td><td>${x.qualified?'yes':'no'}</td><td>${Number.isFinite(x.cfo_difference_hz)?f(x.cfo_difference_hz/1000,2)+' kHz':'—'}</td><td>${(x.receivers||[]).map(r=>Number.isFinite(r.pilot_frequency_offset_hz)?f(r.pilot_frequency_offset_hz/1000,2):'—').join(' / ')} kHz</td><td>${(x.receivers||[]).map(r=>f(r.match_score_margin,4)).join(' / ')}</td></tr>`).join('')+'</tbody></table>':''}`;
const fingerprintUrl=s.fingerprint_plot_url,fingerprintMatches=s.fingerprint_nearest_matches||[];document.querySelector('#fingerprintpanel').hidden=!fingerprintUrl&&!fingerprintMatches.length;document.querySelector('#fingerprintplot').innerHTML=(fingerprintUrl?`<a href="${esc(safeUrl(fingerprintUrl))}" target="_blank"><img loading="lazy" src="${esc(safeUrl(fingerprintUrl))}" alt="Temporal QPSK fingerprint"></a>`:'')+(fingerprintMatches.length?`<div style="grid-column:1/-1"><h3 style="margin-top:12px">Nearest fingerprint comparisons</h3><table><thead><tr><th>Capture</th><th>Known-code similarity</th><th>Temporal QPSK</th><th>Conditional channel</th><th>Minimum confidence</th><th>Family link</th></tr></thead><tbody>${fingerprintMatches.map(x=>`<tr><td><a href="/recordings/beacon/${encodeURIComponent(x.capture_name)}">${esc(x.capture_name)}</a></td><td>${Number.isFinite(x.waveform_family_similarity)?f(100*x.waveform_family_similarity,1)+'%':'—'}</td><td>${Number.isFinite(x.temporal_qpsk_similarity)?f(100*x.temporal_qpsk_similarity,1)+'% '+esc((x.temporal_qpsk_components||[]).join('+')):'—'}</td><td>${Number.isFinite(x.conditional_channel_similarity)?f(100*x.conditional_channel_similarity,1)+'%':'—'}</td><td>${Number.isFinite(x.minimum_pilot_confidence)?f(100*x.minimum_pilot_confidence,1)+'%':'—'}</td><td>${x.family_link?'yes':'no'}</td></tr>`).join('')}</tbody></table></div>`:'');
const artifacts=d.artifacts||[];document.querySelector('#artifactpanel').hidden=!artifacts.length;document.querySelector('#artifacts').innerHTML=artifacts.map(x=>`<a class="artifact" href="${esc(safeUrl(x.url))}" target="_blank">${esc(x.label)} ↗</a>`).join('');
const plots=d.plots||[];document.querySelector('#plotpanel').hidden=!plots.length;document.querySelector('#plots').innerHTML=plots.map((url,index)=>`<a href="${esc(safeUrl(url))}" target="_blank"><img loading="lazy" src="${esc(safeUrl(url))}" alt="Diagnostic plot ${index+1}"></a>`).join('');document.querySelector('#raw').textContent=JSON.stringify(s,null,2);if(d.active)setTimeout(load,5000);
}catch(error){document.querySelector('#subtitle').innerHTML='<span class="error">Recording not found or dashboard unavailable.</span>';document.querySelector('#summary').innerHTML='';console.error(error)}}load();
</script></body></html>'''


def make_handler(model: DashboardModel):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, payload: bytes, content_type: str, status=HTTPStatus.OK,
                  cache_control: str = "no-store"):
            self.send_response(status); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload))); self.send_header("Cache-Control", cache_control)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # Browsers can cancel superseded refreshes; this is not a
                # server failure and should not flood operational logs.
                pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._send(INDEX_HTML.encode(), "text/html; charset=utf-8")
            if path == "/api/recordings":
                return self._send(json.dumps(model.recordings(), allow_nan=False).encode(),
                                  "application/json")
            if path.startswith("/api/recordings/"):
                parts = path.split("/", 4)
                if len(parts) == 5:
                    kind, recording_id = unquote(parts[3]), unquote(parts[4])
                    if (kind in {"beacon", "sweep"} and
                            re.fullmatch(r"[A-Za-z0-9_.-]+", recording_id)):
                        detail = model.recording_detail(kind, recording_id)
                        if detail is not None:
                            return self._send(json.dumps(detail, allow_nan=False).encode(),
                                              "application/json")
                return self._send(b'{"error":"recording not found"}', "application/json",
                                  HTTPStatus.NOT_FOUND)
            if path.startswith("/recordings/"):
                parts = path.split("/", 3)
                if (len(parts) == 4 and parts[2] in {"beacon", "sweep"} and
                        re.fullmatch(r"[A-Za-z0-9_.-]+", unquote(parts[3]))):
                    return self._send(DETAIL_HTML.encode(), "text/html; charset=utf-8")
                return self._send(b'{"error":"recording not found"}', "application/json",
                                  HTTPStatus.NOT_FOUND)
            endpoints = {"/api/snapshot": model.snapshot, "/api/status": model.status,
                         "/api/passes": model.expected_passes, "/api/detections": model.detections,
                         "/api/logs": model.logs, "/api/waterfalls": model.waterfalls,
                         "/api/dither": model.dither_comparisons,
                         "/api/beacon": model.beacon}
            if path in endpoints:
                return self._send(json.dumps(endpoints[path](), allow_nan=False).encode(),
                                  "application/json")
            if path.startswith("/plots/"):
                name = Path(path).name
                target = model.root / "plots" / name
                if name.endswith(".png") and target.is_file():
                    return self._send(target.read_bytes(), "image/png",
                                      cache_control="public, max-age=31536000, immutable")
            if path.startswith("/observations/"):
                name = Path(path).name
                target = model.root / "observations" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-plots/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "plots" / name
                if name.endswith(".png") and target.is_file():
                    return self._send(target.read_bytes(), "image/png",
                                      cache_control="public, max-age=300")
            if path.startswith("/beacon-analyses/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-followups/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "followups" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-decodes/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "decoded" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-tracks/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "tracks" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-channel-links/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "channel-links" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if (path.startswith("/beacon-fragment-diagnostics/") and
                    model.beacon_root is not None):
                name = Path(path).name
                target = model.beacon_root / "reports" / "fragment-diagnostics" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-frame-tracks/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "frame-tracks" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-associations/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "associations" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-decode-plots/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "decoded" / name
                if name.endswith(".png") and target.is_file():
                    return self._send(target.read_bytes(), "image/png",
                                      cache_control="public, max-age=300")
            if path == "/beacon-fingerprint-map.svg" and model.beacon_root is not None:
                return self._send(model.beacon_fingerprint_plot(), "image/svg+xml",
                                  cache_control="no-cache")
            if path.startswith("/beacon-fingerprints/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "fingerprints" / name
                if name.endswith(".json") and target.is_file():
                    return self._send(target.read_bytes(), "application/json")
            if path.startswith("/beacon-fingerprint-plots/") and model.beacon_root is not None:
                name = Path(path).name
                target = model.beacon_root / "reports" / "fingerprints" / name
                if name.endswith(".png") and target.is_file():
                    return self._send(target.read_bytes(), "image/png",
                                      cache_control="public, max-age=300")
            self._send(b'{"error":"not found"}', "application/json", HTTPStatus.NOT_FOUND)

        def log_message(self, format, *args):
            return
    return Handler


def serve_dashboard(observation_dir: Path, *, host: str = "127.0.0.1", port: int = 8765,
                    passes_path: Path | None = None,
                    beacon_root: Path | None = None,
                    samples_per_snapshot: int = 262_144,
                    sample_rate_hz: float = 30_720_000,
                    snapshots_per_chunk: int = 4096):
    model = DashboardModel(observation_dir, passes_path, beacon_root=beacon_root,
        samples_per_snapshot=samples_per_snapshot, sample_rate_hz=sample_rate_hz,
        snapshots_per_chunk=snapshots_per_chunk)
    server = ThreadingHTTPServer((host, port), make_handler(model))
    print(json.dumps({"dashboard": f"http://{host}:{server.server_port}/",
                      "observation_dir": str(model.root)}, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
