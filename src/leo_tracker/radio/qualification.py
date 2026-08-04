"""TLE-blind paired-channel comb qualification."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .artifact import CaptureArtifact
from .moving import MovingCombResult, detect_moving_comb


def _metrics(result: MovingCombResult) -> dict:
    value = asdict(result); value.pop("points")
    return value


def qualify_paired_comb(session: str | Path, *, true_spacing_hz: float = 43_949.5,
                        wrong_spacings_hz: Sequence[float] = (37_000, 50_000),
                        score_margin: float = 1.0, rx_margin: float = 1.0,
                        min_positive_tone_fraction: float = .7, skip_checksum: bool = False,
                        fft_size: int = 8192, integration_s: float = 1.0,
                        spectra_per_integration: int = 24, tone_count: int = 9,
                        search_hz: tuple[float, float] = (-500_000, 500_000),
                        max_drift_hz_s: float = 12_000) -> tuple[dict, dict[str, dict[float, MovingCombResult]]]:
    """Measure true/control combs on both channels and apply explicit gates."""
    session = Path(session)
    artifacts = {name: CaptureArtifact.open(session / name, verify=not skip_checksum)
                 for name in ("rx0", "rx1")}
    a0, a1 = artifacts.values()
    pair0, pair1 = a0.manifest["metadata"].get("pair_session_id"), a1.manifest["metadata"].get("pair_session_id")
    if not pair0 or pair0 != pair1: raise ValueError("rx0/rx1 do not share a pair_session_id")
    if a0.manifest["start_utc_ns"] != a1.manifest["start_utc_ns"]: raise ValueError("paired captures have different start UTC")
    rate0 = a0.manifest["radio_config"]["sample_rate_hz"]
    if rate0 != a1.manifest["radio_config"]["sample_rate_hz"]: raise ValueError("paired sample rates differ")
    spacings = (float(true_spacing_hz), *(float(x) for x in wrong_spacings_hz))
    results: dict[str, dict[float, MovingCombResult]] = {"rx0": {}, "rx1": {}}
    for channel, artifact in artifacts.items():
        for spacing in spacings:
            results[channel][spacing] = detect_moving_comb(artifact.path / "iq.c64", rate0,
                fft_size=fft_size, integration_s=integration_s,
                spectra_per_integration=spectra_per_integration, tone_count=tone_count,
                tone_spacing_hz=spacing, search_hz=search_hz,
                max_drift_hz_s=max_drift_hz_s)
    true0, true1 = results["rx0"][float(true_spacing_hz)], results["rx1"][float(true_spacing_hz)]
    control_deltas = {str(spacing): true0.median_comb_z_score - results["rx0"][spacing].median_comb_z_score
                      for spacing in spacings[1:]}
    rx_delta = true0.median_comb_z_score - true1.median_comb_z_score
    gates = {
        "rx0_beats_wrong_spacings": {
            "passed": all(delta >= score_margin for delta in control_deltas.values()),
            "threshold_min_z_margin": score_margin, "observed_z_margins": control_deltas,
            "reason": "true comb score must exceed every wrong-spacing control by the configured meaningful margin",
        },
        "rx0_beats_rx1": {"passed": rx_delta >= rx_margin, "threshold_min_z_margin": rx_margin,
            "observed_z_margin": rx_delta,
            "reason": "antenna RX0 true-comb score must exceed monitor/control RX1"},
        "rx0_tone_support": {"passed": true0.median_positive_tone_fraction >= min_positive_tone_fraction,
            "threshold_min_fraction": min_positive_tone_fraction,
            "observed_fraction": true0.median_positive_tone_fraction,
            "reason": "a qualified comb must be supported by most documented tones, not one or two peaks"},
    }
    summary = {"schema_version": 1, "workflow": "tle-blind-paired-comb-qualification",
        "pair_session_id": pair0, "capture_ids": {k: v.manifest["capture_id"] for k, v in artifacts.items()},
        "settings": {"true_spacing_hz": true_spacing_hz, "wrong_spacings_hz": list(wrong_spacings_hz),
            "score_margin": score_margin, "rx_margin": rx_margin,
            "min_positive_tone_fraction": min_positive_tone_fraction, "fft_size": fft_size,
            "integration_s": integration_s, "spectra_per_integration": spectra_per_integration,
            "tone_count": tone_count, "search_hz": list(search_hz), "max_drift_hz_s": max_drift_hz_s},
        "metrics": {channel: {str(spacing): _metrics(result) for spacing, result in channel_results.items()}
                    for channel, channel_results in results.items()},
        "deltas": {"rx0_true_minus_wrong_z": control_deltas, "rx0_true_minus_rx1_true_z": rx_delta},
        "gates": gates, "radio_qualified": all(gate["passed"] for gate in gates.values())}
    summary["reasons"] = [gate["reason"] for gate in gates.values() if not gate["passed"]]
    return summary, results
