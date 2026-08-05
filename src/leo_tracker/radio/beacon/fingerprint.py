"""Cross-capture fingerprints for decoded Starlink edge-pilot observations.

The fingerprint deliberately separates repeatable waveform-family evidence from
receiver/channel similarity.  Neither score is a satellite identity claim.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np


FINGERPRINT_SCHEMA = "leo-tracker.starlink-waveform-fingerprint/v1"
INDEX_SCHEMA = "leo-tracker.starlink-waveform-fingerprint-index/v1"
FINGERPRINT_REVISION = 1
FAMILY_LINK_THRESHOLD = .72
MINIMUM_LINK_CONFIDENCE = .65


def _json_ready(value):
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".next")
    temporary.write_text(json.dumps(_json_ready(value), indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _pack_states(values: np.ndarray) -> str:
    states = np.asarray(values, dtype=np.uint8).reshape(-1)
    padding = (-states.size) % 4
    if padding:
        states = np.pad(states, (0, padding))
    packed = (states[0::4] | (states[1::4] << 2) |
              (states[2::4] << 4) | (states[3::4] << 6))
    return packed.tobytes().hex()


def _unpack_states(value: str, count: int) -> np.ndarray:
    packed = np.frombuffer(bytes.fromhex(value), dtype=np.uint8)
    states = np.column_stack((packed & 3, (packed >> 2) & 3,
                              (packed >> 4) & 3, (packed >> 6) & 3)).reshape(-1)
    return states[:count]


def _channel_features(channel: np.ndarray) -> dict:
    channel = np.asarray(channel, dtype=np.complex128)
    magnitude = np.abs(channel)
    magnitude /= max(float(np.linalg.norm(magnitude)), 1e-12)
    phase = np.unwrap(np.angle(channel))
    x = np.arange(channel.size, dtype=float)
    if channel.size > 1:
        phase -= np.polyval(np.polyfit(x, phase, 1), x)
    phase -= float(np.mean(phase))
    return {"normalized_magnitude": magnitude.tolist(),
            "detrended_phase_rad": phase.tolist()}


def _confirmed_drifts(followup: dict) -> list[float]:
    confirmation = followup.get("confirmation", {})
    links = list(confirmation.get("cross_receiver_links", []))
    links.extend(confirmation.get("dual_receiver_links", []))
    links.extend(link for receiver in confirmation.get("receivers", [])
                 for link in receiver.get("links", []))
    return [float(link["drift_hz_s"]) for link in links
            if link.get("drift_hz_s") is not None]


def fingerprint_decode(decode_path: Path, symbols_path: Path, output: Path,
                       *, followup_path: Path | None = None) -> dict:
    """Extract a compact, comparable fingerprint from one decoded observation."""
    decode_path, symbols_path = Path(decode_path), Path(symbols_path)
    decode = json.loads(decode_path.read_text())
    if decode.get("schema") != "leo-tracker.starlink-edge-decode/v1":
        raise ValueError(f"unsupported decode schema in {decode_path}")
    with np.load(symbols_path, allow_pickle=False) as arrays:
        if "combined_pilot_probabilities" in arrays.files:
            pilot_probabilities = np.asarray(arrays["combined_pilot_probabilities"])
            sss_probabilities = np.asarray(arrays["combined_sss_probabilities"])
            pilot_states = np.argmax(pilot_probabilities, axis=-1).astype(np.uint8)
            sss_states = np.argmax(
                np.mean(sss_probabilities, axis=0), axis=-1).astype(np.uint8)
            extraction_mode = "soft_dual_rx"
        else:
            # Decoder revision 1 archives predate posterior storage. Preserve
            # their historical value with a clearly labeled hard-decision
            # average; new captures always use the soft dual-RX path above.
            pilot_equalized = (np.asarray(arrays["rx0_pilot_equalized"]) +
                               np.asarray(arrays["rx1_pilot_equalized"])) / 2
            pilot_constellation = np.exp(1j * np.pi / 2 * (np.arange(4) + .5))
            pilot_states = np.argmin(
                np.abs(pilot_equalized[..., None] - pilot_constellation), axis=-1
                ).astype(np.uint8)
            frame_count = min(arrays["rx0_sss_equalized"].shape[0],
                              arrays["rx1_sss_equalized"].shape[0])
            sss_equalized = (np.asarray(arrays["rx0_sss_equalized"][:frame_count]) +
                             np.asarray(arrays["rx1_sss_equalized"][:frame_count])) / 2
            sss_constellation = np.exp(1j * np.pi / 2 * np.arange(4))
            sss_states = np.argmin(np.abs(np.mean(sss_equalized, axis=0)[..., None] -
                                         sss_constellation), axis=-1).astype(np.uint8)
            extraction_mode = "legacy_hard_dual_rx"
        channel = [_channel_features(arrays[f"rx{receiver}_channel"])
                   for receiver in range(2)]
    followup = (json.loads(Path(followup_path).read_text())
                if followup_path is not None and Path(followup_path).is_file() else {})
    combined = decode.get("combined", {})
    soft = combined.get("soft_dual_rx", {})
    pilot = soft.get("pilot", {})
    sss = soft.get("sss", {})
    receivers = decode.get("receivers", [])
    drifts = _confirmed_drifts(followup)
    symbol_sha256 = (decode.get("symbol_archive_sha256") or
                     hashlib.sha256(symbols_path.read_bytes()).hexdigest())
    report = {
        "schema": FINGERPRINT_SCHEMA,
        "fingerprint_revision": FINGERPRINT_REVISION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "capture_name": decode_path.stem,
        "source": {"decode": str(decode_path.resolve()),
                   "symbols": str(symbols_path.resolve()),
                   "symbol_archive_sha256": symbol_sha256},
        "observation": {"created_utc": decode.get("created_utc"),
                        "selected_time_s": decode.get("selected_observation", {}).get("start_s"),
                        "rf_center_hz": decode.get("capture_parameters", {}).get("rf_center_hz"),
                        "sample_rate_hz": decode.get("capture_parameters", {}).get("sample_rate_hz"),
                        "observation_mode": decode.get("capture_parameters", {}).get(
                            "observation_mode", "narrow"),
                        "edge": decode.get("waveform", {}).get("edge")},
        "waveform_signature": {
            "extraction_mode": extraction_mode,
            "pilot_state_count": int(pilot_states.size),
            "pilot_states_2bit_hex": _pack_states(pilot_states),
            "sss_state_count": int(sss_states.size),
            "sss_states": sss_states.tolist(),
            "pilot_hard_accuracy": pilot.get("hard_symbol_accuracy",
                combined.get("minimum_pilot_accuracy")),
            "pilot_mean_confidence": pilot.get("soft_mean_confidence",
                combined.get("minimum_pilot_accuracy")),
            "pilot_mean_entropy_bits": pilot.get("soft_mean_entropy_bits"),
            "sss_hard_accuracy": sss.get("hard_symbol_accuracy",
                combined.get("minimum_sss_accuracy")),
            "sss_mean_confidence": sss.get("soft_mean_confidence",
                combined.get("minimum_sss_accuracy"))},
        "conditional_receiver_signature": {
            "channels": channel,
            "carrier_offsets_hz": [item.get("carrier_offset_hz") for item in receivers],
            "residual_cfo_refinements_hz": [item.get("residual_cfo_refinement_hz")
                                             for item in receivers],
            "pss_peak_to_median": [item.get("pss", {}).get("peak_to_median")
                                   for item in receivers]},
        "trajectory_context": {
            "confirmed": followup.get("confirmation", {}).get("confirmed", False),
            "confirmed_drift_hz_s": drifts,
            "overlapping_passes": [{key: item.get(key) for key in
                ("name", "norad_id", "observation_utc", "culmination_elevation_deg")}
                for item in followup.get("overlapping_passes", [])[:10]]},
        "interpretation": {
            "waveform_family": "repeatable PSS/SSS/edge-pilot structure",
            "conditional_receiver": "channel shape observed through this fixed RX/LNB path",
            "satellite_identity_claim": False,
            "note": "A match supports common waveform family; TLE/Doppler or decoded identity is required for satellite attribution."}}
    _atomic_json(Path(output), report)
    return report


def compare_fingerprints(first: dict, second: dict) -> dict:
    """Compare two compact fingerprints with chance-normalized code agreement."""
    a, b = first["waveform_signature"], second["waveform_signature"]
    count = min(int(a["pilot_state_count"]), int(b["pilot_state_count"]))
    pa = _unpack_states(a["pilot_states_2bit_hex"], int(a["pilot_state_count"]))[:count]
    pb = _unpack_states(b["pilot_states_2bit_hex"], int(b["pilot_state_count"]))[:count]
    pilot_agreement = float(np.mean(pa == pb)) if count else .25
    sa, sb = np.asarray(a["sss_states"]), np.asarray(b["sss_states"])
    sss_count = min(sa.size, sb.size)
    sss_agreement = float(np.mean(sa[:sss_count] == sb[:sss_count])) if sss_count else .25
    pilot_excess = float(np.clip((pilot_agreement - .25) / .75, 0, 1))
    sss_excess = float(np.clip((sss_agreement - .25) / .75, 0, 1))
    waveform_similarity = .9 * pilot_excess + .1 * sss_excess
    confidences = [value for value in (a.get("pilot_mean_confidence"),
        b.get("pilot_mean_confidence")) if value is not None]
    minimum_confidence = float(min(confidences)) if confidences else 0

    channel_scores = []
    for ca, cb in zip(first["conditional_receiver_signature"]["channels"],
                      second["conditional_receiver_signature"]["channels"]):
        ma, mb = np.asarray(ca["normalized_magnitude"]), np.asarray(cb["normalized_magnitude"])
        magnitude = float(np.dot(ma, mb) / max(np.linalg.norm(ma) * np.linalg.norm(mb), 1e-12))
        qa, qb = np.asarray(ca["detrended_phase_rad"]), np.asarray(cb["detrended_phase_rad"])
        phase = float(np.abs(np.mean(np.exp(1j * (qa - qb)))))
        channel_scores.append((magnitude + phase) / 2)
    channel_similarity = float(np.mean(channel_scores)) if channel_scores else 0
    first_trajectory, second_trajectory = (first.get("trajectory_context", {}),
                                           second.get("trajectory_context", {}))
    first_norad = {item.get("norad_id") for item in
                   first_trajectory.get("overlapping_passes", [])
                   if item.get("norad_id") is not None}
    second_norad = {item.get("norad_id") for item in
                    second_trajectory.get("overlapping_passes", [])
                    if item.get("norad_id") is not None}
    first_drifts = first_trajectory.get("confirmed_drift_hz_s", [])
    second_drifts = second_trajectory.get("confirmed_drift_hz_s", [])
    drift_difference = (min(abs(float(a) - float(b)) for a in first_drifts
                            for b in second_drifts)
                        if first_drifts and second_drifts else None)
    family_link = bool(waveform_similarity >= FAMILY_LINK_THRESHOLD and
                       minimum_confidence >= MINIMUM_LINK_CONFIDENCE)
    return {"pilot_state_agreement": pilot_agreement,
            "pilot_chance_normalized_similarity": pilot_excess,
            "sss_state_agreement": sss_agreement,
            "sss_chance_normalized_similarity": sss_excess,
            "waveform_family_similarity": waveform_similarity,
            "minimum_pilot_confidence": minimum_confidence,
            "conditional_channel_similarity": channel_similarity,
            "shared_overlapping_norad_ids": sorted(first_norad & second_norad),
            "minimum_confirmed_drift_difference_hz_s": drift_difference,
            "family_link": family_link,
            "satellite_identity_claim": False}


def build_fingerprint_index(fingerprints_dir: Path, output: Path) -> dict:
    paths = sorted(Path(fingerprints_dir).glob("*.json"))
    fingerprints = {path.stem: json.loads(path.read_text()) for path in paths}
    fingerprints = {name: value for name, value in fingerprints.items()
                    if value.get("schema") == FINGERPRINT_SCHEMA}
    names = sorted(fingerprints)
    parent = {name: name for name in names}

    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    nearest = {name: [] for name in names}
    comparison_count = 0
    for position, first_name in enumerate(names):
        for second_name in names[position + 1:]:
            metrics = compare_fingerprints(fingerprints[first_name], fingerprints[second_name])
            comparison_count += 1
            nearest[first_name].append({"capture_name": second_name, **metrics})
            nearest[second_name].append({"capture_name": first_name, **metrics})
            if metrics["family_link"]:
                union(first_name, second_name)
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)
    clusters, membership = [], {}
    for members in sorted(groups.values(), key=lambda values: (-len(values), values)):
        cluster_id = "wf-" + hashlib.sha256("\n".join(members).encode()).hexdigest()[:10]
        for member in members:
            membership[member] = cluster_id
        clusters.append({"cluster_id": cluster_id, "member_count": len(members),
                         "members": members, "satellite_identity_claim": False})
    for values in nearest.values():
        values.sort(key=lambda item: (item["waveform_family_similarity"],
                                      item["conditional_channel_similarity"]), reverse=True)
        del values[5:]
    singleton_count = sum(len(members) == 1 for members in groups.values())
    linked_fingerprint_count = sum(len(members) for members in groups.values()
                                   if len(members) > 1)
    report = {"schema": INDEX_SCHEMA, "fingerprint_revision": FINGERPRINT_REVISION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "fingerprint_count": len(names), "comparison_count": comparison_count,
        "linked_fingerprint_count": linked_fingerprint_count,
        "unresolved_singleton_count": singleton_count,
        "largest_cluster_size": max((len(members) for members in groups.values()), default=0),
        "family_link_threshold": FAMILY_LINK_THRESHOLD,
        "minimum_link_confidence": MINIMUM_LINK_CONFIDENCE,
        "clusters": clusters, "membership": membership, "nearest_matches": nearest,
        "limitations": ["clusters represent waveform-family similarity, not satellite identity",
                        "conditional channel similarity includes the fixed LNB/receiver path",
                        "TLE/Doppler compatibility must be evaluated separately"]}
    _atomic_json(Path(output), report)
    return report


def update_fingerprint_store(root: Path, *, capture_name: str | None = None) -> dict:
    """Fingerprint decoded observations and rebuild their comparison index."""
    root = Path(root).resolve()
    decoded = root / "reports" / "decoded"
    fingerprints = root / "reports" / "fingerprints"
    fingerprints.mkdir(parents=True, exist_ok=True)
    paths = ([decoded / f"{capture_name}.json"] if capture_name else
             sorted(decoded.glob("*.json")))
    written, reused, skipped, errors = [], [], [], []
    for decode_path in paths:
        symbols_path = decode_path.with_suffix(".npz")
        output = fingerprints / decode_path.name
        if not decode_path.is_file() or not symbols_path.is_file():
            skipped.append(decode_path.stem)
            continue
        try:
            decode = json.loads(decode_path.read_text())
            symbol_sha256 = (decode.get("symbol_archive_sha256") or
                             hashlib.sha256(symbols_path.read_bytes()).hexdigest())
            existing = json.loads(output.read_text()) if output.is_file() else {}
            if (existing.get("schema") == FINGERPRINT_SCHEMA and
                    existing.get("fingerprint_revision") == FINGERPRINT_REVISION and
                    existing.get("source", {}).get("symbol_archive_sha256") ==
                    symbol_sha256):
                reused.append(decode_path.stem)
                continue
            fingerprint_decode(decode_path, symbols_path, output,
                followup_path=root / "reports" / "followups" / decode_path.name)
            written.append(decode_path.stem)
        except Exception as exc:
            errors.append({"capture": decode_path.stem,
                           "error": f"{type(exc).__name__}: {exc}"})
    index = build_fingerprint_index(fingerprints, fingerprints / "index.json")
    return {"schema": "leo-tracker.starlink-waveform-fingerprint-update/v1",
            "root": str(root), "written": written, "reused_count": len(reused),
            "skipped": skipped, "errors": errors,
            "fingerprint_count": index["fingerprint_count"],
            "cluster_count": len(index["clusters"]),
            "index": str((fingerprints / "index.json").resolve())}
