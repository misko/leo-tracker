"""Persistent lightweight dashboard index for beacon capture artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path


SCHEMA = "leo-tracker.beacon-dashboard-index/v1"


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


def _capture_row(root: Path, name: str, fingerprint_index: dict) -> dict | None:
    report_path = root / "reports" / f"{name}.json"
    followup_path = root / "reports" / "followups" / f"{name}.json"
    decode_path = root / "reports" / "decoded" / f"{name}.json"
    fingerprint_path = root / "reports" / "fingerprints" / f"{name}.json"
    paths = (report_path, followup_path, decode_path, fingerprint_path)
    report = _json(report_path)
    if report.get("schema") != "leo-tracker.starlink-beacon-analysis/v1":
        return None
    manifest, summary = report.get("capture_manifest", {}), report.get("summary", {})
    followup, decode = _json(followup_path), _json(decode_path)
    confirmation = followup.get("confirmation", {})
    combined = decode.get("combined", {})
    pilot = (combined.get("soft_dual_rx") or {}).get("pilot") or {}
    links = (confirmation.get("cross_receiver_links", []) +
        confirmation.get("dual_receiver_links", []) + [
            link for receiver in confirmation.get("receivers", [])
            for link in receiver.get("links", [])])
    strongest = max(links, key=lambda item: abs(float(item.get("drift_hz_s", 0))),
                    default={})
    membership = fingerprint_index.get("membership", {})
    cluster_id = membership.get(name)
    cluster_sizes = {item.get("cluster_id"): item.get("member_count", 0)
        for item in fingerprint_index.get("clusters", [])}
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
        ("Fingerprint JSON", fingerprint_path, f"/beacon-fingerprints/{name}.json")):
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
        "decoded": decode.get("schema") == "leo-tracker.starlink-edge-decode/v1",
        "pilot_accuracy": pilot_accuracy,
        "pilot_confidence": pilot.get("soft_mean_confidence"),
        "pilot_evm": pilot.get("rms_evm"),
        "decode_frame_count": combined.get("minimum_frame_count"),
        "strongest_drift_hz_s": strongest.get("drift_hz_s"),
        "tle_overlap_count": len(followup.get("overlapping_passes", [])),
        "fingerprint_family": cluster_id,
        "fingerprint_family_size": cluster_sizes.get(cluster_id, 0),
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
    statistics = {**common, "capture_manifest": manifest, "summary": summary,
        "analysis": report.get("analysis", {}), "confirmation": confirmation,
        "exact_checks": exact_checks,
        "overlapping_passes": followup.get("overlapping_passes", [])[:10],
        "decode": combined,
        "fingerprint": {"cluster_id": cluster_id,
            "cluster_size": cluster_sizes.get(cluster_id, 0)}}
    return {**common, "_source_signature": _signature(paths),
            "_statistics": statistics, "_plots": plots, "_artifacts": artifacts}


def update_dashboard_index(root: Path, output: Path, *, capture_name: str | None = None) -> dict:
    """Incrementally update a compact index; unchanged multi-MB reports are not reparsed."""
    root, output = Path(root).resolve(), Path(output)
    previous = _json(output)
    rows = {row.get("recording_id"): row for row in previous.get("recordings", [])
            if row.get("recording_id")}
    fingerprint_index = _json(root / "reports" / "fingerprints" / "index.json")
    reports = sorted((root / "reports").glob("*.json"))
    targets = ([root / "reports" / f"{capture_name}.json"] if capture_name else reports)
    for report_path in targets:
        name = report_path.stem
        sidecars = (report_path, root / "reports" / "followups" / report_path.name,
            root / "reports" / "decoded" / report_path.name,
            root / "reports" / "fingerprints" / report_path.name)
        if (capture_name is None and name in rows and
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
        row.setdefault("_statistics", {})["fingerprint"] = {
            "cluster_id": cluster_id, "cluster_size": cluster_sizes.get(cluster_id, 0)}
    ordered = sorted(rows.values(), key=lambda row: row.get("start_utc") or "", reverse=True)
    report = {"schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "summary": {"analyzed_capture_count": len(reports),
            "retained_iq_capture_count": len(list((root / "captures").glob("*/manifest.json"))),
            "followup_capture_count": len(list((root / "reports" / "followups").glob("*.json"))),
            "temporally_confirmed_capture_count": sum(bool(row.get("confirmed"))
                for row in ordered),
            "decoded_capture_count": len(list((root / "reports" / "decoded").glob("*.json"))),
            "fingerprint_count": fingerprint_index.get("fingerprint_count", 0)},
        "recordings": ordered}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n")
    os.replace(temporary, output)
    return report
