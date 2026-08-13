"""What the catalogue said was overhead when a survey probe was taken.

This is the **geometry prior** of the detector plan's three levels of truth, and
it is the weakest of the three on purpose.  A catalogued satellite can be
geometrically in view and simply not transmitting: frame occupancy is one of the
quantities the corpus exists to measure, so treating "predicted nearby" as a
positive label would be circular and would train a detector to find
transmissions that were never sent.  Everything written here is named
``geometry_prior`` and carries :data:`PRIOR_CAVEAT` so a later reader cannot
mistake it for a label by accident.

What it *is* good for is enrichment: given a detection at some CFO, whether a
catalogued satellite was predicted at that CFO is evidence, and given a probe
with nothing predicted anywhere, a detection is either uncatalogued or a false
alarm.  Neither statement is a detection probability.

Four things the record insists on.

**A prediction is a slice, not a point.**  TLE age, SGP4 error along track, the
unmodelled part of the probe's own timestamp and LNB drift each move the
expected frequency by kilohertz.  Every satellite therefore carries a
``tolerance_hz`` built from named terms rather than a bare number, and a
``doppler_rate_hz_s`` so a consumer who knows the probe time better than we do
can slide the prediction instead of discarding it.

**Elevation is recorded, never filtered.**  Qin reports that Starlink SVs
"typically do not transmit below an elevation of ~40°".  The citation is sound
and it is why a Doppler search need not exceed about ±172 kHz, but "typically"
describes behaviour that can change, and filtering the corpus on it would bake
the assumption into the evidence.  Every satellite above the geometric horizon
is written down with its elevation, and the working cutoff is left to be
discovered from the data.  The horizon itself is not a behavioural cutoff: at
negative elevation the Earth is in the way.

**Per receiver, not globally.**  The two LNBs on one radio have independent
references; the pair on ``pluto-19f2`` differ by about 434 kHz.  A satellite's
geometric Doppler is one number, but the frequency each receiver observes is
that Doppler plus that receiver's own calibrated bias, so the prediction is
emitted per receiver.  The bias is an error of the LNB's fixed 9.75 GHz low-band
local oscillator, so it is added as a constant across all eight tunings rather
than scaled with the tuning — see the assumption noted on
:func:`predicted_offsets`.

**Per tuning, not per channel.**  The eight tunings run from 10.71 to 11.69 GHz,
because the two edge-pilot bands of one channel sit 230.6 MHz apart and the four
channels are 250 MHz apart.  Doppler scales with carrier, so the same satellite
is predicted 9% further out at the top tuning than the bottom — several
kilohertz at typical Starlink range rates.  A single channel centre would be
wrong by more than the tolerance it was supposed to define.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

from leo_tracker.orbit.artifacts import TLECatalogArtifact, parse_catalog, utc_iso
from leo_tracker.orbit.doppler import predicted_doppler_hz
from leo_tracker.orbit.propagation import propagate_ecef
from leo_tracker.orbit.topocentric import Observer, look_angle

from .channels import STARLINK_EDGE_PILOT_SUBCARRIERS, starlink_edge_pilot_if_hz
from .lnb_calibration import receiver_centers

#: What the annotation calls itself.  A reader must be able to tell a v1 prior
#: from a later one without inferring it from which keys happen to be present.
TRUTH_SCHEMA = "leo-tracker.survey-geometry-prior/v1"

#: Written into every record, and repeated on every satellite list, because the
#: one way this artifact does damage is by being read as a label.
PRIOR_CAVEAT = (
    "geometry prior only: a catalogued satellite can be in view and not "
    "transmitting. Frame occupancy is one of the things the corpus exists to "
    "measure, so scoring a detector against this list would be circular. Use "
    "for enrichment, never as a positive class.")

#: Name of the sidecar written beside the IQ.
TRUTH_FILENAME = "truth.json"

#: The geometric horizon.  Not a behavioural cutoff — below it the Earth is in
#: the way — and deliberately not Qin's ~40°, which is recorded per satellite
#: as :data:`QIN_TRANSMIT_ELEVATION_DEG` instead of applied.
DEFAULT_HORIZON_DEG = 0.0

#: Qin et al.: Starlink SVs "typically do not transmit below an elevation of
#: ~40°".  Flagged per satellite so the real cutoff can be measured from the
#: corpus rather than assumed into it.
QIN_TRANSMIT_ELEVATION_DEG = 40.0

#: Half-width of the along-track epoch error of a *fresh* TLE, in seconds.  This
#: is ``leo-orbit associate --epoch-search-s``, whose default was chosen because
#: Kassas et al. report small epoch corrections and because broad searches admit
#: unrelated Starlink arcs.
FRESH_EPOCH_UNCERTAINTY_S = 2.5

#: How fast that grows with TLE age.  **This constant is an estimate, not a
#: measurement**: published SGP4 along-track degradation for drag-perturbed LEO
#: runs 1-3 km/day, which at 7.5 km/s is 0.13-0.4 s/day, and this rounds up.
#: The association artifacts already record fitted epoch offsets against TLE
#: age, so this is replaceable by a measurement and should be.
EPOCH_UNCERTAINTY_S_PER_DAY = 0.5

#: Floor on the residual after the along-track term, in Hz.  This is
#: ``leo-orbit associate --maximum-holdout-rms-hz``: the largest withheld-data
#: RMS a fitted association is allowed to have, hence an upper bound on how well
#: a correct TLE reproduces observed Doppler once its epoch has been corrected.
SGP4_RESIDUAL_HZ = 500.0

#: Bias uncertainty when the radio's LNB mismatch has been measured but the
#: artifact carries no spread.  Half of the widest p10-p90 spread measured in
#: the field (13.8 kHz on ``pluto-5d4d``).
MEASURED_BIAS_DRIFT_HZ = 7_000.0

#: Bias uncertainty when there is no calibration at all.  An uncorrected LNB has
#: been measured 434 kHz off; a record with ``lnb_calibration.applied`` false is
#: barely a prediction and this number says so rather than hiding it.
UNCALIBRATED_BIAS_UNCERTAINTY_HZ = 500_000.0

#: Central-difference half-step for the Doppler rate, in seconds.  Short enough
#: that curvature over the interval is negligible, long enough that the
#: difference is not float noise on a range rate near 7 km/s.
RATE_HALF_STEP_S = 0.25


class TruthInputsMissing(RuntimeError):
    """A required input — observer, catalog — could not be resolved."""


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------

def resolve_observer(*, latitude_deg: float | None = None,
                     longitude_deg: float | None = None,
                     altitude_m: float | None = None,
                     context_root: Path | None = None,
                     environ: dict | None = None) -> tuple[Observer, dict]:
    """Find the ground station, and say where the answer came from.

    There is no canonical accessor in this repository: the position is a literal
    repeated across nine shell scripts and two argparse defaults, reachable in
    production only as ``LEO_BEACON_OBSERVER_LAT`` and friends with the literal
    as the shell fallback.  Adding a tenth copy here would make that worse, so
    this reads the same environment the analysis server reads and otherwise
    recovers the position from ``passes.json``, which stamps the observer it was
    generated for.  That is what ``sky_map`` already does — downstream consumers
    take the observer from the upstream artifact rather than from a config —
    and it keeps provenance attached to the number.

    An unresolvable observer raises rather than defaulting.  A geometry prior
    computed for the wrong place is worse than no geometry prior, and silently
    correct-looking output is exactly how a wrong lookup path survives.
    """
    if latitude_deg is not None and longitude_deg is not None:
        return (Observer(float(latitude_deg), float(longitude_deg),
                         float(altitude_m or 0.0)),
                {"source": "explicit"})
    environ = os.environ if environ is None else environ
    latitude = environ.get("LEO_BEACON_OBSERVER_LAT")
    longitude = environ.get("LEO_BEACON_OBSERVER_LON")
    if latitude and longitude:
        return (Observer(float(latitude), float(longitude),
                         float(environ.get("LEO_BEACON_OBSERVER_ALT_M") or 0.0)),
                {"source": "LEO_BEACON_OBSERVER_* environment"})
    if context_root is not None:
        path = Path(context_root) / "passes.json"
        try:
            stamped = (json.loads(path.read_text()).get("observer") or {})
            return (Observer(float(stamped["latitude_deg"]),
                             float(stamped["longitude_deg"]),
                             float(stamped.get("altitude_m") or 0.0)),
                    {"source": "passes.json", "path": str(path)})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TruthInputsMissing(
                f"no observer in {path}: {type(exc).__name__}") from exc
    raise TruthInputsMissing(
        "no observer: pass coordinates, set LEO_BEACON_OBSERVER_LAT/LON, or "
        "give a context root holding passes.json")


def load_catalog(path: Path) -> TLECatalogArtifact:
    """Read a frozen TLE catalog the way ``leo-orbit associate`` reads one."""
    return TLECatalogArtifact.read(Path(path))


def catalog_tles(catalog: TLECatalogArtifact):
    return parse_catalog(catalog.content, source=catalog.source_url,
                         retrieved_at=catalog.retrieved_at)


# --------------------------------------------------------------------------
# tuning geometry and probe timing
# --------------------------------------------------------------------------

def tuning_edge(region: str) -> str:
    """Map the survey's ``lower-edge`` to the channel geometry's ``lower``."""
    edge = str(region).split("-")[0]
    if edge not in STARLINK_EDGE_PILOT_SUBCARRIERS:
        raise ValueError(f"unknown edge region: {region!r}")
    return edge


def tuning_carrier_hz(tuning: dict, lnb_lo_hz: float) -> tuple[float, dict]:
    """The RF this tuning's pilots actually sat at, and a check on it.

    The recorded ``rf_center_hz`` is what the radio was commanded to, so it wins;
    recomputing it from the published channel geometry is a cross-check that
    costs nothing and catches a manifest written by a different tuning list.
    A record that predates the field being written falls back to the geometry.
    """
    derived = starlink_edge_pilot_if_hz(int(tuning["channel"]),
                                        tuning_edge(tuning["region"]),
                                        lnb_lo_hz) + lnb_lo_hz
    recorded = tuning.get("rf_center_hz")
    if recorded is None:
        return derived, {"basis": "derived from channel geometry"}
    return float(recorded), {"basis": "recorded", "derived_hz": derived,
                             "discrepancy_hz": float(recorded) - derived}


def _iq_order(record: dict) -> tuple[list[int], str]:
    """Which retained probe belongs to which tuning record.

    ``summarise`` sorts the tuning records for reading while the IQ stays in the
    order the radio collected it, and ``sample_order`` is what reconciles them.
    Probes written before that field existed are matched by position, which is
    correct for the current tuning list and would stop being correct the moment
    it changes — so the basis is recorded rather than assumed silently.
    """
    tunings = record.get("tunings") or []
    order = record.get("sample_order")
    if not order:
        return list(range(len(tunings))), "record order assumed"
    collected = [(item[0], item[1]) for item in order]
    indexes = []
    for tuning in tunings:
        key = (tuning.get("channel"), tuning.get("region"))
        indexes.append(collected.index(key) if key in collected else len(indexes))
    return indexes, "sample_order"


def probe_window(manifest: dict) -> dict:
    """When the survey's probes were taken, and how well that is known.

    The survey records one timestamp — ``started_utc_ns``, taken before the
    detector kernel is warmed and before the radio context is opened — plus how
    long warming and scanning each took.  The context open between them is not
    timed, so the start of the *scan* is not known exactly; it is bracketed.

    The lower bound is ``started + warm``: scanning cannot begin before warming
    ends.  The upper bound is the capture manifest's own ``created_utc_ns``
    minus the scan duration, because the capture is created after the survey
    hands the radio back.  The midpoint is used and the half-width is carried as
    an uncertainty, which is honest about a gap that has been measured at
    2.8 seconds on the live system and would otherwise be silently ignored.
    """
    record = (manifest.get("metadata") or {}).get("pre_dwell_survey") or {}
    started_ns = record.get("started_utc_ns")
    if not started_ns:
        raise ValueError("survey record carries no started_utc_ns")
    started = float(started_ns) / 1e9
    warm_s = float(record.get("warm_ms") or 0.0) / 1000.0
    total_s = float(record.get("total_ms") or 0.0) / 1000.0
    per_tuning_s = float(record.get("per_tuning_ms") or 0.0) / 1000.0
    lower = started + warm_s
    created_ns = manifest.get("created_utc_ns")
    upper = (float(created_ns) / 1e9 - total_s) if created_ns else lower
    if upper < lower:
        # The capture manifest predates the survey finishing, which should not
        # happen; refusing to invent a bracket is better than inverting one.
        upper, basis = lower, "started+warm only; created_utc_ns precedes it"
    elif created_ns:
        basis = "bracketed between started+warm and created_utc_ns-total"
    else:
        basis = "started+warm only; capture declares no created_utc_ns"
    # Within one tuning the probe is 80 ms of a slot that also pays retune,
    # drain and scoring, so where it sits in the slot is known only to a
    # fraction of the slot.
    return {"scan_start_s": (lower + upper) / 2.0,
            "uncertainty_s": (upper - lower) / 2.0 + per_tuning_s / 2.0,
            "per_tuning_s": per_tuning_s, "basis": basis,
            "started_utc_ns": int(started_ns), "warm_s": warm_s,
            "scan_duration_s": total_s}


def tuning_time(window: dict, iq_index: int) -> datetime:
    """UTC of the probe collected ``iq_index``-th, at the middle of its slot."""
    seconds = window["scan_start_s"] + (iq_index + 0.5) * window["per_tuning_s"]
    return datetime.fromtimestamp(seconds, timezone.utc)


# --------------------------------------------------------------------------
# the prediction itself
# --------------------------------------------------------------------------

def doppler_tolerance_hz(*, doppler_rate_hz_s: float, tle_age_s: float,
                         time_uncertainty_s: float,
                         bias_uncertainty_hz: float) -> dict:
    """The half-width of the frequency slice a prediction really occupies.

    Terms are summed rather than combined in quadrature.  A tolerance exists so
    that a consumer asking "was anything predicted here" gets the right answer
    when the truth is at the edge of the slice; under-covering is the expensive
    failure and the terms are not independent enough for quadrature to be
    defensible anyway.
    """
    age_days = max(tle_age_s, 0.0) / 86400.0
    epoch_s = FRESH_EPOCH_UNCERTAINTY_S + EPOCH_UNCERTAINTY_S_PER_DAY * age_days
    terms = {
        "tle_along_track": abs(doppler_rate_hz_s) * epoch_s,
        "probe_time": abs(doppler_rate_hz_s) * float(time_uncertainty_s),
        "sgp4_residual": SGP4_RESIDUAL_HZ,
        "receiver_bias": float(bias_uncertainty_hz),
    }
    return {"tolerance_hz": sum(terms.values()),
            "terms_hz": {k: round(v, 1) for k, v in terms.items()},
            "epoch_uncertainty_s": epoch_s}


def bias_uncertainty_hz(calibration: dict, radio_id: str | None) -> tuple[float, bool]:
    """How well this radio's LNB offset is known, from its own measurement.

    Returns the uncertainty and whether it rests on a measurement, because an
    absent calibration and a correctly-zero one produce identical centres and
    the difference has already gone unnoticed in the field once.  The measured
    figure is half the radio's own p10-p90 spread rather than a constant: that
    spread is real diurnal wander plus measurement noise, and it is the only
    evidence available about how far the bias moves between calibrations.
    """
    entry = ((calibration or {}).get("radios") or {}).get(radio_id or "") or {}
    if not entry.get("measured"):
        return UNCALIBRATED_BIAS_UNCERTAINTY_HZ, False
    spread = entry.get("spread_hz")
    return (abs(float(spread)) / 2.0 if spread else MEASURED_BIAS_DRIFT_HZ), True


def predicted_offsets(doppler_hz: float,
                      centers_hz: tuple[float, ...]) -> list[float]:
    """Where each receiver should see the carrier, given its own LNB bias.

    The bias is added, not scaled: it is an error of the LNB's fixed 9.75 GHz
    low-band local oscillator and so is the same number of hertz at every one of
    the eight tunings, while the Doppler is not.  **This is an assumption worth
    challenging** — the measurement behind ``receiver_centers`` is a difference
    of observed acquisition offsets, which also absorbs any tuner error that
    fails to cancel between the ports, and tuner error does scale with the IF.
    """
    return [doppler_hz + float(center) for center in centers_hz]


def _satellite_prior(tle, observer: Observer, moment: datetime, *,
                     carrier_hz: float, centers_hz: tuple[float, ...],
                     time_uncertainty_s: float,
                     bias_uncertainty_hz: float) -> dict | None:
    """One catalogued satellite's predicted geometry at one tuning.

    Everything here goes through the machinery the association path already
    uses — ``propagate_ecef``, ``look_angle``, ``predicted_doppler_hz`` — so a
    prior and an association can never disagree about what a TLE implies.
    """
    try:
        look = look_angle(observer, propagate_ecef(tle, moment))
        step = timedelta(seconds=RATE_HALF_STEP_S)
        before = look_angle(observer, propagate_ecef(tle, moment - step))
        after = look_angle(observer, propagate_ecef(tle, moment + step))
    except (ValueError, RuntimeError):
        # A single satellite whose elements SGP4 rejects must not cost the
        # other five hundred their annotation.
        return None
    doppler = predicted_doppler_hz(carrier_hz, look.range_rate_km_s)
    rate = (predicted_doppler_hz(carrier_hz, after.range_rate_km_s)
            - predicted_doppler_hz(carrier_hz, before.range_rate_km_s)
            ) / (2.0 * RATE_HALF_STEP_S)
    age_s = (moment - tle.epoch).total_seconds()
    tolerance = doppler_tolerance_hz(
        doppler_rate_hz_s=rate, tle_age_s=age_s,
        time_uncertainty_s=time_uncertainty_s,
        bias_uncertainty_hz=bias_uncertainty_hz)
    offsets = predicted_offsets(doppler, centers_hz)
    return {
        "norad_id": tle.norad_id, "name": tle.name,
        "tle_epoch": utc_iso(tle.epoch), "tle_age_s": round(age_s, 1),
        "elevation_deg": round(look.elevation_deg, 4),
        "azimuth_deg": round(look.azimuth_deg, 4),
        "range_km": round(look.range_km, 3),
        "range_rate_km_s": round(look.range_rate_km_s, 6),
        "doppler_hz": round(doppler, 1),
        "doppler_rate_hz_s": round(rate, 2),
        "tolerance_hz": round(tolerance["tolerance_hz"], 1),
        "tolerance_terms_hz": tolerance["terms_hz"],
        # Recorded, never applied. The plan is explicit that gating collection
        # on Qin's ~40° would bake the assumption into the evidence, so the flag
        # is here to let the cutoff be measured instead.
        "above_qin_transmit_elevation":
            look.elevation_deg >= QIN_TRANSMIT_ELEVATION_DEG,
        "receivers": [{"receiver": index, "predicted_offset_hz": round(value, 1)}
                      for index, value in enumerate(offsets)],
    }


def annotate_manifest(manifest: dict, *, observer: Observer, tles,
                      calibration: dict,
                      horizon_deg: float = DEFAULT_HORIZON_DEG) -> dict:
    """Build the geometry prior for one capture's survey, without touching disk.

    Split out from the corpus sweep so it can be tested against a hand-built
    manifest and a single known TLE, which is the only way the agreement with
    ``leo-orbit``'s own Doppler is checkable to the hertz.
    """
    from leo_tracker.orbit.cli import _visibility_candidates

    record = (manifest.get("metadata") or {}).get("pre_dwell_survey") or {}
    if record.get("state") != "complete" or not record.get("tunings"):
        raise ValueError("capture carries no complete pre-dwell survey")
    window = probe_window(manifest)
    order, order_basis = _iq_order(record)
    lnb_lo_hz = float(manifest.get("lnb_lo_hz") or 9_750_000_000.0)
    identity = manifest.get("identity") or {}
    radio_id = identity.get("radio_id")
    labels = list(identity.get("receiver_labels") or [])
    centers = tuple(receiver_centers(calibration, radio_id or ""))
    bias_hz, bias_measured = bias_uncertainty_hz(calibration, radio_id)

    moments = [tuning_time(window, index) for index in order]
    candidates = _visibility_candidates(
        tles, observer, min(moments), max(moments) + timedelta(seconds=1e-6),
        horizon_deg, limit=None)

    tunings, in_view, above_qin = [], 0, 0
    for position, tuning in enumerate(record["tunings"]):
        carrier_hz, carrier_basis = tuning_carrier_hz(tuning, lnb_lo_hz)
        moment = moments[position]
        satellites = []
        for tle in candidates:
            prior = _satellite_prior(
                tle, observer, moment, carrier_hz=carrier_hz,
                centers_hz=centers, time_uncertainty_s=window["uncertainty_s"],
                bias_uncertainty_hz=bias_hz)
            if prior is None or prior["elevation_deg"] < horizon_deg:
                continue
            satellites.append(prior)
            above_qin += 1 if prior["above_qin_transmit_elevation"] else 0
        satellites.sort(key=lambda item: item["elevation_deg"], reverse=True)
        in_view += len(satellites)
        tunings.append({
            "index": position, "iq_index": order[position],
            "channel": tuning.get("channel"), "region": tuning.get("region"),
            "edge": tuning_edge(tuning["region"]),
            "rf_center_hz": carrier_hz, "carrier_basis": carrier_basis,
            "if_center_hz": tuning.get("if_center_hz"),
            "time_utc": utc_iso(moment),
            "time_uncertainty_s": round(window["uncertainty_s"], 4),
            "receivers": [
                {"receiver": index,
                 "label": labels[index] if index < len(labels) else None,
                 "center_offset_hz": float(center)}
                for index, center in enumerate(centers)],
            "satellites": satellites})
    return {
        "schema": TRUTH_SCHEMA,
        "level": "geometry_prior",
        "caveat": PRIOR_CAVEAT,
        "generated_utc": utc_iso(datetime.now(timezone.utc)),
        "observer": {"latitude_deg": observer.latitude_deg,
                     "longitude_deg": observer.longitude_deg,
                     "altitude_m": observer.altitude_m},
        "radio_id": radio_id,
        "lnb_lo_hz": lnb_lo_hz,
        "lnb_calibration": {"centers_hz": [float(value) for value in centers],
                            "bias_uncertainty_hz": bias_hz,
                            "applied": bias_measured,
                            "created_utc": (calibration or {}).get("created_utc")},
        "horizon_deg": float(horizon_deg),
        "qin_transmit_elevation_deg": QIN_TRANSMIT_ELEVATION_DEG,
        "doppler_convention": "received_minus_transmitted; approaching is positive",
        "frames": {"propagation": "TEME", "observer_geometry": "ITRF_APPROX"},
        "probe_time": {k: (round(v, 6) if isinstance(v, float) else v)
                       for k, v in window.items()},
        "iq_order_basis": order_basis,
        "counts": {"catalog_satellites": len(tles),
                   "screened_satellites": len(candidates),
                   "tuning_satellite_slots": in_view,
                   "above_qin_transmit_elevation": above_qin},
        "tunings": tunings,
    }


# --------------------------------------------------------------------------
# the corpus sweep
# --------------------------------------------------------------------------

def _inputs_digest(*, observer: Observer, catalog_sha256: str,
                   horizon_deg: float, calibration: dict) -> str:
    """Fingerprint of everything an annotation depends on but does not contain.

    Re-running must be cheap, and it must also not leave a stale prior in place
    after the catalog is refreshed or the LNB recalibrated.  Comparing this
    against the stored digest distinguishes the two.

    :data:`TRUTH_SCHEMA` is part of the payload, so a change to what a prior
    *means* invalidates every held annotation by bumping the schema.  A change
    that only alters how one is computed does not, and must be rolled out with
    ``rebuild``.
    """
    payload = json.dumps({
        "schema": TRUTH_SCHEMA, "catalog_sha256": catalog_sha256,
        "horizon_deg": float(horizon_deg),
        "observer": [observer.latitude_deg, observer.longitude_deg,
                     observer.altitude_m],
        "calibration": (calibration or {}).get("created_utc"),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _write_atomic(path: Path, value: dict) -> None:
    """Temp then rename, so a reader never sees half a prior."""
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def annotate(corpus_root: Path, *, observer: Observer,
             catalog: TLECatalogArtifact,
             calibration: dict | None = None,
             horizon_deg: float = DEFAULT_HORIZON_DEG,
             rebuild: bool = False, limit: int | None = None) -> dict:
    """Write a geometry prior beside every preserved probe that lacks one.

    Never raises on one bad entry.  This runs over a corpus that only grows, and
    a single truncated manifest must not cost the other hundred and fifty their
    annotation; damaged entries are counted and skipped, the way the sampler
    that built the corpus counts and skips damaged captures.
    """
    corpus_root = Path(corpus_root)
    calibration = calibration or {}
    tles = catalog_tles(catalog)
    digest = _inputs_digest(observer=observer, catalog_sha256=catalog.sha256,
                            horizon_deg=horizon_deg, calibration=calibration)
    outcome = {"annotated": 0, "already_current": 0, "stale_rewritten": 0,
               "no_survey": 0, "unreadable": 0, "failed": 0,
               "satellite_slots": 0,
               "catalog_sha256": catalog.sha256, "inputs_sha256": digest}
    if not corpus_root.is_dir():
        return outcome
    for entry in sorted(corpus_root.iterdir()):
        if not entry.is_dir() or not (entry / "corpus.json").is_file():
            continue
        if limit is not None and outcome["annotated"] >= limit:
            break
        existing = entry / TRUTH_FILENAME
        stale = False
        if existing.is_file() and not rebuild:
            try:
                held = json.loads(existing.read_text())
            except (OSError, ValueError):
                held = {}
            if held.get("inputs_sha256") == digest:
                outcome["already_current"] += 1
                continue
            stale = bool(held)
        try:
            manifest = json.loads((entry / "manifest.json").read_text())
        except (OSError, ValueError):
            outcome["unreadable"] += 1
            continue
        survey = (manifest.get("metadata") or {}).get("pre_dwell_survey") or {}
        if survey.get("state") != "complete" or not survey.get("tunings"):
            outcome["no_survey"] += 1
            continue
        try:
            record = annotate_manifest(manifest, observer=observer, tles=tles,
                                       calibration=calibration,
                                       horizon_deg=horizon_deg)
        # A probe whose survey is complete but whose record is malformed is a
        # different fault from one that never had a survey, and conflating them
        # would hide a schema change behind a count that looks routine.
        except (ValueError, KeyError, TypeError, IndexError, OverflowError,
                OSError):
            outcome["failed"] += 1
            continue
        record["capture"] = entry.name
        record["inputs_sha256"] = digest
        record["catalog"] = {"source_url": catalog.source_url,
                             "retrieved_at": utc_iso(catalog.retrieved_at),
                             "sha256": catalog.sha256}
        try:
            _write_atomic(existing, record)
        except OSError:
            outcome["unreadable"] += 1
            continue
        outcome["annotated"] += 1
        outcome["stale_rewritten"] += 1 if stale else 0
        outcome["satellite_slots"] += record["counts"]["tuning_satellite_slots"]
    return outcome


def annotation_status(corpus_root: Path) -> dict:
    """What is annotated, against what is held, and how dense the priors are."""
    corpus_root = Path(corpus_root)
    held = annotated = slots = above = 0
    digests: dict[str, int] = {}
    if corpus_root.is_dir():
        for entry in sorted(corpus_root.iterdir()):
            if not entry.is_dir() or not (entry / "corpus.json").is_file():
                continue
            held += 1
            try:
                record = json.loads((entry / TRUTH_FILENAME).read_text())
            except (OSError, ValueError):
                continue
            if record.get("schema") != TRUTH_SCHEMA:
                continue
            annotated += 1
            counts = record.get("counts") or {}
            slots += int(counts.get("tuning_satellite_slots") or 0)
            above += int(counts.get("above_qin_transmit_elevation") or 0)
            key = str(record.get("inputs_sha256"))
            digests[key] = digests.get(key, 0) + 1
    return {"held": held, "annotated": annotated,
            "tuning_satellite_slots": slots,
            "above_qin_transmit_elevation": above,
            "by_inputs_sha256": digests, "level": "geometry_prior",
            "caveat": PRIOR_CAVEAT}
