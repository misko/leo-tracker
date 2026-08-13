"""Shadow-scoring every preserved probe with every candidate detector.

The comparison this feeds is only as good as the bookkeeping underneath it, and
the bookkeeping fails silently: a probe mapped to the wrong tuning still scores,
a half-written sidecar still parses, and a corpus that quietly skips the entries
it cannot map produces a comparison over a biased subset without ever saying so.
So the properties pinned here are mostly about refusing rather than computing —
what must be skipped, what must be counted, and what must never be guessed.

The exception is the shared conditioned path, which exists because conditioning
six statistics on one correlation set is five times cheaper than recomputing it
six times. That is an optimisation, and this repository's rule is that an
optimisation agrees with the obvious implementation or it is a redesign, so it
is gated against :mod:`relative_phase`'s public functions on the same input.
"""
import json

import numpy as np
import pytest

from leo_tracker.radio.beacon.pilots import (edge_pilot_frame,
                                             matched_pilot_control_scores)
from leo_tracker.radio.beacon.relative_phase import (adjacent_differential,
                                                     anchor_relative_phase,
                                                     symbol_glrt)
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S
from leo_tracker.radio.beacon.survey_scoring import (
    ProbeUnusable, SCORES_SCHEMA, adjudicated, conditioned_suite,
    cross_receiver_checks, distinct_points, pinned_full_frame,
    rolled_control_shift_samples, run, score_entry, scores_path,
    scoring_status, search_cells, tuning_plan)

RATE = 2_500_000.0
PERIOD = RATE * STARLINK_FRAME_DURATION_S
SAMPLES = 15_000                       # 4.5 frames: the fold's own minimum is 4
TUNINGS = ((1, "lower-edge"), (1, "upper-edge"))


def _record(tunings, *, order=True, peak=1.2, shape=(3, 8),
            offset_span_hz=300_000.0, profile=True, profile_span=False,
            threshold=1.33, capture_config=None):
    """A pre-dwell survey record shaped the way the capture host writes one.

    The bank knobs default to the A-era configuration the corpus mostly holds:
    ``profile.shape`` [3, 8] with the span at record level only, which is how
    all 283 of the narrow manifests are actually written. ``profile_span``
    additionally puts the span inside ``profile``, as the 75 widened ones do,
    and ``profile=False`` reproduces a record that never said at all.

    ``capture_config`` is the randomised arm's block, absent on every entry
    taken before the draw went live and carrying ``scored_samples`` on every
    entry taken after it.
    """
    record = {
        "schema": "leo-tracker.pre-dwell-survey/v1", "state": "complete",
        "sample_rate_hz": RATE, "offset_span_hz": offset_span_hz,
        "threshold": threshold,
        "started_utc_ns": 1_760_000_000_000_000_000, "warm_ms": 800.0,
        "total_ms": 400.0, "per_tuning_ms": 50.0,
        "tunings": [{"channel": channel, "region": region,
                     "if_center_hz": 9.6e8, "rf_center_hz": 1.07e10,
                     "receivers": [{"receiver": index, "active": False,
                                    "peak_to_median": peak,
                                    "anchor_agreement": 0, "anchor_count": 8}
                                   for index in (0, 1)]}
                    for channel, region in tunings]}
    if profile:
        record["profile"] = {"block_size": 200_000, "kernel_buffers": 1,
                             "settle_buffers": 0, "probe_s": 0.08,
                             "shape": list(shape)}
        if profile_span:
            record["profile"]["offset_span_hz"] = offset_span_hz
    if capture_config:
        record["capture_config"] = dict(capture_config)
    if order:
        record["sample_order"] = [list(pair) for pair in tunings]
    return record


def _entry(root, name, *, tunings=TUNINGS, order=True, samples=SAMPLES,
           truncate=False, block=None, state="complete", **bank):
    """One preserved corpus entry, IQ and manifest, as the sampler writes it.

    Keyword arguments not named here go to :func:`_record`, which is where the
    capture's own bank is described.
    """
    entry = root / name
    entry.mkdir(parents=True, exist_ok=True)
    record = _record(tunings, order=order, **bank)
    record["state"] = state
    manifest = {
        "state": "complete", "created_utc_ns": 1_760_000_002_000_000_000,
        "identity": {"radio_id": "pluto-test",
                     "receiver_labels": ["lnb-a", "lnb-b"]},
        "metadata": {"pre_dwell_survey": record},
        "survey_iq": {"path": "survey.ci16", "dtype": "ci16_le",
                      "tunings": len(tunings), "samples_per_tuning": samples,
                      "layout": "tuning,sample,receiver,component"}}
    (entry / "manifest.json").write_text(json.dumps(manifest))
    if block is None:
        block = np.zeros((len(tunings), samples, 2, 2), np.int16)
        block[..., 0] = 40                      # a flat, finite, boring probe
    raw = np.asarray(block, np.int16).ravel()
    (entry / "survey.ci16").write_bytes(
        raw[:-4].tobytes() if truncate else raw.tobytes())
    return entry


def _replica(count=SAMPLES, *, epoch=0, edge="lower"):
    """A noiseless pilot in every frame slot, on the fractional frame grid."""
    frame = edge_pilot_frame(RATE, edge)
    values = np.zeros(count, np.complex128)
    slot = 0
    while True:
        start = epoch + int(round(slot * PERIOD))
        if start >= count:
            break
        values[start:start + frame.size] += frame[:count - start]
        slot += 1
    return values.astype(np.complex64)


#: The widened bank, and the hypothesis four steps above zero on its grid:
#: 1.4 MHz over twelve intervals is 116,666.67 Hz apart, so this is exactly on
#: E's grid and 166,666.67 Hz off the nearest point of A's {-300, 0, +300} kHz.
#: Config A cannot represent a signal here at all, which is the whole point.
WIDE_SHAPE = (13, 8)
WIDE_SPAN_HZ = 700_000.0
WIDE_OFFSET_HZ = 4 * 2 * WIDE_SPAN_HZ / 12


def _shifted(values, offset_hz):
    """The same waveform carried to a frequency offset, as the sky does."""
    lag = np.arange(values.size) / RATE
    return (values * np.exp(2j * np.pi * offset_hz * lag)).astype(np.complex64)


def _int16_block(values, tunings=1):
    """One waveform written into every tuning and receiver of a probe block."""
    scaled = values / max(float(np.abs(values).max()), 1e-9) * 6000.0
    block = np.zeros((tunings, values.size, 2, 2), np.int16)
    block[..., 0] = np.rint(scaled.real).astype(np.int16)[None, :, None]
    block[..., 1] = np.rint(scaled.imag).astype(np.int16)[None, :, None]
    return block


# --------------------------------------------------------------------------
# what must be refused
# --------------------------------------------------------------------------

def test_an_entry_without_sample_order_is_skipped_rather_than_guessed(tmp_path):
    """Position agrees with the record list only by luck, and luck is not a map.

    ``summarise`` sorts the tuning records by score while the IQ stays in
    collection order. Roughly half the live corpus predates ``sample_order``,
    and inferring it would attribute a detection to a channel and edge the radio
    was not pointed at — a wrong answer that looks exactly like a right one.
    """
    _entry(tmp_path, "ch1-no-order", order=False)

    outcome = run(tmp_path)

    assert outcome["scored"] == 0
    assert outcome["no_sample_order"] == 1
    assert not scores_path(tmp_path / "ch1-no-order").exists()


def test_the_tuning_map_comes_only_from_the_manifests_own_order(tmp_path):
    """Read directly, so the refusal cannot be softened by a later caller."""
    entry = _entry(tmp_path, "ch1-mapped")
    manifest = json.loads((entry / "manifest.json").read_text())

    plan = tuning_plan(manifest)

    assert [(item["channel"], item["region"]) for item in plan] == list(TUNINGS)
    assert [item["iq_index"] for item in plan] == [0, 1]
    del manifest["metadata"]["pre_dwell_survey"]["sample_order"]
    with pytest.raises(ProbeUnusable, match="sample_order"):
        tuning_plan(manifest)


def test_a_truncated_probe_is_counted_and_the_sweep_carries_on(tmp_path):
    """One short copy must not stop a sweep that runs beside every analysis job.

    The length is checked against the shape the manifest declares rather than
    trusted, because ``reshape`` on a truncated file either raises somewhere far
    away or, worse, succeeds against a different shape.
    """
    _entry(tmp_path, "ch1-damaged", truncate=True)
    _entry(tmp_path, "ch2-intact")

    outcome = run(tmp_path)

    assert outcome["unusable"] == 1
    assert outcome["scored"] == 1
    assert outcome["errors"][0]["entry"] == "ch1-damaged"
    assert not scores_path(tmp_path / "ch1-damaged").exists()


def test_a_survey_that_never_completed_is_not_scored(tmp_path):
    """A failed survey holds no probes; its IQ would be an empty exhibit."""
    _entry(tmp_path, "ch1-failed", state="failed")

    outcome = run(tmp_path)

    assert outcome["scored"] == 0 and outcome["unusable"] == 1


# --------------------------------------------------------------------------
# how it must behave when it runs again
# --------------------------------------------------------------------------

def test_scoring_twice_rescores_nothing(tmp_path):
    """It runs beside every analysis job; repeating it has to be nearly free."""
    _entry(tmp_path, "ch1-idempotent")

    first = run(tmp_path)
    second = run(tmp_path)

    assert first["scored"] == 1
    assert second["scored"] == 0 and second["already_scored"] == 1


def test_a_sidecar_from_an_older_schema_is_replaced_rather_than_trusted(tmp_path):
    """Two definitions of one column silently averaged is the worst outcome."""
    entry = _entry(tmp_path, "ch1-stale")
    scores_path(entry).write_text(json.dumps({"schema": "something-older"}))

    outcome = run(tmp_path)

    assert outcome["scored"] == 1
    assert json.loads(scores_path(entry).read_text())["schema"] == SCORES_SCHEMA


def test_the_sidecar_is_published_in_one_step(tmp_path):
    """A reader must never see half a file, and a crash must leave none.

    The staging name is written, flushed and renamed, so an interrupted run
    leaves the entry unscored — which the next sweep fixes — rather than a file
    that parses far enough to poison an aggregate.
    """
    entry = _entry(tmp_path, "ch1-atomic")
    stale = scores_path(entry).with_suffix(".json.partial")
    stale.write_text("{ truncated")

    run(tmp_path)

    assert json.loads(scores_path(entry).read_text())["schema"] == SCORES_SCHEMA
    assert not list(entry.glob("*.partial"))


def test_a_finished_sidecar_announces_itself_in_its_first_bytes(tmp_path):
    """The sweep runs on every analysis job over hundreds of files on a share.

    Each sidecar is ~350 kB, so parsing every one of them to learn it is already
    done would cost more than scoring the single entry that is not. ``schema``
    is written first for exactly that reason, and sorting the keys would bury it
    behind the observation list where the shortcut cannot see it.
    """
    entry = _entry(tmp_path, "ch1-prefix")

    run(tmp_path)

    head = scores_path(entry).read_bytes()[:80].decode()
    assert head.startswith(f'{{"schema": "{SCORES_SCHEMA}"')


def test_the_limit_bounds_one_analysis_job(tmp_path):
    """Scoring costs ~57 s an entry; a job must not inherit the whole backlog."""
    for index in range(3):
        _entry(tmp_path, f"ch{index}-limited")

    outcome = run(tmp_path, limit=1)

    assert outcome["scored"] == 1 and outcome["budget_reached"] is True


def test_rebuild_rescores_what_is_already_done(tmp_path):
    """The statistic changes more often than the corpus does.

    Every constant here — the coarse shapes, the symbol counts, the transform
    size — is under active comparison, so re-deriving the whole corpus has to be
    one flag rather than a manual sweep of two hundred directories.
    """
    _entry(tmp_path, "ch1-rebuild")
    run(tmp_path)

    outcome = run(tmp_path, rebuild=True)

    assert outcome["scored"] == 1 and outcome["already_scored"] == 0


def test_a_wall_clock_budget_stops_before_starting_another_entry(tmp_path):
    """Bounded between entries, never inside one.

    A half-scored probe would enter the comparison as a hole that is missing
    for a reason correlated with what it contains, which is worse than one that
    is simply absent — so the budget is checked before an entry begins and
    never abandons one partway.
    """
    _entry(tmp_path, "ch1-budget")

    outcome = run(tmp_path, maximum_seconds=0.0)

    assert outcome["scored"] == 0 and outcome["budget_reached"] is True
    assert not scores_path(tmp_path / "ch1-budget").exists()


def test_status_separates_scored_from_scorable(tmp_path):
    """An entry nobody can map is not a backlog item; it will never be scored."""
    _entry(tmp_path, "ch1-status-done")
    _entry(tmp_path, "ch2-status-todo")
    _entry(tmp_path, "ch3-status-unmappable", order=False)
    run(tmp_path, limit=1)

    status = scoring_status(tmp_path)

    assert status["held"] == 3
    assert status["scored"] == 1
    assert status["eligible_unscored"] == 1


# --------------------------------------------------------------------------
# what a scored entry has to contain
# --------------------------------------------------------------------------

def test_the_sidecar_records_the_bank_the_capture_ran_not_this_hosts(tmp_path):
    """A capture's configuration is a fact about the capture, not about today.

    Measured over all 375 corpus manifests: 283 record ``profile.shape``
    [3, 8] over +/-300 kHz gating at 1.33 and 92 record [13, 8] over +/-700 kHz
    gating at 1.252 — yet every sidecar written so far reports [13, 8], this
    host's ``fast_scan.SURVEY_BANK`` at scoring time, consulted instead of the
    capture. That is what made a two-regime corpus look homogeneous, and it hid
    a reproduction check running against a bank 46 observations could not even
    represent. The gate is the same defect and worse: ``detection_threshold``
    has no entry for (3, 8) and emits the 1.40 fallback, a number no deployment
    has ever used, under a key named for the deployment.
    """
    from leo_tracker.radio.beacon import fast_scan
    entry = _entry(tmp_path, "ch1-narrow", tunings=((1, "lower-edge"),),
                   shape=(3, 8), offset_span_hz=300_000.0, threshold=1.33)
    # The trap: this host's constant is a different bank from the capture's.
    assert tuple(fast_scan.SURVEY_BANK) != (3, 8)

    payload = score_entry(entry)

    assert payload["deployed_shape"] == [3, 8]
    assert payload["deployed_threshold"] == 1.33
    assert payload["capture_bank"]["shape"] == [3, 8]
    assert payload["capture_bank"]["offset_span_hz"] == 300_000.0
    assert payload["capture_bank"]["known"] is True


def test_a_capture_that_never_said_which_bank_it_ran_is_unknown_not_todays(
        tmp_path):
    """A missing fact and a current-config guess must not share a spelling.

    Every probe taken before the profile was written down carries no bank at
    all. Filling that hole with whatever this host runs today produces a
    sidecar that states, in the same words and with the same confidence as a
    measured entry, something nobody measured — and the reader has no way to
    tell the two apart. None is the honest answer and it is also the useful
    one, because it is what makes the reproduction check exclude the row rather
    than score it against a bank invented for the occasion.
    """
    entry = _entry(tmp_path, "ch1-silent", tunings=((1, "lower-edge"),),
                   profile=False, offset_span_hz=None, threshold=None)

    payload = score_entry(entry)

    assert payload["deployed_shape"] is None
    assert payload["deployed_threshold"] is None
    assert payload["capture_bank"]["shape"] is None
    assert payload["capture_bank"]["offset_span_hz"] is None
    assert payload["capture_bank"]["known"] is False


def test_rerunning_the_deployed_detector_reproduces_the_capture_hosts_number(tmp_path):
    """The only end-to-end check on everything in front of the comparison.

    Config A ran on these exact samples at capture time and the answer is in the
    manifest. The mapping, the reshape, the receiver index and the bank build
    must all be right for re-running it here to land on the same value, and every
    one of them fails silently otherwise. Measured at exactly 0.0 across sixteen
    observations of a real entry, so approximate agreement is not the bar.
    """
    entry = _entry(tmp_path, "ch1-reproduce")
    # The synthetic manifest quotes a made-up 1.2 where a real one quotes what
    # the capture host measured, so what the capture host would have written is
    # first scored out and put back — after which zero is the only right answer.
    measured = {(item["channel"], item["region"], item["receiver"]):
                item["coarse"]["A"]["peak_to_median"]
                for item in score_entry(entry)["observations"]
                if item["arm"] == "target"}
    manifest = json.loads((entry / "manifest.json").read_text())
    for tuning in manifest["metadata"]["pre_dwell_survey"]["tunings"]:
        for scored in tuning["receivers"]:
            scored["peak_to_median"] = measured[
                (tuning["channel"], tuning["region"], scored["receiver"])]
    (entry / "manifest.json").write_text(json.dumps(manifest))

    payload = score_entry(entry)

    assert payload["deployed_reproduction"]["checked"] == 4
    assert payload["deployed_reproduction"]["worst_delta"] == 0.0
    assert all(item["deployed"]["anchor_agreement"] is not None
               for item in payload["observations"]
               if item["arm"] == "target")


def test_a_capture_on_the_widened_bank_is_reproduced_against_that_bank(
        tmp_path):
    """Re-running config A against a config E verdict is not a check.

    The corpus spans the widening, and the check was hard-wired to A. Measured
    over the 800 target observations the corpus's sidecars hold: against each
    capture's own bank 0 of 800 disagree past 1e-6, worst 5.165e-08; against A
    always, 160 of 800 do, worst 5.604e-01. Many sit off A's grid entirely —
    A's hypotheses are 300 kHz apart and this signal is at 466,666.67 Hz, which
    A cannot represent — and the rest land on the 0 Hz point both grids share,
    where the offset and the epoch agree and only the fold's median, taken over
    a curve already maximised across the offset axis, betrays the wrong bank.
    """
    entry = _entry(tmp_path, "ch1-widened", tunings=((1, "lower-edge"),),
                   block=_int16_block(_shifted(_replica(), WIDE_OFFSET_HZ)),
                   shape=WIDE_SHAPE, offset_span_hz=WIDE_SPAN_HZ,
                   profile_span=True, threshold=1.252)
    # What a capture host running the widened bank wrote down about this probe.
    first = score_entry(entry)
    measured = {item["receiver"]: item["coarse"]["E"]
                for item in first["observations"] if item["arm"] == "target"}
    assert measured[0]["frequency_offset_hz"] == pytest.approx(WIDE_OFFSET_HZ,
                                                              abs=1.0)
    manifest = json.loads((entry / "manifest.json").read_text())
    for tuning in manifest["metadata"]["pre_dwell_survey"]["tunings"]:
        for scored in tuning["receivers"]:
            wide = measured[scored["receiver"]]
            scored["peak_to_median"] = wide["peak_to_median"]
            scored["frequency_offset_hz"] = wide["frequency_offset_hz"]
    (entry / "manifest.json").write_text(json.dumps(manifest))

    payload = score_entry(entry)

    assert payload["deployed_reproduction"]["worst_delta"] == 0.0
    assert payload["deployed_reproduction"]["checked"] == 2
    assert payload["deployed_reproduction"]["excluded"] == 0
    assert payload["deployed_reproduction"]["bank"] == "E"
    # And A really is a different measurement, not a rounding of the same one:
    # its disagreement is orders above the 5.03e-08 a matched bank produces.
    target = next(item for item in first["observations"]
                  if item["arm"] == "target" and item["receiver"] == 0)
    assert abs(target["coarse"]["A"]["peak_to_median"]
               - measured[0]["peak_to_median"]) > 1e-3


def test_the_reproduction_scores_the_window_the_capture_host_scored(tmp_path):
    """Two sides scoring different sample sets is not a reproduction check.

    The capture host scores a bounded prefix — ``capture_config.scored_samples``
    is 200,000 on every randomised arm, the cheapest arm's whole probe — so its
    cost does not grow with the draw. The analysis host recomputes over the
    whole preserved probe, which the draw makes 200,000, 400,000 or 800,000.
    Three of the four live arms are longer than the cap, so the two sides score
    different windows and disagree by construction, and the disagreement is not
    a defect in the mapping the check exists to find.

    Proven directly from the IQ on capture
    ch1-lower-edge-narrow-pluto-5d4d-20260813T062858Z (arm 80ms-5.0MSps,
    ``scored_samples`` 200,000, ``samples_per_tuning`` 400,000): the mean over
    the first 200,000 samples is 87.4947, which is the manifest's deployed
    ``mean_power`` exactly, and the mean over all 400,000 is 84.8204, which is
    the recomputed one exactly. Over the sidecars that existed then, 16 of 16
    target observations on the long arms failed, worst delta 0.1555, cheapest
    6.945e-04 — a permanent false alarm that the corpus grows: 23 of the 40
    randomised entries already preserve more than they scored, and each new
    capture has a 3-in-4 chance of joining them.

    Where the record carries no ``capture_config`` at all — 353 of the 393
    manifests, every probe taken before the draw went live — the whole probe is
    the window the capture scored, and that is what gets used.
    """
    from leo_tracker.radio.beacon.survey_scoring import receiver_samples
    # A probe twice as long as the window the capture host scored, whose second
    # half lands on a different epoch: the prefix and the whole are genuinely
    # different measurements of different sky, not the same one rounded twice.
    prefix = _replica()
    whole = np.concatenate([prefix, _replica(epoch=int(PERIOD // 2))])
    scored_entry = _entry(tmp_path, "ch1-prefix", tunings=((1, "lower-edge"),),
                          samples=SAMPLES, block=_int16_block(prefix))
    entry = _entry(tmp_path, "ch1-bounded-prefix", tunings=((1, "lower-edge"),),
                   samples=whole.size, block=_int16_block(whole),
                   capture_config={"name": "test-arm", "probe_s": whole.size / RATE,
                                   "sample_rate_hz": RATE,
                                   "samples_per_tuning": whole.size,
                                   "scored_probe_s": SAMPLES / RATE,
                                   "scored_samples": SAMPLES,
                                   "scored_on": "capture host, bounded prefix"})
    # What the capture host wrote down, computed over the prefix it scored.
    bounded = {item["receiver"]: item["coarse"]["A"]["peak_to_median"]
               for item in score_entry(scored_entry)["observations"]
               if item["arm"] == "target"}
    manifest = json.loads((entry / "manifest.json").read_text())
    for tuning in manifest["metadata"]["pre_dwell_survey"]["tunings"]:
        for scored in tuning["receivers"]:
            scored["peak_to_median"] = bounded[scored["receiver"]]
    (entry / "manifest.json").write_text(json.dumps(manifest))

    payload = score_entry(entry)

    assert payload["samples_per_tuning"] == whole.size
    assert payload["deployed_reproduction"]["checked"] == 2
    assert payload["deployed_reproduction"]["excluded"] == 0
    assert payload["deployed_reproduction"]["worst_delta"] == pytest.approx(
        0.0, abs=1e-6)
    # Which window, recorded rather than implied, at both levels.
    assert payload["deployed_reproduction"]["scored_samples"] == SAMPLES
    assert all(item["deployed_reproduction_samples"] == SAMPLES
               for item in payload["observations"] if item["arm"] == "target")
    # And the whole probe really is a different number, so a check that used it
    # would be comparing two measurements rather than reproducing one.
    full = next(item for item in payload["observations"]
                if item["arm"] == "target" and item["receiver"] == 0)
    assert abs(full["coarse"]["A"]["peak_to_median"] - bounded[0]) > 1e-3
    # The whole probe is still what everything else is measured over: the probe
    # is preserved so the analysis host can score all of it.
    block = receiver_samples(
        np.fromfile(entry / "survey.ci16", "<i2").reshape(1, whole.size, 2, 2),
        0, 0)
    assert block.size == whole.size


def test_a_capture_that_scored_its_whole_probe_is_not_truncated(tmp_path):
    """353 of the 393 manifests predate the draw and carry no window at all.

    Absence of ``capture_config`` is not absence of a window: the capture host
    scored everything it kept, so the whole preserved probe *is* the window and
    truncating to some default would break the 353 entries that are currently
    the only ones the check passes on.
    """
    entry = _entry(tmp_path, "ch1-pre-draw", tunings=((1, "lower-edge"),),
                   block=_int16_block(_replica()))
    first = score_entry(entry)
    measured = {item["receiver"]: item["coarse"]["A"]["peak_to_median"]
                for item in first["observations"] if item["arm"] == "target"}
    manifest = json.loads((entry / "manifest.json").read_text())
    for tuning in manifest["metadata"]["pre_dwell_survey"]["tunings"]:
        for scored in tuning["receivers"]:
            scored["peak_to_median"] = measured[scored["receiver"]]
    (entry / "manifest.json").write_text(json.dumps(manifest))

    payload = score_entry(entry)

    assert payload["deployed_reproduction"]["worst_delta"] == 0.0
    assert payload["deployed_reproduction"]["scored_samples"] == SAMPLES
    assert payload["deployed_reproduction"]["scored_samples_source"] == \
        "whole preserved probe; the record declares no scored_samples"


def test_a_capture_whose_bank_is_unknown_is_excluded_rather_than_failed(
        tmp_path):
    """An unanswerable check must not answer, and must not vanish either.

    A probe whose record never said which bank it ran cannot be reproduced by
    anything: there is nothing to re-run. Scoring it against whichever config
    happens to be first manufactures a disagreement out of a missing fact, and
    silently dropping it shrinks the denominator without telling the reader the
    corpus is only partly verified. So it is excluded, and the exclusions are
    counted where the count is read.
    """
    entry = _entry(tmp_path, "ch1-unbanked", tunings=((1, "lower-edge"),),
                   profile=False, offset_span_hz=None, threshold=None)

    payload = score_entry(entry)

    assert payload["deployed_reproduction"]["checked"] == 0
    assert payload["deployed_reproduction"]["excluded"] == 2
    assert payload["deployed_reproduction"]["worst_delta"] is None
    assert all(item["deployed_reproduction_delta"] is None
               and item["deployed_reproduction_excluded"]
               for item in payload["observations"] if item["arm"] == "target")


def test_a_bank_is_a_shape_and_a_span_and_neither_names_it_alone():
    """Half a bank identity matched against half a bank identity is a guess.

    Thirteen hypotheses over +/-300 kHz is a 50 kHz grid and three over
    +/-700 kHz is a 466 kHz one; no deployment has ever run either, and matching
    on shape alone would happily reproduce a capture against a grid 2.33x the
    spacing it actually searched — the same class of substitution the whole
    check exists to catch, one level down. Both halves are pinned here because
    a mutation dropping the span comparison from ``_reproduction_config``
    otherwise passes the entire file.
    """
    from leo_tracker.radio.beacon.survey_scoring import _reproduction_config

    assert _reproduction_config({"shape": [3, 8], "offset_span_hz": 300_000.0,
                                 "known": True}) == "A"
    assert _reproduction_config({"shape": [13, 8], "offset_span_hz": 700_000.0,
                                 "known": True}) == "E"
    # Right shapes, wrong spans: two grids nothing has ever searched.
    assert _reproduction_config({"shape": [13, 8], "offset_span_hz": 300_000.0,
                                 "known": True}) is None
    assert _reproduction_config({"shape": [3, 8], "offset_span_hz": 700_000.0,
                                 "known": True}) is None


def test_a_record_that_names_a_shape_but_no_span_has_not_named_its_bank():
    """A half-recorded bank is an unrecorded bank, and must read as one.

    ``known`` is what decides whether the reproduction check runs at all, so a
    record carrying a shape and no span has to fail it: the span is half the
    configuration, and continuing with the half that is present would reproduce
    against a grid nobody wrote down. A mutation dropping ``bool(span)`` from
    the flag passes every other test in this file.
    """
    from leo_tracker.radio.beacon.survey_scoring import (_reproduction_config,
                                                         capture_bank)

    half = capture_bank({"profile": {"shape": [13, 8]}})

    assert half["shape"] == [13, 8]
    assert half["offset_span_hz"] is None
    assert half["known"] is False
    assert _reproduction_config(half) is None
    # And the whole thing, for contrast, from a record that did say.
    whole = capture_bank({"profile": {"shape": [13, 8],
                                      "offset_span_hz": 700_000.0},
                          "threshold": 1.252})
    assert whole["known"] is True
    assert _reproduction_config(whole) == "E"


def test_an_observation_this_host_cannot_re_run_is_counted_not_dropped():
    """The one hole in code whose whole principle is counted, never dropped.

    When the selected config is missing from an observation's coarse dict the
    delta guard returns None and the exclusion reason returns None too, so the
    observation lands in neither count and the denominator shrinks in silence —
    exactly the failure ``excluded`` was added to prevent, one branch further
    in. Unreachable today because ``score_entry`` always computes every config;
    it stops being unreachable the moment a config is added, dropped or fails,
    and the check would then over-report its own coverage without a word. The
    reason is the exact negative of the delta guard, so every in-scope
    observation lands in exactly one of the two counts.
    """
    from leo_tracker.radio.beacon.survey_scoring import (_reproduction_delta,
                                                         _reproduction_excluded)
    scored = {"peak_to_median": 1.2}
    bank = {"shape": [3, 8], "offset_span_hz": 300_000.0, "known": True}

    delta = _reproduction_delta(scored, {}, "target", "A")
    reason = _reproduction_excluded(scored, {}, "target", bank, "A")

    assert delta is None
    assert reason is not None
    assert (delta is None) != (reason is None)


def test_every_claim_names_a_place_and_says_how_hard_it_searched(tmp_path):
    """A score cannot be checked by anything else; a certificate can.

    ``search_cells`` travels with it because a maximum over 43,329 cells and an
    evaluation at one are different statistics — 2.2 dB apart, measured — and a
    reader who pools them will conclude the opposite of the truth.
    """
    entry = _entry(tmp_path, "ch1-certificates")

    payload = score_entry(entry)

    target = [item for item in payload["observations"]
              if item["arm"] == "target"]
    assert len(target) == 4                    # two tunings, two receivers
    methods = {certificate["method"]
               for certificate in target[0]["certificates"]}
    assert methods == {"coarse-A", "coarse-E", "anchor-8", "differential-16",
                       "differential-32", "glrt-32", "glrt-64",
                       "full-frame-300"}
    for certificate in target[0]["certificates"]:
        assert certificate["epoch_sample"] >= 0
        assert certificate["cfo_hz"] is not None
        assert certificate["search_cells"] >= 1
        assert certificate["elapsed_ms"] > 0
    assert target[0]["utc"].endswith("Z")


def test_a_rolled_control_given_a_free_epoch_re_finds_the_signal_it_should_not():
    """Rolling the pilot codes rolls the waveform, so the control is not a null.

    The 17-roll template is the plain frame displaced ``17 * 11 = 187`` samples,
    coherence 0.909, and a control allowed to choose its own epoch lands exactly
    there — on the real signal. Its p99 on the corpus reaches 1.851 against the
    cross-edge 1.252, and a threshold calibrated on it would have had the survey
    fire on 1.8% of sky instead of 21%. This is why every certificate carries
    ``control_epoch`` and why only pinned controls calibrate anything.
    """
    samples = _replica()

    _, control = matched_pilot_control_scores(samples, RATE, edge="lower",
                                              frequency_offsets_hz=(0.0,))
    pinned = pinned_full_frame(samples, RATE, 0, 0.0, edge="lower")

    shift = rolled_control_shift_samples(RATE)
    assert shift == 187
    # Epoch zero minus 187 wraps to one frame period short of it.
    assert control["sample_index"] == pytest.approx(round(PERIOD) - shift, abs=2)
    assert control["score"] > 0.85                   # it found the signal
    assert pinned["control_score"] < 0.15            # held still, it finds none
    # The same statistic on both sides, which is what makes it a margin: the
    # searched score is one frame's normalised correlation and so is this.
    assert pinned["score"] == pytest.approx(1.0, abs=1e-5)


def test_the_searched_rolled_control_is_recorded_beside_the_pinned_one(tmp_path):
    """Documented as an exhibit rather than merely avoided in the code.

    The certificate keeps the honest pinned control for its margin and carries
    the searched one and its epoch shift alongside, so the review can print the
    gap and a later reader does not have to rediscover it.
    """
    entry = _entry(tmp_path, "ch1-trap")

    payload = score_entry(entry)

    full = next(certificate
                for certificate in payload["observations"][0]["certificates"]
                if certificate["method"] == "full-frame-300")
    assert full["control_epoch"] == "pinned"
    # Pinned and still not this score's null: the score is a maximum over every
    # lag while the control sits at one.
    assert full["epoch_searched"] is True
    assert full["searched_control_score"] is not None
    assert full["rolled_shift_samples"] == 187
    assert full["margin"] == pytest.approx(
        full["score"] - full["control_score"])
    assert payload["rolled_control_shift_samples"] == 187
    assert payload["probe_ms"] == pytest.approx(1000.0 * SAMPLES / RATE)
    # Both coarse gates, because this host re-runs both banks — and separately
    # the capture's own, because the deployed shape moved from 3x8 to 13x8
    # mid-flight and a sidecar that quoted either constant for every capture
    # would be describing a host rather than a probe.
    assert set(payload["scorer_coarse_threshold"]) == {"A", "E"}
    assert payload["deployed_shape"] == [3, 8]


def test_the_wrong_code_control_travels_with_every_candidate_claim(tmp_path):
    """A score of 0.4 is evidence only if the rolled code does not also reach it.

    The coarse banks are the exception and must say so: :mod:`fast_scan` has no
    rolled-template path, so their false-alarm evidence rests on the cross-edge
    arm alone and a null margin is honest where a zero would be a lie.
    """
    entry = _entry(tmp_path, "ch1-controls")

    payload = score_entry(entry)

    by_method = {certificate["method"]: certificate
                 for certificate in payload["observations"][0]["certificates"]}
    for method in ("anchor-8", "differential-32", "glrt-64", "full-frame-300"):
        assert by_method[method]["control_score"] is not None
        assert by_method[method]["margin"] == pytest.approx(
            by_method[method]["score"] - by_method[method]["control_score"])
    assert by_method["coarse-E"]["control_score"] is None
    assert by_method["coarse-E"]["margin"] is None


def test_the_cross_edge_null_scores_the_opposite_edges_code_on_this_probe(tmp_path):
    """Target-pilot-free by construction: those codes sit 230 MHz away.

    Run on the same IQ rather than on another probe, so the null shares this
    tuning's gain and interference, and in both directions rather than only the
    plan's lower-on-upper, with the direction recorded so either can be sliced
    back out.
    """
    entry = _entry(tmp_path, "ch1-null")

    payload = score_entry(entry, null_stride=1)

    nulls = [item for item in payload["observations"]
             if item["arm"] == "cross-edge-null"]
    assert len(nulls) == 4
    directions = {item["null_direction"] for item in nulls}
    assert directions == {"upper-on-lower", "lower-on-upper"}
    for item in nulls:
        assert item["template_edge"] != item["edge"]


def test_the_null_arm_can_be_thinned_without_thinning_the_target_arm(tmp_path):
    """The null only has to accumulate; the target arm is what is being measured."""
    entry = _entry(tmp_path, "ch1-stride")

    payload = score_entry(entry, null_stride=2)

    arms = [item["arm"] for item in payload["observations"]]
    assert arms.count("target") == 4
    assert 0 < arms.count("cross-edge-null") < 4


def test_claims_naming_the_same_place_are_conditioned_once(tmp_path):
    """Five candidates inherit one coarse epoch, so their claims collide often.

    Epochs are compared modulo the frame period, the way two receivers are
    compared in :mod:`analysis`: a claim one whole frame later is the same claim
    about the same signal.
    """
    certificates = [
        {"method": "coarse-E", "epoch_sample": 1000, "cfo_hz": 100.0},
        {"method": "anchor-8", "epoch_sample": 1003, "cfo_hz": 150.0},
        {"method": "glrt-32", "epoch_sample": 1000 + int(round(PERIOD)),
         "cfo_hz": 100.0},
        {"method": "glrt-64", "epoch_sample": 1000, "cfo_hz": 9_000.0},
    ]

    points = distinct_points(certificates, RATE)

    assert len(points) == 2
    merged = next(point for point in points if len(point["claimed_by"]) == 3)
    assert sorted(merged["claimed_by"]) == ["anchor-8", "coarse-E", "glrt-32"]


def test_every_method_is_asked_about_every_claimed_place(tmp_path):
    """The point of the whole design: methods validate each other's claims.

    A method that cannot acquire a weak signal may still confirm it firmly once
    somebody else says where to look, and that is invisible if each method only
    ever searches alone.
    """
    entry = _entry(tmp_path, "ch1-confirm")

    payload = score_entry(entry, null_stride=1)

    observation = payload["observations"][0]
    assert observation["points"]
    for point in observation["points"]:
        assert set(point["methods"]) == {
            "anchor-8", "differential-16", "differential-32", "glrt-32",
            "glrt-64", "full-frame-full", "full-frame-acquire",
            "full-frame-verify"}
        for value in point["methods"].values():
            assert value["control_score"] is not None
            assert value["cross_edge_score"] is not None
            # Frames are never averaged away before storage: a max-over-frames
            # combiner keeps the exponential tail the average loses and is the
            # better one under 2% occupancy.
            assert value["frame_max"] is not None
        assert point["claimed_by"]
        assert point["adjudication"]["withheld"] == "unknown"


# --------------------------------------------------------------------------
# the shared conditioned path, gated against the reference it replaces
# --------------------------------------------------------------------------

def test_the_shared_conditioned_path_reproduces_the_reference_detectors():
    """One correlation set serving six statistics must equal six separate runs.

    :func:`conditioned_suite` computes the widest contiguous run once and slices
    it, which is five times cheaper and is exactly the kind of change that turns
    into a silent redesign. So it is pinned against
    :mod:`relative_phase`'s public functions on the same samples, at the
    tolerance of the arithmetic rather than of the eye.
    """
    samples = _replica()

    suite = conditioned_suite(samples, RATE, 0, 0.0, edge="lower")

    for count in (16, 32):
        reference = adjacent_differential(samples, RATE, 0, 0.0, edge="lower",
                                          symbol_count=count)
        shared = suite["scores"][f"differential-{count}"]
        assert shared["score"] == pytest.approx(reference["score"], rel=1e-12)
        assert shared["residual_frequency_offset_hz"] == pytest.approx(
            reference["residual_frequency_offset_hz"], rel=1e-9, abs=1e-9)
    anchor = anchor_relative_phase(samples, RATE, 0, 0.0, edge="lower",
                                   search=False)
    assert suite["scores"]["anchor-8"]["score"] == pytest.approx(
        anchor["score"], rel=1e-12)


def test_a_conditioned_glrt_is_the_searched_one_with_the_search_removed():
    """Same S(f); the searched version reports its maximum, this one a value.

    On a noiseless replica at zero residual the maximum *is* the value at zero,
    which is the one input where the two must coincide exactly — and if they do
    not, the conditioned column is measuring something else entirely.
    """
    samples = _replica()

    searched = symbol_glrt(samples, RATE, 0, 0.0, edge="lower", symbol_count=32)
    suite = conditioned_suite(samples, RATE, 0, 0.0, edge="lower")

    assert searched["residual_frequency_offset_hz"] == pytest.approx(0.0, abs=1.0)
    assert suite["scores"]["glrt-32"]["score"] == pytest.approx(
        searched["score"], rel=1e-12)


def test_the_searched_detectors_margin_subtracts_its_own_statistic():
    """A margin is a difference of like for like, or it is not a margin.

    The searched certificate is one frame's normalised correlation maximised
    over lags; the adjudicator's blocks are a mean over ~59 frames of a
    per-frame correlation over pilot samples only. Subtracting the second from
    the first would be two different numbers with a minus sign between them, so
    the searched candidate keeps its own control at a pinned epoch and the
    adjudicator stays the confirmer.
    """
    samples = _replica()

    pinned = pinned_full_frame(samples, RATE, 0, 0.0, edge="lower")
    blocks = adjudicated(samples, RATE, 0, 0.0, edge="lower")["blocks"]

    assert pinned["score"] == pytest.approx(1.0, abs=1e-5)
    assert blocks["full"]["score"] == pytest.approx(1.0, abs=1e-3)
    # Same on a noiseless replica by construction; the point is that they are
    # different statistics, which only shows on real data.
    assert pinned["margin"] > 0.8
    assert blocks["full"]["frames"] > 1


def test_the_300_symbol_confirmer_is_the_adjudicator_rather_than_a_second_one():
    """Delegated, not rebuilt, so there is one set of conventions rather than two.

    :mod:`adjudicate` already conditions this statistic at the claimed point,
    splits the pilots into disjoint ACQUIRE and VERIFY halves, and holds the
    wrong-code control at the same epoch. A noiseless replica must read 1.0 on
    every block, which is the repository's ``|r^H s| / sqrt(|r|^2 |s|^2)``
    convention and the reason an ACQUIRE score and a VERIFY score compare.
    """
    samples = _replica()

    verdict = adjudicated(samples, RATE, 0, 0.0, edge="lower")

    assert set(verdict["blocks"]) == {"full", "acquire", "verify"}
    for name, block in verdict["blocks"].items():
        assert block["score"] == pytest.approx(1.0, abs=1e-3), name
        assert block["control_score"] < 0.2, name
    # Only the verdict block pays for its per-frame array; carrying all three
    # would be ~1.5 MB an entry for the review to parse.
    assert len(verdict["blocks"]["verify"]["frame_scores"]) > 1
    assert "frame_scores" not in verdict["blocks"]["full"]
    # Today's proposers read every symbol, so nothing was really withheld.
    assert verdict["withheld"] == "unknown"


def test_a_conditioned_score_needs_no_search_and_says_so():
    """One cell by definition, which is the whole reason it is worth recording.

    The advantage is 2.2 dB measured over a bounded 3,333 x 11 search, not the
    5.2 dB an exponential tail gives: that tail belongs to a single-frame power
    statistic, while a score averaged over ~59 frames has a nearly Gaussian
    null. The cell count is not bookkeeping either way — it is which population
    a score belongs to.
    """
    assert search_cells("coarse-A", RATE, 200_000) == 3 * round(PERIOD)
    assert search_cells("coarse-E", RATE, 200_000) == 13 * round(PERIOD)
    assert search_cells("differential-32", RATE, 200_000) == 1
    assert search_cells("anchor-8", RATE, 200_000) == 1
    assert search_cells("full-frame-300", RATE, 200_000) > 190_000


def test_evaluating_where_the_signal_is_not_costs_almost_all_of_the_score():
    """Conditioning is only meaningful if the statistic actually depends on it."""
    samples = _replica()

    on_point = conditioned_suite(samples, RATE, 0, 0.0, edge="lower")
    off_point = conditioned_suite(samples, RATE, 0, 90_000.0, edge="lower")

    assert on_point["scores"]["glrt-32"]["score"] > 0.9
    assert off_point["scores"]["glrt-32"]["score"] < 0.5


# --------------------------------------------------------------------------
# checks the detector did not make itself
# --------------------------------------------------------------------------

def test_the_receivers_are_compared_the_way_the_dwell_path_compares_them():
    """Same wrapped epoch difference and same offset difference, same words.

    The antennas are co-located, so a real satellite lands on the same frame
    epoch at both ports with offsets differing by exactly the measured LNB bias
    — and the check needs neither absolute offset, which matters because the
    calibration structurally cannot establish either one.
    """
    observations = [
        {"arm": "target", "channel": 1, "region": "lower-edge", "receiver": 0,
         "certificates": [{"method": "glrt-32", "epoch_sample": 1000,
                           "cfo_hz": 434_000.0}]},
        {"arm": "target", "channel": 1, "region": "lower-edge", "receiver": 1,
         "certificates": [{"method": "glrt-32",
                           "epoch_sample": 1002 + int(round(PERIOD)),
                           "cfo_hz": 1_500.0}]},
    ]

    uncalibrated = cross_receiver_checks(observations, RATE)
    calibrated = cross_receiver_checks(observations, RATE,
                                       receiver_centers=(434_000.0, 0.0),
                                       calibrated=True)

    assert uncalibrated[0]["epoch_difference_samples"] == pytest.approx(2, abs=1)
    assert uncalibrated[0]["cfo_difference_hz"] == pytest.approx(432_500.0)
    assert uncalibrated[0]["agrees"] is False
    assert calibrated[0]["cfo_residual_after_bias_hz"] == pytest.approx(1_500.0)
    assert calibrated[0]["agrees"] is True


def test_the_geometry_prior_carries_the_share_of_the_span_it_already_covers(
        tmp_path):
    """Otherwise a match reads as evidence when it may only be arithmetic.

    The plan measures the prior covering ~74% of the search space, at which
    point "a satellite was predicted there" is about 1.35:1 — and the way to
    stop that being over-read is to print the coverage beside it, not to leave
    the caveat in a document nobody has open.
    """
    from leo_tracker.radio.beacon.survey_scoring import geometry_checks

    satellites = [{"norad_id": 1, "name": "STARLINK-1", "tolerance_hz": 20_000.0,
                   "receivers": [{"receiver": 0, "predicted_offset_hz": 50_000.0}]},
                  {"norad_id": 2, "name": "STARLINK-2", "tolerance_hz": 20_000.0,
                   "receivers": [{"receiver": 0, "predicted_offset_hz": 60_000.0}]}]

    report = geometry_checks(
        [{"method": "glrt-32", "cfo_hz": 55_000.0},
         {"method": "glrt-64", "cfo_hz": 400_000.0}],
        satellites, 0, span_hz=100_000.0)

    assert report["by_method"]["glrt-32"]["matches"] == 2
    assert report["by_method"]["glrt-64"]["matches"] == 0
    assert report["by_method"]["glrt-32"]["best"]["norad_id"] == 1
    # 30 kHz to 80 kHz of a 200 kHz span, merged rather than double counted.
    assert report["prior_coverage_fraction"] == pytest.approx(0.25)


def test_prior_coverage_is_priced_over_the_span_this_comparison_searches(
        tmp_path):
    """The denominator belongs to the search being priced, not to the capture.

    ``prior_coverage_fraction`` prices *this* comparison's claims, and every
    claim in the row comes from the candidate coarse config's +/-700 kHz search.
    Swapping in the capture's own narrower span does not narrow what was
    searched; it only narrows the denominator, while ``geometry_checks`` goes on
    matching unclamped — 722 of 9,792 certificates on A-era captures carry
    |cfo_hz| above 300,000, the largest 694,750 — so a claim outside the
    capture's span can still match a satellite the same span excluded from the
    denominator. The number does not even move the way swapping it was meant to
    move it: ``_prior_coverage`` clamps its numerator too, so on a measured
    [3, 8]/300 kHz capture the fraction is 0.3611 at 700 kHz and 0.1969 at
    300 kHz — it *falls* 1.83x rather than the 2.33x rise the swap claimed.
    Priced over the span the claims were drawn from, both halves clamp at the
    same edge and the fraction means what it says.
    """
    entry = _entry(tmp_path, "ch1-geometry", tunings=((1, "lower-edge"),),
                   shape=(3, 8), offset_span_hz=300_000.0, threshold=1.33)
    # One satellite inside the capture's span and one entirely outside it, so
    # the two denominators are different numbers and the test can tell which
    # one was used: 100 kHz + 300 kHz over 1.4 MHz, against 100 kHz over 600 kHz.
    satellites = [
        {"norad_id": 1, "name": "STARLINK-1", "tolerance_hz": 50_000.0,
         "receivers": [{"receiver": index, "predicted_offset_hz": 0.0}
                       for index in (0, 1)]},
        {"norad_id": 2, "name": "STARLINK-2", "tolerance_hz": 150_000.0,
         "receivers": [{"receiver": index, "predicted_offset_hz": 550_000.0}
                       for index in (0, 1)]}]
    (entry / "truth.json").write_text(json.dumps(
        {"tunings": [{"iq_index": 0, "satellites": satellites}]}))

    payload = score_entry(entry)

    geometry = next(item["geometry"] for item in payload["observations"]
                    if item["arm"] == "target" and item["receiver"] == 0)
    assert geometry["prior_coverage_fraction"] == pytest.approx(400_000.0
                                                                / 1_400_000.0)
    # The capture's own span would have said this instead, and it is the wrong
    # denominator for a claim this comparison is free to make outside it.
    assert geometry["prior_coverage_fraction"] != pytest.approx(100_000.0
                                                                / 600_000.0)
    # A claim 550 kHz out matches a satellite the 300 kHz denominator drops.
    from leo_tracker.radio.beacon.survey_scoring import geometry_checks
    narrow = geometry_checks([{"method": "coarse-E", "cfo_hz": 550_000.0}],
                             satellites, 0, span_hz=300_000.0)
    assert narrow["by_method"]["coarse-E"]["matches"] == 1
    assert narrow["prior_coverage_fraction"] == pytest.approx(100_000.0
                                                              / 600_000.0)
