from __future__ import annotations

import json
from pathlib import Path
import time

from .association import associate_tracks
from .adapters import comb_tracks, connected_component_tracks
from .broadband import (consensus_pilot_tracks, detect_activity_windows,
                        track_envelope_and_edges, track_spectral_translation)
from .controls import gain_transition_times
from .dedoppler import search_dedoppler
from .models import TrackerReport
from .observation import load_tracking_observation
from .viterbi import search_viterbi_ridges
from .tle_match import match_joint_tracks_to_tles


SCHEMA = "leo-tracker.tracker-ensemble/v1"
REFERENCES = {
    "dedoppler-linear/v1": "https://github.com/UCBerkeleySETI/turbo_seti",
    "viterbi-ridge/v1": "https://doi.org/10.1109/TIT.1967.1054010",
    "spectral-texture-translation/v1":
        "https://doi.org/10.1364/OL.33.000156",
    "coherent-carrier": "https://github.com/gnss-sdr/gnss-sdr",
}


def run_tracker_ensemble(measurement: Path, *, windows=None,
                         pass_catalog: dict | None = None,
                         integration_s: float = .5,
                         dedoppler_window_s: float = 10,
                         dedoppler_step_s: float = 5,
                         minimum_drift_hz_s: float = -15_000,
                         maximum_drift_hz_s: float = 15_000,
                         drift_step_hz_s: float = 500) -> TrackerReport:
    observation = load_tracking_observation(measurement)
    if windows is None:
        windows = detect_activity_windows(observation)
    if not windows:
        windows = [(float(observation.time_s[0]), float(observation.time_s[-1]))]
    # Retain only selected rows. Boolean selection owns a compact array, so the
    # decompressed full-dwell backing allocation can be released immediately.
    observation = observation.select_windows(windows)
    timings = {}; candidates = []

    started = time.perf_counter()
    candidates.extend(connected_component_tracks(observation, windows=windows))
    timings["connected-component-centroid/v1"] = time.perf_counter()-started

    started = time.perf_counter()
    dedoppler = []
    for window_start, window_stop in windows:
        selected = observation.window(window_start, window_stop)
        dedoppler.extend(search_dedoppler(selected, integration_s=integration_s,
            window_s=min(dedoppler_window_s, window_stop-window_start),
            step_s=dedoppler_step_s, minimum_drift_hz_s=minimum_drift_hz_s,
            maximum_drift_hz_s=maximum_drift_hz_s,
            drift_step_hz_s=drift_step_hz_s))
    timings["dedoppler-linear/v1"] = time.perf_counter()-started
    candidates.extend(dedoppler)

    started = time.perf_counter()
    viterbi = search_viterbi_ridges(observation, windows=windows,
                                    integration_s=integration_s)
    timings["viterbi-ridge/v1"] = time.perf_counter()-started
    candidates.extend(viterbi)

    started = time.perf_counter()
    comb = []
    for window_start, window_stop in windows:
        selected = observation.window(window_start, window_stop)
        comb.extend(comb_tracks(selected, integration_s=integration_s,
            window_s=min(dedoppler_window_s, window_stop-window_start),
            step_s=dedoppler_step_s))
    candidates.extend(comb)
    timings["comb-viterbi/v1"] = time.perf_counter()-started

    started = time.perf_counter()
    populations = consensus_pilot_tracks(dedoppler+viterbi)
    timings["multi-pilot-consensus/v1"] = time.perf_counter()-started
    candidates.extend(populations)

    started = time.perf_counter()
    candidates.extend(track_envelope_and_edges(observation, windows))
    timings["broadband-envelope-and-edges/v1"] = time.perf_counter()-started

    started = time.perf_counter()
    candidates.extend(track_spectral_translation(observation, windows))
    timings["spectral-texture-translation/v1"] = time.perf_counter()-started

    joint = []
    for tracker in sorted({item.tracker for item in candidates}):
        selected = [item for item in candidates if item.tracker == tracker]
        associations = associate_tracks(selected)
        # Association indexes are local to ``selected``; convert to the report
        # candidate list so persisted references are stable.
        indexes = [candidates.index(item) for item in selected]
        joint.extend(type(item)(item.tracker,
            (indexes[item.member_indexes[0]], indexes[item.member_indexes[1]]),
            item.receiver_path_correlation, item.receiver_frequency_offset_hz,
            item.drift_difference_hz_s, item.confidence, item.qualified,
            item.warnings) for item in associations)
    configuration = {"integration_s": integration_s,
        "dedoppler_window_s": dedoppler_window_s,
        "dedoppler_step_s": dedoppler_step_s,
        "minimum_drift_hz_s": minimum_drift_hz_s,
        "maximum_drift_hz_s": maximum_drift_hz_s,
        "drift_step_hz_s": drift_step_hz_s,
        "analysis_windows_s": [list(item) for item in windows],
        "references": REFERENCES}
    metrics = {"runtime_s_by_tracker": timings,
        "candidate_count_by_tracker": {tracker: sum(item.tracker == tracker
            for item in candidates) for tracker in sorted({item.tracker for item in candidates})},
        "qualified_count_by_tracker": {tracker: sum(item.tracker == tracker and item.qualified
            for item in candidates) for tracker in sorted({item.tracker for item in candidates})},
        "gain_transitions": gain_transition_times(
            observation.time_s, observation.hardware_gain_db)}
    identifications = []
    if pass_catalog is not None:
        observed_carrier = observation.center_frequency_hz+(observation.lnb_lo_hz or 0)
        identifications = match_joint_tracks_to_tles(candidates, joint, pass_catalog,
            capture_start_unix_s=float(observation.utc_ns[0]/1e9-observation.time_s[0]),
            observed_carrier_hz=observed_carrier)
    return TrackerReport(SCHEMA, str(measurement), configuration,
                         tuple(candidates), tuple(joint), metrics,
                         tuple(identifications))


def write_tracker_ensemble(measurement: Path, output: Path, *,
                           passes: Path | None = None,
                           plot: Path | None = None, **kwargs) -> dict:
    if passes is not None:
        kwargs["pass_catalog"] = json.loads(passes.read_text())
    report = run_tracker_ensemble(measurement, **kwargs).to_dict()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    if plot is not None:
        from .plot import plot_tracker_report
        plot_tracker_report(measurement, report, plot)
    return report
