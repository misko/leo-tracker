"""Turning accumulated probe scores into a comparison, without inventing one.

Real sky carries no labels, so the review can report distributions, thresholds
against two nulls, corroboration and cost — and cannot report a detection
probability. The failure mode is not arithmetic; it is a reader taking "method X
fires more often" for "method X is more sensitive" when it is equally the shape
of a worse false-alarm rate. So most of what is pinned here is that the report
keeps its populations apart and states its own limits: searched and conditioned
scores never pool, a threshold drawn from too few null realisations is labelled
rather than quoted, and the sentence that prevents the misreading is in the
output rather than in a document nobody has open.

Synthetic sidecars throughout. Aggregation is arithmetic over what the scorer
wrote, and it is checked against numbers a reader can verify by hand — which is
not possible if the input is a real probe.
"""
import json

import pytest

from leo_tracker.radio.beacon.survey_comparison import (
    MINIMUM_EXCEEDANCES, PREAMBLE, REVIEW_SCHEMA, describe, format_review,
    load_scores, pairwise_agreement, review, rolled_control_trap,
    searched_comparison, threshold_from, time_continuity)
from leo_tracker.radio.beacon.survey_scoring import SCORES_SCHEMA


def _certificate(method, score, *, control=None, epoch=1000, cfo=1000.0,
                 cells=1, elapsed=10.0, control_epoch="pinned", **extra):
    return {"method": method, "epoch_sample": epoch, "epoch_s": epoch / 2.5e6,
            "cfo_hz": cfo, "score": score, "control_score": control,
            "control_epoch": None if control is None else control_epoch,
            "margin": None if control is None else score - control,
            "residual_cfo_hz": 0.0, "search_cells": cells,
            "coarse_config": "E", "elapsed_ms": elapsed, **extra}


def _observation(arm, *, receiver=0, channel=1, region="lower-edge",
                 certificates=(), utc="2026-08-12T20:00:00Z", points=(),
                 geometry=None, delta=0.0):
    return {"arm": arm, "template_edge": "lower",
            "null_direction": None if arm == "target" else "upper-on-lower",
            "iq_index": 0, "channel": channel, "region": region,
            "edge": region.split("-")[0], "receiver": receiver,
            "receiver_label": "lnb-a", "if_center_hz": 9.6e8,
            "rf_center_hz": 1.07e10, "utc": utc,
            "deployed": {"peak_to_median": 1.2, "anchor_agreement": 0},
            "deployed_reproduction_delta": delta,
            "coarse": {}, "certificates": list(certificates),
            "points": list(points), "geometry": geometry}


def _payload(capture, observations, *, elapsed=50.0, cross_receiver=(),
             cross_edge=()):
    return {"schema": SCORES_SCHEMA, "capture": capture,
            "radio_id": "pluto-test", "sample_rate_hz": 2.5e6,
            "deployed_reproduction": {"checked": len(observations),
                                      "worst_delta": 0.0},
            "observations": list(observations),
            "cross_receiver": list(cross_receiver),
            "cross_edge": list(cross_edge), "elapsed_s": elapsed}


def _write(root, payload):
    entry = root / payload["capture"]
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "scores.json").write_text(json.dumps(payload))
    return entry


# --------------------------------------------------------------------------
# reading, and refusing to read
# --------------------------------------------------------------------------

def test_a_sidecar_of_another_schema_is_left_out_of_the_aggregate(tmp_path):
    """Two definitions of one column silently averaged is the worst outcome."""
    _write(tmp_path, _payload("good", [_observation("target")]))
    stale = _write(tmp_path, _payload("stale", [_observation("target")]))
    (stale / "scores.json").write_text(json.dumps({"schema": "older/v0"}))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "scores.json").write_text("{ truncated")

    loaded = load_scores(tmp_path)

    assert [payload["capture"] for payload in loaded] == ["good"]


# --------------------------------------------------------------------------
# thresholds and the limits of a finite null
# --------------------------------------------------------------------------

def test_a_threshold_says_how_many_realisations_it_rests_on():
    """A 1% quantile of forty samples is a number, not a measurement.

    Below roughly five exceedances the quantile is set by two or three draws and
    moves by whole percent when one more arrives. The plan makes the same point
    from the other end: 1,456 clean realisations support ~0.2% and no further.
    """
    small = threshold_from([float(index) for index in range(40)],
                           false_alarm_rate=0.01)
    ample = threshold_from([float(index) for index in range(2_000)],
                           false_alarm_rate=0.01)

    assert small["samples"] == 40
    assert small["supported"] is False
    assert small["finest_rate"] == pytest.approx(MINIMUM_EXCEEDANCES / 40)
    # What it really achieves, which is not what was asked for: the order
    # statistic ran out of samples and landed on the largest draw.
    assert small["effective_rate"] == pytest.approx(0.0)
    assert ample["supported"] is True
    assert ample["threshold"] == pytest.approx(1_980, abs=2)
    assert ample["effective_rate"] == pytest.approx(0.01, abs=0.002)


def test_a_null_with_nothing_in_it_yields_no_threshold_rather_than_zero():
    """Zero would silently make every score a detection."""
    empty = threshold_from([])

    assert empty["threshold"] is None and empty["supported"] is False


def test_a_control_that_chose_its_own_epoch_never_calibrates_a_threshold(tmp_path):
    """Rolling the codes rolls the waveform, so a free epoch re-finds the signal.

    The 17-roll template is the plain frame displaced 187 samples with coherence
    0.909, and a rolled bank's winning epoch is exactly ``true_epoch - 187``. Its
    p99 on the corpus reaches 1.851 against the cross-edge 1.252, correlating
    0.967 with the matched score — calibrating on it would have had the survey
    fire on 1.8% of sky instead of 21%. The guard lives here rather than in the
    producer, because the producer is where somebody will add a new detector.
    """
    _write(tmp_path, _payload("searched-control", [
        _observation("target", certificates=[
            _certificate("free-control", 0.9, control=1.8,
                         control_epoch="searched"),
            _certificate("searched-score", 0.9, control=0.02,
                         control_epoch="pinned", epoch_searched=True)]),
        _observation("cross-edge-null", certificates=[
            _certificate("free-control", 0.2, control=1.7,
                         control_epoch="searched"),
            _certificate("searched-score", 0.2, control=0.02,
                         control_epoch="pinned", epoch_searched=True)])]))

    summary = searched_comparison(load_scores(tmp_path))

    # A control free to choose its epoch is not a null at all.
    assert summary["free-control"]["wrong_code_null"]["threshold"] is None
    # A control pinned to one lag is not the null of a maximum over many lags.
    assert summary["searched-score"]["wrong_code_null"]["threshold"] is None
    assert summary["free-control"]["has_control"] is False
    assert summary["free-control"]["cross_edge_null"]["threshold"] == pytest.approx(0.2)


def test_the_two_nulls_are_reported_separately_because_they_can_disagree(tmp_path):
    """Cross-edge holds under a search; wrong-code holds only with a pinned epoch.

    Where both are valid they still disagree, because a rolled sequence keeps
    some correlation with the true one: where a pilot is present the control
    rises with it and the wrong-code threshold sits higher. Collapsing them into
    a single 'null' hides exactly the disagreement worth reading.
    """
    observations = []
    for index in range(20):
        observations.append(_observation(
            "target", receiver=index % 2,
            certificates=[_certificate("glrt-32", 0.9, control=0.5)]))
        observations.append(_observation(
            "cross-edge-null", receiver=index % 2,
            certificates=[_certificate("glrt-32", 0.1, control=0.05)]))
    _write(tmp_path, _payload("two-nulls", observations))

    summary = searched_comparison(load_scores(tmp_path))["glrt-32"]

    assert summary["cross_edge_null"]["threshold"] == pytest.approx(0.1)
    assert summary["wrong_code_null"]["threshold"] == pytest.approx(0.5)
    assert summary["cross_edge_null"]["fires"]["rate"] == 1.0
    assert summary["wrong_code_null"]["fires"]["rate"] == 1.0
    assert summary["claims"] == 20


def test_a_method_without_a_wrong_code_control_reports_none_not_zero(tmp_path):
    """The kernel bank has no rolled-template path, and a zero would be a lie."""
    _write(tmp_path, _payload("coarse-only", [
        _observation("target", certificates=[_certificate("coarse-E", 1.4)]),
        _observation("cross-edge-null",
                     certificates=[_certificate("coarse-E", 1.2)])]))

    summary = searched_comparison(load_scores(tmp_path))["coarse-E"]

    assert summary["has_control"] is False
    assert summary["wrong_code_null"]["threshold"] is None
    assert summary["cross_edge_null"]["threshold"] == pytest.approx(1.2)


# --------------------------------------------------------------------------
# aggregation over many entries
# --------------------------------------------------------------------------

def test_the_review_accumulates_across_entries_rather_than_re_deciding(tmp_path):
    """One sweep of real sky is one draw; the whole design is to add them up."""
    for index in range(5):
        _write(tmp_path, _payload(f"capture-{index}", [
            _observation("target", certificates=[
                _certificate("glrt-32", 0.5 + index / 100, control=0.1)]),
            _observation("cross-edge-null", certificates=[
                _certificate("glrt-32", 0.1 + index / 100, control=0.05)])]))

    report = review(tmp_path)

    assert report["schema"] == REVIEW_SCHEMA
    assert report["entries"] == 5
    assert report["observations"] == 10
    assert report["searched"]["glrt-32"]["claims"] == 5
    assert report["searched"]["glrt-32"]["score"]["p50"] == pytest.approx(0.52)
    assert report["cost_s_per_entry"]["p50"] == pytest.approx(50.0)


def test_a_limit_reviews_a_subset_without_changing_how_it_is_computed(tmp_path):
    for index in range(4):
        _write(tmp_path, _payload(f"capture-{index}",
                                  [_observation("target")]))

    assert review(tmp_path, limit=2)["entries"] == 2


def test_methods_are_compared_at_a_common_false_alarm_rate_not_a_common_score(
        tmp_path):
    """The statistics are normalised differently; only the rate is comparable.

    ``differential-32`` and ``coarse-E`` do not even share units — one is a
    coherence fraction in [0,1], the other a peak-to-median above 1 — so a shared
    numeric threshold would be meaningless and a shared null quantile is the
    only honest way to ask whether they fire on the same probes.
    """
    observations = []
    for index in range(10):
        loud = index < 6
        observations.append(_observation("target", channel=index, certificates=[
            _certificate("coarse-E", 1.9 if loud else 1.1),
            _certificate("differential-32", 0.8 if loud else 0.05,
                         control=0.02)]))
        observations.append(_observation("cross-edge-null", channel=index,
                                         certificates=[
            _certificate("coarse-E", 1.2),
            _certificate("differential-32", 0.06, control=0.02)]))
    _write(tmp_path, _payload("common-rate", observations))
    payloads = load_scores(tmp_path)

    agreement = pairwise_agreement(payloads, searched_comparison(payloads))

    assert agreement["fired"]["coarse-E"] == 6
    assert agreement["fired"]["differential-32"] == 6
    pair = agreement["pairs"]["coarse-E|differential-32"]
    assert pair["both"] == 6 and pair["jaccard"] == pytest.approx(1.0)


def test_a_claim_that_moves_faster_than_physics_is_counted_as_implausible(
        tmp_path):
    """Doppler is bounded at 12.7 kHz/s worst case by the catalogue sweep.

    Two claims on one tuning implying a faster slew are not one satellite being
    tracked; they are two unrelated numbers, and counting that is free because
    the times and offsets are already written down.
    """
    _write(tmp_path, _payload("continuity", [
        _observation("target", utc="2026-08-12T20:00:00Z",
                     certificates=[_certificate("glrt-32", 0.5, cfo=0.0)]),
        _observation("target", utc="2026-08-12T20:00:10Z",
                     certificates=[_certificate("glrt-32", 0.5, cfo=50_000.0)]),
        _observation("target", utc="2026-08-12T20:00:20Z",
                     certificates=[_certificate("glrt-32", 0.5,
                                                cfo=500_000.0)])]))

    continuity = time_continuity(load_scores(tmp_path))["glrt-32"]

    assert continuity["pairs"] == 2
    assert continuity["plausible"] == 1          # 5 kHz/s yes, 45 kHz/s no
    assert continuity["typical"] == 1
    assert continuity["plausible_rate"] == pytest.approx(0.5)


def test_an_observation_with_no_timestamp_is_left_out_of_continuity(tmp_path):
    """A probe whose survey record predates the timestamps still has scores."""
    _write(tmp_path, _payload("no-time", [
        _observation("target", utc=None,
                     certificates=[_certificate("glrt-32", 0.5)]),
        _observation("target", utc=None,
                     certificates=[_certificate("glrt-32", 0.5)])]))

    assert time_continuity(load_scores(tmp_path)) == {}


def test_the_null_arm_never_enters_the_population_being_measured(tmp_path):
    """It is the yardstick; counting it as a claim would measure the yardstick."""
    _write(tmp_path, _payload("arms", [
        _observation("target",
                     certificates=[_certificate("glrt-32", 0.9, control=0.1)]),
        _observation("cross-edge-null",
                     certificates=[_certificate("glrt-32", 0.2, control=0.1)])]))

    summary = searched_comparison(load_scores(tmp_path))["glrt-32"]

    assert summary["claims"] == 1
    assert summary["score"]["count"] == 1
    assert summary["cross_edge_null"]["samples"] == 1


def test_conditioning_at_a_claim_the_method_made_itself_is_not_confirmation(
        tmp_path):
    """Reading a maximisation back at its own argmax is not an independent test.

    It is the same number the search produced. Only a score at *another*
    method's claim is evidence about that claim, which is why the confirmation
    rate excludes self-claimed rows and counts them apart.
    """
    point = {"point_id": 0, "epoch_sample": 1000, "cfo_hz": 1000.0,
             "claimed_by": ["glrt-32"], "correlation_ms": 90.0,
             "methods": {"glrt-32": {"score": 0.9, "control_score": 0.1,
                                     "margin": 0.8, "cross_edge_score": 0.05,
                                     "residual_cfo_hz": 0.0, "elapsed_ms": 0.2},
                         "differential-32": {"score": 0.7, "control_score": 0.1,
                                             "margin": 0.6,
                                             "cross_edge_score": 0.05,
                                             "residual_cfo_hz": 0.0,
                                             "elapsed_ms": 0.2}}}
    _write(tmp_path, _payload("conditioned", [
        _observation("target", points=[point],
                     certificates=[_certificate("glrt-32", 0.9, control=0.1)])]))

    conditioned = review(tmp_path)["conditioned"]

    assert conditioned["glrt-32"]["conditioned_points"] == 1
    assert conditioned["glrt-32"]["on_other_methods_claims"] == 0
    assert conditioned["differential-32"]["on_other_methods_claims"] == 1


# --------------------------------------------------------------------------
# what the printed report has to say out loud
# --------------------------------------------------------------------------

def test_confirmation_is_counted_per_proposer_not_pooled(tmp_path):
    """A method can be a poor searcher and a fine confirmer, or the reverse.

    The plan's architecture wants one of each — something cheap to say where to
    look and something sharp to settle it — and a pooled confirmation rate
    cannot tell them apart. A proposer never confirms its own claim: reading a
    maximisation back at its own argmax is not a second opinion.
    """
    def _point(point_id, claimed_by, glrt, diff):
        return {"point_id": point_id, "epoch_sample": 1000 + point_id,
                "cfo_hz": 1000.0, "claimed_by": list(claimed_by),
                "correlation_ms": 90.0, "methods": {
                    "glrt-32": {"score": glrt, "control_score": 0.01,
                                "margin": glrt - 0.01, "cross_edge_score": 0.02,
                                "control_epoch": "pinned", "elapsed_ms": 0.2},
                    "differential-32": {"score": diff, "control_score": 0.01,
                                        "margin": diff - 0.01,
                                        "cross_edge_score": 0.02,
                                        "control_epoch": "pinned",
                                        "elapsed_ms": 0.2}}}

    observations = [_observation("target", channel=index, points=[
        _point(index, ["coarse-E"], glrt=0.9 if index < 8 else 0.001,
               diff=0.001)]) for index in range(10)]
    _write(tmp_path, _payload("confirm", observations))

    matrix = review(tmp_path)["confirmation"]

    assert matrix["coarse-E->glrt-32"]["claims"] == 10
    assert matrix["coarse-E->glrt-32"]["confirmed"] == 8
    assert matrix["coarse-E->differential-32"]["confirmed"] == 0
    assert "coarse-E->coarse-E" not in matrix
    assert "CONFIRMATION" in format_review(review(tmp_path))


def test_the_report_states_that_firing_more_is_not_the_same_as_seeing_more(
        tmp_path):
    """The one misreading that would invert every conclusion in the table.

    It travels in the output rather than in a document, because the output is
    what gets pasted into a discussion.
    """
    _write(tmp_path, _payload("printed", [
        _observation("target",
                     certificates=[_certificate("glrt-32", 0.9, control=0.1)]),
        _observation("cross-edge-null",
                     certificates=[_certificate("glrt-32", 0.2, control=0.1)])]))

    printed = format_review(review(tmp_path))

    assert PREAMBLE in printed
    assert "no injection" in printed
    assert "false-alarm rate" in printed
    assert "SEARCHED" in printed and "CONDITIONED" in printed
    assert "CORROBORATION" in printed and "AGREEMENT" in printed


def test_the_rolled_control_trap_is_written_down_rather_than_stepped_around(
        tmp_path):
    """A future reader will find ``matched_pilot_control_scores`` and reach for it.

    Avoiding the defect in code leaves nothing for them; measuring it on the same
    probes leaves the number. The searched control should sit far above the
    pinned one and should land at ``-187`` samples — which wraps to 3,146 — where
    the real signal is.
    """
    observations = []
    for index in range(6):
        observations.append(_observation("target", channel=index, certificates=[
            _certificate("full-frame-300", 0.6, control=0.02,
                         searched_control_score=0.58,
                         searched_control_epoch_shift_samples=3146.0,
                         rolled_shift_samples=187)]))
    _write(tmp_path, _payload("trap", observations))
    payloads = load_scores(tmp_path)

    trap = rolled_control_trap(payloads)

    assert trap["observations"] == 6
    assert trap["pinned_control"]["p50"] == pytest.approx(0.02)
    assert trap["searched_control"]["p50"] == pytest.approx(0.58)
    assert trap["landed_on_the_shifted_signal"] == 6
    assert "ROLLED-CONTROL TRAP" in format_review(review(tmp_path))


def test_two_probe_lengths_are_never_pooled_into_one_threshold(tmp_path):
    """A null threshold belongs to a probe length: p99 1.310 at 20 ms, 1.137 at 80.

    Nothing else in this codebase tracks that, so pooling would silently produce
    a threshold that is wrong for both populations that made it.
    """
    short = _payload("short", [_observation("target")])
    short["probe_ms"] = 20.0
    long = _payload("long", [_observation("target")])
    long["probe_ms"] = 80.0
    _write(tmp_path, short)
    _write(tmp_path, long)

    report = review(tmp_path)

    assert report["probe_lengths"]["probe_ms"] == [20.0, 80.0]
    assert report["probe_lengths"]["single_length"] is False
    assert "MIXED" in format_review(report)


def test_the_deployed_gate_is_shown_beside_a_calibrated_one(tmp_path):
    """1.33 against a clean 80 ms null is about 0.06%, roughly 17x too strict.

    So the deployed detector has been missing detections rather than
    manufacturing them, and the plan's 8.11%/2.72%/2.11% figures are wrong and
    self-contradictory — contamination inflates a threshold, and an inflated
    threshold fires less. Printing both numbers side by side is what stops the
    wrong one being carried forward again.
    """
    payload = _payload("gate", [
        _observation("target", certificates=[_certificate("coarse-A", 1.4)]),
        _observation("cross-edge-null",
                     certificates=[_certificate("coarse-A", 1.2)])])
    payload["deployed_threshold"] = 1.33
    _write(tmp_path, payload)

    printed = format_review(review(tmp_path))

    assert "deployed gate is 1.330" in printed
    assert "costs detections rather than manufacturing them" in printed


def test_an_unsupported_threshold_is_marked_in_the_table(tmp_path):
    """A number that cannot carry the rate asked of it must not read as one."""
    _write(tmp_path, _payload("thin", [
        _observation("target",
                     certificates=[_certificate("glrt-32", 0.9, control=0.1)]),
        _observation("cross-edge-null",
                     certificates=[_certificate("glrt-32", 0.2, control=0.1)])]))

    printed = format_review(review(tmp_path))

    assert "*" in printed
    assert "not supported by the null population" in printed


def test_a_failed_reproduction_stops_the_reader_before_the_tables(tmp_path):
    """If config A does not reproduce, every row above is about the wrong sky."""
    payload = _payload("mismapped",
                       [_observation("target", delta=0.31,
                                     certificates=[_certificate("coarse-A", 1.4)])])
    _write(tmp_path, payload)

    printed = format_review(review(tmp_path))

    assert "NOT EXACT" in printed


def test_an_empty_corpus_reviews_to_an_empty_report_rather_than_an_error(tmp_path):
    """The stage runs from the first job, long before anything is scored."""
    report = review(tmp_path)

    assert report["entries"] == 0
    assert format_review(report).startswith(PREAMBLE.split("\n")[0])


def test_describe_reports_nothing_rather_than_zero_for_an_empty_population():
    """Zero is a value; absence is not, and the difference reaches the table."""
    assert describe([]) == {"count": 0}
    assert describe([None, None]) == {"count": 0}
    assert describe([1.0, 2.0, 3.0])["p50"] == pytest.approx(2.0)
