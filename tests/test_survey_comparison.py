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
                 geometry=None, delta=0.0, bank="A", excluded=None,
                 samples=None):
    return {"arm": arm, "template_edge": "lower",
            "null_direction": None if arm == "target" else "upper-on-lower",
            "iq_index": 0, "channel": channel, "region": region,
            "edge": region.split("-")[0], "receiver": receiver,
            "receiver_label": "lnb-a", "if_center_hz": 9.6e8,
            "rf_center_hz": 1.07e10, "utc": utc,
            "deployed": {"peak_to_median": 1.2, "anchor_agreement": 0},
            "deployed_reproduction_delta": delta,
            "deployed_reproduction_bank": bank if arm == "target" else None,
            "deployed_reproduction_samples": samples,
            "deployed_reproduction_excluded": excluded,
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
    assert "MIXED LENGTH" in format_review(report)


def test_two_sample_rates_are_never_pooled_either(tmp_path):
    """The same defect on the axis the length check cannot see.

    The survey draws four configurations and two of them are 80 ms probes at
    different rates, so a guard on probe length alone passes them as one
    population. Rate sets the kernel taps — 11 at 2.5 MS/s, 22 at 5 — the epoch
    count the fold maximises over, and how much of the sampled band is noise.
    A threshold belongs to one rate exactly as much as to one length.
    """
    slow = _payload("slow", [_observation("target")])
    slow["probe_ms"], slow["sample_rate_hz"] = 80.0, 2.5e6
    slow["capture_config"] = {"name": "80ms-2.5MSps"}
    fast = _payload("fast", [_observation("target")])
    fast["probe_ms"], fast["sample_rate_hz"] = 80.0, 5.0e6
    fast["capture_config"] = {"name": "80ms-5.0MSps"}
    _write(tmp_path, slow)
    _write(tmp_path, fast)

    report = review(tmp_path)
    rendered = format_review(report)

    # The length guard is satisfied and would have passed this silently.
    assert report["probe_lengths"]["single_length"] is True
    assert report["probe_lengths"]["single_rate"] is False
    assert report["probe_lengths"]["single_configuration"] is False
    assert report["probe_lengths"]["capture_configs"] == ["80ms-2.5MSps",
                                                          "80ms-5.0MSps"]
    assert "MIXED RATE" in rendered and "MIXED LENGTH" not in rendered


def test_one_configuration_is_named_rather_than_merely_not_complained_about(
        tmp_path):
    """A reader must see which arm a clean report came from, not just that it is clean.

    "no warning" and "80 ms at 2.5 MS/s" are different amounts of information,
    and only the second lets a later reader pool this report with another one.
    """
    single = _payload("single", [_observation("target")])
    single["probe_ms"], single["sample_rate_hz"] = 160.0, 5.0e6
    single["capture_config"] = {"name": "160ms-5.0MSps"}
    _write(tmp_path, single)

    rendered = format_review(review(tmp_path))

    assert "160 ms" in rendered and "5 MS/s" in rendered
    assert "160ms-5.0MSps" in rendered
    assert "MIXED" not in rendered


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
    # This host's table for the banks it re-runs, and — separately, because
    # they are separate facts — what the capture itself searched and gated at.
    payload["scorer_coarse_threshold"] = {"A": 1.33, "E": 1.255}
    payload["deployed_shape"] = [13, 8]
    payload["deployed_threshold"] = 1.252
    payload["capture_bank"] = {"shape": [13, 8], "offset_span_hz": 700_000.0,
                               "threshold": 1.252, "known": True}
    _write(tmp_path, payload)

    report = review(tmp_path)
    printed = format_review(report)

    assert report["deployed"]["regimes"] == [
        {"shape": [13, 8], "offset_span_hz": 700_000.0, "threshold": 1.252,
         "entries": 1}]
    # Shape, span and gate travel together, so the line cannot assert a pairing
    # by accident of three sets being sorted independently.
    assert "1 entry: shape [13, 8] over +/-700 kHz gating at 1.252" in printed
    assert "coarse-A: this host would gate at 1.330" in printed
    assert "cross-edge 1% threshold here is 1.200" in printed
    assert "costing detections, not manufacturing them" in printed
    # Real backgrounds, not synthetic: the two agree, and both sit above noise.
    assert "agree to 0.1% in the mean" in printed


def test_the_capture_gate_line_keeps_shape_span_and_gate_paired(tmp_path):
    """Three sorted sets side by side assert a pairing nobody ever ran.

    Ground truth on the share, counted with its date in
    ``survey_scoring.CORPUS_CENSUS``: roughly three fifths of the records are
    (3, 8) over +/-300 kHz gating at 1.33 and the rest are (13, 8) over
    +/-700 kHz gating at 1.252, both in bulk. Collected as three independent
    sets and printed in a row,
    that reads "bank shape [[3, 8], [13, 8]] over +/-300 kHz, +/-700 kHz, gating
    at 1.252/1.330" — which taken positionally says (3, 8) gated at 1.252 and
    (13, 8) at 1.330, the pairing reversed. This is the same flattening of a
    two-regime corpus that the sidecar fix exists to undo, moved up one level:
    the fact is the tuple, and a count per regime is what makes it checkable.
    """
    narrow = _payload("narrow", [
        _observation("target", certificates=[_certificate("coarse-A", 1.4)]),
        _observation("cross-edge-null",
                     certificates=[_certificate("coarse-A", 1.2)])])
    narrow["deployed_shape"] = [3, 8]
    narrow["deployed_threshold"] = 1.33
    narrow["capture_bank"] = {"shape": [3, 8], "offset_span_hz": 300_000.0,
                              "threshold": 1.33, "known": True}
    wide = _payload("wide", [
        _observation("target", bank="E",
                     certificates=[_certificate("coarse-E", 1.4)])])
    wide["deployed_shape"] = [13, 8]
    wide["deployed_threshold"] = 1.252
    wide["capture_bank"] = {"shape": [13, 8], "offset_span_hz": 700_000.0,
                            "threshold": 1.252, "known": True}
    silent = _payload("silent", [_observation("target", bank=None)])
    for payload in (narrow, wide, silent):
        _write(tmp_path, payload)

    report = review(tmp_path)
    printed = format_review(report)

    assert report["deployed"]["regimes"] == [
        {"shape": [3, 8], "offset_span_hz": 300_000.0, "threshold": 1.33,
         "entries": 1},
        {"shape": [13, 8], "offset_span_hz": 700_000.0, "threshold": 1.252,
         "entries": 1}]
    assert report["deployed"]["unknown"] == 1
    assert "1 entry: shape [3, 8] over +/-300 kHz gating at 1.330" in printed
    assert "1 entry: shape [13, 8] over +/-700 kHz gating at 1.252" in printed
    assert "1 entry does not say" in printed


def test_a_capture_lands_in_one_bank_bucket_or_the_other_and_never_both(
        tmp_path):
    """A regime count and an unknown count that overlap add up to more entries
    than the corpus holds.

    ``regimes`` reads the shape from ``deployed_shape`` *or* the capture bank
    beside it, because a sidecar can carry either; ``unknown`` read
    ``deployed_shape`` alone. So a payload recording its bank only under
    ``capture_bank`` — which is exactly what a sidecar written before the
    top-level copy existed looks like — was counted as a regime and as an
    unknown at the same time, and a reader adding the printed lines up got one
    more entry than the review said it read.

    The two are the same predicate now, taken once: the regime it lands in, or
    the count of entries that named no bank at all.
    """
    half = _payload("half-recorded", [_observation("target")])
    # No ``deployed_shape`` key at all, and the bank recorded beside it.
    half["capture_bank"] = {"shape": [3, 8], "offset_span_hz": 300_000.0,
                            "threshold": 1.33, "known": True}
    half["deployed_threshold"] = 1.33
    _write(tmp_path, half)
    _write(tmp_path, _payload("silent", [_observation("target", bank=None)]))

    report = review(tmp_path)
    deployed = report["deployed"]

    assert report["entries"] == 2
    assert deployed["regimes"] == [
        {"shape": [3, 8], "offset_span_hz": 300_000.0, "threshold": 1.33,
         "entries": 1}]
    assert deployed["unknown"] == 1
    assert sum(regime["entries"] for regime in deployed["regimes"]) \
        + deployed["unknown"] == report["entries"]


def test_the_machine_readable_gate_holds_no_unpaired_sets(tmp_path):
    """The printed line was fixed; the JSON a script reads still held the trap.

    Three independently sorted sets — shape ``[[3, 8], [13, 8]]``, span
    ``[300000.0, 700000.0]``, gate ``[1.252, 1.33]`` — read positionally say
    (3, 8) gated at 1.252 over +/-300 kHz. Ground truth is (3, 8) over
    +/-300 kHz at **1.33** and (13, 8) over +/-700 kHz at 1.252, so the
    positional reading has the gates swapped. The report text was rewritten to
    print one line per regime; the machine-readable path kept all three sets,
    and the machine-readable path is the one a later script reads with no human
    in between.

    They are gone rather than paired: ``regimes`` already carries every value
    that was in them, with the pairing that makes each one checkable against
    the manifests, so an unpaired copy beside it can only be read wrongly.
    """
    narrow = _payload("narrow", [_observation("target")])
    narrow["deployed_shape"] = [3, 8]
    narrow["deployed_threshold"] = 1.33
    narrow["capture_bank"] = {"shape": [3, 8], "offset_span_hz": 300_000.0,
                              "threshold": 1.33, "known": True}
    wide = _payload("wide", [_observation("target", bank="E")])
    wide["deployed_shape"] = [13, 8]
    wide["deployed_threshold"] = 1.252
    wide["capture_bank"] = {"shape": [13, 8], "offset_span_hz": 700_000.0,
                            "threshold": 1.252, "known": True}
    for payload in (narrow, wide):
        _write(tmp_path, payload)

    deployed = review(tmp_path)["deployed"]

    assert deployed["regimes"] == [
        {"shape": [3, 8], "offset_span_hz": 300_000.0, "threshold": 1.33,
         "entries": 1},
        {"shape": [13, 8], "offset_span_hz": 700_000.0, "threshold": 1.252,
         "entries": 1}]
    assert set(deployed) == {"regimes", "unknown", "scorer_threshold"}


def test_sidecars_at_another_schema_are_counted_rather_than_vanishing(tmp_path):
    """"Nothing scored yet" is false for a corpus with scored entries on disk.

    A schema bump drops every existing sidecar from the aggregate — correctly,
    because the two versions mean different things by the same key — but
    ``load_scores`` drops them with a bare ``continue`` and no counter, so for
    the whole re-score window the review reports an empty corpus. Every sidecar
    on the share is at the old schema and none at the new one, counted with its
    date in ``survey_scoring.CORPUS_CENSUS``, and re-scoring costs 74.6 s and
    66.2 s per entry under capture load — the better part of a day for a corpus
    this size, and it only gets longer. For that whole day the report would tell
    a reader nothing has been scored while the scoring sits on disk.
    """
    stale = _write(tmp_path, _payload("stale", [_observation("target")]))
    (stale / "scores.json").write_text(json.dumps(
        {"schema": "leo-tracker.survey-detector-comparison/v1",
         "capture": "stale", "observations": []}))

    report = review(tmp_path)
    printed = format_review(report)

    assert report["entries"] == 0
    assert report["other_schema"] == {
        "leo-tracker.survey-detector-comparison/v1": 1}
    assert "nothing scored yet" not in printed
    assert ("1 sidecar at leo-tracker.survey-detector-comparison/v1 waiting to "
            "be re-scored") in printed


def test_the_limit_bounds_what_is_read_and_never_the_census(tmp_path):
    """A count printed as a corpus fact has to be over the corpus.

    ``read_scores`` stopped scanning the moment it had ``limit`` sidecars of the
    current schema, so the census only saw the off-schema files that happened to
    sort before the break. Demonstrated here: three at the current schema and
    five at the old one census as five unlimited and as **nothing** at
    ``limit=2`` — and ``cli.py`` passes ``--limit`` straight through, so the very
    count added to stop "nothing scored yet" being printed over a corpus holding
    a hundred scored entries could itself report an empty backlog while the
    backlog sat on disk.

    The limit is what one analysis job reads. The census is what the corpus
    holds, and it is taken over every entry either way — from the sidecar's
    opening bytes past the limit, so bounding the job still bounds its cost.
    The entry count is bounded too, so the number of current-schema sidecars the
    limit left unread is printed beside it rather than left to be inferred from
    a total that is not there.
    """
    for index in range(3):
        _write(tmp_path, _payload(f"a-current-{index}",
                                  [_observation("target")]))
    for index in range(5):
        stale = _write(tmp_path, _payload(f"z-stale-{index}",
                                          [_observation("target")]))
        (stale / "scores.json").write_text(json.dumps(
            {"schema": "leo-tracker.survey-detector-comparison/v1",
             "capture": f"z-stale-{index}", "observations": []}))

    whole = review(tmp_path)
    bounded = review(tmp_path, limit=2)
    printed = format_review(bounded)

    assert whole["entries"] == 3
    assert whole["other_schema"] == {
        "leo-tracker.survey-detector-comparison/v1": 5}
    # The limit bounds the aggregate and nothing else.
    assert bounded["entries"] == 2
    assert bounded["other_schema"] == whole["other_schema"]
    assert bounded["sidecars"]["scanned"] == 8
    assert bounded["sidecars"]["read"] == 2
    assert bounded["sidecars"]["beyond_limit"] == 1
    assert ("5 at leo-tracker.survey-detector-comparison/v1, another schema "
            "and not counted here") in printed
    # And the reader is told the entry count is a limit rather than a total.
    assert "1 more at this schema the limit did not read" in printed


def test_a_sidecar_that_will_not_parse_is_counted_rather_than_dropped(tmp_path):
    """The bare ``continue`` this function's own docstring rules out.

    ``read_scores`` gained a census because dropping a sidecar of another schema
    without counting it printed "nothing scored yet" over a corpus holding a
    hundred scored entries — and then went on dropping every *unparseable*
    sidecar with a second bare ``continue`` and no counter, in the same loop. A
    half-written file and a file that never existed are different facts: the
    first is damage and the second is simply an entry nobody has scored yet, and
    most of a corpus is the second.
    """
    _write(tmp_path, _payload("good", [_observation("target")]))
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "scores.json").write_text("{ truncated")
    # An entry with no sidecar at all: unscored, which is the normal state of
    # most of the corpus and must not read as damage.
    (tmp_path / "unscored").mkdir()

    report = review(tmp_path)
    printed = format_review(report)

    assert report["entries"] == 1
    assert report["sidecars"]["scanned"] == 2
    assert report["sidecars"]["unreadable"] == 1
    assert "1 that would not parse" in printed


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


def test_a_float_residual_below_tolerance_is_not_a_broken_pipeline(tmp_path):
    """Bitwise zero is not something a float recomputation can be held to.

    ``all(delta == 0.0)`` fired on 448 of 448 observations — every report ever
    printed — because it demanded exact equality from a threaded reduction.
    Measured across the corpus's 800 correctly-banked target observations the
    median delta is 1.10e-08 and the worst 5.165e-08; the cheapest *genuine*
    defect, a reproduction run against a bank the capture never searched, lands
    at 1.896e-04 at its very closest. The two populations are 3,670x apart with
    nothing in between, so 1e-6 sits 19.4x above the worst honest residual and
    190x below the cheapest real one. The residual itself is not noise: the
    fold is bit-reproducible on one host, and 1e-8 is what ``-ffast-math`` and
    ``-march=native`` cost when the sidecar was written by a different build.
    """
    _write(tmp_path, _payload("floaty", [
        _observation("target", delta=1.35e-08, bank="E",
                     certificates=[_certificate("coarse-E", 1.4)])]))

    report = review(tmp_path)
    printed = format_review(report)

    assert "NOT EXACT" not in printed
    assert "reproduced within" in printed
    assert report["deployed_reproduction"]["reproduced"] is True
    assert report["deployed_reproduction"]["above_tolerance"] == 0
    assert report["deployed_reproduction"]["tolerance"] == pytest.approx(1e-6)
    # Not bitwise, and the report says which rather than rounding it away.
    assert report["deployed_reproduction"]["bitwise_exact"] == 0


def test_observations_that_could_not_be_checked_read_as_partial_not_clean(
        tmp_path):
    """Verified, unverifiable and contradicted are three different reports.

    A corpus half of whose observations were never checked is not a clean
    corpus, and it is not a broken one either. Reporting it as clean claims
    coverage the check never had; reporting it as broken buries the rows that
    genuinely did reproduce. The count of exclusions is the only thing that
    tells the reader which of the three they are holding.
    """
    _write(tmp_path, _payload("partial", [
        _observation("target", delta=2.0e-08, bank="E",
                     certificates=[_certificate("coarse-E", 1.4)]),
        _observation("target", receiver=1, delta=None, bank=None,
                     excluded="capture record does not say which bank it "
                              "searched",
                     certificates=[_certificate("coarse-E", 1.3)])]))

    report = review(tmp_path)
    printed = format_review(report)

    assert "PARTIAL" in printed
    assert "NOT EXACT" not in printed
    assert report["deployed_reproduction"]["count"] == 1
    assert report["deployed_reproduction"]["excluded"] == 1
    # The banner says PARTIAL, so the flag beside it must not say verified.
    assert report["deployed_reproduction"]["reproduced"] is False


def test_reproduced_never_claims_coverage_the_check_did_not_have(tmp_path):
    """A flag read by a machine has to mean what the banner beside it means.

    ``bool(checked) and not above`` never consults ``excluded``, so one checked
    observation beside ninety-six unchecked ones sets ``reproduced`` True while
    the printed banner directly above it says PARTIAL. Whichever of the two a
    reader trusts, one of them is lying, and the machine-readable one is the one
    that gets carried into another script without a human in between.
    """
    _write(tmp_path, _payload("thin-coverage", [
        _observation("target", delta=3.0e-08, bank="E",
                     certificates=[_certificate("coarse-E", 1.4)]),
        *[_observation("target", receiver=index % 2, delta=None, bank=None,
                       excluded="capture record does not say which bank it "
                                "searched")
          for index in range(96)]]))

    report = review(tmp_path)
    printed = format_review(report)

    assert report["deployed_reproduction"]["count"] == 1
    assert report["deployed_reproduction"]["excluded"] == 96
    assert report["deployed_reproduction"]["above_tolerance"] == 0
    assert report["deployed_reproduction"]["reproduced"] is False
    assert "PARTIAL" in printed


def test_a_failed_reproduction_stops_the_reader_before_the_tables(tmp_path):
    """If the capture's own bank does not reproduce, every row is wrong sky.

    A tolerance is not a softening: 0.31 is seven orders above the 5.165e-08 a
    matched bank produces, and the wording that stops the reader has to survive
    the tolerance being introduced or the tolerance has swallowed the defect it
    was measured against.
    """
    payload = _payload("mismapped",
                       [_observation("target", delta=0.31,
                                     certificates=[_certificate("coarse-A", 1.4)])])
    _write(tmp_path, payload)

    report = review(tmp_path)
    printed = format_review(report)

    assert "NOT EXACT" in printed
    assert report["deployed_reproduction"]["reproduced"] is False
    assert report["deployed_reproduction"]["above_tolerance"] == 1
    assert report["deployed_reproduction"]["worst_above_tolerance"] == \
        pytest.approx(0.31)


def test_the_cheapest_real_defect_ever_measured_is_still_a_failure(tmp_path):
    """The tolerance has to sit below the defect it was measured against.

    1.896e-04 is the *closest* any reproduction against a bank the capture never
    searched ever came to agreeing, over 800 observations; the honest residuals
    top out at 5.165e-08. Nothing else in this file lives in the 1e-6..1e-3 band,
    so loosening ``REPRODUCTION_TOLERANCE`` to 1e-3 would swallow every real
    defect the corpus has ever produced with only an echo assertion on the
    constant objecting — and an echo assertion is not a check, it is the same
    number written twice.
    """
    _write(tmp_path, _payload("cheapest-defect", [
        _observation("target", delta=1.896e-04, bank="E",
                     certificates=[_certificate("coarse-E", 1.4)])]))

    report = review(tmp_path)
    printed = format_review(report)

    assert "NOT EXACT" in printed
    assert report["deployed_reproduction"]["reproduced"] is False
    assert report["deployed_reproduction"]["above_tolerance"] == 1


def test_the_not_exact_banner_still_says_how_much_was_checked(tmp_path):
    """A verdict on a sixth of a corpus has to name the other five sixths.

    The failure branch runs before the exclusion branch and never mentions
    exclusions, so when both are non-zero — measured live at 160 exclusions
    beside 16 failures — the reader is stopped by a NOT EXACT that says nothing
    about how much of the corpus it looked at. "Sixteen of a thousand
    disagree" and "sixteen of the sixteen we could check disagree, and there
    were a hundred and sixty more we could not" are different findings.
    """
    _write(tmp_path, _payload("both", [
        _observation("target", delta=0.31, bank="E",
                     certificates=[_certificate("coarse-E", 1.4)]),
        _observation("target", receiver=1, delta=None, bank=None,
                     excluded="capture record does not say which bank it "
                              "searched",
                     certificates=[_certificate("coarse-E", 1.3)])]))

    report = review(tmp_path)
    printed = format_review(report)

    assert "NOT EXACT" in printed
    assert report["deployed_reproduction"]["excluded"] == 1
    assert "1 more could not be checked at all" in printed
    assert "capture record does not say which bank it searched" in printed


def test_the_review_schema_names_the_version_whose_keys_these_are(tmp_path):
    """Keys that changed meaning under one version name is what a version is for.

    ``report["deployed"]["threshold"]`` was a dict of this host's per-config
    gates, became a list of the gates the captures applied, and is gone;
    ``deployed_reproduction["exact"]`` became ``reproduced``; ``sidecars`` and
    ``deployed_reproduction["scored_samples"]`` are new. A consumer holding a v1
    document and a consumer holding this one disagree about what four keys mean
    while both read "v1", which is the exact argument that earned
    ``SCORES_SCHEMA`` its bump one layer down.

    The version is asserted as a literal rather than against the constant. An
    assertion that reads ``report["schema"] == REVIEW_SCHEMA`` passes whatever
    the constant says, so it pins the plumbing and not the version.
    """
    _write(tmp_path, _payload("versioned", [_observation("target")]))

    report = review(tmp_path)

    assert report["schema"] == "leo-tracker.survey-detector-review/v2"
    assert report["schema"] == REVIEW_SCHEMA
    # The keys the bump is about, so a later reader can see what moved.
    assert "threshold" not in report["deployed"]
    assert "reproduced" in report["deployed_reproduction"]
    assert "exact" not in report["deployed_reproduction"]
    assert "scored_samples" in report["deployed_reproduction"]
    assert "sidecars" in report


def test_the_window_named_in_the_banner_is_one_something_was_checked_over(
        tmp_path):
    """A banner cannot name a window nothing in it was measured over.

    The window set was collected from every target observation, excluded ones
    included — and an excluded observation is one that produced no delta at all,
    so its window contributed nothing to the statistic the banner is about. On
    the live corpus that is not a corner case: whole entries are excluded for
    naming a bank this comparison does not re-run, and they sit on the longest
    arms, so the banner would read "over the capture's own 200,000/800,000-sample
    window" when every delta behind it came from 200,000.
    """
    _write(tmp_path, _payload("windows", [
        _observation("target", delta=2.0e-08, bank="E", samples=200_000,
                     certificates=[_certificate("coarse-E", 1.4)]),
        _observation("target", receiver=1, delta=None, bank=None,
                     samples=800_000,
                     excluded="capture record does not say which bank it "
                              "searched",
                     certificates=[_certificate("coarse-E", 1.3)])]))

    report = review(tmp_path)
    printed = format_review(report)

    assert report["deployed_reproduction"]["count"] == 1
    assert report["deployed_reproduction"]["excluded"] == 1
    assert report["deployed_reproduction"]["scored_samples"] == [200_000]
    assert "200,000-sample window" in printed
    assert "800,000" not in printed


def test_two_pooled_windows_are_named_as_two(tmp_path):
    """A singular noun over two windows is the pooling this file exists to stop.

    The corpus holds several capture arms and the reproduction check runs over
    whichever prefix each one scored, so a review across arms pools windows —
    which is precisely what ``_reproduction``'s own docstring says two entries in
    that list means. Printed under "the capture's own 200,000/400,000-sample
    window" it reads as one window written oddly, and the reader has to notice a
    slash to see that two populations were averaged.
    """
    _write(tmp_path, _payload("pooled", [
        _observation("target", delta=1.0e-08, bank="E", samples=200_000,
                     certificates=[_certificate("coarse-E", 1.4)]),
        _observation("target", receiver=1, delta=2.0e-08, bank="E",
                     samples=400_000,
                     certificates=[_certificate("coarse-E", 1.3)])]))

    report = review(tmp_path)
    printed = format_review(report)

    assert report["deployed_reproduction"]["scored_samples"] == [200_000,
                                                                 400_000]
    assert ("over the 2 different windows the captures scored "
            "(200,000/400,000 samples, pooled)") in printed


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
