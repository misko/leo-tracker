"""Common-result adapters for the established event and comb trackers."""
from __future__ import annotations

import numpy as np

from ..blind_comb import search_blind_comb
from ..events import detect_receiver_events
from .models import TrackCandidate
from .observation import TrackingObservation


def connected_component_tracks(observation: TrackingObservation, *,
                               threshold_db: float = .35,
                               windows=None,
                               maximum_candidates_per_receiver: int = 64) -> list[TrackCandidate]:
    selected_observations = ([observation] if not windows else
        [observation.window(start, stop) for start, stop in windows])
    candidates = []
    for selected in selected_observations:
      detected = detect_receiver_events(selected.spectra_db, selected.time_s,
          selected.frequency_hz, threshold_db=threshold_db)
      for receiver, events in enumerate(detected):
       receiver_candidates = []
       for event in events:
            times, path = np.asarray(event.time_s), np.asarray(event.centroid_hz)
            if times.size < 2:
                continue
            slope = float(np.polyfit(times-np.mean(times), path, 1)[0])
            qualified = bool(not event.broadband and event.duration_s >= 2 and
                             250 <= abs(slope) <= 15_000)
            warnings = []
            if event.broadband: warnings.append("connected component is broadband")
            if event.duration_s < 2: warnings.append("duration below two seconds")
            receiver_candidates.append(TrackCandidate("connected-component-centroid/v1", receiver,
                event.start_time_s, event.stop_time_s, tuple(event.time_s),
                tuple(event.centroid_hz), slope,
                frequency_low_hz=event.frequency_low_hz,
                frequency_high_hz=event.frequency_high_hz,
                signal_score=event.peak_residual_db, qualified=qualified,
                warnings=tuple(warnings), diagnostics={
                    "polarity": event.polarity, "broadband": event.broadband,
                    "occupancy_fraction": event.occupancy_fraction,
                    "median_bandwidth_hz": event.median_bandwidth_hz}))
       candidates.extend(sorted(receiver_candidates,
           key=lambda item: (item.qualified, item.signal_score, item.stop_time_s-item.start_time_s),
           reverse=True)[:maximum_candidates_per_receiver])
    return candidates


def comb_tracks(observation: TrackingObservation, *, spacing_hz: float = 43_949.5,
                nominal_offset_hz: float = 0, search_half_width_hz: float | None = None,
                integration_s: float = .5, window_s: float = 10,
                step_s: float = 5, permutations: int = 0) -> list[TrackCandidate]:
    # Nine teeth span eight spacings; retain guard bins so the held-out comb
    # never degenerates to an all--inf score near an undersized fixture/band.
    if np.ptp(observation.frequency_hz) < 8*spacing_hz+32*observation.bin_width_hz:
        return []
    if search_half_width_hz is None:
        search_half_width_hz = .4*float(np.ptp(observation.frequency_hz))
    report = search_blind_comb(observation.spectra_db, observation.utc_ns,
        observation.frequency_hz, nominal_offset_hz=nominal_offset_hz,
        search_half_width_hz=search_half_width_hz,
        integration_s=integration_s, window_s=window_s, step_s=step_s,
        comb_spacing_hz=spacing_hz, permutations=permutations)
    candidates = []
    for window_index, window in enumerate(report["candidates"]):
        # ``blind_comb`` reports elapsed time from the supplied artifact's first
        # UTC row. Restore the parent capture's elapsed-time origin for bounded
        # window replays and dashboard overlays.
        times = np.asarray(window["time_s"], float)+float(observation.time_s[0])
        for receiver in range(2):
            path = np.asarray(window["paths_hz"][receiver], float)
            receiver_report = window["receivers"][receiver]
            candidates.append(TrackCandidate("comb-viterbi/v1", receiver,
                float(times[0]), float(times[-1]), tuple(times.tolist()),
                tuple(path.tolist()), float(receiver_report["fitted_drift_hz_s"]),
                frequency_low_hz=float(path.min()), frequency_high_hz=float(path.max()),
                supporting_features=int(receiver_report["detected_tooth_count"]),
                signal_score=float(receiver_report["score_db"]),
                false_alarm_probability=window.get("empirical_false_alarm_probability"),
                qualified=bool(window["qualified"]),
                warnings=tuple(window["rejection_reasons"]), diagnostics={
                    "window_index": window_index, "spacing_hz": spacing_hz,
                    "path_correlation": window["path_correlation"],
                    "stationary_improvement_db": receiver_report["stationary_improvement_db"],
                    "wrong_spacing_improvement_db": receiver_report[
                        "comb_spacing_improvement_db"]}))
    return candidates
