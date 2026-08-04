"""End-to-end event analysis and honest v2 diagnostic rendering."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import math
from pathlib import Path

import numpy as np

from leo_tracker.fusion.event_matching import rank_event_against_passes

from .doppler_fit import fit_doppler_segments
from .candidate_quality import qualify_joint_event
from .doppler_observation import classify_doppler_observation
from .events import detect_receiver_events, residual_waterfall
from .joint_tracking import associate_receiver_events, estimate_lnb_offset_hz
from .measurement import load_measurement_waterfall
from .tle_search import search_tle_doppler
from .blind_comb import search_blind_comb
from .blind_carrier import search_blind_carriers
from .tle_hopping import compare_carrier_to_tles
from .tuning_dither import (center_transitions_in_interval,
                            dither_phase_locked,
                            reconstruct_interleaved_spectra,
                            retune_transient_confounded)


ANALYSIS_SCHEMA = "leo-tracker.measurement-analysis/v2"


def analyze_measurement_waterfall(path: Path, *, threshold_db: float = .35,
                                  minimum_time_bins: int = 3,
                                  minimum_frequency_bins: int = 3,
                                  event_frequency_bins: int = 1024,
                                  broadband_fraction: float = .65,
                                  carrier_hz: float | None = None,
                                  pass_catalog: dict | None = None,
                                  tle_dwell_window_s: float = 30.0,
                                  tle_dwell_step_s: float = 10.0,
                                  tle_minimum_window_s: float = 8.0,
                                  tle_comb_spacing_hz: float = 43_900.0,
                                  tle_minimum_comb_spacing_improvement: float = .03,
                                  tle_broadband_frequency_fraction: float = .2,
                                  tle_maximum_broadband_row_fraction: float = .2,
                                  blind_comb: bool = True,
                                  blind_comb_search_half_width_hz: float = 1_000_000,
                                  blind_carrier: bool = True) -> dict:
    artifact = load_measurement_waterfall(path)
    try:
        capture_identity = json.loads(str(artifact["identity_json"]))
    except (KeyError, json.JSONDecodeError):
        capture_identity = {}
    spectra, interleaved_dither = reconstruct_interleaved_spectra(artifact)
    utc = np.asarray(artifact["utc_ns"], np.int64)
    times = (utc - utc[0]) / 1e9
    frequencies = np.asarray(artifact["frequency_offsets_hz"], float)
    event_frequency_bins = min(int(event_frequency_bins), spectra.shape[2])
    if event_frequency_bins < 64:
        raise ValueError("event frequency bins must be at least 64")
    if spectra.shape[2] % event_frequency_bins:
        raise ValueError("event frequency bins must divide artifact bin count")
    event_spectra, event_frequencies = spectra, frequencies
    if event_frequency_bins < spectra.shape[2]:
        width = spectra.shape[2] // event_frequency_bins
        linear = np.power(10.0, spectra/10.0)
        event_spectra = 10*np.log10(linear.reshape(
            *spectra.shape[:2], event_frequency_bins, width).mean(axis=3))
        event_frequencies = frequencies.reshape(event_frequency_bins, width).mean(axis=1)
    receiver_events = detect_receiver_events(event_spectra, times, event_frequencies,
        threshold_db=threshold_db, min_time_bins=minimum_time_bins,
        min_frequency_bins=minimum_frequency_bins,
        broadband_fraction=broadband_fraction)
    pairs = associate_receiver_events(receiver_events[0], receiver_events[1]) if len(receiver_events) == 2 else []
    lnb_offset = None
    try:
        lnb_offset = estimate_lnb_offset_hz(pairs)
    except ValueError:
        pass
    if carrier_hz is None:
        lo = float(artifact["lnb_lo_hz"]) if "lnb_lo_hz" in artifact else 0.0
        carrier_hz = lo + float(artifact["center_frequency_hz"])
    pair_reports = []
    for pair in pairs:
        first, second = receiver_events[0][pair.rx0_index], receiver_events[1][pair.rx1_index]
        times0, path0 = np.asarray(first.time_s), np.asarray(first.centroid_hz)
        times1, path1 = np.asarray(second.time_s), np.asarray(second.centroid_hz) - pair.lnb_offset_hz
        start, stop = max(times0[0], times1[0]), min(times0[-1], times1[-1])
        common = times0[(times0 >= start) & (times0 <= stop)]
        combined = (path0[(times0 >= start) & (times0 <= stop)] + np.interp(common, times1, path1)) / 2
        fits = [] if pair.broadband else [item.to_dict() for item in fit_doppler_segments(
            common, combined, carrier_hz=carrier_hz, order=2)]
        matching = None
        if pass_catalog is not None and common.size >= 3:
            absolute_ns = utc[0] + np.rint(common*1e9).astype(np.int64)
            matching = rank_event_against_passes(absolute_ns, combined, pass_catalog)
        pair_report = {"association": pair.to_dict(), "fits": fits,
                       "tle_matching": matching}
        pair_report["qualification"] = qualify_joint_event(
            pair_report, first.to_dict(), second.to_dict())
        pair_report["doppler_observation"] = classify_doppler_observation(
            pair_report, first.to_dict(), second.to_dict(), carrier_hz)
        if "center_frequency_hz_by_snapshot" in artifact:
            snapshot_centers = np.asarray(
                artifact["center_frequency_hz_by_snapshot"], float)
            transitions = center_transitions_in_interval(
                times, snapshot_centers, start, stop)
            transition_times = times[1:][np.diff(snapshot_centers) != 0]
            transient_guard_s = 0.75
            transient_confounded, nearest_start_transition_s = (
                retune_transient_confounded(
                    start, transition_times, transient_guard_s))
            observation = pair_report["doppler_observation"]
            observation["dither_transitions_crossed"] = transitions
            observation["nearest_transition_to_start_s"] = nearest_start_transition_s
            observation["retune_transient_guard_s"] = transient_guard_s
            observation["retune_transient_confounded"] = transient_confounded
            observation["sky_fixed_by_dither"] = bool(
                observation["qualified"] and transitions > 0 and
                not transient_confounded)
            observation["origin_classification"] = (
                "sky-fixed" if observation["sky_fixed_by_dither"] else
                "retune-transient-confounded" if transient_confounded else
                "not-tested-across-transition")
        pair_reports.append(pair_report)
    provisional = [item["doppler_observation"] for item in pair_reports
        if (item.get("doppler_observation") or {}).get("sky_fixed_by_dither")]
    phase_locked, phase_center_s = dither_phase_locked([
        float(item["nearest_transition_to_start_s"]) for item in provisional
        if item.get("nearest_transition_to_start_s") is not None])
    if phase_locked:
        for observation in provisional:
            distance = observation.get("nearest_transition_to_start_s")
            if distance is not None and abs(float(distance)-float(phase_center_s)) <= .20:
                observation["sky_fixed_by_dither"] = False
                observation["dither_phase_locked_confounded"] = True
                observation["dither_phase_center_s"] = phase_center_s
                observation["origin_classification"] = "dither-phase-locked-confounded"
    intervals = np.diff(utc) / 1e9
    read_durations = np.asarray(artifact.get("read_duration_ns", []), float)
    finite_read_durations = read_durations[np.isfinite(read_durations)]
    retained_s = spectra.shape[1] * int(artifact["samples_per_snapshot"]) / float(artifact["sample_rate_hz"])
    span_s = (utc[-1] - utc[0]) / 1e9 + int(artifact["samples_per_snapshot"]) / float(artifact["sample_rate_hz"])
    tle_search = None if pass_catalog is None else search_tle_doppler(
        spectra, utc, frequencies, pass_catalog, threshold_db=threshold_db,
        dwell_window_s=tle_dwell_window_s, dwell_step_s=tle_dwell_step_s,
        minimum_window_s=tle_minimum_window_s,
        comb_spacing_hz=tle_comb_spacing_hz,
        minimum_comb_spacing_improvement=tle_minimum_comb_spacing_improvement,
        broadband_frequency_fraction=tle_broadband_frequency_fraction,
        maximum_broadband_row_fraction=tle_maximum_broadband_row_fraction)
    rf_center_hz = carrier_hz
    starlink_centers = np.asarray((11_075_000_000.0, 11_325_000_000.0, 11_575_000_000.0))
    nominal_offset_hz = float(starlink_centers[np.argmin(abs(starlink_centers-rf_center_hz))]
                              - rf_center_hz)
    blind_search = None
    if blind_comb:
        try:
            blind_search = search_blind_comb(
                spectra, utc, frequencies, nominal_offset_hz=nominal_offset_hz,
                search_half_width_hz=blind_comb_search_half_width_hz,
                broadband_threshold_db=threshold_db,
                broadband_frequency_fraction=tle_broadband_frequency_fraction,
                maximum_broadband_row_fraction=tle_maximum_broadband_row_fraction)
        except ValueError as error:
            blind_search = {"schema": "leo-tracker.blind-comb-search/v1",
                            "available": False, "reason": str(error),
                            "window_count": 0, "qualified_count": 0,
                            "candidates": []}
    carrier_search = None
    if blind_carrier:
        try:
            carrier_search = search_blind_carriers(
                spectra, utc, frequencies, nominal_offset_hz=nominal_offset_hz,
                search_half_width_hz=blind_comb_search_half_width_hz,
                broadband_threshold_db=threshold_db,
                broadband_frequency_fraction=tle_broadband_frequency_fraction,
                maximum_broadband_row_fraction=tle_maximum_broadband_row_fraction)
        except ValueError as error:
            carrier_search = {"schema": "leo-tracker.blind-carrier-search/v1",
                "available": False, "reason": str(error), "window_count": 0,
                "qualified_count": 0, "candidates": []}
    hopping_search = None
    carrier_candidates = [] if carrier_search is None else carrier_search.get("candidates", [])
    if pass_catalog is not None and carrier_candidates:
        hopping_search = compare_carrier_to_tles(carrier_candidates[0], int(utc[0]),
                                                 pass_catalog)
    return {"schema": ANALYSIS_SCHEMA, "source": str(path),
        "capture_identity": capture_identity,
        "measurement": {"gain_mode": str(artifact["gain_mode"]),
            "configured_gain_db": None if np.isnan(float(artifact["configured_gain_db"])) else float(artifact["configured_gain_db"]),
            "retained_sample_time_s": retained_s, "observation_span_s": span_s,
            "duty_fraction": retained_s/span_s, "median_snapshot_interval_s": float(np.median(intervals)),
            "maximum_clip_fraction": None if np.all(np.isnan(artifact["clip_fraction"])) else float(np.nanmax(artifact["clip_fraction"])),
            "artifact_frequency_bins": int(spectra.shape[2]),
            "event_frequency_bins": int(event_frequency_bins),
            "median_read_duration_s": (None if not finite_read_durations.size else
                float(np.median(finite_read_durations))/1e9),
            "maximum_read_duration_s": (None if not finite_read_durations.size else
                float(np.max(finite_read_durations))/1e9),
            "host_bracket_half_width_s": (None if not finite_read_durations.size else
                float(np.max(finite_read_durations))/2e9)},
        "events": [[event.to_dict() for event in events] for events in receiver_events],
        "joint_events": pair_reports, "estimated_lnb_offset_hz": lnb_offset,
        "carrier_hz": carrier_hz, "tle_guided_search": tle_search,
        "blind_comb_search": blind_search, "blind_carrier_search": carrier_search,
        "tle_carrier_hopping_search": hopping_search,
        "interleaved_dither": interleaved_dither}


def paired_doppler_paths(analysis: dict, receiver: int) -> list[dict]:
    """Return only dual-receiver-associated centroid paths for plotting."""
    if receiver not in (0, 1):
        raise ValueError("receiver must be 0 or 1")
    events = (analysis.get("events") or [])
    if receiver >= len(events):
        return []
    paths = []
    for index, item in enumerate(analysis.get("joint_events") or []):
        association = item.get("association") or {}
        event_index = association.get(f"rx{receiver}_index")
        if event_index is None or not 0 <= int(event_index) < len(events[receiver]):
            continue
        event = events[receiver][int(event_index)]
        if event.get("broadband"):
            continue
        observation = item.get("doppler_observation") or {}
        drift = association.get(f"rx{receiver}_drift_hz_s",
                                observation.get("mean_drift_hz_s", 0.0))
        paths.append({"track_index": index, "time_s": event.get("time_s", []),
            "centroid_hz": event.get("centroid_hz", []),
            "drift_hz_s": float(drift),
            "validated": bool(observation.get("qualified", False)),
            "path_correlation": float(association.get("centered_path_correlation", 0)),
            "association_score": float(association.get("association_score", 0))})
    return paths


def annotated_doppler_tracks(analysis: dict, *, maximum: int = 16,
                             minimum_duration_s: float = .5) -> list[dict]:
    """Select readable, time-distributed dual-RX tracks for plot annotation.

    The event detector deliberately emits many overlapping fragments.  This
    view keeps the strongest coherent fragment in each 30-second interval,
    then fills remaining slots by quality.  It does not change detection or
    qualification results; it is only a display policy.
    """
    by_receiver = [{item["track_index"]: item for item in paired_doppler_paths(
        analysis, receiver)} for receiver in (0, 1)]
    candidates = []
    for track_index in sorted(set(by_receiver[0]) & set(by_receiver[1])):
        paths = [by_receiver[receiver][track_index] for receiver in (0, 1)]
        durations = []
        for path in paths:
            time_s = np.asarray(path["time_s"], float)
            durations.append(float(np.ptp(time_s)) if len(time_s) > 1 else 0.0)
        duration = min(durations)
        correlation = min(path["path_correlation"] for path in paths)
        association = min(path["association_score"] for path in paths)
        if duration < minimum_duration_s or correlation < .35:
            continue
        midpoint = float(np.mean(np.asarray(paths[0]["time_s"], float)))
        score = duration * (.5 + max(0.0, correlation)) * (.5 + association)
        if any(path["validated"] for path in paths):
            score *= 2
        candidates.append({"track_index": track_index, "paths": paths,
            "duration_s": duration, "midpoint_s": midpoint, "score": score,
            "validated": any(path["validated"] for path in paths)})
    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected, occupied_intervals = [], set()
    for candidate in candidates:
        interval = int(candidate["midpoint_s"] // 30)
        if interval not in occupied_intervals:
            selected.append(candidate); occupied_intervals.add(interval)
        if len(selected) == maximum:
            break
    if len(selected) < maximum:
        selected_ids = {item["track_index"] for item in selected}
        selected.extend(item for item in candidates
                        if item["track_index"] not in selected_ids)
        selected = selected[:maximum]
    return sorted(selected, key=lambda item: item["midpoint_s"])


def plot_measurement_analysis(path: Path, analysis: dict, output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    artifact = load_measurement_waterfall(path)
    spectra, interleaved_dither = reconstruct_interleaved_spectra(artifact)
    utc = np.asarray(artifact["utc_ns"], np.int64); times = (utc-utc[0])/1e9
    frequencies = np.asarray(artifact["frequency_offsets_hz"], float)/1e6
    residuals = np.asarray([residual_waterfall(item) for item in spectra])
    common = np.mean(residuals, axis=0) if len(residuals) == 2 else residuals[0]
    difference = residuals[0]-residuals[1] if len(residuals) == 2 else np.zeros_like(residuals[0])
    fig, axes = plt.subplots(3, 2, figsize=(15, 12), sharex=True, sharey=True,
                             constrained_layout=True)
    absolute_limits = np.percentile(spectra, [.5, 99.5])
    extent = [frequencies[0], frequencies[-1], times[0], times[-1]]
    annotated_tracks = annotated_doppler_tracks(analysis)
    track_colors = plt.colormaps["turbo"](
        np.linspace(.08, .92, max(1, len(annotated_tracks))))
    for receiver in range(min(2, spectra.shape[0])):
        image = axes[0, receiver].imshow(spectra[receiver], origin="lower", aspect="auto",
            extent=extent, cmap="viridis", vmin=absolute_limits[0], vmax=absolute_limits[1])
        axes[0, receiver].set_title(
            f"RX{receiver} absolute raw-code PSD" +
            (" · reconstructed RF axis" if interleaved_dither else ""))
        fig.colorbar(image, ax=axes[0, receiver], label="dB raw-code²/Hz")
        residual_image = axes[1, receiver].imshow(residuals[receiver], origin="lower",
            aspect="auto", extent=extent, cmap="RdBu_r", vmin=-1, vmax=1)
        axes[1, receiver].set_title(
            f"RX{receiver} temporal residual · {len(annotated_tracks)} fitted Doppler tracks")
        frequency_span_hz = max(1.0, float(np.ptp(frequencies))*1e6)
        time_span_s = max(1.0, float(np.ptp(times)))
        for display_index, (candidate, color) in enumerate(
                zip(annotated_tracks, track_colors), 1):
            track = candidate["paths"][receiver]
            track_times = np.asarray(track["time_s"], float)
            track_hz = np.asarray(track["centroid_hz"], float)
            if len(track_times) < 2:
                continue
            slope_hz_s, intercept_hz = np.polyfit(track_times, track_hz, 1)
            endpoints_s = np.asarray([track_times.min(), track_times.max()])
            endpoints_hz = slope_hz_s*endpoints_s + intercept_hz
            tilt_deg = np.degrees(np.arctan2(
                (endpoints_hz[1]-endpoints_hz[0])/frequency_span_hz,
                (endpoints_s[1]-endpoints_s[0])/time_span_s))
            linewidth = 2.8 if candidate["validated"] else 2.0
            axes[1, receiver].plot(endpoints_hz/1e6, endpoints_s, color="black",
                lw=linewidth+2.2, alpha=.8, solid_capstyle="round")
            axes[1, receiver].plot(endpoints_hz/1e6, endpoints_s, color=color,
                lw=linewidth, alpha=.98, solid_capstyle="round")
            label = (f"D{display_index:02d}  {slope_hz_s/1000:+.2f} kHz/s\n"
                     f"tilt {tilt_deg:+.0f}° · {candidate['duration_s']:.1f}s")
            axes[1, receiver].annotate(label,
                (endpoints_hz[1]/1e6, endpoints_s[1]), xytext=(5, 3),
                textcoords="offset points", fontsize=6.5, color="white",
                va="bottom", ha="left", clip_on=True,
                bbox={"boxstyle": "round,pad=.25", "facecolor": "#10131a",
                      "edgecolor": color, "alpha": .88, "linewidth": .8})
        axes[1, receiver].text(.01, .99,
            "D##: paired track · rate is physical · tilt is plot-relative from vertical",
            transform=axes[1, receiver].transAxes, va="top", ha="left",
            fontsize=7, color="white",
            bbox={"facecolor": "black", "alpha": .55, "edgecolor": "none", "pad": 2})
        tle_search = analysis.get("tle_guided_search") or {}
        candidates = tle_search.get("candidates") or []
        if candidates:
            candidate = next((item for item in candidates if item.get("qualified")), candidates[0])
            points = candidate.get("predicted_points") or []
            receiver_report = next((item for item in candidate.get("receivers", [])
                                    if int(item.get("receiver", -1)) == receiver), None)
            if len(points) == 3 and receiver_report is not None:
                def timestamp(value):
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                        timezone.utc).timestamp()
                point_times = np.asarray([timestamp(point["time"]) for point in points])
                point_doppler = np.asarray([point["expected_doppler_hz"] for point in points])
                absolute_times = utc / 1e9
                predicted = np.interp(absolute_times, point_times, point_doppler)
                window_start = timestamp(candidate.get("window_start_utc", points[0]["time"]))
                window_stop = timestamp(candidate.get("window_stop_utc", points[-1]["time"]))
                supported = (absolute_times >= window_start) & (absolute_times <= window_stop)
                supported_indexes = np.flatnonzero(supported)
                reference = predicted[supported_indexes[len(supported_indexes)//2]]
                relative = predicted - reference
                path_mhz = (float(receiver_report["frequency_bias_hz"]) + relative) / 1e6
                style = "-" if candidate.get("qualified") else "--"
                model_label = candidate.get("signal_model", "single-tone")
                label = (f"TLE {candidate['norad_id']} matched path" if candidate.get("qualified")
                         else f"best rejected TLE {candidate['norad_id']}") + f" · {model_label}"
                axes[1, receiver].plot(path_mhz[supported], times[supported], style, color="#ff00ff",
                                       lw=1.5, alpha=.9, label=label)
                axes[1, receiver].legend(loc="upper right", fontsize=7)
        blind = analysis.get("blind_comb_search") or {}
        blind_candidates = blind.get("candidates") or []
        if blind_candidates:
            candidate = next((item for item in blind_candidates if item.get("qualified")),
                             blind_candidates[0])
            axes[1, receiver].plot(np.asarray(candidate["paths_hz"][receiver])/1e6,
                candidate["time_s"], color="#00ff88", lw=1.6,
                linestyle="-" if candidate.get("qualified") else "--",
                label=("RF-blind qualified comb" if candidate.get("qualified")
                       else "best rejected RF-blind comb"))
            axes[1, receiver].legend(loc="upper right", fontsize=7)
        carrier = analysis.get("blind_carrier_search") or {}
        carrier_candidates = carrier.get("candidates") or []
        if carrier_candidates:
            candidate = next((item for item in carrier_candidates if item.get("qualified")),
                             carrier_candidates[0])
            axes[1, receiver].plot(np.asarray(candidate["paths_hz"][receiver])/1e6,
                candidate["time_s"], color="#ff8c00", lw=1.5,
                linestyle="-" if candidate.get("qualified") else ":",
                label=("RF-blind carrier" if candidate.get("qualified")
                       else "best rejected RF-blind carrier"))
            axes[1, receiver].legend(loc="upper right", fontsize=7)
        fig.colorbar(residual_image, ax=axes[1, receiver], label="Residual dB")
    for axis, values, title in ((axes[2, 0], common, "RX common-mode residual"),
                                (axes[2, 1], difference, "RX0 − RX1 residual")):
        rendered = axis.imshow(values, origin="lower", aspect="auto", extent=extent,
                               cmap="RdBu_r", vmin=-1, vmax=1)
        axis.set_title(title); fig.colorbar(rendered, ax=axis, label="Residual dB")
    for axis in axes[:, 0]: axis.set_ylabel("Elapsed time (s)")
    for axis in axes[-1]: axis.set_xlabel("Baseband frequency offset (MHz)")
    lo_hz = float(artifact["lnb_lo_hz"]) if "lnb_lo_hz" in artifact else 0.0
    rf_center_hz = lo_hz + float(artifact["center_frequency_hz"])
    tle_search = analysis.get("tle_guided_search")
    tle_label = ("TLE unavailable" if tle_search is None else
                 f"TLE {tle_search['qualified_count']}/{tle_search['overlapping_passes']}")
    blind = analysis.get("blind_comb_search") or {}
    blind_label = f"blind comb {blind.get('qualified_count', 0)}/{blind.get('window_count', 0)}"
    carrier = analysis.get("blind_carrier_search") or {}
    carrier_label = f"blind carrier {carrier.get('qualified_count', 0)}/{carrier.get('window_count', 0)}"
    fig.suptitle(f"Measurement v2 · RF {rf_center_hz/1e9:.6f} GHz · "
                 f"{float(np.median(np.diff(artifact['frequency_offsets_hz'])))/1e3:.1f} kHz/bin · "
                 f"gain {analysis['measurement']['gain_mode']} · "
                 f"duty {analysis['measurement']['duty_fraction']*100:.1f}% · {tle_label} · "
                 f"{blind_label} · {carrier_label}")
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=140); plt.close(fig)
    carrier_candidates = (analysis.get("blind_carrier_search") or {}).get("candidates") or []
    if carrier_candidates:
        candidate = next((item for item in carrier_candidates if item.get("qualified")),
                         carrier_candidates[0])
        configuration = (analysis.get("blind_carrier_search") or {}).get("configuration") or {}
        center_hz = float(configuration.get("nominal_offset_hz", 0))
        half_width_hz = float(configuration.get("search_half_width_hz", 1_000_000))
        selected = np.flatnonzero(abs(np.asarray(artifact["frequency_offsets_hz"])-center_hz)
                                  <= half_width_hz)
        if selected.size:
            start = float(candidate["time_s"][0]); stop = float(candidate["time_s"][-1])
            zoom_rows = np.flatnonzero((times >= max(times[0], start-5)) &
                                       (times <= min(times[-1], stop+5)))
            zf = frequencies[selected]
            zoom_fig, zoom_axes = plt.subplots(2, 2, figsize=(13, 8),
                                               constrained_layout=True)
            for receiver in range(2):
                values = residuals[receiver][np.ix_(zoom_rows, selected)]
                rendered = zoom_axes[0, receiver].imshow(values, origin="lower", aspect="auto",
                    extent=[zf[0], zf[-1], times[zoom_rows[0]], times[zoom_rows[-1]]],
                    cmap="RdBu_r", vmin=-1, vmax=1)
                zoom_axes[0, receiver].plot(np.asarray(candidate["paths_hz"][receiver])/1e6,
                    candidate["time_s"], color="#ff8c00", lw=2, label="held-out validated path")
                hopping = analysis.get("tle_carrier_hopping_search") or {}
                hopping_candidates = hopping.get("candidates") or []
                if hopping_candidates:
                    fit = hopping_candidates[0]["starlink_spacing_fit"]
                    model = (np.asarray(candidate["paths_hz"][receiver])-
                             np.asarray(fit["residuals_hz"][receiver]))/1e6
                    zoom_axes[0, receiver].plot(model, candidate["time_s"], color="#d100ff",
                        lw=1.4, linestyle="--", label="TLE + carrier-index model")
                zoom_axes[0, receiver].set(title=f"RX{receiver} nominal-channel zoom",
                    xlabel="Baseband offset (MHz)", ylabel="Elapsed time (s)")
                zoom_axes[0, receiver].legend(fontsize=8); zoom_fig.colorbar(
                    rendered, ax=zoom_axes[0, receiver], label="Temporal residual (dB)")
                profiles = candidate.get("motion_compensated_profiles") or []
                if receiver < len(profiles):
                    profile = profiles[receiver]
                    offsets = np.asarray(profile["peak_offsets_from_track_hz"])/1e3
                    prominence = np.asarray(profile["peak_prominence_db"])
                    zoom_axes[1, receiver].stem(offsets, prominence, basefmt=" ")
                    zoom_axes[1, receiver].axvline(0, color="#ff8c00", lw=1)
                    zoom_axes[1, receiver].set(title=f"RX{receiver} motion-compensated peaks",
                        xlabel="Offset from tracked carrier (kHz)", ylabel="Prominence (dB)")
                    zoom_axes[1, receiver].grid(alpha=.2)
            zoom_fig.suptitle(f"RF-blind carrier detail · {candidate['window_start_utc']} · "
                              f"qualified={candidate.get('qualified', False)}")
            zoom_output = output.with_name(output.stem+"-carrier-zoom"+output.suffix)
            zoom_fig.savefig(zoom_output, dpi=150); plt.close(zoom_fig)


def write_measurement_analysis(path: Path, output: Path, *, plot: Path | None = None,
                               pass_catalog_path: Path | None = None, **kwargs) -> dict:
    catalog = None if pass_catalog_path is None else json.loads(Path(pass_catalog_path).read_text())
    report = analyze_measurement_waterfall(path, pass_catalog=catalog, **kwargs)
    report["pass_catalog"] = (None if pass_catalog_path is None else {
        "path": str(pass_catalog_path),
        "generated_at": catalog.get("generated_at"),
        "carrier_hz": catalog.get("carrier_hz"),
    })
    gps = (report.get("capture_identity") or {}).get("gps_fix")
    observer = None if catalog is None else catalog.get("observer")
    report["observer_validation"] = None
    if gps and observer:
        lat1, lat2 = map(math.radians, (float(gps["latitude_deg"]),
                                       float(observer["latitude_deg"])))
        delta_lat = lat2-lat1
        delta_lon = math.radians(float(observer["longitude_deg"])
                                 - float(gps["longitude_deg"]))
        a = math.sin(delta_lat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(delta_lon/2)**2
        separation_m = 2*6_371_000*math.asin(math.sqrt(a))
        clock_offset = gps.get("host_minus_gps_s")
        report["observer_validation"] = {
            "gps_to_catalog_separation_m": separation_m,
            "position_within_100m": separation_m <= 100,
            "observed_host_minus_gps_s": clock_offset,
            "timing_within_1s": (None if clock_offset is None else abs(float(clock_offset)) <= 1),
            "gps_fix_quality": gps.get("fix_quality"),
            "gps_satellites": gps.get("satellites"), "gps_hdop": gps.get("hdop"),
        }
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if plot is not None: plot_measurement_analysis(path, report, plot)
    return report
