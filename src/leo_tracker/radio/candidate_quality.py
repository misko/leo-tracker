"""Conservative qualification for dual-receiver orbital Doppler candidates."""
from __future__ import annotations


def qualify_joint_event(pair_report: dict, rx0_event: dict, rx1_event: dict, *,
                        minimum_duration_s: float = 3.0,
                        minimum_time_iou: float = 0.5,
                        minimum_path_correlation: float = 0.7,
                        maximum_receiver_drift_difference_hz_s: float = 3_000,
                        minimum_fit_points: int = 15,
                        maximum_fit_residual_hz: float = 30_000,
                        maximum_fit_drift_uncertainty_hz_s: float = 3_000,
                        minimum_tle_score: float = 0.2,
                        maximum_tle_drift_difference_hz_s: float = 1_500,
                        maximum_tle_residual_hz: float = 30_000) -> dict:
    """Return an auditable pass/fail decision; never infer missing evidence."""
    association = pair_report.get("association") or {}
    fits = pair_report.get("fits") or []
    matching = pair_report.get("tle_matching")
    reasons: list[str] = []
    if min(float(rx0_event.get("duration_s", 0)),
           float(rx1_event.get("duration_s", 0))) < minimum_duration_s:
        reasons.append("insufficient dual-receiver duration")
    if float(association.get("time_iou", 0)) < minimum_time_iou:
        reasons.append("insufficient receiver time overlap")
    if float(association.get("centered_path_correlation", 0)) < minimum_path_correlation:
        reasons.append("receiver paths are not correlated")
    if abs(float(association.get("drift_difference_hz_s", float("inf")))) > maximum_receiver_drift_difference_hz_s:
        reasons.append("receiver drift estimates disagree")
    supported = [fit for fit in fits
        if int(fit.get("points", 0)) >= minimum_fit_points
        and float(fit.get("residual_rms_hz", float("inf"))) <= maximum_fit_residual_hz
        and float(fit.get("drift_uncertainty_hz_s", float("inf"))) <= maximum_fit_drift_uncertainty_hz_s]
    if not supported:
        reasons.append("no stable supported Doppler fit")
    hypotheses = [] if matching is None else matching.get("hypotheses", [])
    if matching is None:
        reasons.append("TLE catalog was not supplied")
    elif matching.get("classification") != "ranked-hypothesis":
        reasons.append("TLE match is absent or ambiguous")
    elif not hypotheses:
        reasons.append("TLE match has no hypothesis")
    else:
        best = hypotheses[0]
        if float(best.get("score", 0)) < minimum_tle_score:
            reasons.append("TLE match score is too low")
        if float(best.get("drift_difference_hz_s", float("inf"))) > maximum_tle_drift_difference_hz_s:
            reasons.append("measured and predicted Doppler slopes disagree")
        if float(best.get("residual_rms_hz", float("inf"))) > maximum_tle_residual_hz:
            reasons.append("TLE Doppler residual is too large")
    return {"qualified": not reasons, "rejection_reasons": reasons,
            "minimum_duration_s": minimum_duration_s,
            "supported_fit_count": len(supported)}
