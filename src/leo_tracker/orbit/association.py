"""Long-track association of receiver-referenced Doppler with archived TLEs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
from sgp4.api import Satrec, SatrecArray, jday

from .artifacts import TLECatalogArtifact, parse_catalog, parse_utc, utc_iso
from .archive import SNAPSHOT_SCHEMA
from .doppler import SPEED_OF_LIGHT_M_S
from .tle import TLE
from .topocentric import Observer


ASSOCIATION_SCHEMA = "leo-tracker.starlink-tle-association/v2"
TRACK_SCHEMA = "leo-tracker.starlink-continuous-track/v1"


def _read_catalog_or_snapshot(path: Path) -> tuple[TLECatalogArtifact, Path]:
    path = Path(path)
    value = json.loads(path.read_text())
    if value.get("schema") != SNAPSHOT_SCHEMA:
        return TLECatalogArtifact.from_dict(value), path
    root = path.parent.parent if path.parent.name == "snapshots" else path.parent
    object_path = root / value["object"]
    return TLECatalogArtifact.read(object_path), object_path


def catalog_doppler(tles: list[TLE], observer: Observer,
                    unix_times_s: np.ndarray, carrier_hz: float | np.ndarray
                    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized SGP4 Doppler/elevation prediction for satellites by time."""
    times = np.asarray(unix_times_s, dtype=float)
    if not tles or times.ndim != 1 or not times.size:
        raise ValueError("at least one TLE and time are required")
    carriers = np.asarray(carrier_hz, dtype=float)
    if (carriers.ndim > 1 or (carriers.ndim == 1 and carriers.shape != times.shape) or
            np.any(carriers <= 0) or not np.all(np.isfinite(carriers)) or
            not np.all(np.isfinite(times))):
        raise ValueError("carrier and times must be finite and positive")
    moments = [datetime.fromtimestamp(value, timezone.utc) for value in times]
    parts = [jday(item.year, item.month, item.day, item.hour, item.minute,
                  item.second + item.microsecond / 1e6) for item in moments]
    jd = np.asarray([item[0] for item in parts])
    fraction = np.asarray([item[1] for item in parts])
    satellites = SatrecArray([Satrec.twoline2rv(item.line1, item.line2) for item in tles])
    errors, positions, velocities = satellites.sgp4(jd, fraction)

    centuries = (jd + fraction - 2451545.0) / 36525.0
    gmst_deg = (280.46061837 + 360.98564736629 *
        (jd + fraction - 2451545.0) + .000387933 * centuries**2 -
        centuries**3 / 38710000.0) % 360.0
    theta = np.radians(gmst_deg)
    c, s = np.cos(theta)[None, :], np.sin(theta)[None, :]
    x = c * positions[:, :, 0] + s * positions[:, :, 1]
    y = -s * positions[:, :, 0] + c * positions[:, :, 1]
    z = positions[:, :, 2]
    vx_rot = c * velocities[:, :, 0] + s * velocities[:, :, 1]
    vy_rot = -s * velocities[:, :, 0] + c * velocities[:, :, 1]
    vz = velocities[:, :, 2]
    omega = 7.29211514670698e-5
    vx, vy = vx_rot + omega * y, vy_rot - omega * x

    observer_ecef = np.asarray(observer.ecef_km())
    dx, dy, dz = x - observer_ecef[0], y - observer_ecef[1], z - observer_ecef[2]
    distance = np.sqrt(dx**2 + dy**2 + dz**2)
    range_rate_km_s = (dx * vx + dy * vy + dz * vz) / distance
    lat, lon = np.radians([observer.latitude_deg, observer.longitude_deg])
    up = (np.cos(lat) * np.cos(lon) * dx + np.cos(lat) * np.sin(lon) * dy +
          np.sin(lat) * dz)
    elevation_deg = np.degrees(np.arcsin(np.clip(up / distance, -1, 1)))
    doppler_hz = -carriers * range_rate_km_s * 1000 / SPEED_OF_LIGHT_M_S
    valid = errors == 0
    return (np.where(valid, doppler_hz, np.nan),
            np.where(valid, elevation_deg, -90.0), errors)


def _screen_tles(tles: list[TLE], observer: Observer, start_s: float, stop_s: float,
                 carrier_hz: float, horizon_deg: float,
                 candidate_limit: int) -> list[TLE]:
    count = max(3, int(np.ceil((stop_s - start_s) / 30)) + 1)
    times = np.linspace(start_s, stop_s, count)
    _, elevation, _ = catalog_doppler(tles, observer, times, carrier_hz)
    maximum = np.nanmax(elevation, axis=1)
    selected = np.flatnonzero(maximum >= horizon_deg)
    selected = selected[np.argsort(maximum[selected])[::-1]]
    if candidate_limit:
        selected = selected[:candidate_limit]
    return [tles[int(index)] for index in selected]


def _observation_rows(track: dict, default_carrier_hz: float | None = None) -> dict:
    rows = []
    consensus_only = True
    for observation in track.get("observations", []):
        if not observation.get("utc"):
            continue
        time_s = parse_utc(observation["utc"]).timestamp()
        carrier_hz = float(observation.get("nominal_rf_hz", default_carrier_hz or 1.0))
        if carrier_hz <= 0:
            continue
        nuisance_group = int(observation.get("nuisance_group", 0))
        valid_receivers = [receiver for receiver in observation.get("receivers", [])
                           if receiver.get("valid")]
        # A production association is based on a simultaneous dual-RX RF
        # observation, not a single-path detector excursion. This also makes
        # the fitted relative LNB nuisance observable at every retained epoch.
        if len(valid_receivers) != 2:
            continue
        consensus = observation.get("consensus", {})
        if consensus.get("valid") and consensus.get(
                "receiver_referenced_cfo_hz") is not None:
            # The tracker has already measured RX1-RX0 offset/drift using the
            # simultaneous paths. Consume that calibrated consensus once;
            # fitting two unrelated receiver slopes here would throw away the
            # calibration and let nuisance terms absorb orbital curvature.
            rows.append({"time_s": time_s, "receiver": nuisance_group,
                         "carrier_hz": carrier_hz,
                         "observed_hz": float(
                             consensus["receiver_referenced_cfo_hz"]),
                         "sigma_hz": max(float(consensus.get(
                             "frequency_sigma_hz", 100)), 10.0)})
            continue
        for receiver in valid_receivers:
            consensus_only = False
            rows.append({"time_s": time_s,
                         "receiver": 2 * nuisance_group + int(receiver["receiver"]),
                         "carrier_hz": carrier_hz,
                         "observed_hz": float(receiver["frequency_offset_hz"]),
                         "sigma_hz": max(float(receiver.get("formal_sigma_hz", 100)), 10.0)})
    if not rows:
        return {"times": np.empty(0), "receiver": np.empty(0, int),
                "observed": np.empty(0), "sigma": np.empty(0),
                "carrier": np.empty(0), "common_drift": False}
    return {"times": np.asarray([item["time_s"] for item in rows]),
            "receiver": np.asarray([item["receiver"] for item in rows], int),
            "observed": np.asarray([item["observed_hz"] for item in rows]),
            "sigma": np.asarray([item["sigma_hz"] for item in rows]),
            "carrier": np.asarray([item["carrier_hz"] for item in rows]),
            # Consensus has already removed differential RX1/RX0 LNB drift.
            # What remains is one physical LNB/satellite-clock drift shared by
            # all retuned channel groups, plus static per-channel offsets.
            "common_drift": consensus_only}


def _fit_affine_nuisance(times: np.ndarray, receivers: np.ndarray,
                         target: np.ndarray, sigma: np.ndarray,
                         train: np.ndarray, maximum_drift_hz_s: float,
                         common_drift: bool = False,
                         ) -> tuple[np.ndarray, np.ndarray]:
    reference = float(np.mean(times[train]))
    centered = times - reference
    identities = sorted(set(map(int, receivers)))
    if common_drift:
        design = np.column_stack(
            [(receivers == receiver).astype(float) for receiver in identities] +
            [centered])
    else:
        design = np.column_stack([
            values for receiver in identities
            for values in ((receivers == receiver).astype(float),
                           (receivers == receiver).astype(float) * centered)])
    weighted = design[train] / sigma[train, None]
    coefficients, *_ = np.linalg.lstsq(weighted, target[train] / sigma[train], rcond=None)
    # A free line can exactly absorb the geometric Doppler slope of a short LEO
    # arc. Bound only the oscillator drift; constant LNB offsets remain free.
    # Refit each intercept after clipping so the constraint does not bias it.
    if common_drift:
        drift_index = len(identities)
        coefficients[drift_index] = np.clip(
            coefficients[drift_index], -maximum_drift_hz_s, maximum_drift_hz_s)
        for position, receiver in enumerate(identities):
            selected = train & (receivers == receiver)
            weights = 1 / sigma[selected] ** 2
            coefficients[position] = np.average(
                target[selected] - coefficients[drift_index] * centered[selected],
                weights=weights)
    else:
        for position, receiver in enumerate(identities):
            drift_index = 2 * position + 1
            coefficients[drift_index] = np.clip(
                coefficients[drift_index], -maximum_drift_hz_s, maximum_drift_hz_s)
            selected = train & (receivers == receiver)
            weights = 1 / sigma[selected] ** 2
            coefficients[2 * position] = np.average(
                target[selected] - coefficients[drift_index] * centered[selected],
                weights=weights)
    return target - design @ coefficients, np.concatenate(([reference], coefficients))


def _groupwise_temporal_split(times: np.ndarray, receivers: np.ndarray,
                              train_fraction: float) -> tuple[np.ndarray, np.ndarray]:
    """Hold out the latest observations within every nuisance group.

    Starlink can move between RF channels sequentially, so one global time
    split can place an entire later channel in holdout. Its static tuner/LNB
    offset would then be unidentifiable from training data. Splitting each
    group chronologically retains genuine future-data validation while ensuring
    that every fitted channel/receiver nuisance is represented on both sides.
    """
    train = np.zeros(times.size, bool)
    holdout = np.zeros(times.size, bool)
    for receiver in sorted(set(map(int, receivers))):
        selected = np.flatnonzero(receivers == receiver)
        if selected.size < 2:
            raise ValueError("each nuisance group requires training and holdout observations")
        ordered = selected[np.argsort(times[selected], kind="stable")]
        unique_times = np.unique(times[ordered])
        if unique_times.size >= 2:
            split = float(unique_times[0] + train_fraction *
                          (unique_times[-1] - unique_times[0]))
            group_train = ordered[times[ordered] <= split]
            group_holdout = ordered[times[ordered] > split]
        else:
            cutoff = int(np.clip(np.ceil(train_fraction * ordered.size),
                                 1, ordered.size - 1))
            group_train, group_holdout = ordered[:cutoff], ordered[cutoff:]
        if not group_train.size or not group_holdout.size:
            cutoff = int(np.clip(np.ceil(train_fraction * ordered.size),
                                 1, ordered.size - 1))
            group_train, group_holdout = ordered[:cutoff], ordered[cutoff:]
        train[group_train] = True
        holdout[group_holdout] = True
    return train, holdout


def rank_doppler_track(rows: dict, tles: list[TLE], predicted_times_s: np.ndarray,
                       predicted_doppler_hz: np.ndarray, *,
                       epoch_search_s: float = 2.0, epoch_step_s: float = .05,
                       epoch_coarse_step_s: float = .5,
                       train_fraction: float = .6,
                       maximum_nuisance_drift_hz_s: float = 200.0,
                       predicted_is_fractional: bool = False) -> list[dict]:
    """Rank TLE curves using held-out residual after per-RX offset/drift fitting."""
    times = np.asarray(rows["times"], float)
    receivers = np.asarray(rows["receiver"], int)
    observed = np.asarray(rows["observed"], float)
    sigma = np.asarray(rows["sigma"], float)
    common_drift = bool(rows.get("common_drift", False))
    if len(tles) != predicted_doppler_hz.shape[0]:
        raise ValueError("TLE and prediction counts differ")
    if (times.size < 6 or not 0 < train_fraction < 1 or
            maximum_nuisance_drift_hz_s <= 0 or epoch_search_s <= 0 or
            epoch_step_s <= 0 or epoch_coarse_step_s < epoch_step_s):
        raise ValueError("at least six observations and a valid split are required")
    train, holdout = _groupwise_temporal_split(times, receivers, train_fraction)
    if np.count_nonzero(train) < 3 or np.count_nonzero(holdout) < 3:
        raise ValueError("temporal split leaves too few training or held-out observations")
    # TLE along-track error behaves like an epoch offset. Search a useful
    # interval efficiently: first locate the smooth Doppler-error basin on a
    # coarse grid, then retain the requested fine resolution around that basin.
    coarse_step = max(epoch_step_s, epoch_coarse_step_s)
    coarse_shifts = np.arange(-epoch_search_s,
        epoch_search_s + coarse_step / 2, coarse_step)
    coarse_shifts = np.unique(np.clip(np.concatenate((
        coarse_shifts, [-epoch_search_s, epoch_search_s])),
        -epoch_search_s, epoch_search_s))
    results = []
    for index, tle in enumerate(tles):
        def evaluate(shifts: np.ndarray, current: tuple | None = None
                     ) -> tuple | None:
            best = current
            for shift in shifts:
                predicted = np.interp(times + shift, predicted_times_s,
                                      predicted_doppler_hz[index], left=np.nan,
                                      right=np.nan)
                if predicted_is_fractional:
                    predicted = predicted * np.asarray(rows["carrier"], float)
                if not np.all(np.isfinite(predicted)):
                    continue
                residuals, nuisance = _fit_affine_nuisance(
                    times, receivers, observed - predicted, sigma, train,
                    maximum_nuisance_drift_hz_s, common_drift=common_drift)
                train_rms = float(np.sqrt(np.mean(residuals[train] ** 2)))
                holdout_rms = float(np.sqrt(np.mean(residuals[holdout] ** 2)))
                candidate = (train_rms, holdout_rms, float(shift), residuals,
                             nuisance)
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
            return best

        best = evaluate(coarse_shifts)
        if best is not None and coarse_step > epoch_step_s:
            center = best[2]
            fine_start = max(-epoch_search_s, center - coarse_step)
            fine_stop = min(epoch_search_s, center + coarse_step)
            fine_shifts = np.arange(fine_start,
                fine_stop + epoch_step_s / 2, epoch_step_s)
            fine_shifts = np.unique(np.clip(np.concatenate((
                fine_shifts, [fine_start, fine_stop])), fine_start, fine_stop))
            best = evaluate(fine_shifts, best)
        if best is None:
            continue
        train_rms, holdout_rms, shift, residuals, nuisance = best
        identities = sorted(set(map(int, receivers)))
        if common_drift:
            drift = float(nuisance[1 + len(identities)])
            nuisance_rows = [{"receiver": receiver,
                "frequency_offset_hz": float(nuisance[1 + position]),
                "frequency_drift_hz_s": drift}
                for position, receiver in enumerate(identities)]
        else:
            nuisance_rows = [{"receiver": receiver,
                "frequency_offset_hz": float(nuisance[1 + 2 * position]),
                "frequency_drift_hz_s": float(nuisance[2 + 2 * position])}
                for position, receiver in enumerate(identities)]
        results.append({"norad_id": tle.norad_id, "name": (tle.name or "").strip(),
            "tle_epoch": utc_iso(tle.epoch), "tle_sha256": tle.sha256,
            "epoch_adjustment_s": shift,
            "epoch_at_search_boundary": bool(
                abs(abs(shift) - epoch_search_s) <= epoch_step_s / 2 + 1e-9),
            "train_residual_rms_hz": train_rms,
            "holdout_residual_rms_hz": holdout_rms,
            "full_residual_rms_hz": float(np.sqrt(np.mean(residuals ** 2))),
            "nuisance_reference_utc": utc_iso(datetime.fromtimestamp(
                nuisance[0], timezone.utc)),
            "nuisance_model": ("per_group_offset_common_drift"
                               if common_drift else "per_group_affine"),
            "receiver_nuisance": nuisance_rows})
    results.sort(key=lambda item: (item["holdout_residual_rms_hz"],
                                   item["train_residual_rms_hz"]))
    for rank, item in enumerate(results, 1):
        item["rank"] = rank
    return results


def _ranking_gate(ranked: list[dict], *, maximum_holdout_rms_hz: float,
                  minimum_margin_hz: float) -> dict:
    best = ranked[0] if ranked else None
    second_rms = ranked[1]["holdout_residual_rms_hz"] if len(ranked) > 1 else None
    margin = ((second_rms - best["holdout_residual_rms_hz"])
              if best and second_rms is not None else None)
    passed = bool(best and best["holdout_residual_rms_hz"] <=
                  maximum_holdout_rms_hz and
                  not best["epoch_at_search_boundary"] and
                  (margin is None or margin >= minimum_margin_hz))
    return {"passed": passed, "best_norad_id": best["norad_id"] if best else None,
            "best_name": best["name"] if best else None,
            "holdout_residual_rms_hz": (
                best["holdout_residual_rms_hz"] if best else None),
            "margin_to_second_hz": margin,
            "epoch_adjustment_s": best["epoch_adjustment_s"] if best else None,
            "epoch_at_search_boundary": (
                best["epoch_at_search_boundary"] if best else None)}


def associate_tracks(observations_path: Path, catalog_path: Path, output: Path, *,
                     observer: Observer, horizon_deg: float = 5.0,
                     candidate_limit: int = 256, minimum_duration_s: float = 20.0,
                     minimum_dual_epochs: int = 45,
                     minimum_coverage_fraction: float = .18,
                     epoch_search_s: float = 2.5, epoch_step_s: float = .05,
                     epoch_coarse_step_s: float = .5,
                     prediction_step_s: float = .1,
                     maximum_nuisance_drift_hz_s: float = 200.0,
                     maximum_holdout_rms_hz: float = 500.0,
                     minimum_margin_hz: float = 100.0,
                     stability_train_fractions: tuple[float, ...] = (.5, .6, .7),
                     sensitivity_drift_fraction: float = .8) -> dict:
    """Associate every sufficiently long continuous RF track with archived TLEs."""
    report = json.loads(Path(observations_path).read_text())
    if report.get("schema") != TRACK_SCHEMA:
        raise ValueError("input is not a continuous Starlink track artifact")
    if minimum_dual_epochs < 3 or not 0 < minimum_coverage_fraction <= 1:
        raise ValueError("association coverage requirements are invalid")
    if (not stability_train_fractions or
            any(not 0 < value < 1 for value in stability_train_fractions) or
            not 0 < sensitivity_drift_fraction < 1):
        raise ValueError("association stability requirements are invalid")
    catalog, resolved_catalog_path = _read_catalog_or_snapshot(catalog_path)
    all_tles = parse_catalog(catalog.content, source=catalog.source_url,
                             retrieved_at=catalog.retrieved_at)
    carrier_hz = float(report["signal"]["nominal_rf_hz"])
    associations = []
    for track in report.get("tracks", []):
        rows = _observation_rows(track, carrier_hz)
        if rows["times"].size < 6:
            associations.append({"track_id": track["track_id"], "qualified": False,
                                 "reason": "fewer than three valid dual-receiver epochs",
                                 "candidates": []})
            continue
        start, stop = float(np.min(rows["times"])), float(np.max(rows["times"]))
        duration = stop - start
        if duration < minimum_duration_s:
            associations.append({"track_id": track["track_id"], "qualified": False,
                "reason": f"track duration {duration:.3f}s is below {minimum_duration_s:.3f}s",
                "duration_s": duration, "candidates": []})
            continue
        epoch_count = int(np.unique(rows["times"]).size)
        output_rate_hz = float(report.get("configuration", {}).get(
            "output_rate_hz", 10.0))
        expected_epochs = max(duration * output_rate_hz + 1, 1)
        coverage_fraction = min(epoch_count / expected_epochs, 1.0)
        if (epoch_count < minimum_dual_epochs or
                coverage_fraction < minimum_coverage_fraction):
            associations.append({"track_id": track["track_id"], "qualified": False,
                "reason": (f"track has {epoch_count} measured dual-receiver epochs "
                           f"at {coverage_fraction:.3f} coverage; requires at least "
                           f"{minimum_dual_epochs} epochs and "
                           f"{minimum_coverage_fraction:.3f} coverage"),
                "duration_s": duration, "dual_epoch_count": epoch_count,
                "coverage_fraction": coverage_fraction, "candidates": []})
            continue
        candidates = _screen_tles(all_tles, observer, start, stop, carrier_hz,
                                  horizon_deg, candidate_limit)
        if not candidates:
            associations.append({"track_id": track["track_id"], "qualified": False,
                "reason": "no archived TLE crossed the configured horizon",
                "duration_s": duration, "visible_candidate_count": 0,
                "candidates": []})
            continue
        grid = np.arange(start - epoch_search_s - prediction_step_s,
                         stop + epoch_search_s + 2 * prediction_step_s,
                         prediction_step_s)
        # Predict fractional carrier shift on a common grid, then scale every
        # observation by its actual channel RF. This is essential after a
        # Starlink transmitter changes 250 MHz channels.
        predicted, _, _ = catalog_doppler(candidates, observer, grid, 1.0)
        ranked = rank_doppler_track(rows, candidates, grid, predicted,
            epoch_search_s=epoch_search_s, epoch_step_s=epoch_step_s,
            epoch_coarse_step_s=epoch_coarse_step_s,
            maximum_nuisance_drift_hz_s=maximum_nuisance_drift_hz_s,
            predicted_is_fractional=True)
        best = ranked[0] if ranked else None
        primary_gate = _ranking_gate(ranked,
            maximum_holdout_rms_hz=maximum_holdout_rms_hz,
            minimum_margin_hz=minimum_margin_hz)
        stability = []
        cases = [(fraction, maximum_nuisance_drift_hz_s)
                 for fraction in stability_train_fractions if abs(fraction - .6) > 1e-9]
        cases.append((.6, maximum_nuisance_drift_hz_s * sensitivity_drift_fraction))
        for train_fraction, drift_bound in cases:
            sensitivity_ranked = rank_doppler_track(
                rows, candidates, grid, predicted, epoch_search_s=epoch_search_s,
                epoch_step_s=epoch_step_s,
                epoch_coarse_step_s=epoch_coarse_step_s,
                train_fraction=train_fraction,
                maximum_nuisance_drift_hz_s=drift_bound,
                predicted_is_fractional=True)
            gate = _ranking_gate(sensitivity_ranked,
                maximum_holdout_rms_hz=maximum_holdout_rms_hz,
                minimum_margin_hz=minimum_margin_hz)
            stability.append({"train_fraction": train_fraction,
                              "maximum_nuisance_drift_hz_s": drift_bound, **gate})
        stable_identity = bool(primary_gate["best_norad_id"] is not None and
            all(item["best_norad_id"] == primary_gate["best_norad_id"]
                for item in stability))
        stability_passed = bool(stable_identity and primary_gate["passed"] and
                                all(item["passed"] for item in stability))
        margin = primary_gate["margin_to_second_hz"]
        qualified = stability_passed
        associations.append({"track_id": track["track_id"], "qualified": qualified,
            "duration_s": duration, "dual_epoch_count": epoch_count,
            "coverage_fraction": coverage_fraction,
            "visible_candidate_count": len(candidates),
            "best_norad_id": best["norad_id"] if best else None,
            "best_holdout_residual_rms_hz": (
                best["holdout_residual_rms_hz"] if best else None),
            "margin_to_second_hz": margin if best else None,
            "stability": {"passed": stability_passed,
                "same_norad_across_cases": stable_identity,
                "primary": {"train_fraction": .6,
                    "maximum_nuisance_drift_hz_s": maximum_nuisance_drift_hz_s,
                    **primary_gate}, "sensitivity_cases": stability},
            "requirements": {"minimum_duration_s": minimum_duration_s,
                "minimum_dual_epochs": minimum_dual_epochs,
                "minimum_coverage_fraction": minimum_coverage_fraction,
                "maximum_holdout_residual_rms_hz": maximum_holdout_rms_hz,
                "minimum_margin_to_second_hz": minimum_margin_hz,
                "epoch_adjustment_must_be_interior": True,
                "same_norad_across_stability_cases": True,
                "stability_train_fractions": list(stability_train_fractions),
                "sensitivity_drift_fraction": sensitivity_drift_fraction},
            "candidates": ranked[:20]})
    result = {"schema": ASSOCIATION_SCHEMA,
        "created_utc": utc_iso(datetime.now(timezone.utc)),
        "source_observations": str(Path(observations_path).resolve()),
        "catalog": {"path": str(Path(catalog_path).resolve()),
                    "resolved_object_path": str(resolved_catalog_path.resolve()),
                    "source_url": catalog.source_url,
                    "retrieved_at": utc_iso(catalog.retrieved_at),
                    "sha256": catalog.sha256},
        "observer": {"latitude_deg": observer.latitude_deg,
                     "longitude_deg": observer.longitude_deg,
                     "altitude_m": observer.altitude_m},
        "carrier_hz": carrier_hz,
        "configuration": {"horizon_deg": horizon_deg,
            "candidate_limit": candidate_limit, "epoch_search_s": epoch_search_s,
            "minimum_duration_s": minimum_duration_s,
            "minimum_dual_epochs": minimum_dual_epochs,
            "minimum_coverage_fraction": minimum_coverage_fraction,
            "epoch_step_s": epoch_step_s,
            "epoch_coarse_step_s": epoch_coarse_step_s,
            "prediction_step_s": prediction_step_s,
            "maximum_nuisance_drift_hz_s": maximum_nuisance_drift_hz_s,
            "maximum_holdout_rms_hz": maximum_holdout_rms_hz,
            "minimum_margin_hz": minimum_margin_hz,
            "validation_split": "per_nuisance_group_chronological",
            "stability_train_fractions": list(stability_train_fractions),
            "sensitivity_drift_fraction": sensitivity_drift_fraction},
        "associations": associations,
        "summary": {"track_count": len(associations),
                    "qualified_association_count": sum(
                        item["qualified"] for item in associations)}}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    return result
