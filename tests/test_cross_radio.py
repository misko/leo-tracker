"""Two radios, one sky: the checks that keep the construction from lying.

This site cannot inject a signal, so every earlier comparison could only say
"method X fires more often" — which is equally the shape of a worse false-alarm
rate.  Two radios scanning the same tuning at the same instant give the missing
axis, and the axis is only real if four things hold, so all four are pinned
here rather than trusted:

* the join is by **instant**, not by tuning.  On an opposite-order sweep the two
  radios sit on different edges at the same index and on the same edge at
  different indexes, so a join that reaches for the matching ``(channel, edge)``
  silently compares two dwells apart and calls it simultaneous.  The
  transposition inverts the whole experiment without changing a single count,
  which is why it gets its own test in both directions.
* ``lnb-a`` is dead — flat ~1.19 at every tuning since 04:44 UTC — and a dead
  receiver in the population is a receiver that never coincides, which drags
  every occupancy estimate down while looking like quiet sky.
* the estimator uses the **calibrated** per-cell false-alarm rate.  With ``p``
  forced to zero the same three counts give a different occupancy and a
  different detection probability, and nothing in the output would say so.
* confirmation leaves the scored method out.  A cell only method M fires on is
  M's claim; counting it as evidence for M is the circularity the whole
  cross-radio construction exists to escape.

Synthetic sidecars throughout, with the arithmetic small enough to check by
hand: a real probe cannot show whether the join transposed.
"""
import json
import math

import pytest

from leo_tracker.radio.beacon.cross_radio import (
    CROSS_RADIO_SCHEMA, DEAD_RECEIVERS, OFFSET_BINS_HZ, cell_false_alarm,
    channel_instant_verdicts, format_review, guard_band_curve, join_cells,
    join_null_cells, load_pairs, method_roc, null_thresholds, review,
    solve_coincidence)
from leo_tracker.radio.beacon.survey_scoring import SCORES_SCHEMA

#: A point score that clears any threshold these fixtures calibrate, and one
#: that clears none.  Two values keep the fixtures readable: what is being
#: tested is the bookkeeping around the decision, never the decision itself.
HIGH, LOW = 0.90, 0.10

#: The rate these fixtures calibrate their thresholds at, and it is not the 1%
#: the corpus runs at.  A 1% quantile of a null population this small **is its
#: own maximum** — 24 draws put the order statistic on the largest one — so at
#: 1% nothing exceeds the threshold and every synthetic p is zero.  That is a
#: property of a fixture with two dozen null draws, not of the corpus, which
#: carries 300-plus null points per (method, rate, length) and realises a
#: per-cell p of 6 to 7 percent.  Calibrating the fixtures at 20% puts the
#: threshold between the two score levels so the bookkeeping above it can be
#: checked at all.
FIXTURE_FALSE_ALARM_RATE = 0.2

METHODS = ("anchor-8", "differential-16", "differential-32", "glrt-32",
           "glrt-64", "full-frame-full", "full-frame-acquire",
           "full-frame-verify")


def _point(fired_by, *, cfo_hz=0.0, point_id=0, only=None):
    """One candidate (epoch, CFO) with a score for every method.

    ``fired_by`` is the set of methods scoring HIGH here; ``only`` restricts the
    methods present at all, which is how a method with no score is told apart
    from a method that scored low.
    """
    methods = {}
    for method in (only or METHODS):
        methods[method] = {"score": HIGH if method in fired_by else LOW,
                           "control_score": LOW, "margin": 0.0,
                           "cross_edge_score": LOW, "residual_cfo_hz": 0.0,
                           "frame_max": HIGH, "control_epoch": "pinned",
                           "elapsed_ms": 0.5}
    return {"point_id": point_id, "epoch_sample": 1000 + point_id,
            "cfo_hz": cfo_hz, "claimed_by": sorted(fired_by),
            "methods": methods}


def _observation(*, instant, channel, region, receiver, label, points,
                 arm="target"):
    edge = region.split("-")[0]
    return {"arm": arm, "template_edge": edge,
            "null_direction": None if arm == "target" else f"x-on-{edge}",
            "iq_index": instant, "channel": channel, "region": region,
            "edge": edge, "receiver": receiver, "receiver_label": label,
            "if_center_hz": None, "rf_center_hz": None, "utc": None,
            "deployed": None, "coarse": {}, "certificates": [],
            "points": list(points), "geometry": None}


def _order(edge_order, channels):
    """What one radio scanned, in the order it scanned it."""
    first = "upper-edge" if edge_order == "U" else "lower-edge"
    second = "lower-edge" if edge_order == "U" else "upper-edge"
    return [[channel, region] for channel in channels
            for region in (first, second)]


def _guard(rate):
    return rate / 2.0 - 1_875_000.0 / 2.0


def _write(root, *, sweep, radio, labels, edge_order, channels, rate, probe_ms,
           target, nulls=None, matched_arm=True, peer="peer", centres=None,
           skew=None):
    """One radio of one synchronised sweep, manifest and scores together."""
    order = _order(edge_order, channels)
    capture = f"sync-{sweep}-{radio}"
    entry = root / capture
    entry.mkdir(parents=True, exist_ok=True)
    arm = {"name": f"{probe_ms:g}ms-{rate / 1e6:.2f}MSps",
           "probe_s": probe_ms / 1e3, "sample_rate_hz": rate,
           "pilot_band_fits": _guard(rate) >= 0}
    manifest = {
        "schema": "leo-tracker.capture-manifest/v1", "state": "complete",
        "sample_rate_hz": rate,
        "identity": {"radio_id": radio, "receiver_labels": list(labels)},
        "metadata": {"pre_dwell_survey": {
            "schema": "leo-tracker.pre-dwell-survey/v1", "state": "complete",
            "sample_rate_hz": rate, "sample_order": order,
            "profile": {"probe_s": probe_ms / 1e3},
            "synchronised_scan": {
                "paired_sweep": sweep, "peer_radio": peer,
                "edge_order": edge_order, "arm": arm,
                "matched_arm": matched_arm,
                "pilot_band_fits": arm["pilot_band_fits"],
                "skew_ms": {"per_tuning": list(skew or [0.0155] * len(order)),
                            "median": 0.0155, "max": max(skew or [0.054])}}}}}
    observations = []
    for instant, (channel, region) in enumerate(order):
        for receiver, label in enumerate(labels):
            points = target(instant, label)
            if points is not None:
                observations.append(_observation(
                    instant=instant, channel=channel, region=region,
                    receiver=receiver, label=label, points=points))
        for receiver, label in enumerate(labels):
            points = nulls(instant, label) if nulls else None
            if points is not None:
                observations.append(_observation(
                    instant=instant, channel=channel, region=region,
                    receiver=receiver, label=label, points=points,
                    arm="cross-edge-null"))
    (entry / "manifest.json").write_text(json.dumps(manifest))
    (entry / "scores.json").write_text(json.dumps({
        "schema": SCORES_SCHEMA, "capture": capture, "radio_id": radio,
        "sample_rate_hz": rate, "probe_ms": probe_ms,
        "samples_per_tuning": int(rate * probe_ms / 1e3),
        "pilot_guard_hz": _guard(rate),
        "receiver_centers_hz": [(centres or {}).get(label, 0.0)
                                for label in labels],
        "observations": observations, "cross_receiver": [], "cross_edge": [],
        "elapsed_s": 1.0}))
    return entry


def _quiet(*_args):
    return [_point(())]


def _loud(*_args):
    return [_point(METHODS)]


def _sweep(root, sweep, *, order_a="U", order_b="U", channels=(1, 2, 3, 4),
           rate=10e6, probe_ms=160.0, target_a=_quiet, target_b=_quiet,
           nulls_a=None, nulls_b=None, matched_arm=True, centres=None,
           skew=None):
    _write(root, sweep=sweep, radio="pluto-aa", labels=("lnb-c", "lnb-d"),
           edge_order=order_a, channels=channels, rate=rate, probe_ms=probe_ms,
           target=target_a, nulls=nulls_a, matched_arm=matched_arm,
           peer="pluto-bb", centres=centres, skew=skew)
    _write(root, sweep=sweep, radio="pluto-bb", labels=("lnb-a", "lnb-b"),
           edge_order=order_b, channels=channels, rate=rate, probe_ms=probe_ms,
           target=target_b, nulls=nulls_b, matched_arm=matched_arm,
           peer="pluto-aa", centres=centres, skew=skew)


# The planted sky the estimator has to walk back, laid out as a table rather
# than a formula so the three rates below can be counted off it by eye.  Over
# the sixteen cells one four-channel sweep makes — eight instants, two live
# receivers on A against the one live receiver on B — this gives
# P(A) = 6/16, P(B) = 8/16, P(AB) = 4/16, and the null arm fires on 3 of its 24
# live cells for p = 0.125.
_SKY_A = {"lnb-c": (0, 1, 4), "lnb-d": (0, 2, 5)}
_SKY_B = {"lnb-b": (0, 1, 2, 3)}


def _planted_a(instant, label):
    return [_point(METHODS if instant in _SKY_A.get(label, ()) else ())]


def _planted_b(instant, label):
    return [_point(METHODS if instant in _SKY_B.get(label, ()) else ())]


def _planted_null(instant, _label):
    return [_point(METHODS if instant == 0 else ())]


# --------------------------------------------------------------------------
# the join: instant, not tuning
# --------------------------------------------------------------------------

def test_an_opposite_order_sweep_pairs_both_edges_of_one_channel_at_one_instant(tmp_path):
    """The geometry the whole opposite-order arm exists to produce.

    Radio A draws U and steps upper, lower, upper...; radio B draws L and steps
    lower, upper, lower....  At index 0 they are on the *same channel* and
    *opposite edges*, simultaneously — which is what makes this arm able to say
    whether the old phi 0.73 edge correlation was the 515 ms of scan gap or the
    sky.  A join that matched ``(channel, edge)`` instead would return A's index
    0 against B's index 1: the same edge, one dwell apart, and every conclusion
    reversed while the counts stayed plausible.
    """
    _sweep(tmp_path, "20260814T000001Z", order_a="U", order_b="L")

    pairs, _census = load_pairs(tmp_path)
    cells = join_cells(pairs[0])

    assert pairs[0]["geometry"] == "opposite-edge"
    assert cells, "an opposite-order sweep must still produce joined cells"
    for cell in cells:
        assert cell["a"]["instant"] == cell["b"]["instant"]
        assert cell["a"]["channel"] == cell["b"]["channel"]
        assert cell["a"]["edge"] != cell["b"]["edge"]
        assert cell["geometry"] == "opposite-edge"
    # Index 0 is A-upper against B-lower, which is exactly the transposition a
    # tuning-keyed join gets wrong.
    first = [cell for cell in cells if cell["a"]["instant"] == 0]
    assert {cell["a"]["edge"] for cell in first} == {"upper"}
    assert {cell["b"]["edge"] for cell in first} == {"lower"}


def test_a_same_order_sweep_pairs_one_tuning_across_independent_hardware(tmp_path):
    """The other half of the transposition, which a one-sided test misses.

    Same-order is the replication arm: identical (channel, edge), one instant,
    separate LNBs and separate Plutos.  Reading it as opposite-order would claim
    a cross-edge result from data that never observed two edges at once.
    """
    _sweep(tmp_path, "20260814T000002Z", order_a="L", order_b="L")

    pairs, _census = load_pairs(tmp_path)
    cells = join_cells(pairs[0])

    assert pairs[0]["geometry"] == "same-edge"
    assert cells
    for cell in cells:
        assert cell["a"]["instant"] == cell["b"]["instant"]
        assert cell["a"]["channel"] == cell["b"]["channel"]
        assert cell["a"]["edge"] == cell["b"]["edge"]
        assert cell["geometry"] == "same-edge"


def test_a_sweep_only_one_radio_scored_is_not_joined_against_itself(tmp_path):
    """Half a pair is not a pair, and the corpus is always mid-import."""
    _write(tmp_path, sweep="20260814T000003Z", radio="pluto-aa",
           labels=("lnb-c", "lnb-d"), edge_order="U", channels=(1,), rate=10e6,
           probe_ms=160.0, target=_quiet)

    pairs, census = load_pairs(tmp_path)

    assert pairs == []
    assert census["unpaired_sweeps"] == 1


# --------------------------------------------------------------------------
# the dead receiver
# --------------------------------------------------------------------------

def test_the_dead_receiver_is_excluded_from_target_and_from_null(tmp_path):
    """lnb-a has been flat ~1.19 at every tuning since 04:44 UTC.

    A receiver with no signal path never coincides with anything, so leaving it
    in the population lowers every occupancy estimate and every detection
    probability while looking exactly like quiet sky.  It has to be gone from
    the null too: a dead port's null is not a null, it is silence, and a
    false-alarm rate measured on it is optimistic in the same direction.
    """
    _sweep(tmp_path, "20260814T000004Z", order_a="U", order_b="U",
           channels=(1,), target_a=_loud, target_b=_loud,
           nulls_a=_loud, nulls_b=_loud)

    pairs, _census = load_pairs(tmp_path)
    cells = join_cells(pairs[0])
    nulls = join_null_cells(pairs[0])

    assert "lnb-a" in DEAD_RECEIVERS
    labels = {cell["a"]["receiver_label"] for cell in cells}
    labels |= {cell["b"]["receiver_label"] for cell in cells}
    assert labels == {"lnb-c", "lnb-d", "lnb-b"}
    null_labels = {cell["a"]["receiver_label"] for cell in nulls}
    null_labels |= {cell["b"]["receiver_label"] for cell in nulls}
    assert "lnb-a" not in null_labels
    # Two live receivers on A against the one live receiver on B, at each of the
    # two instants a single channel has.
    assert len(cells) == 4
    assert pairs[0]["excluded_receivers"] == {"lnb-a": 4}


# --------------------------------------------------------------------------
# the estimator
# --------------------------------------------------------------------------

def test_the_estimator_recovers_a_planted_occupancy_and_both_detection_rates():
    """Three measurable rates and a calibrated p give f, dA and dB.

    Planted: f = 0.4 of cells hold sky, chain A sees it 0.8 of the time, chain B
    0.6, and either chain fires on empty sky 0.05 of the time.  Then
    P(A) = 0.4(0.8) + 0.6(0.05) = 0.35, P(B) = 0.27 and
    P(AB) = 0.4(0.8)(0.6) + 0.6(0.05)^2 = 0.1935 — and the solver has to walk
    that back.  dA and dB are detection probabilities: the quantity an injected
    signal would have measured, and the reason this arm was built.
    """
    solved = solve_coincidence(0.35, 0.27, 0.1935, 0.05)

    assert solved["solvable"] is True
    assert solved["f"] == pytest.approx(0.4)
    assert solved["d_a"] == pytest.approx(0.8)
    assert solved["d_b"] == pytest.approx(0.6)
    # The excess over p^2 is what a reader can check without the model: chance
    # coincidence of two independent false alarms is 0.0025.
    assert solved["excess_over_p_squared"] == pytest.approx(0.1935 - 0.0025)


def test_the_two_chains_agreement_is_reported_as_phi_beside_the_model(tmp_path):
    """The 515 ms question is answerable from these counts, so it is answered.

    The two edges of a channel used to be scanned two dwells apart and their
    detection correlation capped at phi 0.73 while cross-method agreement on
    identical samples reached 0.97.  Opposite-edge cells here are simultaneous,
    so the same coefficient computed on them says whether the gap was the scan
    latency or the sky.  phi sits beside ``covariance`` rather than inside the
    model fit because it is a property of the three counts and stays meaningful
    when the model does not fit.
    """
    solved = solve_coincidence(0.35, 0.27, 0.1935, 0.05)
    unsolvable = solve_coincidence(0.20, 0.20, 0.01, 0.05)

    assert solved["phi"] == pytest.approx(
        0.099 / math.sqrt(0.35 * 0.65 * 0.27 * 0.73))
    assert unsolvable["phi"] == pytest.approx(
        -0.03 / math.sqrt(0.20 * 0.80 * 0.20 * 0.80))

    _sweep(tmp_path, "20260814T000019Z", order_a="U", order_b="L",
           target_a=_planted_a, target_b=_planted_b, nulls_a=_planted_null,
           nulls_b=_planted_null)
    text = format_review(review(tmp_path,
                                false_alarm_rate=FIXTURE_FALSE_ALARM_RATE))

    assert "phi" in text and "0.73" in text
    # The old 0.73 was measured between two edges INSIDE one radio, which shares
    # a Pluto and an ADC clock and so inflates agreement. Comparing a
    # cross-radio phi straight to it would be the same category error this
    # module exists to avoid, so the like-for-like comparison — same-edge
    # against opposite-edge, both across the same hardware boundary — is the one
    # the text has to point at.
    assert "not directly comparable" in text


def test_an_anticoincident_pair_is_reported_unsolvable_rather_than_clamped():
    """Fewer coincidences than independence predicts is not f = 0.

    Both chains fire on a fifth of cells and agree on a fiftieth — less often
    than chance — so no occupancy in (0, 1] explains the three counts.  The
    honest report is that the model does not fit; clamping to a boundary would
    print 0.0 or 1.0 as though it had been measured.
    """
    solved = solve_coincidence(0.20, 0.20, 0.01, 0.05)

    assert solved["solvable"] is False
    assert solved["f"] is None
    assert solved["d_a"] is None and solved["d_b"] is None
    assert "coincidence" in solved["reason"]
    # The counts that produced the refusal survive it, so the reader can see how
    # far outside the model they landed.
    assert solved["covariance"] == pytest.approx(0.01 - 0.04)


def test_the_estimator_uses_the_calibrated_false_alarm_rate_not_zero(tmp_path):
    """p = 0 is a different experiment, and it reads as the same one.

    With the empty-sky rate forced to zero every firing becomes evidence of sky,
    so f, dA and dB all move — and no column in the report would show it.  The
    planted table gives P(A) = 0.375, P(B) = 0.5, P(AB) = 0.25 against a
    measured p = 0.125, which the model resolves to f = 0.6.  Solve the same
    three counts with p = 0 and f comes out 0.75, a quarter high, with dA and dB
    dragged after it.
    """
    _sweep(tmp_path, "20260814T000005Z", target_a=_planted_a,
           target_b=_planted_b, nulls_a=_planted_null, nulls_b=_planted_null)

    report = review(tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    rates = report["false_alarm"]["per_cell"]
    pooled = report["occupancy"]["methods"]["anchor-8"]["pooled"]

    assert rates["anchor-8"]["rate"] == pytest.approx(0.125)
    assert pooled["p"] == pytest.approx(0.125)
    assert (pooled["p_a"], pooled["p_b"], pooled["p_ab"]) == (
        pytest.approx(0.375), pytest.approx(0.5), pytest.approx(0.25))
    assert pooled["f"] == pytest.approx(0.6)
    zeroed = solve_coincidence(pooled["p_a"], pooled["p_b"], pooled["p_ab"], 0.0)
    assert zeroed["f"] == pytest.approx(0.75)
    assert pooled["f"] != pytest.approx(zeroed["f"]), (
        "solving with p fixed at 0 must not reproduce the calibrated estimate")


def test_the_false_alarm_rate_is_per_cell_and_says_how_many_cells(tmp_path):
    """A per-point rate and a per-cell rate differ by the points in a cell.

    The coincidence model is over cells — one chain, one tuning, one instant,
    maximising over its own candidates — so p has to be the rate that decision
    realises on null cells, not the per-point quantile that set the threshold.
    """
    _sweep(tmp_path, "20260814T000007Z", nulls_a=_planted_null,
           nulls_b=_planted_null)

    pairs, _census = load_pairs(tmp_path)
    entries = [entry for pair in pairs for entry in pair["radios"]]
    thresholds = null_thresholds(entries,
                                 false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    measured = cell_false_alarm(entries, thresholds)

    # Eight instants; the null arm fires at instant 0 only.  lnb-a is out, so the
    # live null cells are lnb-c and lnb-d on A and lnb-b on B: 3 per instant.
    assert measured["anchor-8"]["cells"] == 24
    assert measured["anchor-8"]["count"] == 3
    assert measured["anchor-8"]["rate"] == pytest.approx(0.125)


# --------------------------------------------------------------------------
# no circularity
# --------------------------------------------------------------------------

def test_confirmation_leaves_the_scored_method_out(tmp_path):
    """A cell only M fires on is M's claim, not evidence for M.

    The peer radio's verdict is built from the other seven methods, so a method
    that fires alone everywhere gets no recall from its own enthusiasm.  With
    itself left in, the same corpus reports perfect recall for a method nothing
    else agrees with — the exact result the cross-radio construction was built
    to make impossible.
    """
    def only_anchor(_instant, _label):
        return [_point(("anchor-8",))]

    _sweep(tmp_path, "20260814T000008Z", target_a=only_anchor,
           target_b=only_anchor, nulls_a=_quiet, nulls_b=_quiet)

    pairs, _census = load_pairs(tmp_path)
    entries = [entry for pair in pairs for entry in pair["radios"]]
    thresholds = null_thresholds(entries,
                                 false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    cells = [cell for pair in pairs for cell in join_cells(pair)]
    roc = method_roc(cells, thresholds)

    assert roc["anchor-8"]["positives"] == 0, (
        "no other method fired, so the leave-one-out proxy has no positives")
    assert roc["anchor-8"]["recall"] is None
    assert roc["anchor-8"]["discipline"] == "cross-hardware + leave-one-out"
    # It still fires on every proxy-negative cell, which is the honest reading of
    # a method agreeing with nobody.
    assert roc["anchor-8"]["false_alarm"] == pytest.approx(1.0)


def test_recall_is_scored_across_the_hardware_boundary(tmp_path):
    """M decides on radio A and is graded on what radio B saw independently.

    Grading M against its own radio's other receivers would share a Pluto, an
    ADC clock and a USB bus — which is how the two-receiver model produced an f
    of 0.226 to 0.422 for one sky.
    """
    _sweep(tmp_path, "20260814T000009Z", target_a=_loud, target_b=_loud,
           nulls_a=_quiet, nulls_b=_quiet)

    pairs, _census = load_pairs(tmp_path)
    entries = [entry for pair in pairs for entry in pair["radios"]]
    cells = [cell for pair in pairs for cell in join_cells(pair)]
    roc = method_roc(cells, null_thresholds(
        entries, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE))

    assert roc["anchor-8"]["radio_pairs"] == ["pluto-aa|pluto-bb"]
    assert roc["anchor-8"]["recall"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# the guard band
# --------------------------------------------------------------------------

def test_the_guard_band_curve_splits_by_sample_rate_not_by_sample_count(tmp_path):
    """Rate sets the guard; sample count does not, and the two alias.

    80 ms at 2.5 MS/s and 160 ms at 1.25 MS/s are both 200,000 samples and their
    guards are +312.5 kHz and -312.5 kHz — one fits the 1.875 MHz pilot band with
    room for Doppler, the other never fits it at all.  Binned by sample count
    they merge into one curve and the collapse this analysis exists to find
    averages away.
    """
    def offsets(_instant, _label):
        return [_point(METHODS, cfo_hz=50e3, point_id=0),
                _point((), cfo_hz=400e3, point_id=1)]

    _sweep(tmp_path, "20260814T000010Z", rate=2.5e6, probe_ms=80.0,
           target_a=offsets, target_b=offsets, nulls_a=_quiet, nulls_b=_quiet)
    _sweep(tmp_path, "20260814T000011Z", rate=1.25e6, probe_ms=160.0,
           target_a=offsets, target_b=offsets, nulls_a=_quiet, nulls_b=_quiet)

    pairs, _census = load_pairs(tmp_path)
    entries = [entry for pair in pairs for entry in pair["radios"]]
    curve = guard_band_curve(entries, null_thresholds(
        entries, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE))

    assert sorted(curve["by_rate"]) == [1.25e6, 2.5e6], (
        "two rates sharing a sample count are two curves, not one")
    assert curve["by_rate"][2.5e6]["guard_hz"] == pytest.approx(312_500.0)
    assert curve["by_rate"][1.25e6]["guard_hz"] == pytest.approx(-312_500.0)
    assert curve["by_rate"][1.25e6]["pilot_band_fits"] is False
    assert curve["by_rate"][2.5e6]["samples_per_tuning"] == [200_000]
    assert curve["by_rate"][1.25e6]["samples_per_tuning"] == [200_000]
    for rate in (1.25e6, 2.5e6):
        # Each rate carries only its own points: three live receivers, eight
        # instants, one point per offset bin.
        inside = curve["by_rate"][rate]["bins"][0]
        assert inside["n"] == 24, "sample-count binning would double this"
        assert inside["fired"]["anchor-8"] == 24
    beyond = [item for item in curve["by_rate"][2.5e6]["bins"]
              if item["low"] >= 312_500.0]
    assert beyond and all(item["beyond_guard"] for item in beyond)
    assert OFFSET_BINS_HZ[0] == 0.0


def test_the_guard_band_curve_names_which_receiver_the_beyond_guard_points_are(tmp_path):
    """The offset axis is confounded with the port, and the corpus proves it.

    At 2.5 MS/s the corpus puts 73 of its 88 beyond-guard candidates on lnb-c —
    the one receiver sitting 604.2 kHz off its nominal centre — because an LNB
    bias physically moves the signal inside the ADC band.  So "detection beyond
    the guard" and "detection on lnb-c" are very nearly the same population, and
    a curve that does not say which receiver its far bins came from cannot tell
    a guard effect from a port effect.  The split is reported so the reader can
    see the confound rather than be protected from it.
    """
    def offsets(_instant, label):
        return [_point(METHODS, cfo_hz=400e3 if label == "lnb-c" else 50e3)]

    _sweep(tmp_path, "20260814T000015Z", rate=2.5e6, probe_ms=80.0,
           target_a=offsets, target_b=offsets, nulls_a=_quiet, nulls_b=_quiet,
           centres={"lnb-c": 604_159.8})

    pairs, _census = load_pairs(tmp_path)
    entries = [entry for pair in pairs for entry in pair["radios"]]
    curve = guard_band_curve(entries, null_thresholds(
        entries, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE))
    rate = curve["by_rate"][2.5e6]

    assert rate["by_receiver"]["lnb-c"]["centre_hz"] == pytest.approx(604_159.8)
    assert rate["by_receiver"]["lnb-d"]["centre_hz"] == pytest.approx(0.0)
    # Every beyond-guard candidate here belongs to the biased port, which is the
    # shape the corpus has and the reason the far bins cannot be read as a
    # property of the sample rate.
    assert rate["beyond_guard_receivers"] == {"lnb-c": 8}
    assert rate["by_receiver"]["lnb-c"]["beyond"]["n"] == 8
    assert rate["by_receiver"]["lnb-d"]["beyond"]["n"] == 0
    assert rate["by_receiver"]["lnb-d"]["inside"]["n"] == 8
    # The bias-corrected offset is the sky-Doppler reading and it is a different
    # number: 400 kHz in the band is 204 kHz of Doppler on a port biased 604.
    assert rate["by_receiver"]["lnb-c"]["beyond"]["median_corrected_hz"] == \
        pytest.approx(204_159.8)
    assert "lnb-c" in format_review(review(
        tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE))


def test_a_sweep_whose_measured_skew_blows_the_design_bound_is_flagged(tmp_path):
    """"Same instant" is a claim about the hardware, and it is measured.

    The synchronised capture path was specified at a median skew of 0.0155 ms
    and a maximum of 0.054 ms.  The corpus contains sweeps far outside that, and
    a simultaneity claim that quietly averages them is the kind of thing that
    later turns out to have carried the whole result.  The number is printed
    with the design bound beside it rather than being summarised into a median.
    """
    # 8.3409 and 0.06 are over the 0.054 ms bound; 0.02 straddles it from below,
    # so a comparison written the wrong way round counts three instead of two.
    _sweep(tmp_path, "20260814T000016Z", target_a=_loud, target_b=_loud,
           nulls_a=_quiet, nulls_b=_quiet,
           skew=[0.0155, 0.0155, 8.3409, 0.0155, 0.0155, 0.06, 0.02, 0.02])

    report = review(tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    text = format_review(report)

    assert report["pairs"]["skew_ms"]["max"] == pytest.approx(8.3409)
    assert report["pairs"]["skew_beyond_design"]["tunings"] == 2
    assert report["pairs"]["skew_beyond_design"]["of"] == 8
    assert report["pairs"]["skew_beyond_design"]["design_max_ms"] == 0.054
    assert "8.3409" in text
    assert "0.054" in text


# --------------------------------------------------------------------------
# the channel-instant verdict
# --------------------------------------------------------------------------

def test_the_channel_instant_verdict_takes_its_false_alarm_from_the_null(tmp_path):
    """Two independent radios, both edges of one channel, one instant.

    The conjunction's false-alarm rate is measured on null cells joined the same
    way, because the product of two per-cell rates assumes an independence the
    two radios have not been shown to have.
    """
    _sweep(tmp_path, "20260814T000012Z", order_a="U", order_b="L",
           target_a=_loud, target_b=_loud, nulls_a=_planted_null,
           nulls_b=_planted_null)

    pairs, _census = load_pairs(tmp_path)
    entries = [entry for pair in pairs for entry in pair["radios"]]
    verdicts = channel_instant_verdicts(pairs, null_thresholds(
        entries, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE))

    assert len(verdicts["verdicts"]) == 8, "four channels, two instants each"
    one = verdicts["verdicts"][0]
    assert one["geometry"] == "opposite-edge"
    assert one["channel"] == 1 and one["confirmed"] is True
    assert sorted(one["edges"]) == ["lower", "upper"]
    assert one["radios"] == ["pluto-aa", "pluto-bb"]
    # Both null arms fire at instant 0 and nowhere else, so the conjunction
    # false-alarms on one of the eight instants rather than on p^2 of them.
    assert verdicts["false_alarm"]["cells"] == 8
    assert verdicts["false_alarm"]["count"] == 1
    assert verdicts["false_alarm"]["rate"] == pytest.approx(0.125)
    assert verdicts["false_alarm"]["basis"] == "measured on cross-edge null cells"


# --------------------------------------------------------------------------
# the review
# --------------------------------------------------------------------------

def test_the_review_leads_with_the_spread_of_f_across_the_algorithms(tmp_path):
    """f is a property of the sky, so eight algorithms must return one number.

    Inside a single radio they returned 0.226 to 0.422 for one sky, and that
    disagreement is the evidence the two receivers were not independent.  The
    same spread across radios is the internal check on this whole construction,
    so it is printed whether it passes or fails, with N beside it.
    """
    _sweep(tmp_path, "20260814T000013Z", target_a=_planted_a,
           target_b=_planted_b, nulls_a=_planted_null, nulls_b=_planted_null)
    _sweep(tmp_path, "20260814T000014Z", order_a="U", order_b="L",
           target_a=_planted_a, target_b=_planted_b, nulls_a=_planted_null,
           nulls_b=_planted_null)

    report = review(tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    text = format_review(report)

    assert report["schema"] == CROSS_RADIO_SCHEMA
    spread = report["occupancy"]["f_spread"]
    assert spread["methods"] == 8
    # Every method fires on the same cells here, so the eight agree exactly.
    # The corpus is what decides whether they do; the check is that the number
    # is computed and printed either way.
    assert spread["spread"] == pytest.approx(0.0)
    assert "OCCUPANCY f" in text
    assert "lnb-a" in text and "excluded" in text
    # The pooled f mixes the two geometries, and they do not mean quite the same
    # thing: on a same-edge cell both chains observe one tuning, while on an
    # opposite-edge cell "coincidence" means both edges of the channel were live
    # at once. Pooling is defensible and it is not silent.
    assert "pools both geometries" in text
    # N travels with every population: the corpus is small and growing, and a
    # rate without its denominator cannot be read.
    assert f"cells {report['cells']['joined']}" in text
    assert str(report["pairs"]["joined"]) in text
    assert "no injection" in text.lower()


def test_the_whole_report_survives_strict_json(tmp_path):
    """``--json`` exists to be consumed by something else, so it has to parse.

    The offset bins run to infinity and Python will happily write ``Infinity``
    into a document that then fails in every strict JSON reader there is. A
    report that only round-trips through Python is not an interchange format.
    """
    _sweep(tmp_path, "20260814T000017Z", order_a="U", order_b="L",
           target_a=_planted_a, target_b=_planted_b, nulls_a=_planted_null,
           nulls_b=_planted_null)

    report = review(tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    encoded = json.dumps(report, allow_nan=False, sort_keys=True, default=str)

    assert json.loads(encoded)["schema"] == CROSS_RADIO_SCHEMA


def test_a_method_that_never_confirms_ranks_below_one_that_sometimes_does(tmp_path):
    """0% recall is a measurement; no positives to score against is not.

    They have to rank differently, because a method that was graded and failed
    and a method that could not be graded at all are different findings — and
    the falsy-zero mistake collapses them into one, sorting a measured 0% as
    though nothing had been measured.
    """
    def anchor_only(_instant, _label):
        return [_point(("anchor-8",))]

    _sweep(tmp_path, "20260814T000018Z", target_a=anchor_only,
           target_b=anchor_only, nulls_a=_quiet, nulls_b=_quiet)

    report = review(tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    text = format_review(report)

    # Nothing but anchor-8 fires, so anchor-8's leave-one-out proxy never turns
    # positive and it has no recall; every other method is graded against
    # anchor-8's firing and scores a real 0%.
    assert report["roc"]["anchor-8"]["recall"] is None
    assert report["roc"]["differential-16"]["recall"] == pytest.approx(0.0)
    section = text.split("PER-ALGORITHM ROC")[1].split("CHANNEL-INSTANT")[0]
    assert section.index("differential-16") < section.index("anchor-8")


def test_the_f_spread_is_reported_against_its_own_sampling_noise(tmp_path):
    """A spread of 0.047 means nothing without the scale it is read against.

    Watched over a growing corpus the spread moved 0.099 -> 0.065 -> 0.047 as
    pairs went 30 -> 55 -> 84, which is what a statistic dominated by sampling
    noise looks like, and a bare threshold on it flips verdict as the corpus
    grows. So the same cells are resampled and the spread recomputed, giving the
    scale sampling noise alone produces. Here every algorithm fires on exactly
    the same cells, so the spread is 0 in every resample and the interval is
    degenerate — which is the point: the machinery reports 0, not a small
    positive number invented by the resampling.
    """
    _sweep(tmp_path, "20260814T000020Z", target_a=_planted_a,
           target_b=_planted_b, nulls_a=_planted_null, nulls_b=_planted_null)

    report = review(tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    again = review(tmp_path, false_alarm_rate=FIXTURE_FALSE_ALARM_RATE)
    noise = report["occupancy"]["f_spread"]["sampling"]

    assert noise["draws"] > 0
    assert noise["observed"] == pytest.approx(0.0)
    assert noise["p05"] == pytest.approx(0.0)
    assert noise["p95"] == pytest.approx(0.0)
    # Reproducible: a report that moves when nothing moved cannot be compared
    # against yesterday's.
    assert again["occupancy"]["f_spread"]["sampling"] == noise
    text = format_review(report)
    assert "resampl" in text
    # The interval is optimistic and says so: the two extreme algorithms were
    # picked out of the same cells that produced the interval.
    assert "optimistic" in text


def test_a_corpus_with_no_pairs_says_so_instead_of_printing_zeroes(tmp_path):
    """Nothing scored yet is a state the operator will see for hours."""
    report = review(tmp_path)
    text = format_review(report)

    assert report["pairs"]["joined"] == 0
    assert report["occupancy"]["f_spread"]["methods"] == 0
    assert "no paired sweep" in text.lower()
