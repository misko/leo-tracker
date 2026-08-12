"""Annotating preserved probes with the geometry that was overhead.

The plan keeps three levels of truth apart, and this is the weakest one. A
catalogued satellite can be in view and not transmitting; frame occupancy is
one of the quantities the corpus exists to measure. So the property these tests
protect is not "the prediction is right" but "the prediction cannot be mistaken
for a label, and where it is used as a prior it is arithmetically the same
prediction the association path would make".

Everything is checked against one real Starlink element set over the real site,
because the failure modes that matter — a carrier taken from the wrong tuning,
a bias applied to the wrong receiver, a sign convention that only agrees at
zero — all produce output that looks entirely plausible.
"""
from datetime import datetime, timedelta, timezone
import json

import pytest

from leo_tracker.orbit.artifacts import TLECatalogArtifact, parse_utc
from leo_tracker.orbit.cli import _sample_json
from leo_tracker.orbit.tle import parse_tle
from leo_tracker.orbit.topocentric import Observer
from leo_tracker.passes.prediction import _sample
from leo_tracker.radio.beacon.survey_truth import (PRIOR_CAVEAT, TRUTH_FILENAME,
                                                   TRUTH_SCHEMA,
                                                   TruthInputsMissing,
                                                   UNCALIBRATED_BIAS_UNCERTAINTY_HZ,
                                                   annotate, annotate_manifest,
                                                   annotation_status,
                                                   probe_window,
                                                   resolve_observer)

#: A real Starlink element set, and a real moment at which it stands 41.9
#: degrees over the real site closing at 5.2 km/s. A synthetic orbit would let
#: a sign error survive.
STARLINK = """STARLINK-11326 [DTC]
1 62010U 24213C   26223.84691075  .00143862  00000+0  11683-2 0  9990
2 62010  53.1572 356.4445 0001156  57.0969 303.0160 15.69783314 99282"""

OBSERVER = Observer(37.849165355010086, -122.48567658142287, 0.0)

#: The same instant used to choose the element set above.
IN_VIEW = datetime(2026, 8, 12, 19, 46, 46, tzinfo=timezone.utc)
#: Ten minutes later the same satellite is twelve degrees below the horizon.
BELOW_HORIZON = IN_VIEW + timedelta(minutes=10)

#: The eight low-band edge tunings as the live system records them, RF centres
#: included. Copied from a field manifest rather than recomputed, so a change to
#: the channel geometry shows up here as a disagreement instead of cancelling.
TUNINGS = (
    (1, "lower-edge", 10_709_687_500.0), (1, "upper-edge", 10_940_312_500.0),
    (2, "lower-edge", 10_959_687_500.0), (2, "upper-edge", 11_190_312_500.0),
    (3, "lower-edge", 11_209_687_500.0), (3, "upper-edge", 11_440_312_500.0),
    (4, "lower-edge", 11_459_687_500.0), (4, "upper-edge", 11_690_312_500.0),
)

#: pluto-19f2 as it was actually measured: its two LNBs sit 434 kHz apart.
CALIBRATION = {
    "schema": "leo-tracker.lnb-calibration/v1",
    "created_utc": "2026-08-12T19:01:53.291118Z",
    "radios": {"pluto-19f2": {
        "measured": True, "mismatch_hz": 434408.4, "spread_hz": 10178.2,
        "sample_count": 230, "receiver_candidate_counts": [527, 3835],
        "receiver_labels": ["lnb-c", "lnb-d"]}}}


def _catalog(text=STARLINK):
    return TLECatalogArtifact.create(
        "fixture:starlink", datetime(2026, 8, 12, 7, 7, 40, tzinfo=timezone.utc),
        text.encode())


def _tles(text=STARLINK):
    from leo_tracker.radio.beacon.survey_truth import catalog_tles
    return catalog_tles(_catalog(text))


def _manifest(moment=IN_VIEW, *, radio_id="pluto-19f2", state="complete",
              tunings=TUNINGS, sample_order=None, warm_ms=0.0, total_ms=0.0,
              per_tuning_ms=0.0, created_utc_ns=None, started_utc_ns=None):
    """A capture manifest shaped the way the live system writes one.

    Timing collapses to a single instant by default, so that a test about the
    carrier axis is not also a test about the time axis. ``probe_window`` gets
    its own test with the field's real numbers.
    """
    started = (started_utc_ns if started_utc_ns is not None
               else int(moment.timestamp() * 1e9))
    record = {
        "schema": "leo-tracker.pre-dwell-survey/v1", "state": state,
        "started_utc_ns": started, "warm_ms": warm_ms, "total_ms": total_ms,
        "per_tuning_ms": per_tuning_ms, "threshold": 1.33,
        "active": [], "active_count": 0,
        "tunings": [{"channel": channel, "region": region,
                     "rf_center_hz": rf, "if_center_hz": rf - 9.75e9,
                     "receivers": [{"receiver": 0, "active": False,
                                    "peak_to_median": 1.1},
                                   {"receiver": 1, "active": False,
                                    "peak_to_median": 1.2}]}
                    for channel, region, rf in tunings]}
    if sample_order is not None:
        record["sample_order"] = sample_order
    return {"state": "complete", "lnb_lo_hz": 9_750_000_000,
            "created_utc_ns": (created_utc_ns if created_utc_ns is not None
                               else started),
            "identity": {"radio_id": radio_id,
                         "receiver_labels": ["lnb-c", "lnb-d"]},
            "survey_iq": {"path": "survey.ci16", "tunings": len(tunings),
                          "samples_per_tuning": 200000},
            "metadata": {"channel_number": 1, "region": "lower-edge",
                         "pre_dwell_survey": record}}


def _entry(root, name, manifest=None, *, iq=b"probe-bytes", corpus=True):
    """One preserved probe, laid out the way survey_corpus.sample writes it."""
    entry = root / name
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "manifest.json").write_text(json.dumps(manifest or _manifest()))
    (entry / "survey.ci16").write_bytes(iq)
    if corpus:
        (entry / "corpus.json").write_text(json.dumps(
            {"schema": "leo-tracker.survey-corpus/v1", "capture": name,
             "reasons": ["random"], "bytes": len(iq), "sha256": "0" * 64}))
    return entry


def _annotate(manifest=None, *, text=STARLINK, horizon_deg=0.0,
              calibration=CALIBRATION):
    return annotate_manifest(manifest or _manifest(), observer=OBSERVER,
                             tles=_tles(text), calibration=calibration,
                             horizon_deg=horizon_deg)


# ---------------------------------------------------------------------------
# agreement with the machinery that already exists
# ---------------------------------------------------------------------------

def test_the_predicted_doppler_is_the_one_leo_orbit_would_have_predicted():
    """Two paths to the same number must not be allowed to drift apart.

    ``leo-orbit passes`` writes ``expected_doppler_hz`` for every track sample
    and the association path fits against it. If this annotator computed
    Doppler even slightly differently — a sign, a frame, a kilometre-per-second
    that was really metres — every enrichment measurement would be quietly
    scored against a prediction the rest of the repository disagrees with.
    """
    record = _annotate()
    tuning = record["tunings"][0]
    satellite = tuning["satellites"][0]

    expected = _sample_json(
        _sample(parse_tle(STARLINK), OBSERVER, parse_utc(tuning["time_utc"])),
        tuning["rf_center_hz"])

    assert satellite["doppler_hz"] == pytest.approx(
        expected["expected_doppler_hz"], abs=1.0)
    assert satellite["elevation_deg"] == pytest.approx(
        expected["elevation_deg"], abs=1e-3)
    assert satellite["range_rate_km_s"] == pytest.approx(
        expected["range_rate_km_s"], abs=1e-5)


def test_a_closing_satellite_predicts_a_positive_offset():
    """A sign convention only ever tested at zero is not tested.

    This element set is closing at 5.2 km/s, so the received carrier must sit
    *above* the transmitted one. The repository already carries an unresolved
    positive-slope sign bug on a quarter of qualified Doppler tracks; a prior
    that negated Doppler would move every prediction to the wrong side of the
    search and look like a detector failure rather than an annotation failure.
    """
    satellite = _annotate()["tunings"][0]["satellites"][0]

    assert satellite["range_rate_km_s"] < 0
    assert satellite["doppler_hz"] > 100_000


# ---------------------------------------------------------------------------
# the prior is a prior
# ---------------------------------------------------------------------------

def test_the_record_says_it_is_a_prior_and_not_a_label():
    """The one way this artifact does damage is by being read as truth.

    Optimising a detector against "a catalogued satellite was nearby" teaches
    it to find transmissions that were never sent, because occupancy is
    precisely what is unmeasured. The caveat is in the file so that a later
    reader cannot reach for it without reading it.
    """
    record = _annotate()

    assert record["schema"] == TRUTH_SCHEMA
    assert record["level"] == "geometry_prior"
    assert "not transmitting" in record["caveat"]
    assert record["caveat"] == PRIOR_CAVEAT


def test_a_prediction_carries_a_tolerance_built_from_named_terms():
    """A prediction defines a slice, not a point.

    TLE age, SGP4 error and LNB drift each put the true carrier kilohertz from
    the predicted one. A bare number invites a consumer to match on equality
    and conclude the sky was empty; named terms let a reviewer see which of
    them dominates and argue with that one alone.
    """
    satellite = _annotate()["tunings"][0]["satellites"][0]

    assert satellite["tolerance_hz"] > 1_000
    assert set(satellite["tolerance_terms_hz"]) == {
        "tle_along_track", "probe_time", "sgp4_residual", "receiver_bias"}
    assert sum(satellite["tolerance_terms_hz"].values()) == pytest.approx(
        satellite["tolerance_hz"], abs=1.0)
    # And the rate, so a consumer who knows the probe time better than we do
    # can slide the prediction instead of discarding it.
    assert abs(satellite["doppler_rate_hz_s"]) > 0


def test_an_uncalibrated_radio_widens_the_slice_rather_than_pretending():
    """Zero is what an absent calibration and a perfect one both look like.

    An uncorrected LNB has been measured 434 kHz off. A record that silently
    used a zero centre would place every prediction hundreds of kilohertz from
    where that receiver actually observes, and nothing downstream could tell.
    """
    record = _annotate(calibration={})

    assert record["lnb_calibration"]["applied"] is False
    assert (record["tunings"][0]["satellites"][0]["tolerance_terms_hz"]
            ["receiver_bias"] == UNCALIBRATED_BIAS_UNCERTAINTY_HZ)


# ---------------------------------------------------------------------------
# per receiver, per tuning
# ---------------------------------------------------------------------------

def test_each_receiver_is_predicted_at_its_own_calibrated_bias():
    """The two LNBs have independent references and differ by 434 kHz here.

    One prediction for both ports would be wrong by that whole difference for
    one of them, which is more than twice the entire physical Doppler span.
    """
    record = _annotate()
    tuning = record["tunings"][0]
    satellite = tuning["satellites"][0]
    first, second = satellite["receivers"]

    assert [item["center_offset_hz"] for item in tuning["receivers"]] == [
        434408.4, 0.0]
    assert first["predicted_offset_hz"] - second["predicted_offset_hz"] == (
        pytest.approx(434408.4, abs=0.2))
    assert second["predicted_offset_hz"] == pytest.approx(
        satellite["doppler_hz"], abs=0.2)


def test_the_bias_is_added_at_every_tuning_not_scaled_with_the_carrier():
    """The LNB error belongs to a fixed 9.75 GHz oscillator, not to the tuning.

    Doppler scales with the carrier; an oscillator offset does not. Scaling it
    would put the top tuning 40 kHz out on a 434 kHz bias.
    """
    record = _annotate()

    differences = {round(satellite["receivers"][0]["predicted_offset_hz"]
                         - satellite["receivers"][1]["predicted_offset_hz"], 1)
                   for tuning in record["tunings"]
                   for satellite in tuning["satellites"]}
    assert differences == {434408.4}


def test_doppler_is_computed_at_each_tunings_own_carrier():
    """The eight tunings span 10.71 to 11.69 GHz, nearly a gigahertz.

    Two edge bands of one channel sit 230.6 MHz apart and the channels are
    250 MHz apart, so the same satellite is predicted 9% further out at the top
    tuning than at the bottom — tens of kilohertz at Starlink range rates, far
    more than the tolerance. A single channel centre would be wrong by more
    than the slice it was meant to define.
    """
    record = _annotate()
    lowest, highest = record["tunings"][0], record["tunings"][7]
    bottom = lowest["satellites"][0]
    top = highest["satellites"][0]

    assert lowest["rf_center_hz"] == 10_709_687_500.0
    assert highest["rf_center_hz"] == 11_690_312_500.0
    # Same instant, same geometry: Doppler is exactly proportional to carrier.
    assert top["doppler_hz"] / bottom["doppler_hz"] == pytest.approx(
        highest["rf_center_hz"] / lowest["rf_center_hz"], rel=1e-5)
    assert top["doppler_hz"] - bottom["doppler_hz"] > 15_000


def test_the_recorded_carrier_is_cross_checked_against_channel_geometry():
    """A manifest written by a different tuning list must not pass silently.

    The radio was commanded to the recorded frequency, so that is what the
    prediction uses; recomputing it from the published channel geometry costs
    nothing and is the only thing that would catch a relabelled tuning.
    """
    record = _annotate()

    for tuning in record["tunings"]:
        assert tuning["carrier_basis"]["basis"] == "recorded"
        assert tuning["carrier_basis"]["discrepancy_hz"] == pytest.approx(0.0,
                                                                         abs=1.0)


# ---------------------------------------------------------------------------
# elevation is evidence, not a filter
# ---------------------------------------------------------------------------

def test_elevation_is_recorded_and_qins_cutoff_is_only_flagged():
    """Qin's "typically not below ~40 degrees" describes behaviour, not physics.

    Filtering the corpus on it would bake the assumption into the evidence and
    make the working cutoff undiscoverable — the corpus would agree with the
    assumption by construction. So the flag is written and never applied.
    """
    record = _annotate()
    satellite = record["tunings"][0]["satellites"][0]

    assert record["horizon_deg"] == 0.0
    assert record["qin_transmit_elevation_deg"] == 40.0
    assert satellite["elevation_deg"] == pytest.approx(41.9, abs=0.1)
    assert satellite["above_qin_transmit_elevation"] is True


def test_a_satellite_below_qins_cutoff_is_still_written_down():
    """The whole point of collecting across the elevation range.

    A satellite at ten degrees that turns out to transmit is a finding; one
    filtered out at annotation time is a finding that cannot be made.
    """
    moment = IN_VIEW + timedelta(minutes=5, seconds=10)

    record = _annotate(_manifest(moment))
    found = record["tunings"][0]["satellites"]

    assert found, "the satellite is above the horizon and must be recorded"
    assert 0.0 < found[0]["elevation_deg"] < 40.0
    assert found[0]["above_qin_transmit_elevation"] is False


def test_an_empty_sky_yields_an_empty_annotation_rather_than_an_error():
    """Most probes see nothing catalogued at some tunings, and that is data.

    A probe with nothing predicted anywhere is the negative control the false
    alarm rate is calibrated on, so it has to be representable.
    """
    record = _annotate(_manifest(BELOW_HORIZON))

    assert len(record["tunings"]) == 8
    assert all(tuning["satellites"] == [] for tuning in record["tunings"])
    assert record["counts"]["tuning_satellite_slots"] == 0


# ---------------------------------------------------------------------------
# probe timing
# ---------------------------------------------------------------------------

def test_the_probe_time_is_bracketed_rather_than_guessed():
    """The survey times its start, its warm-up and its scan, but not the gap.

    ``started_utc_ns`` is taken before the kernel is warmed and before the
    radio context is opened, and opening a USB context is not timed. Measured
    on the live system that leaves 2.8 unaccounted seconds, over which a
    Starlink Doppler moves kilohertz. Bracketing it against the capture's own
    creation time turns the gap into a recorded uncertainty rather than a
    silent bias.
    """
    started = int(IN_VIEW.timestamp() * 1e9)
    window = probe_window(_manifest(
        started_utc_ns=started, warm_ms=162.6, total_ms=1733.7,
        per_tuning_ms=216.7,
        created_utc_ns=started + int(4.668 * 1e9)))

    assert window["scan_start_s"] == pytest.approx(
        IN_VIEW.timestamp() + (0.1626 + 2.9343) / 2, abs=1e-3)
    assert window["uncertainty_s"] == pytest.approx(
        (2.9343 - 0.1626) / 2 + 0.2167 / 2, abs=1e-3)
    assert "bracketed" in window["basis"]


def test_a_later_tuning_is_predicted_at_a_later_moment():
    """The eight probes are 217 ms apart, not simultaneous.

    A satellite moves 1.6 km between the first tuning and the last, which at
    these carriers is kilohertz of Doppler. Stamping all eight with one time
    would put a systematic error into exactly the quantity being predicted.
    """
    started = int(IN_VIEW.timestamp() * 1e9)
    record = _annotate(_manifest(
        started_utc_ns=started, warm_ms=162.6, total_ms=1733.7,
        per_tuning_ms=216.7, created_utc_ns=started + int(1.897 * 1e9)))

    moments = [parse_utc(tuning["time_utc"]) for tuning in record["tunings"]]
    assert moments == sorted(moments)
    assert (moments[7] - moments[0]).total_seconds() == pytest.approx(7 * 0.2167,
                                                                     abs=1e-3)
    assert record["tunings"][0]["time_uncertainty_s"] > 0


def test_the_iq_index_follows_sample_order_not_the_sorted_record():
    """The records are sorted for reading; the IQ is in collection order.

    ``summarise`` sorts tunings by channel and region while the retained probes
    stay as the radio took them. Scoring one tuning's samples against another
    tuning's prediction is silent and total, so the mapping is carried rather
    than assumed.
    """
    reversed_order = [[channel, region] for channel, region, _ in TUNINGS][::-1]

    record = _annotate(_manifest(sample_order=reversed_order))

    assert record["iq_order_basis"] == "sample_order"
    assert [tuning["iq_index"] for tuning in record["tunings"]] == list(
        range(7, -1, -1))
    # Probes recorded before the field existed are matched by position, which
    # is right for the current tuning list; the basis says so rather than
    # letting a later list be mismatched in silence.
    older = _annotate()
    assert older["iq_order_basis"] == "record order assumed"
    assert [tuning["iq_index"] for tuning in older["tunings"]] == list(range(8))


# ---------------------------------------------------------------------------
# the corpus sweep
# ---------------------------------------------------------------------------

def test_annotating_writes_a_sidecar_beside_the_iq(tmp_path):
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-a")

    outcome = annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
                       calibration=CALIBRATION)

    record = json.loads(
        (tmp_path / "ch1-lower-edge-narrow-pluto-19f2-a" / TRUTH_FILENAME
         ).read_text())
    assert outcome["annotated"] == 1
    assert record["capture"] == "ch1-lower-edge-narrow-pluto-19f2-a"
    assert record["catalog"]["sha256"] == _catalog().sha256
    assert len(record["tunings"]) == 8


def test_annotating_twice_does_the_work_once(tmp_path):
    """It runs over a corpus that only grows; repeating must be nearly free."""
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-b")

    first = annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
                     calibration=CALIBRATION)
    held = (tmp_path / "ch1-lower-edge-narrow-pluto-19f2-b"
            / TRUTH_FILENAME).read_text()
    second = annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
                      calibration=CALIBRATION)

    assert (first["annotated"], second["annotated"]) == (1, 0)
    assert second["already_current"] == 1
    assert (tmp_path / "ch1-lower-edge-narrow-pluto-19f2-b"
            / TRUTH_FILENAME).read_text() == held


def test_a_corpus_that_grew_since_the_last_run_is_caught_up(tmp_path):
    """The sampler adds probes continuously; this runs behind it.

    Annotating only what is new is what makes it affordable to run on a timer,
    and it is the only mode it will ever run in once the corpus is large.
    """
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-old")
    annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
             calibration=CALIBRATION)
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-new")

    outcome = annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
                       calibration=CALIBRATION)

    assert outcome["annotated"] == 1 and outcome["already_current"] == 1
    assert (tmp_path / "ch1-lower-edge-narrow-pluto-19f2-new"
            / TRUTH_FILENAME).is_file()


def test_a_refreshed_catalog_makes_a_held_annotation_stale(tmp_path):
    """Idempotence must not become staleness.

    A prior propagated from week-old elements is a different prior. Skipping on
    the file merely existing would freeze the corpus to whichever catalog
    happened to be staged the first time anyone ran this.
    """
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-c")
    annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
             calibration=CALIBRATION)

    refreshed = TLECatalogArtifact.create(
        "fixture:starlink", datetime(2026, 8, 12, 21, 9, 13, tzinfo=timezone.utc),
        (STARLINK + "\n").encode())
    outcome = annotate(tmp_path, observer=OBSERVER, catalog=refreshed,
                       calibration=CALIBRATION)

    assert outcome["annotated"] == 1 and outcome["stale_rewritten"] == 1


def test_a_damaged_entry_is_counted_and_skipped_not_raised(tmp_path):
    """One truncated manifest must not cost the other hundred and fifty.

    The three faults are counted apart on purpose. A capture that never had a
    survey is routine; a survey that completed and then would not annotate is a
    schema change, and a count that lumped them together would hide it.
    """
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-d")
    broken = _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-broken")
    (broken / "manifest.json").write_text("{not json")
    failed = _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-failed",
                    _manifest(state="failed"))
    malformed = _manifest()
    malformed["metadata"]["pre_dwell_survey"]["tunings"][0]["region"] = "middle"
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-malformed", malformed)

    outcome = annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
                       calibration=CALIBRATION)

    assert outcome["annotated"] == 1
    assert outcome["unreadable"] == 1
    assert outcome["no_survey"] == 1
    assert outcome["failed"] == 1
    assert not (failed / TRUTH_FILENAME).exists()
    assert not list(tmp_path.glob("*/*.partial"))


def test_status_reports_how_dense_the_priors_are(tmp_path):
    """Enrichment is meaningless if every hertz is near some prediction.

    Five hundred catalogued satellites over the horizon at once is plausible at
    these altitudes, so the density of the prior is a property the corpus has
    to be able to report about itself.
    """
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-e")
    _entry(tmp_path, "ch1-lower-edge-narrow-pluto-19f2-f",
           _manifest(BELOW_HORIZON))
    annotate(tmp_path, observer=OBSERVER, catalog=_catalog(),
             calibration=CALIBRATION)

    status = annotation_status(tmp_path)

    assert status["held"] == 2 and status["annotated"] == 2
    assert status["tuning_satellite_slots"] == 8
    assert status["above_qin_transmit_elevation"] == 8
    assert status["level"] == "geometry_prior"


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

def test_the_observer_is_resolved_from_the_environment_the_scripts_use():
    """There is no canonical accessor: production reads these variables.

    Nine shell scripts pass ``LEO_BEACON_OBSERVER_LAT`` with the site literal as
    the fallback. Reading the same variables keeps a prior computed here in the
    same place as an association computed by the analysis server.
    """
    observer, provenance = resolve_observer(environ={
        "LEO_BEACON_OBSERVER_LAT": "37.849165355010086",
        "LEO_BEACON_OBSERVER_LON": "-122.48567658142287"})

    assert observer == OBSERVER
    assert "environment" in provenance["source"]


def test_the_observer_is_recovered_from_the_staged_passes_artifact(tmp_path):
    """Downstream consumers take the observer from the upstream artifact.

    ``sky_map`` already does this rather than carrying its own copy, and
    ``passes.json`` stamps the observer it was generated for, so the number
    arrives with provenance instead of as a tenth transcription of a literal.
    """
    (tmp_path / "passes.json").write_text(json.dumps({
        "observer": {"latitude_deg": 37.849165355010086,
                     "longitude_deg": -122.48567658142287, "altitude_m": 0.0}}))

    observer, provenance = resolve_observer(context_root=tmp_path, environ={})

    assert observer == OBSERVER
    assert provenance["source"] == "passes.json"


def test_an_unresolvable_observer_raises_instead_of_defaulting(tmp_path):
    """A prior computed for the wrong place is worse than no prior at all.

    Defaulting to a literal produces output that looks entirely correct and is
    wrong by however far the radio has moved, which is exactly how a wrong
    lookup path survived in the calibration code until it was made to say so.
    """
    with pytest.raises(TruthInputsMissing):
        resolve_observer(environ={})
