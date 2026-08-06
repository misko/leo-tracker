"""Persistent lightweight dashboard index for beacon capture artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from statistics import median


SCHEMA = "leo-tracker.beacon-dashboard-index/v2"


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _signature(paths: tuple[Path, ...]) -> list[int]:
    values = []
    for path in paths:
        try:
            stat = path.stat()
            values.extend((stat.st_mtime_ns, stat.st_size))
        except OSError:
            values.extend((0, 0))
    return values


def confirmed_beacon_events(confirmation: dict, *, merge_gap_s: float = .25) -> list[dict]:
    """Merge duplicate/adjacent RX confirmation links into distinct time events."""
    links = (confirmation.get("cross_receiver_links", []) +
        confirmation.get("dual_receiver_links", []) + [
            link for receiver in confirmation.get("receivers", [])
            for link in receiver.get("links", [])])
    intervals = []
    for link in links:
        start, stop = link.get("start_s"), link.get("stop_s")
        if start is None or stop is None:
            continue
        start, stop = float(start), float(stop)
        intervals.append({"start_s": min(start, stop), "stop_s": max(start, stop),
                          "link_count": 1})
    merged = []
    for interval in sorted(intervals, key=lambda item: (item["start_s"], item["stop_s"])):
        if merged and interval["start_s"] <= merged[-1]["stop_s"] + merge_gap_s:
            merged[-1]["stop_s"] = max(merged[-1]["stop_s"], interval["stop_s"])
            merged[-1]["link_count"] += 1
        else:
            merged.append(dict(interval))
    if not merged and confirmation.get("confirmed"):
        return [{"start_s": None, "stop_s": None, "link_count": 0}]
    return merged


def capture_radio_parameters(manifest: dict) -> dict:
    """Return a compact, display-ready audit of configured and observed radio state."""
    identity = manifest.get("identity", {}) or {}
    metadata = manifest.get("metadata", {}) or {}
    telemetry = manifest.get("gain_telemetry", {}) or {}
    sample_statistics = manifest.get("sample_statistics", {}) or {}
    stream_timing = manifest.get("stream_timing", {}) or {}
    entries = telemetry.get("entries", []) or []
    receiver_count = int(manifest.get("receiver_count", 0) or 0)
    enabled = identity.get("enabled_channels") or list(range(receiver_count))
    gains = []
    for receiver in range(receiver_count):
        values = [float(entry["rx_gain_db"][receiver]) for entry in entries
                  if isinstance(entry.get("rx_gain_db"), list) and
                  len(entry["rx_gain_db"]) > receiver and
                  entry["rx_gain_db"][receiver] is not None]
        gains.append({"receiver": receiver, "sample_count": len(values),
                      "minimum_db": min(values) if values else None,
                      "median_db": median(values) if values else None,
                      "maximum_db": max(values) if values else None})
    chunks = manifest.get("chunks", []) or []
    return {
        "tuning": {
            "if_center_hz": manifest.get("center_frequency_hz"),
            "lnb_lo_hz": manifest.get("lnb_lo_hz"),
            "rf_center_hz": manifest.get("rf_center_hz"),
            "sample_rate_hz": manifest.get("sample_rate_hz"),
            "rf_bandwidth_hz": manifest.get("bandwidth_hz"),
            "tuning_basis": metadata.get("tuning_basis"),
        },
        "receivers": {
            "receiver_count": receiver_count or None,
            "enabled_channels": enabled,
            "layout": manifest.get("layout"),
            "gain_mode_requested": manifest.get("gain_mode"),
            "configured_manual_gain_db": manifest.get("configured_gain_db"),
            "gain_mode_readback": identity.get("gain_mode_readback"),
            "gain_readback_by_receiver": gains,
            "gain_telemetry_interval_s": telemetry.get("target_interval_s"),
            "gain_telemetry_note": telemetry.get("note"),
        },
        "gain_experiment": {
            "experiment_id": metadata.get("gain_experiment_id"),
            "assigned_gain_mode": metadata.get("assigned_gain_mode"),
            "agc_assignment_probability": metadata.get("agc_assignment_probability"),
            "random_draw_u32": metadata.get("gain_random_draw_u32"),
            "agc_settle_s": metadata.get("agc_settle_s"),
        },
        "hardware": {
            "kind": identity.get("kind"),
            "implementation": identity.get("implementation"),
            "transport": identity.get("transport"),
            "uri": identity.get("uri"),
            "serial": identity.get("serial"),
            "host_temperature_c": identity.get("host_temperature_c"),
            "radio_temperature_c": identity.get("radio_temperature_c"),
        },
        "signal": {
            "adc_nominal_full_scale": sample_statistics.get("adc_nominal_full_scale"),
            "near_full_scale_threshold": sample_statistics.get(
                "near_full_scale_threshold"),
            "receivers": sample_statistics.get("receivers", []),
            "note": sample_statistics.get("note"),
        },
        "stream": {key: stream_timing.get(key) for key in (
            "sample_time_s", "wall_span_s", "host_read_duty_fraction", "read_count",
            "total_read_duration_s", "maximum_read_duration_s",
            "total_positive_host_gap_s", "maximum_positive_host_gap_s", "note")},
        "capture": {
            "state": manifest.get("state"),
            "observation_mode": metadata.get("observation_mode"),
            "channel_number": metadata.get("channel_number"),
            "region": metadata.get("region"),
            "created_utc_ns": manifest.get("created_utc_ns"),
            "requested_duration_s": manifest.get("requested_duration_s"),
            "requested_samples_per_receiver": manifest.get(
                "requested_samples_per_receiver"),
            "captured_samples_per_receiver": sum(int(chunk.get("sample_count", 0) or 0)
                                                   for chunk in chunks),
            "chunk_samples": manifest.get("chunk_samples"),
            "chunk_count": len(chunks),
            "read_count": sum(int(chunk.get("read_count", 0) or 0) for chunk in chunks),
            "stored_bytes": sum(int(chunk.get("bytes", 0) or 0) for chunk in chunks),
            "dtype": manifest.get("dtype"),
            "timestamp_semantics": identity.get("timestamp_semantics"),
        },
    }


def _capture_row(root: Path, name: str, fingerprint_index: dict) -> dict | None:
    report_path = root / "reports" / f"{name}.json"
    followup_path = root / "reports" / "followups" / f"{name}.json"
    decode_path = root / "reports" / "decoded" / f"{name}.json"
    fingerprint_path = root / "reports" / "fingerprints" / f"{name}.json"
    fingerprint_plot_path = root / "reports" / "fingerprints" / f"{name}.png"
    frame_track_path = root / "reports" / "frame-tracks" / f"{name}.json"
    track_path = root / "reports" / "tracks" / f"{name}.json"
    association_path = root / "reports" / "associations" / f"{name}.json"
    channel_link_path = root / "reports" / "channel-links" / f"{name}.json"
    linked_association_path = (root / "reports" / "associations" /
                               f"{name}-channel-link.json")
    paths = (report_path, followup_path, decode_path, fingerprint_path,
             fingerprint_plot_path, frame_track_path, track_path, association_path,
             channel_link_path, linked_association_path)
    report = _json(report_path)
    if report.get("schema") != "leo-tracker.starlink-beacon-analysis/v1":
        return None
    manifest, summary = report.get("capture_manifest", {}), report.get("summary", {})
    followup, decode = _json(followup_path), _json(decode_path)
    frame_track = _json(frame_track_path)
    track_report, association = _json(track_path), _json(association_path)
    channel_link, linked_association = (_json(channel_link_path),
                                        _json(linked_association_path))
    confirmation = followup.get("confirmation", {})
    combined = decode.get("combined", {})
    pilot = (combined.get("soft_dual_rx") or {}).get("pilot") or {}
    links = (confirmation.get("cross_receiver_links", []) +
        confirmation.get("dual_receiver_links", []) + [
            link for receiver in confirmation.get("receivers", [])
            for link in receiver.get("links", [])])
    strongest = max(links, key=lambda item: abs(float(item.get("drift_hz_s", 0))),
                    default={})
    beacon_events = confirmed_beacon_events(confirmation)
    membership = fingerprint_index.get("membership", {})
    cluster_id = membership.get(name)
    cluster_sizes = {item.get("cluster_id"): item.get("member_count", 0)
        for item in fingerprint_index.get("clusters", [])}
    nearest_matches = fingerprint_index.get("nearest_matches", {}).get(name, [])[:5]
    metadata = manifest.get("metadata", {})
    created_ns = int(manifest.get("created_utc_ns", 0) or 0)
    created_utc = (datetime.fromtimestamp(created_ns / 1e9, timezone.utc).isoformat()
                   .replace("+00:00", "Z") if created_ns else None)
    pilot_accuracy = pilot.get("hard_symbol_accuracy", combined.get(
        "minimum_pilot_accuracy"))
    status = ("confirmed" if confirmation.get("confirmed") else
              "qualified" if summary.get("exact_qualified_count") else
              "candidate" if (summary.get("exact_candidate_count") or
                              summary.get("single_receiver_candidate_count")) else "analyzed")
    gain = manifest.get("gain_mode") or "unknown"
    if manifest.get("configured_gain_db") is not None:
        gain += f" {float(manifest['configured_gain_db']):g} dB"
    plot_path = root / "reports" / "plots" / f"{name}.png"
    decode_plot_path = root / "reports" / "decoded" / f"{name}.png"
    plots = ([f"/beacon-plots/{name}.png"] if plot_path.is_file() else [])
    if decode_plot_path.is_file():
        plots.append(f"/beacon-decode-plots/{name}.png")
    artifacts = [{"label": "Analysis JSON", "url": f"/beacon-analyses/{name}.json"}]
    for label, path, url in (
        ("Follow-up JSON", followup_path, f"/beacon-followups/{name}.json"),
        ("Decode JSON", decode_path, f"/beacon-decodes/{name}.json"),
        ("Fingerprint JSON", fingerprint_path, f"/beacon-fingerprints/{name}.json"),
        ("Conditioned 750 Hz frame JSON", frame_track_path,
         f"/beacon-frame-tracks/{name}.json"),
        ("Continuous 10 Hz track JSON", track_path, f"/beacon-tracks/{name}.json"),
        ("Gapped 10 Hz hypothesis JSON", channel_link_path,
         f"/beacon-channel-links/{name}.json"),
        ("TLE association JSON", association_path,
         f"/beacon-associations/{name}.json"),
        ("Gapped-track TLE association JSON", linked_association_path,
         f"/beacon-associations/{name}-channel-link.json")):
        if path.is_file():
            artifacts.append({"label": label, "url": url})
    common = {"kind": "beacon", "recording_id": name, "start_utc": created_utc,
        "status": status, "mode": metadata.get("observation_mode", "narrow"),
        "channel": metadata.get("channel_number"), "region": metadata.get("region"),
        "if_center_hz": manifest.get("center_frequency_hz"),
        "rf_center_hz": manifest.get("rf_center_hz"),
        "sample_rate_hz": manifest.get("sample_rate_hz"),
        "bandwidth_hz": manifest.get("bandwidth_hz"),
        "duration_s": manifest.get("requested_duration_s"), "gain": gain,
        "candidate_count": int(summary.get("exact_candidate_count", 0) or 0) +
            int(summary.get("single_receiver_candidate_count", 0) or 0),
        "dual_candidate_count": int(summary.get("exact_candidate_count", 0) or 0),
        "single_receiver_candidate_count": int(
            summary.get("single_receiver_candidate_count", 0) or 0),
        "confirmed": bool(confirmation.get("confirmed")),
        "beacon_detected_count": len(beacon_events),
        "decoded": decode.get("schema") == "leo-tracker.starlink-edge-decode/v1",
        "pilot_accuracy": pilot_accuracy,
        "pilot_confidence": pilot.get("soft_mean_confidence"),
        "pilot_evm": pilot.get("rms_evm"),
        "decode_frame_count": combined.get("minimum_frame_count"),
        "strongest_drift_hz_s": strongest.get("drift_hz_s"),
        "tle_overlap_count": len(followup.get("overlapping_passes", [])),
        "continuous_track_count": track_report.get("summary", {}).get("track_count", 0),
        "conditioned_frame_count": frame_track.get("summary", {}).get(
            "frame_observation_count", 0),
        "conditioned_dual_valid_frame_count": frame_track.get("summary", {}).get(
            "dual_valid_frame_count", 0),
        "longest_track_duration_s": max(filter(lambda value: value is not None, [
            track_report.get("summary", {}).get("longest_dual_valid_duration_s",
                track_report.get("summary", {}).get("longest_valid_duration_s")),
            channel_link.get("summary", {}).get("longest_hypothesis_duration_s")]),
            default=None),
        "qualified_tle_association_count": (
            association.get("summary", {}).get("qualified_association_count", 0) +
            linked_association.get("summary", {}).get(
                "qualified_association_count", 0)),
        "fingerprint_family": cluster_id,
        "fingerprint_family_size": cluster_sizes.get(cluster_id, 0),
        "fingerprint_plot_url": (f"/beacon-fingerprint-plots/{name}.png"
                                 if fingerprint_plot_path.is_file() else None),
        "fingerprint_nearest_matches": nearest_matches,
        "exact_coverage_fraction": summary.get("exact_temporal_coverage_fraction"),
        "detail_url": f"/recordings/beacon/{name}"}
    exact_checks = [{"start_s": item.get("start_s"),
        "candidate": item.get("candidate"), "qualified": item.get("qualified"),
        "epoch_difference_samples": item.get("epoch_difference_samples"),
        "cfo_difference_hz": item.get("cfo_difference_hz"),
        "receivers": [{"pss_peak_to_median": receiver.get("pss", {}).get(
                "peak_to_median"),
            "pilot_score_margin": receiver.get("pilot", {}).get("score_margin"),
            "pilot_frequency_offset_hz": receiver.get("pilot", {}).get(
                "frequency_offset_hz"),
            "match_score_margin": receiver.get("acquisition", {}).get(
                "match_score_margin"),
            "selected_center_offset_hz": receiver.get("acquisition", {}).get(
                "selected_center_offset_hz")}
            for receiver in item.get("receivers", [])]}
        for item in report.get("exact_checks", [])]
    statistics = {**common, "capture_manifest": manifest,
        "radio_parameters": capture_radio_parameters(manifest), "summary": summary,
        "analysis": report.get("analysis", {}), "confirmation": confirmation,
        "confirmed_beacon_events": beacon_events,
        "exact_checks": exact_checks,
        "overlapping_passes": followup.get("overlapping_passes", [])[:10],
        "decode": combined,
        "frame_tracking": frame_track,
        "continuous_tracking": {"configuration": track_report.get("configuration", {}),
                                "summary": track_report.get("summary", {}),
                                "tracks": track_report.get("tracks", [])},
        "continuous_linking": channel_link,
        "tle_association": association,
        "linked_tle_association": linked_association,
        "fingerprint": {"cluster_id": cluster_id,
            "cluster_size": cluster_sizes.get(cluster_id, 0)}}
    return {**common, "_source_signature": _signature(paths),
            "_statistics": statistics, "_plots": plots, "_artifacts": artifacts}


def update_dashboard_index(root: Path, output: Path, *, capture_name: str | None = None) -> dict:
    """Incrementally update a compact index; unchanged multi-MB reports are not reparsed."""
    root, output = Path(root).resolve(), Path(output)
    previous = _json(output)
    rows = ({row.get("recording_id"): row for row in previous.get("recordings", [])
             if row.get("recording_id")} if previous.get("schema") == SCHEMA else {})
    fingerprint_index = _json(root / "reports" / "fingerprints" / "index.json")
    reports = sorted(path for path in (root / "reports").glob("*.json")
                     if path.resolve() != output.resolve())
    targets = ([root / "reports" / f"{capture_name}.json"] if capture_name else reports)
    for report_path in targets:
        name = report_path.stem
        sidecars = (report_path, root / "reports" / "followups" / report_path.name,
            root / "reports" / "decoded" / report_path.name,
            root / "reports" / "fingerprints" / report_path.name,
            root / "reports" / "fingerprints" / f"{name}.png",
            root / "reports" / "frame-tracks" / report_path.name,
            root / "reports" / "tracks" / report_path.name,
            root / "reports" / "associations" / report_path.name,
            root / "reports" / "channel-links" / report_path.name,
            root / "reports" / "associations" /
                f"{report_path.stem}-channel-link.json")
        radio_parameters = rows.get(name, {}).get("_statistics", {}).get(
            "radio_parameters", {})
        if (capture_name is None and name in rows and
                "signal" in radio_parameters and "stream" in radio_parameters and
                rows[name].get("_source_signature") == _signature(sidecars)):
            continue
        row = _capture_row(root, name, fingerprint_index)
        if row is not None:
            rows[name] = row
    membership = fingerprint_index.get("membership", {})
    cluster_sizes = {item.get("cluster_id"): item.get("member_count", 0)
        for item in fingerprint_index.get("clusters", [])}
    for name, row in rows.items():
        cluster_id = membership.get(name)
        row["fingerprint_family"] = cluster_id
        row["fingerprint_family_size"] = cluster_sizes.get(cluster_id, 0)
        row["fingerprint_nearest_matches"] = fingerprint_index.get(
            "nearest_matches", {}).get(name, [])[:5]
        row.setdefault("_statistics", {})["fingerprint"] = {
            "cluster_id": cluster_id, "cluster_size": cluster_sizes.get(cluster_id, 0)}
        row["_statistics"]["fingerprint_nearest_matches"] = row[
            "fingerprint_nearest_matches"]
    ordered = sorted(rows.values(), key=lambda row: row.get("start_utc") or "", reverse=True)
    report = {"schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "summary": {"analyzed_capture_count": len(reports),
            "retained_iq_capture_count": len(list((root / "captures").glob("*/manifest.json"))),
            "followup_capture_count": len(list((root / "reports" / "followups").glob("*.json"))),
            "temporally_confirmed_capture_count": sum(bool(row.get("confirmed"))
                for row in ordered),
            "decoded_capture_count": len(list((root / "reports" / "decoded").glob("*.json"))),
            "continuous_track_capture_count": len(list(
                (root / "reports" / "tracks").glob("*.json"))),
            "conditioned_frame_track_capture_count": len(list(
                (root / "reports" / "frame-tracks").glob("*.json"))),
            "tle_association_capture_count": len(list(
                (root / "reports" / "associations").glob("*.json"))),
            "fingerprint_count": fingerprint_index.get("fingerprint_count", 0)},
        "recordings": ordered}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n")
    os.replace(temporary, output)
    return report
