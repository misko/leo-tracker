"""Rank compact monitor tiles for pass-time revisiting."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def rank_regions(report_path: Path, *, count: int = 6, offset: int = 0) -> list[dict]:
    """Rank centers by repeatable spectral structure on both receivers."""
    if count < 1:
        raise ValueError("region count must be at least one")
    if offset < 0:
        raise ValueError("region offset cannot be negative")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema") != "leo-tracker.radio-monitor/v1":
        raise ValueError("unsupported monitor report schema")
    arrays = np.load(report_path.parent / report["spectra"])
    psd = np.asarray(arrays["psd_db"], dtype=float)
    centers = np.asarray(arrays["centers_hz"], dtype=float)
    if psd.ndim != 4 or psd.shape[2] != centers.size:
        raise ValueError("monitor spectra have an invalid shape")
    ranked = []
    for index, center in enumerate(centers):
        tile = psd[:, :, index, :]
        contrast = np.percentile(tile, 99, axis=-1) - np.median(tile, axis=-1)
        channel_medians = np.median(contrast, axis=0)
        agreement_penalty = abs(float(channel_medians[0] - channel_medians[1]))
        temporal_penalty = float(np.median(np.std(contrast, axis=0)))
        score = float(np.min(channel_medians) - .25 * agreement_penalty - .1 * temporal_penalty)
        ranked.append({"center_frequency_hz": float(center), "score": score,
                       "rx0_contrast_db": float(channel_medians[0]),
                       "rx1_contrast_db": float(channel_medians[1])})
    return sorted(ranked, key=lambda item: item["score"], reverse=True)[offset:offset + count]


def write_region_plan(report_path: Path, output: Path, *, count: int = 6,
                      offset: int = 0) -> dict:
    regions = rank_regions(report_path, count=count, offset=offset)
    if not regions:
        raise ValueError("region offset is beyond available centers")
    value = {"schema": "leo-tracker.radio-regions/v1",
             "source_monitor": str(report_path), "rank_offset": offset, "regions": regions,
             "centers_hz": [item["center_frequency_hz"] for item in regions]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def read_centers(path: Path) -> list[float]:
    value = json.loads(path.read_text(encoding="utf-8"))
    centers = value.get("centers_hz")
    if not isinstance(centers, list) or not centers:
        raise ValueError("center plan must contain a non-empty centers_hz list")
    result = [float(item) for item in centers]
    if any(not np.isfinite(item) or item <= 0 for item in result):
        raise ValueError("center frequencies must be finite and positive")
    return result
