"""Deterministic survey/dwell plans for Starlink Doppler monitoring."""
from __future__ import annotations

from typing import Any

import math

from .starlink import get_channel

SCHEMA = "leo-tracker.starlink-hybrid-plan/v1"


def requires_fallback(sample_rate_hz: float, duty_fraction: float,
                      minimum_duty_fraction: float = 0.80) -> bool:
    """Return whether a nominal dwell should fall back for its next cycle."""
    if sample_rate_hz <= 0 or not 0 <= duty_fraction <= 1:
        raise ValueError("sample rate and duty fraction are invalid")
    if not 0 < minimum_duty_fraction <= 1:
        raise ValueError("minimum duty fraction must be in (0, 1]")
    return sample_rate_hz > 2_500_000 and duty_fraction < minimum_duty_fraction


def should_retain_iq(legacy_report: dict[str, Any],
                     tracker_report: dict[str, Any] | None = None) -> bool:
    """Keep triggered IQ when either independent analysis path qualifies it.

    The coherent estimators operate on the staged IQ, so discarding it before
    considering the tracker ensemble would create a systematic blind spot for
    events the legacy connected-component detector misses.
    """
    legacy_qualified = any(
        (item.get("qualification") or {}).get("qualified")
        or (item.get("doppler_observation") or {}).get("qualified")
        for item in legacy_report.get("joint_events", []))
    if legacy_qualified or tracker_report is None:
        return bool(legacy_qualified)
    return any(item.get("qualified")
               for item in tracker_report.get("joint_tracks", []))


def survey_centers(*, channels: tuple[int, ...] = (3, 4),
                   occupied_bandwidth_hz: float = 240_000_000,
                   usable_bandwidth_hz: float = 18_000_000) -> list[float]:
    """Tile complete occupied channels without gaps in usable bandwidth."""
    if not channels or occupied_bandwidth_hz <= 0 or usable_bandwidth_hz <= 0:
        raise ValueError("hybrid survey dimensions must be positive")
    if usable_bandwidth_hz > occupied_bandwidth_hz:
        raise ValueError("usable survey bandwidth cannot exceed an occupied channel")
    result: list[float] = []
    half_span = occupied_bandwidth_hz/2
    first_offset = -half_span+usable_bandwidth_hz/2
    last_offset = half_span-usable_bandwidth_hz/2
    count = max(1, math.ceil((last_offset-first_offset)/usable_bandwidth_hz)+1)
    step = 0.0 if count == 1 else (last_offset-first_offset)/(count-1)
    for number in channels:
        channel = get_channel(number)
        if channel.lnb_band != "low":
            raise ValueError("hybrid plan currently requires low-band LNB channels")
        result.extend(channel.if_center_hz+first_offset+index*step
                      for index in range(count))
    return result


def build_hybrid_plan(*, survey_sample_rate_hz: float = 30_720_000,
                      survey_bandwidth_hz: float = 20_000_000,
                      usable_survey_bandwidth_hz: float = 18_000_000,
                      dwell_sample_rate_hz: float = 4_000_000,
                      dwell_bandwidth_hz: float = 3_600_000,
                      fallback_sample_rate_hz: float = 2_500_000,
                      fallback_bandwidth_hz: float = 2_300_000,
                      dwell_seconds: float = 600,
                      block_size: int = 262_144) -> dict:
    values = (survey_sample_rate_hz, survey_bandwidth_hz,
              usable_survey_bandwidth_hz, dwell_sample_rate_hz,
              dwell_bandwidth_hz, fallback_sample_rate_hz,
              fallback_bandwidth_hz, dwell_seconds, block_size)
    if any(value <= 0 for value in values):
        raise ValueError("hybrid plan values must be positive")
    if survey_bandwidth_hz > survey_sample_rate_hz:
        raise ValueError("survey bandwidth cannot exceed its sample rate")
    if dwell_bandwidth_hz > dwell_sample_rate_hz:
        raise ValueError("dwell bandwidth cannot exceed its sample rate")
    if fallback_bandwidth_hz > fallback_sample_rate_hz:
        raise ValueError("fallback bandwidth cannot exceed its sample rate")
    centers = survey_centers(usable_bandwidth_hz=usable_survey_bandwidth_hz)
    return {"schema": SCHEMA, "lnb_lo_hz": 9_750_000_000,
        "channels": [3, 4], "survey": {
            "sample_rate_hz": survey_sample_rate_hz,
            "bandwidth_hz": survey_bandwidth_hz,
            "usable_bandwidth_hz": usable_survey_bandwidth_hz,
            "centers_hz": centers, "tile_count": len(centers)},
        "dwell": {"sample_rate_hz": dwell_sample_rate_hz,
            "bandwidth_hz": dwell_bandwidth_hz, "duration_s": dwell_seconds,
            "block_size": block_size,
            "snapshots": math.ceil(dwell_seconds*dwell_sample_rate_hz/block_size)},
        "fallback": {"sample_rate_hz": fallback_sample_rate_hz,
            "bandwidth_hz": fallback_bandwidth_hz,
            "duration_s": dwell_seconds, "block_size": block_size,
            "snapshots": math.ceil(dwell_seconds*fallback_sample_rate_hz/block_size)}}
