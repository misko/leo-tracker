"""TLE-independent preservation of plausible dual-receiver Doppler tracks."""
from __future__ import annotations

import math

SPEED_OF_LIGHT_M_S = 299_792_458.0
EARTH_RADIUS_M = 6_371_000.0
EARTH_MU_M3_S2 = 3.986_004_418e14


def overhead_equivalent_altitude_m(radial_acceleration_m_s2: float) -> float | None:
    """Circular-orbit altitude producing this peak overhead radial acceleration."""
    acceleration = abs(float(radial_acceleration_m_s2))
    if not math.isfinite(acceleration) or acceleration <= 0:
        return None
    # a ~= v²/h and v² = mu/(R+h), hence h(R+h)=mu/a.
    return (-EARTH_RADIUS_M + math.sqrt(
        EARTH_RADIUS_M**2 + 4*EARTH_MU_M3_S2/acceleration))/2


def classify_doppler_observation(pair_report: dict, rx0_event: dict,
                                 rx1_event: dict, carrier_hz: float, *,
                                 minimum_duration_s: float = 2.0,
                                 minimum_path_correlation: float = 0.70,
                                 minimum_time_iou: float = 0.50,
                                 maximum_drift_difference_hz_s: float = 1_500,
                                 minimum_abs_drift_hz_s: float = 250,
                                 maximum_abs_drift_hz_s: float = 10_000) -> dict:
    """Classify an observation without conflating detection and identification."""
    if carrier_hz <= 0:
        raise ValueError("carrier frequency must be positive")
    association = pair_report.get("association") or {}
    drift0 = float(association.get("rx0_drift_hz_s", float("nan")))
    drift1 = float(association.get("rx1_drift_hz_s", float("nan")))
    mean_drift = (drift0+drift1)/2
    duration = min(float(rx0_event.get("duration_s", 0)),
                   float(rx1_event.get("duration_s", 0)))
    acceleration = -SPEED_OF_LIGHT_M_S*mean_drift/carrier_hz
    altitude = overhead_equivalent_altitude_m(acceleration)
    reasons = []
    if bool(association.get("broadband")):
        reasons.append("broadband event")
    if duration < minimum_duration_s:
        reasons.append("duration below observation threshold")
    if float(association.get("time_iou", 0)) < minimum_time_iou:
        reasons.append("insufficient receiver time overlap")
    if float(association.get("centered_path_correlation", 0)) < minimum_path_correlation:
        reasons.append("receiver paths are not correlated")
    if not math.isfinite(mean_drift) or not (
            minimum_abs_drift_hz_s <= abs(mean_drift) <= maximum_abs_drift_hz_s):
        reasons.append("drift outside tracked LEO range")
    if abs(drift0-drift1) > maximum_drift_difference_hz_s:
        reasons.append("receiver drift estimates disagree")
    starlink_zone = bool(altitude is not None and 300_000 <= altitude <= 2_000_000)
    return {
        "schema": "leo-tracker.doppler-observation/v1",
        "qualified": not reasons,
        "rejection_reasons": reasons,
        "duration_s": duration,
        "mean_drift_hz_s": mean_drift,
        "rx0_drift_hz_s": drift0,
        "rx1_drift_hz_s": drift1,
        "drift_difference_hz_s": abs(drift0-drift1),
        "path_correlation": float(association.get("centered_path_correlation", 0)),
        "frequency_direction": "decreasing" if mean_drift < 0 else "increasing",
        "radial_acceleration_m_s2": acceleration,
        "overhead_equivalent_altitude_km": None if altitude is None else altitude/1000,
        "starlink_altitude_zone": starlink_zone,
        "identified": False,
        "geometry_warning": "altitude proxy is valid only near an overhead closest approach",
    }
