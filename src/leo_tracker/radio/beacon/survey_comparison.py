"""Turn accumulated probe scores into a comparison a human can act on.

This reads what :mod:`survey_scoring` wrote and answers the question the plan
actually asks — *what is the cheapest phase-invariant known-code statistic that
reaches the required detection rate at a fixed whole-search false-alarm rate* —
using the only evidence real sky can supply.

**There is no injection here, so there is no Pd, and this file refuses to imply
one.**  What can be measured on unlabelled sky is: how often each method makes a
claim, how far its claims stand above two different nulls, how often independent
checks corroborate them, how often other methods confirm them when told where to
look, and what all of that costs.  What cannot be measured is whether a claim is
true.  The single easiest mistake a reader can make is to see "method X fires
more often" and read it as "method X is more sensitive", when firing more often
is *also exactly* what a worse false-alarm rate looks like — so every count here
travels beside the two nulls and the corroboration rates that separate the two
readings, and the printed report says so at the top rather than in a footnote.

Four populations, kept apart on purpose
---------------------------------------

============  ==========================================  =====================
population    where it comes from                         what it calibrates
============  ==========================================  =====================
searched      a method maximising over its own cells      searched thresholds
conditioned   a method evaluated at someone else's claim  confirmation thresholds
cross-edge    the opposite edge's code, same IQ           **every** null
wrong-code    the same code rolled 17, epoch **pinned**   nulls where the epoch
                                                          was not searched
============  ==========================================  =====================

A searcher over 43,342 cells and a confirmer over one do not share a threshold:
for an exponential-tailed statistic at 1% the two sit 5.2 dB apart.  Pooling them
is the mistake this layout exists to prevent, and ``search_cells`` travels with
every row so the reader can see which is which.

**The two nulls are not co-equal, and the cross-edge one is primary.**  Rolling
the pilot codes by 17 symbols displaces the waveform by 17 symbol periods, so a
rolled control that is free to pick its own epoch re-finds the real signal 187
samples away — measured coherence 0.909, p99 1.851 against the cross-edge 1.252,
correlating 0.967 with the matched score.  A threshold calibrated on it would
have had the survey fire on 1.8% of sky instead of 21%.  Held at one epoch it is
sound.  So a wrong-code control calibrates a threshold only when its certificate
says ``control_epoch == "pinned"``; this file drops the rest rather than trusting
the producer, and reports the gap between the two modes as its own section, since
documenting the trap is worth more than quietly stepping around it.

Thresholds are quantiles of a finite null, so the finest false-alarm rate a
population can support is bounded by its size.  Every threshold reports how many
null realisations produced it and whether the requested rate is inside what they
can carry; a 1% threshold from 40 samples is a number, not a measurement, and it
is labelled ``unsupported`` rather than printed as though it were one.

**Thresholds are probe-length dependent** and nothing else in the codebase
tracks it: a clean null reaches p99 1.310 / 1.189 / 1.137 at 20 / 40 / 80 ms.
Entries scored at different lengths are therefore never pooled into one
threshold; the review says which lengths it found and refuses to average them.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from .structure import STARLINK_FRAME_DURATION_S
from .survey_scoring import (CLEAN_NULL_P99_BY_PROBE_MS,
                             MAXIMUM_DOPPLER_RATE_HZ_S, SCORES_FILENAME,
                             SCORES_SCHEMA, TYPICAL_DOPPLER_RATE_HZ_S)

#: What the aggregate calls itself.
REVIEW_SCHEMA = "leo-tracker.survey-detector-review/v1"

#: False-alarm rate every threshold is calibrated at unless asked otherwise.
#: 1% is the plan's own comparison rate; it is not a production threshold, which
#: needs the whole-search harness of stage 0.4.
DEFAULT_FALSE_ALARM_RATE = 0.01

#: A quantile is only worth quoting when enough null realisations sit above it.
#: Five is the usual floor for a count to stop being dominated by its own
#: Poisson noise, so a rate finer than ``5 / samples`` is reported unsupported
#: rather than quietly extrapolated.
MINIMUM_EXCEEDANCES = 5

#: How close a searched rolled control has to land to ``-roll * 11`` before it
#: counts as having re-found the signal rather than found noise.  Two samples:
#: the peak smears over a lag or two and the frame period is fractional.
ROLLED_SHIFT_TOLERANCE_SAMPLES = 2.0


def _wrapped_gap(value: float | None, target: float, period: float) -> float:
    """Distance between two epochs on a circle of one frame period."""
    if value is None or target is None:
        return float("inf")
    gap = abs(float(value) - float(target)) % period
    return min(gap, period - gap)


#: The header every printed report opens with.  It is not decoration: the report
#: is designed to be pasted into a discussion, and the one misreading that would
#: invert its conclusions has to travel with it.
PREAMBLE = (
    "Real sky, no injection: there is no ground truth here and therefore no "
    "detection probability.\nA method that fires more often may be more "
    "sensitive OR may have a worse false-alarm rate;\nthe two nulls and the "
    "corroboration columns are what tell those apart, not the count.")


# --------------------------------------------------------------------------
# reading what the scorer wrote
# --------------------------------------------------------------------------

def load_scores(corpus_root: Path, *, limit: int | None = None) -> list[dict]:
    """Every score sidecar of the current schema, oldest capture name first.

    A sidecar that will not parse is skipped rather than fatal, for the same
    reason the scorer skips a damaged probe: an aggregate that refuses to
    produce anything because one file of two hundred is short is less useful
    than one that says so and carries on.
    """
    root, loaded = Path(corpus_root), []
    if not root.is_dir():
        return loaded
    for entry in sorted(root.iterdir()):
        if limit is not None and len(loaded) >= limit:
            break
        try:
            payload = json.loads((entry / SCORES_FILENAME).read_text())
        except (OSError, ValueError):
            continue
        if payload.get("schema") == SCORES_SCHEMA:
            loaded.append(payload)
    return loaded


def certificate_rows(payloads: list[dict]):
    """One row per claim: the searched population, flattened for counting."""
    for payload in payloads:
        for observation in payload.get("observations") or []:
            for certificate in observation.get("certificates") or []:
                yield {"capture": payload["capture"],
                       "radio_id": payload.get("radio_id"),
                       "arm": observation["arm"],
                       "null_direction": observation.get("null_direction"),
                       "channel": observation["channel"],
                       "region": observation["region"],
                       "edge": observation["edge"],
                       "receiver": observation["receiver"],
                       "utc": observation.get("utc"),
                       "deployed": observation.get("deployed") or {},
                       **certificate}


def conditioned_rows(payloads: list[dict]):
    """One row per (claim, confirming method): the conditioned population."""
    for payload in payloads:
        for observation in payload.get("observations") or []:
            for point in observation.get("points") or []:
                for method, value in (point.get("methods") or {}).items():
                    yield {"capture": payload["capture"],
                           "arm": observation["arm"],
                           "channel": observation["channel"],
                           "region": observation["region"],
                           "receiver": observation["receiver"],
                           "utc": observation.get("utc"),
                           "point_id": point["point_id"],
                           "epoch_sample": point["epoch_sample"],
                           "cfo_hz": point["cfo_hz"],
                           "claimed_by": point.get("claimed_by") or [],
                           "confirmer": method,
                           "self_claimed": method in (point.get("claimed_by") or []),
                           **value}


# --------------------------------------------------------------------------
# distributions and thresholds
# --------------------------------------------------------------------------

def _at(ordered: list[float], fraction: float) -> float:
    """Plain order statistic of an already-sorted list; no interpolation.

    Deliberately a value the population actually produced rather than one
    between two values it produced, so a threshold can always be pointed at.
    """
    if not ordered:
        return float("nan")
    return float(ordered[min(len(ordered) - 1,
                             max(0, int(round(fraction * (len(ordered) - 1)))))])


def describe(values: list[float]) -> dict:
    """Five order statistics from one sort, and a count that excludes absence.

    ``None`` is dropped rather than read as zero: a method with no wrong-code
    control and a method whose control is zero are different facts, and a
    summary that conflates them puts the difference beyond recovery.
    """
    kept = sorted(value for value in values if value is not None)
    if not kept:
        return {"count": 0}
    return {"count": len(kept), "min": _at(kept, 0.0), "p50": _at(kept, 0.50),
            "p90": _at(kept, 0.90), "p99": _at(kept, 0.99),
            "max": _at(kept, 1.0)}


def threshold_from(nulls: list[float], *,
                   false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """The value a null population exceeds at ``false_alarm_rate``, and whether it can.

    Reports ``supported`` false when fewer than :data:`MINIMUM_EXCEEDANCES`
    realisations would sit above the threshold, because at that point the
    quantile is being set by two or three samples and moves by whole percent
    when one more arrives.  The plan makes the same point from the other side:
    1,456 clean realisations support ~0.2% and no further.
    """
    kept = sorted(value for value in nulls if value is not None)
    if not kept:
        return {"threshold": None, "samples": 0, "supported": False,
                "finest_rate": None, "effective_rate": None,
                "false_alarm_rate": false_alarm_rate}
    finest = MINIMUM_EXCEEDANCES / len(kept)
    threshold = _at(kept, 1.0 - false_alarm_rate)
    # What the threshold actually achieves on the population that produced it,
    # which is not the rate that was asked for once the order statistic runs out
    # of samples: at 56 realisations a "1%" threshold is really about 3.6%.
    exceeded = sum(1 for value in kept if value > threshold)
    return {"threshold": threshold, "samples": len(kept), "finest_rate": finest,
            "effective_rate": exceeded / len(kept),
            "false_alarm_rate": false_alarm_rate,
            "supported": bool(false_alarm_rate >= finest)}


def _fires(values: list[float], threshold: float | None) -> dict:
    if threshold is None or not values:
        return {"count": 0, "of": len(values), "rate": None}
    hits = sum(1 for value in values if value is not None and value > threshold)
    return {"count": hits, "of": len(values), "rate": hits / len(values)}


def searched_comparison(payloads: list[dict], *,
                        false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """Per method: what it claims, what the nulls say about it, and its cost.

    The cross-edge null is the same method's score on the opposite edge's
    template, and it is the one that holds whatever the method searched.  The
    wrong-code null is the *control* score on the target arm — same IQ, same
    epoch, same offset, a code that is not there — and it is used **only** where
    the certificate says the control's epoch was pinned, because a control free
    to choose its epoch lands on the real signal 187 samples away.

    Thresholds are computed on ``score`` for both, so they are directly
    comparable; the margin gets its own threshold from the cross-edge arm, where
    neither term contains a pilot.
    """
    rows = list(certificate_rows(payloads))
    methods = sorted({row["method"] for row in rows})
    report = {}
    for method in methods:
        mine = [row for row in rows if row["method"] == method]
        target = [row for row in mine if row["arm"] == "target"]
        null = [row for row in mine if row["arm"] == "cross-edge-null"]
        scores = [row["score"] for row in target]
        margins = [row["margin"] for row in target if row["margin"] is not None]
        # Both conditions, not one. A control that chose its own epoch is not a
        # null; a control pinned to one lag is not the null of a score maximised
        # over 196,668 of them either. Either way the cross-edge arm is what is
        # left, which is why it is the primary calibration rather than one of a
        # pair.
        controls = [row["control_score"] for row in target
                    if row["control_score"] is not None
                    and row.get("control_epoch") == "pinned"
                    and not row.get("epoch_searched")]
        cross = threshold_from([row["score"] for row in null],
                               false_alarm_rate=false_alarm_rate)
        wrong = threshold_from(controls, false_alarm_rate=false_alarm_rate)
        cross_margin = threshold_from(
            [row["margin"] for row in null if row["margin"] is not None],
            false_alarm_rate=false_alarm_rate)
        elapsed = [row["elapsed_ms"] for row in mine
                   if row.get("elapsed_ms") is not None]
        report[method] = {
            "claims": len(target),
            "search_cells": mine[0]["search_cells"] if mine else None,
            "score": describe(scores), "margin": describe(margins),
            "control": describe(controls),
            "cross_edge_null": {**cross,
                                "fires": _fires(scores, cross["threshold"])},
            "wrong_code_null": {**wrong,
                                "fires": _fires(scores, wrong["threshold"])},
            "margin_null": {**cross_margin,
                            "fires": _fires(margins, cross_margin["threshold"])},
            "elapsed_ms": describe(elapsed),
            "has_control": bool(controls),
            "null_by_direction": {
                direction: describe([row["score"] for row in null
                                     if row["null_direction"] == direction])
                for direction in sorted({row["null_direction"] for row in null}
                                        - {None})},
        }
    return report


def conditioned_comparison(payloads: list[dict], *,
                           false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """Per method as a *confirmer*: how well it answers somebody else's claim.

    Two things are separated here that a single number would hide.  A method's
    score at a point it proposed itself is not evidence about that point — it is
    the same maximisation that produced it, read back.  A method's score at a
    point *another* method proposed is an independent test, and it is the one
    the architecture cares about, so ``self_claimed`` rows are excluded from the
    confirmation rate and counted separately.
    """
    rows = [row for row in conditioned_rows(payloads)]
    methods = sorted({row["confirmer"] for row in rows})
    report = {}
    for method in methods:
        mine = [row for row in rows if row["confirmer"] == method]
        target = [row for row in mine if row["arm"] == "target"]
        others = [row for row in target if not row["self_claimed"]]
        cross = threshold_from(
            [row.get("cross_edge_score") for row in target],
            false_alarm_rate=false_alarm_rate)
        # Nothing conditioned searches an epoch, so every control here should be
        # pinned; the filter is the guard for the day somebody adds one that is
        # not, rather than an expression of doubt about today's.
        wrong = threshold_from(
            [row.get("control_score") for row in target
             if row.get("control_epoch") == "pinned"],
            false_alarm_rate=false_alarm_rate)
        scores = [row["score"] for row in others]
        report[method] = {
            "conditioned_points": len(target),
            "on_other_methods_claims": len(others),
            "score": describe(scores),
            "margin": describe([row.get("margin") for row in others]),
            "cross_edge_null": {**cross,
                                "confirms": _fires(scores, cross["threshold"])},
            "wrong_code_null": {**wrong,
                                "confirms": _fires(scores, wrong["threshold"])},
            "elapsed_ms": describe([row.get("elapsed_ms") for row in mine]),
        }
    return report


# --------------------------------------------------------------------------
# agreement, corroboration, continuity
# --------------------------------------------------------------------------

def confirmation_matrix(payloads: list[dict], conditioned: dict) -> dict:
    """For each proposer, the share of its claims each confirmer stands behind.

    The question the architecture actually asks.  A method can be a poor searcher
    and an excellent confirmer, or the reverse, and the plan's bake-off wants one
    of each — a cheap statistic to propose where to look and a sharp one to
    settle it.  Counting how often each method fires on its own is blind to that
    distinction; this is not.

    Each confirmer is held at its *conditioned* cross-edge threshold, which is
    far below its searched one because a conditioned test pays no extreme-value
    penalty.  A claim the proposer also made itself is excluded from its own
    column: reading a maximisation back at its own argmax is not a second
    opinion.
    """
    thresholds = {method: summary["cross_edge_null"]["threshold"]
                  for method, summary in conditioned.items()}
    counts: dict[tuple[str, str], list[int]] = {}
    for row in conditioned_rows(payloads):
        if row["arm"] != "target":
            continue
        confirmer, threshold = row["confirmer"], thresholds.get(row["confirmer"])
        if threshold is None or row.get("score") is None:
            continue
        for proposer in row["claimed_by"]:
            if proposer == confirmer:
                continue
            slot = counts.setdefault((proposer, confirmer), [0, 0])
            slot[0] += int(row["score"] > threshold)
            slot[1] += 1
    return {f"{proposer}->{confirmer}":
            {"confirmed": hits, "claims": total,
             "rate": hits / total if total else None}
            for (proposer, confirmer), (hits, total) in sorted(counts.items())}


def _key(row: dict) -> tuple:
    return (row["capture"], row["channel"], row["region"], row["receiver"])


def pairwise_agreement(payloads: list[dict], searched: dict) -> dict:
    """Which methods fire on the same probes, at each one's own threshold.

    Every method is thresholded on its *own* cross-edge null, so the comparison
    is between methods held at a common false-alarm rate rather than a common
    score — the only comparison that means anything when the statistics are
    normalised differently.
    """
    rows = [row for row in certificate_rows(payloads) if row["arm"] == "target"]
    fired = {}
    for method, summary in searched.items():
        threshold = summary["cross_edge_null"]["threshold"]
        if threshold is None:
            continue
        fired[method] = {_key(row) for row in rows
                         if row["method"] == method and row["score"] > threshold}
    methods = sorted(fired)
    matrix = {}
    for left in methods:
        for right in methods:
            if left >= right:
                continue
            both = len(fired[left] & fired[right])
            either = len(fired[left] | fired[right])
            matrix[f"{left}|{right}"] = {
                "both": both, "either": either,
                "only_left": len(fired[left] - fired[right]),
                "only_right": len(fired[right] - fired[left]),
                "jaccard": both / either if either else None}
    return {"fired": {method: len(keys) for method, keys in fired.items()},
            "pairs": matrix}


def corroboration(payloads: list[dict], searched: dict) -> dict:
    """How often each method's claims survive a check it did not make itself.

    This is the column that separates a sensitive detector from a noisy one, and
    it is the only one here that does not come out of the detector's own
    arithmetic.  Cross-receiver is the strongest: the antennas are co-located,
    so a real satellite lands on the same frame epoch at both ports with offsets
    differing by exactly the measured LNB bias — and neither absolute offset has
    to be known for that to be checkable.
    """
    report = {}
    for payload in payloads:
        for check in payload.get("cross_receiver") or []:
            slot = report.setdefault(check["method"], _empty_corroboration())
            slot["cross_receiver"]["of"] += 1
            slot["cross_receiver"]["count"] += int(check["agrees"])
            slot["epoch_difference_samples"].append(
                check["epoch_difference_samples"])
            slot["cfo_residual_after_bias_hz"].append(
                check["cfo_residual_after_bias_hz"])
        for check in payload.get("cross_edge") or []:
            slot = report.setdefault(check["method"], _empty_corroboration())
            slot["cross_edge"]["of"] += 1
            slot["cross_edge"]["count"] += int(check["agrees"])
        for observation in payload.get("observations") or []:
            geometry = observation.get("geometry")
            if not geometry or observation["arm"] != "target":
                continue
            for method, match in (geometry.get("by_method") or {}).items():
                slot = report.setdefault(method, _empty_corroboration())
                slot["geometry"]["of"] += 1
                slot["geometry"]["count"] += int(bool(match.get("matches")))
                slot["prior_coverage"].append(
                    geometry.get("prior_coverage_fraction", 0.0))
    for method, slot in report.items():
        for name in ("cross_receiver", "cross_edge", "geometry"):
            counted = slot[name]
            counted["rate"] = (counted["count"] / counted["of"]
                               if counted["of"] else None)
        slot["epoch_difference_samples"] = describe(
            slot["epoch_difference_samples"])
        slot["cfo_residual_after_bias_hz"] = describe(
            slot["cfo_residual_after_bias_hz"])
        slot["prior_coverage"] = describe(slot["prior_coverage"])
        slot["fired"] = searched.get(method, {}).get(
            "cross_edge_null", {}).get("fires", {}).get("count")
    return report


def _empty_corroboration() -> dict:
    return {"cross_receiver": {"count": 0, "of": 0},
            "cross_edge": {"count": 0, "of": 0},
            "geometry": {"count": 0, "of": 0},
            "epoch_difference_samples": [], "cfo_residual_after_bias_hz": [],
            "prior_coverage": []}


def rolled_control_trap(payloads: list[dict], *,
                        false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """The same wrong-code control scored two ways, so the trap is on the record.

    ``full-frame-300`` is the only candidate here that searches its own epoch,
    which makes it the only one that can demonstrate the failure directly: the
    identical rolled template is scored once at the epoch the exact detector
    chose and once free to choose its own.  Free, it should land
    ``roll * 11`` samples away — 187 at 2.5 MS/s, which wraps to 3,146 modulo
    the frame period — carrying the real signal with it, and the threshold it
    implies should be far above the pinned one.

    Recorded rather than merely avoided.  A future reader who finds
    ``matched_pilot_control_scores`` and reaches for its control column needs
    the number in front of them, not a comment in a module they will not open.
    """
    rows = [row for row in certificate_rows(payloads)
            if row["arm"] == "target"
            and row.get("searched_control_score") is not None]
    if not rows:
        return {"observations": 0}
    pinned = [row["control_score"] for row in rows]
    searched = [row["searched_control_score"] for row in rows]
    expected = rows[0].get("rolled_shift_samples")
    shifts = [row.get("searched_control_epoch_shift_samples") for row in rows]
    period = (payloads[0].get("sample_rate_hz", 2_500_000.0)
              * STARLINK_FRAME_DURATION_S)
    landed = sum(1 for shift in shifts
                 if _wrapped_gap(shift, period - expected, period)
                 <= ROLLED_SHIFT_TOLERANCE_SAMPLES)
    return {"observations": len(rows), "method": "full-frame-300",
            "pinned_control": describe(pinned),
            "searched_control": describe(searched),
            "pinned_threshold": threshold_from(
                pinned, false_alarm_rate=false_alarm_rate),
            "searched_threshold": threshold_from(
                searched, false_alarm_rate=false_alarm_rate),
            "expected_shift_samples": expected,
            "epoch_shift_samples": describe(shifts),
            "landed_on_the_shifted_signal": landed}


def _parse_utc(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def time_continuity(payloads: list[dict]) -> dict:
    """Do consecutive claims on one tuning move at a rate physics permits?

    Between two probes of the same channel, edge and receiver, a real satellite's
    offset moves at its Doppler rate — bounded by the catalogue sweep at
    5.5 kHz/s all-sky p99.9 and 12.7 kHz/s worst case.  A pair of claims that
    implies a faster slew is not one satellite being tracked; it is two
    unrelated numbers.  This is weak evidence per pair and strong in bulk, and
    it costs nothing but sorting what is already written down.
    """
    tracks: dict[tuple, list[tuple[float, float]]] = {}
    for payload in payloads:
        for observation in payload.get("observations") or []:
            if observation["arm"] != "target":
                continue
            moment = _parse_utc(observation.get("utc"))
            if moment is None:
                continue
            for certificate in observation.get("certificates") or []:
                tracks.setdefault(
                    (payload.get("radio_id"), observation["channel"],
                     observation["region"], observation["receiver"],
                     certificate["method"]), []).append(
                        (moment, certificate["cfo_hz"]))
    report: dict[str, dict] = {}
    for key, points in tracks.items():
        method = key[4]
        slot = report.setdefault(method, {"pairs": 0, "plausible": 0,
                                          "typical": 0, "rates_hz_s": []})
        points.sort()
        for (first_t, first_f), (next_t, next_f) in zip(points, points[1:]):
            gap = next_t - first_t
            if gap <= 0:
                continue
            rate = abs(next_f - first_f) / gap
            slot["pairs"] += 1
            slot["plausible"] += int(rate <= MAXIMUM_DOPPLER_RATE_HZ_S)
            slot["typical"] += int(rate <= TYPICAL_DOPPLER_RATE_HZ_S)
            slot["rates_hz_s"].append(rate)
    for slot in report.values():
        slot["rate_hz_s"] = describe(slot.pop("rates_hz_s"))
        slot["plausible_rate"] = (slot["plausible"] / slot["pairs"]
                                  if slot["pairs"] else None)
    return report


# --------------------------------------------------------------------------
# the review
# --------------------------------------------------------------------------

def review(corpus_root: Path, *, limit: int | None = None,
           false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """Everything the accumulated sidecars can say, in one document."""
    payloads = load_scores(corpus_root, limit=limit)
    searched = searched_comparison(payloads, false_alarm_rate=false_alarm_rate)
    conditioned = conditioned_comparison(payloads,
                                         false_alarm_rate=false_alarm_rate)
    return {
        "schema": REVIEW_SCHEMA,
        "caveat": PREAMBLE,
        "entries": len(payloads),
        "captures": sorted({payload["capture"] for payload in payloads}),
        "observations": sum(len(payload.get("observations") or [])
                            for payload in payloads),
        "false_alarm_rate": false_alarm_rate,
        "probe_lengths": _probe_lengths(payloads),
        "deployed_threshold": sorted({payload.get("deployed_threshold")
                                      for payload in payloads}
                                     - {None}),
        "rolled_control_trap": rolled_control_trap(
            payloads, false_alarm_rate=false_alarm_rate),
        "searched": searched,
        "conditioned": conditioned,
        "confirmation": confirmation_matrix(payloads, conditioned),
        "agreement": pairwise_agreement(payloads, searched),
        "corroboration": corroboration(payloads, searched),
        "continuity": time_continuity(payloads),
        "deployed_reproduction": _reproduction(payloads),
        "cost_s_per_entry": describe([payload.get("elapsed_s")
                                      for payload in payloads]),
    }


def _probe_lengths(payloads: list[dict]) -> dict:
    """Which probe lengths went into this aggregate, and whether that is one.

    A null threshold is not a property of a detector, it is a property of a
    detector at a probe length: a clean null reaches p99 1.310 at 20 ms and
    1.137 at 80 ms.  Pooling two lengths produces a threshold that is right for
    neither, and nothing else in this codebase notices — so the review counts
    the lengths it found and says so rather than averaging them.
    """
    found = sorted({round(payload.get("probe_ms") or 0.0, 3)
                    for payload in payloads} - {0.0})
    return {"probe_ms": found, "single_length": len(found) <= 1,
            "clean_null_p99_reference": CLEAN_NULL_P99_BY_PROBE_MS}


def _reproduction(payloads: list[dict]) -> dict:
    """Whether re-running config A here returns the capture host's own answer.

    The one end-to-end check on everything in front of the comparison.  If this
    is not zero the ``sample_order`` mapping, the reshape or the receiver index
    is wrong, and every number in the report above is about the wrong sky — so
    it is reported at the top rather than buried, and a non-zero value should
    stop the reader before the tables rather than after them.
    """
    deltas = [observation.get("deployed_reproduction_delta")
              for payload in payloads
              for observation in payload.get("observations") or []]
    checked = [delta for delta in deltas if delta is not None]
    return {**describe(deltas),
            "exact": bool(checked) and all(delta == 0.0 for delta in checked)}


def _abbreviate(method: str) -> str:
    """A column header that still names one method.

    ``differential-16`` and ``differential-32`` share their first eight
    characters, so truncating the family alone would put two different methods
    under one heading — the symbol count has to survive.
    """
    family, _, count = method.rpartition("-")
    return f"{family[:4]}-{count}" if family else method[:8]


def _mark(threshold: dict) -> str:
    """A star beside a threshold its own null population cannot carry."""
    return (" " if threshold.get("threshold") is None or threshold["supported"]
            else "*")


def _cell(value, spec: str = "8.3f", missing: str = "       -") -> str:
    if value is None or value != value:                # NaN is never equal to itself
        return missing
    return format(value, spec)


def format_review(report: dict) -> str:
    """The comparison as a table, with the misreading spelled out above it."""
    reproduction = report.get("deployed_reproduction") or {}
    checked = reproduction.get("count", 0)
    if not checked:
        provenance = "deployed config A not re-run: nothing scored yet"
    elif reproduction.get("exact"):
        provenance = (f"deployed config A reproduced exactly on all {checked} "
                      "observations")
    else:
        provenance = (
            "deployed config A reproduced with worst delta "
            f"{_cell(reproduction.get('max'), '.3e', 'n/a')} on {checked} "
            "observations  <-- NOT EXACT: the tuning mapping or the reshape is "
            "wrong and nothing below is trustworthy")
    lengths = report.get("probe_lengths") or {}
    found = lengths.get("probe_ms") or []
    length_line = ("probe length " + ", ".join(f"{value:g} ms" for value in found)
                   if found else "probe length unknown")
    if not lengths.get("single_length", True):
        length_line += ("  <-- MIXED: a null threshold belongs to one probe "
                        "length (clean p99 1.310/1.189/1.137 at 20/40/80 ms); "
                        "these must not be pooled")
    lines = [PREAMBLE, "",
             f"entries {report['entries']}   observations {report['observations']}"
             f"   false-alarm rate {report['false_alarm_rate']:.3f}",
             length_line, provenance, ""]

    lines.append("SEARCHED  — each method maximising over its own cells")
    lines.append(f"{'method':>16} {'cells':>7} {'claims':>7} {'score p50':>10}"
                 f" {'score p90':>10} {'margin p50':>11} {'t(cross)':>9}"
                 f" {'fires':>7} {'t(wrong)':>9} {'fires':>7} {'ms p50':>8}")
    for method, summary in sorted(report["searched"].items()):
        cross, wrong = summary["cross_edge_null"], summary["wrong_code_null"]
        lines.append(
            f"{method:>16} {summary['search_cells'] or 0:>7}"
            f" {summary['claims']:>7}"
            f" {_cell(summary['score'].get('p50'), '10.4f', '         -')}"
            f" {_cell(summary['score'].get('p90'), '10.4f', '         -')}"
            f" {_cell(summary['margin'].get('p50'), '11.4f', '          -')}"
            f" {_cell(cross.get('threshold'), '9.4f', '        -')}{_mark(cross)}"
            f"{_cell(cross['fires'].get('rate'), '6.1%', '     -')}"
            f" {_cell(wrong.get('threshold'), '9.4f', '        -')}{_mark(wrong)}"
            f"{_cell(wrong['fires'].get('rate'), '6.1%', '     -')}"
            f" {_cell(summary['elapsed_ms'].get('p50'), '8.1f')}")
    lines.append("  * threshold not supported by the null population it was "
                 "drawn from: the order statistic ran")
    lines.append("    out of samples, so it realises effective_rate rather than "
                 "the rate asked for. Both are")
    lines.append("    in the JSON. More entries is the only fix.")
    lines.append("  t(cross) is the primary calibration. t(wrong) appears only "
                 "for a method whose exact score and")
    lines.append("  control share one epoch: a rolled control free to pick its "
                 "own re-finds the real signal, and a")
    lines.append("  control pinned to one lag is not the null of a score "
                 "maximised over 196,668 of them either.")
    lines.append("  So full-frame-300 has no t(wrong), and coarse-A/coarse-E "
                 "have no wrong-code control at all —")
    lines.append("  the kernel bank has no rolled-template path. All three rest "
                 "on the cross-edge null alone.")
    for value in report.get("deployed_threshold") or []:
        coarse = (report["searched"].get("coarse-A") or {}).get(
            "cross_edge_null", {}).get("threshold")
        lines.append(
            f"  deployed gate is {value:.3f} against a cross-edge "
            f"{report['false_alarm_rate']:.0%} threshold of "
            f"{_cell(coarse, '.3f', 'n/a')} for coarse-A: the deployed number "
            "is the stricter of the two,")
        lines.append("  which costs detections rather than manufacturing them. "
                     "The plan's 8.11%/2.72%/2.11%")
        lines.append("  false-alarm figures are wrong and self-contradictory; a "
                     "clean 80 ms null puts 1.33 near 0.06%.")
    lines.append("")

    trap = report.get("rolled_control_trap") or {}
    if trap.get("observations"):
        lines.append("ROLLED-CONTROL TRAP — the same wrong-code control, "
                     "epoch pinned vs epoch searched")
        lines.append(
            f"  {trap['method']}: pinned p50 "
            f"{_cell(trap['pinned_control'].get('p50'), '.4f', 'n/a')}"
            f"  searched p50 "
            f"{_cell(trap['searched_control'].get('p50'), '.4f', 'n/a')}"
            f"   thresholds "
            f"{_cell(trap['pinned_threshold'].get('threshold'), '.4f', 'n/a')}"
            f" vs "
            f"{_cell(trap['searched_threshold'].get('threshold'), '.4f', 'n/a')}"
            f"   n={trap['observations']}")
        lines.append(
            f"  the searched control landed on the signal's own epoch minus "
            f"{trap.get('expected_shift_samples')} samples in "
            f"{trap['landed_on_the_shifted_signal']}/{trap['observations']} "
            "cases (median shift "
            f"{_cell(trap['epoch_shift_samples'].get('p50'), '.0f', 'n/a')}).")
        lines.append("  Rolling the codes rolls the waveform, so a control free "
                     "to choose its epoch is not a null.")
        lines.append("  It is kept here as the exhibit; nothing above is "
                     "calibrated on it.")
        lines.append("")

    lines.append("CONDITIONED — each method evaluated where another method "
                 "claimed, no search")
    lines.append(f"{'method':>16} {'points':>7} {'others':>7} {'score p50':>10}"
                 f" {'t(cross)':>9} {'confirms':>9} {'t(wrong)':>9}"
                 f" {'confirms':>9} {'ms p50':>8}")
    for method, summary in sorted(report["conditioned"].items()):
        cross, wrong = summary["cross_edge_null"], summary["wrong_code_null"]
        lines.append(
            f"{method:>16} {summary['conditioned_points']:>7}"
            f" {summary['on_other_methods_claims']:>7}"
            f" {_cell(summary['score'].get('p50'), '10.4f', '         -')}"
            f" {_cell(cross.get('threshold'), '9.4f', '        -')}"
            f" {_cell(cross['confirms'].get('rate'), '9.1%', '        -')}"
            f" {_cell(wrong.get('threshold'), '9.4f', '        -')}"
            f" {_cell(wrong['confirms'].get('rate'), '9.1%', '        -')}"
            f" {_cell(summary['elapsed_ms'].get('p50'), '8.1f')}")
    lines.append("  A conditioned test needs no extreme-value margin, so its "
                 "threshold is far below the")
    lines.append("  searched one for the same false-alarm rate. Never compare a "
                 "conditioned score with a")
    lines.append("  searched threshold, or with another method's searched score.")
    lines.append("")

    confirmation = report.get("confirmation") or {}
    if confirmation:
        proposers = sorted({pair.split("->")[0] for pair in confirmation})
        confirmers = sorted({pair.split("->")[1] for pair in confirmation})
        lines.append("CONFIRMATION — share of each proposer's claims that each "
                     "confirmer stands behind")
        lines.append(" " * 17 + "".join(f"{_abbreviate(name):>9}"
                                        for name in confirmers)
                     + "     <- confirmer")
        for proposer in proposers:
            row = [f"{proposer:>16}"]
            for confirmer in confirmers:
                slot = confirmation.get(f"{proposer}->{confirmer}")
                row.append(_cell(None if slot is None else slot["rate"],
                                 '9.1%', '        -'))
            lines.append("".join(row))
        lines.append("  Read down a column for how good a confirmer is, across "
                     "a row for how confirmable a")
        lines.append("  proposer's claims are. A method may be a poor searcher "
                     "and a fine confirmer; the plan's")
        lines.append("  architecture wants one of each, and firing counts alone "
                     "cannot see that difference.")
        lines.append("")

    lines.append("CORROBORATION — checks the detector did not make itself")
    lines.append(f"{'method':>16} {'x-recv':>8} {'x-edge':>8} {'geometry':>9}"
                 f" {'prior cov':>10} {'|dEpoch| p50':>13} {'|dCFO-bias| p50':>16}")
    for method, slot in sorted(report["corroboration"].items()):
        lines.append(
            f"{method:>16}"
            f" {_cell(slot['cross_receiver'].get('rate'), '8.1%', '       -')}"
            f" {_cell(slot['cross_edge'].get('rate'), '8.1%', '       -')}"
            f" {_cell(slot['geometry'].get('rate'), '9.1%', '        -')}"
            f" {_cell(slot['prior_coverage'].get('p50'), '10.1%', '         -')}"
            f" {_cell(slot['epoch_difference_samples'].get('p50'), '13.1f')}"
            f" {_cell(slot['cfo_residual_after_bias_hz'].get('p50'), '16.0f')}")
    lines.append("  geometry is a prior, never a label: a catalogued satellite "
                 "can be in view and silent,")
    lines.append("  and 'prior cov' is the share of the searched span some "
                 "predicted slice already covers —")
    lines.append("  a match is worth about 1/coverage to 1, which is often "
                 "barely better than even.")
    lines.append("  THESE COLUMNS FAVOUR COARSE ESTIMATORS. coarse-A and "
                 "coarse-E report one of a handful of")
    lines.append("  grid values, so two independent noise draws agree with each "
                 "other for free, while a")
    lines.append("  continuous estimator has to hit the same hertz twice. Read "
                 "agreement rates beside the")
    lines.append("  |dCFO| spreads, not instead of them.")
    lines.append("  EVERY CANDIDATE INHERITS THE COARSE EPOCH, so the |dEpoch| "
                 "half of the cross-receiver check")
    lines.append("  is the same test for all of them and cannot separate them. "
                 "Only the |dCFO| half is the")
    lines.append("  candidate's own; full-frame-300, which searches its own "
                 "epoch, is the one exception.")
    lines.append("")

    lines.append("TIME CONTINUITY — consecutive claims on one tuning")
    lines.append("  Only discriminating when consecutive probes are close in "
                 "time: an hour apart, any offset")
    lines.append("  in the search span implies a rate physics allows, and the "
                 "test passes on everything.")
    lines.append(f"{'method':>16} {'pairs':>7} {'<=12.7 kHz/s':>13}"
                 f" {'<=5.5 kHz/s':>12} {'rate p50 Hz/s':>14}")
    for method, slot in sorted(report["continuity"].items()):
        typical = slot["typical"] / slot["pairs"] if slot["pairs"] else None
        lines.append(
            f"{method:>16} {slot['pairs']:>7}"
            f" {_cell(slot.get('plausible_rate'), '13.1%', '            -')}"
            f" {_cell(typical, '12.1%', '           -')}"
            f" {_cell(slot['rate_hz_s'].get('p50'), '14.0f')}")
    lines.append("")

    lines.append("AGREEMENT — probes where two methods both fire, each at its "
                 "own cross-edge threshold")
    lines.append(f"{'pair':>36} {'both':>6} {'left':>6} {'right':>6}"
                 f" {'jaccard':>8}")
    for pair, counts in sorted(report["agreement"]["pairs"].items()):
        lines.append(f"{pair:>36} {counts['both']:>6} {counts['only_left']:>6}"
                     f" {counts['only_right']:>6}"
                     f" {_cell(counts.get('jaccard'), '8.2f')}")
    lines.append("")
    cost = report["cost_s_per_entry"]
    lines.append(f"cost: {_cell(cost.get('p50'), '.1f', '-')} s per entry "
                 f"(median of {cost.get('count', 0)})")
    return "\n".join(lines)
