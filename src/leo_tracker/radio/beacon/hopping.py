"""Fast, provenance-preserving Starlink channel-hop capture sessions."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Iterable

from ..paired import paired_sample_count
from .artifact import capture_beacon_iq
from .channels import starlink_edge_pilot_if_hz


HOP_SESSION_SCHEMA = "leo-tracker.starlink-hop-session/v1"


def _utc_iso_ns(value: int) -> str:
    return datetime.fromtimestamp(value / 1e9, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def capture_hop_session(source, destination: Path, *,
                        channels: Iterable[int] = tuple(range(1, 9)),
                        region: str = "lower-edge", dwell_s: float = 1.0,
                        sample_rate_hz: float = 2_500_000,
                        bandwidth_hz: float = 2_500_000,
                        lnb_lo_hz: float = 9_750_000_000,
                        settle_buffers: int = 2, chunk_s: float = 1.0,
                        gain_mode: str = "manual",
                        configured_gain_db: float | None = None,
                        identity: dict | None = None,
                        metadata: dict | None = None) -> dict:
    """Retune one open dual-RX stream and publish one fixed capture per channel.

    Retune-transient samples are consumed but never written into a child IQ
    artifact.  Each child therefore retains the ordinary beacon artifact schema
    and can pass through the existing detector independently.
    """
    ordered = tuple(int(channel) for channel in channels)
    if not ordered or len(set(ordered)) != len(ordered) or any(
            channel not in range(1, 9) for channel in ordered):
        raise ValueError("channels must be a nonempty unique subset of 1 through 8")
    if region not in ("lower-edge", "upper-edge"):
        raise ValueError("hop capture requires a published edge region")
    if min(dwell_s, sample_rate_hz, bandwidth_hz, lnb_lo_hz, chunk_s) <= 0:
        raise ValueError("hop capture rates and durations must be positive")
    if bandwidth_hz > sample_rate_hz or settle_buffers < 0:
        raise ValueError("invalid hop bandwidth or settling guard")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    session_path = destination / "session.json"
    started_ns = time.time_ns()
    session = {"schema": HOP_SESSION_SCHEMA, "state": "capturing",
        "created_utc": _utc_iso_ns(started_ns), "channel_order": list(ordered),
        "region": region, "dwell_s": float(dwell_s),
        "sample_rate_hz": float(sample_rate_hz),
        "bandwidth_hz": float(bandwidth_hz), "lnb_lo_hz": float(lnb_lo_hz),
        "settle_buffers": int(settle_buffers),
        "identity": dict(identity if identity is not None else source.identity),
        "metadata": dict(metadata or {}), "segments": []}
    _atomic_json(session_path, session)
    blocks = iter(source.blocks())
    try:
        for sequence, channel in enumerate(ordered):
            edge = region.removesuffix("-edge")
            center_hz = starlink_edge_pilot_if_hz(channel, edge, lnb_lo_hz)
            retune_requested_ns = time.time_ns()
            source.retune(center_hz)
            discarded_samples = 0
            discarded_first_utc_ns = discarded_last_utc_ns = None
            for _ in range(settle_buffers):
                block = next(blocks)
                discarded_samples += paired_sample_count(block)
                if discarded_first_utc_ns is None:
                    discarded_first_utc_ns = int(block.utc_ns)
                discarded_last_utc_ns = int(block.utc_ns)
            child_name = f"{sequence:02d}-ch{channel}-{region}"
            child = destination / child_name
            manifest = capture_beacon_iq(blocks, child,
                sample_rate_hz=sample_rate_hz, center_frequency_hz=center_hz,
                bandwidth_hz=bandwidth_hz, duration_s=dwell_s,
                lnb_lo_hz=lnb_lo_hz, chunk_s=min(chunk_s, dwell_s),
                identity=dict(identity if identity is not None else source.identity),
                gain_mode=gain_mode,
                configured_gain_db=configured_gain_db,
                metadata={"channel_number": channel, "region": region,
                    "observation_mode": "channel-hop",
                    "hop_session": str(destination.resolve()),
                    "hop_sequence": sequence,
                    "nominal_if_hz": center_hz,
                    "nominal_rf_hz": center_hz + lnb_lo_hz,
                    "configured_tuning_offset_hz": 0.0,
                    "tuning_basis": "published Starlink edge-pilot geometry"})
            first_read_ns = (manifest["stream_timing"].get(
                "first_read_start_utc_ns") or manifest["chunks"][0]["first_utc_ns"])
            segment = {"sequence": sequence, "channel_number": channel,
                "region": region, "capture": child_name,
                "if_center_hz": float(center_hz),
                "rf_center_hz": float(center_hz + lnb_lo_hz),
                "retune_requested_utc": _utc_iso_ns(retune_requested_ns),
                "discarded_settle_buffers": int(settle_buffers),
                "discarded_settle_samples": discarded_samples,
                "discarded_first_utc": (_utc_iso_ns(discarded_first_utc_ns)
                                         if discarded_first_utc_ns else None),
                "discarded_last_utc": (_utc_iso_ns(discarded_last_utc_ns)
                                        if discarded_last_utc_ns else None),
                "first_read_start_utc": _utc_iso_ns(int(first_read_ns)),
                "completed_utc": _utc_iso_ns(int(manifest["completed_utc_ns"])),
                "samples_per_receiver": int(
                    manifest["captured_samples_per_receiver"])}
            session["segments"].append(segment)
            _atomic_json(session_path, session)
    except BaseException:
        session["state"] = "interrupted"
        session["completed_utc"] = _utc_iso_ns(time.time_ns())
        _atomic_json(session_path, session)
        raise
    finally:
        source.close()
    session["state"] = "complete"
    session["completed_utc"] = _utc_iso_ns(time.time_ns())
    session["duration_wall_s"] = (time.time_ns() - started_ns) / 1e9
    _atomic_json(session_path, session)
    return session
