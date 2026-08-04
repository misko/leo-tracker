"""Compare measured carrier motion with TLE Doppler plus discrete carrier hops."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

import numpy as np


SCHEMA = "leo-tracker.tle-carrier-hopping/v1"


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc).timestamp()


def _fit_spacing(paths_hz: np.ndarray, predicted_hz: np.ndarray, spacing_hz: float,
                 *, uncertainty_hz: float, hop_penalty: float) -> dict:
    measured = paths_hz-paths_hz[:, :1]
    predicted = predicted_hz-predicted_hz[0]
    excess = measured-predicted[None, :]
    largest = int(np.ceil(np.max(abs(excess))/spacing_hz))+2
    states = np.arange(-largest, largest+1)
    emission = np.sum(((excess[:, :, None]-states[None, None, :]*spacing_hz) /
                       uncertainty_hz)**2, axis=0)
    accumulated = emission[0].copy(); back = np.zeros(emission.shape, np.int16)
    for row in range(1, emission.shape[0]):
        transition = accumulated[:, None]+hop_penalty*abs(states[:, None]-states[None, :])
        transition[abs(states[:, None]-states[None, :]) > 1] = np.inf
        selected = np.argmin(transition, axis=0)
        accumulated = emission[row]+transition[selected, np.arange(states.size)]
        back[row] = selected
    indexes = np.empty(emission.shape[0], int); indexes[-1] = int(np.argmin(accumulated))
    for row in range(emission.shape[0]-1, 0, -1):
        indexes[row-1] = int(back[row, indexes[row]])
    carrier_indexes = states[indexes]
    residuals = excess-carrier_indexes[None, :]*spacing_hz
    hop_count = int(np.sum(abs(np.diff(carrier_indexes))))
    return {"spacing_hz": float(spacing_hz), "carrier_indexes": carrier_indexes.tolist(),
        "hop_count": hop_count, "hop_rows": (np.flatnonzero(np.diff(carrier_indexes) != 0)+1).tolist(),
        "receiver_rms_residual_hz": np.sqrt(np.mean(residuals**2, axis=1)).tolist(),
        "joint_rms_residual_hz": float(np.sqrt(np.mean(residuals**2))),
        "maximum_absolute_residual_hz": float(np.max(abs(residuals))),
        "penalized_objective": float(np.min(accumulated)),
        "residuals_hz": residuals.tolist()}


def compare_carrier_to_tles(candidate: dict, capture_start_utc_ns: int, catalog: dict, *,
                            spacing_hz: float = 43_949.5,
                            wrong_spacings_hz: Sequence[float] = (35_000.0, 52_000.0),
                            uncertainty_hz: float = 7_500.0,
                            hop_penalty: float = 9.0,
                            minimum_wrong_spacing_advantage: float = .10) -> dict:
    """Rank overlapping passes without modifying the independently measured path."""
    paths = np.asarray(candidate["paths_hz"], float)
    relative_times = np.asarray(candidate["time_s"], float)
    if paths.ndim != 2 or paths.shape[0] != 2 or paths.shape[1] != relative_times.size:
        raise ValueError("candidate must contain two receiver paths matching time_s")
    if relative_times.size < 5 or np.any(np.diff(relative_times) <= 0):
        raise ValueError("candidate times must be increasing")
    absolute_times = float(capture_start_utc_ns)/1e9+relative_times
    reports = []
    for satellite in catalog.get("satellites", []):
        for pass_item in satellite.get("passes", []):
            points = [pass_item["rise"], pass_item["culmination"], pass_item["set"]]
            pass_times = np.asarray([_timestamp(point["time"]) for point in points])
            if absolute_times[0] < pass_times[0] or absolute_times[-1] > pass_times[-1]:
                continue
            predicted = np.interp(absolute_times, pass_times,
                [float(point["expected_doppler_hz"]) for point in points])
            correct = _fit_spacing(paths, predicted, spacing_hz,
                                   uncertainty_hz=uncertainty_hz, hop_penalty=hop_penalty)
            controls = [_fit_spacing(paths, predicted, value,
                                     uncertainty_hz=uncertainty_hz, hop_penalty=hop_penalty)
                        for value in wrong_spacings_hz]
            best_control = min(controls, key=lambda item: item["penalized_objective"])
            advantage = ((best_control["penalized_objective"]-correct["penalized_objective"]) /
                         max(best_control["penalized_objective"], np.finfo(float).eps))
            reasons = []
            if advantage < minimum_wrong_spacing_advantage:
                reasons.append("Starlink spacing does not beat carrier-hop spacing controls")
            if correct["joint_rms_residual_hz"] > uncertainty_hz:
                reasons.append("TLE plus carrier hops leaves excessive frequency residual")
            reports.append({"name": satellite["name"].strip(),
                "norad_id": int(satellite["norad_id"]),
                "max_elevation_deg": float(pass_item["culmination"]["elevation_deg"]),
                "window_start_utc": datetime.fromtimestamp(absolute_times[0], timezone.utc).isoformat().replace("+00:00", "Z"),
                "window_stop_utc": datetime.fromtimestamp(absolute_times[-1], timezone.utc).isoformat().replace("+00:00", "Z"),
                "predicted_net_doppler_hz": float(predicted[-1]-predicted[0]),
                "measured_net_shift_hz": [float(path[-1]-path[0]) for path in paths],
                "starlink_spacing_fit": correct, "wrong_spacing_fits": controls,
                "wrong_spacing_advantage_fraction": float(advantage),
                "qualified": not reasons, "rejection_reasons": reasons})
    reports.sort(key=lambda item: (item["qualified"],
        item["wrong_spacing_advantage_fraction"],
        -item["starlink_spacing_fit"]["penalized_objective"]), reverse=True)
    return {"schema": SCHEMA, "configuration": {"spacing_hz": spacing_hz,
        "wrong_spacings_hz": list(wrong_spacings_hz), "uncertainty_hz": uncertainty_hz,
        "hop_penalty": hop_penalty,
        "minimum_wrong_spacing_advantage": minimum_wrong_spacing_advantage},
        "overlapping_passes": len(reports),
        "qualified_count": sum(item["qualified"] for item in reports), "candidates": reports}
