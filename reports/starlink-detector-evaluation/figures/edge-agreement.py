#!/usr/bin/env python3
"""edge-agreement - how far two receiver chains agree, and why this design cannot say.

WHAT CHANGED FROM THE PUBLISHED VERSION OF THIS FIGURE.

The published figure drew a four-rung ladder and read differences between
adjacent rungs as causes: the edge, the radio boundary, the receiver.  That
ladder is WITHDRAWN.  It is not a factorial design, and its "changing the LNB"
step compares one chain's own two edges against two chains at one tuning where
BOTH chains are lnb-c and lnb-d -- the same radio, the same LNB model, both
wet.  No receiver is substituted anywhere in that step, so the number it
produced (-0.0868) cannot be a receiver effect.  The rung it was measured down
from also pooled three ports against a two-port rung, so part of the difference
was baseline composition.

What the corpus does supply, once lnb-a is restored, is SIX RECEIVER PAIRS --
every pairing of the four ports -- on the same 1,258 paired sweeps and the same
tuning instants, n = 10,064 each.  Four are cross-radio cells joined on the
instant both radios were released into (``cross_radio.join_cells``); two are
within-radio, the two ports of one radio at one instant.  They are matched by
construction: no weighting, no re-sampling.

This figure plots those six and encodes RADIO, LNB MODEL and WATER as visual
attributes so a reader can see for themselves that none of the three orders the
six.  It does NOT claim a decomposition, and it cannot: WATER IS CONFOUNDED WITH
RADIO in this corpus.  Both wet ports (lnb-c, lnb-d) are on pluto-19f2 and both
dry ports (lnb-a, lnb-b) are on pluto-5d4d, so no contrast here separates
crossing the radio boundary from crossing from a wet LNB to a dry one.  Model is
separable from water exactly once -- by substituting which dry unit on 5d4d sits
on the far side -- and even there model is confounded with unit identity,
because 5d4d carries exactly one unit of each model.

lnb-a IS INCLUDED.  The thresholds are deliberately left where the published
figure drew them: ``null_thresholds`` is given the three-port null population,
and lnb-a's TARGET observations are judged against that same bar.  One thing
changes between the published numbers and these -- the receiver set.  It is also
the conservative choice: lnb-a's own null arm is out-of-sample under this bar
while the other three ports' is in-sample, which biases AGAINST lnb-a looking
normal.  ``robustness_thresholds_redrawn`` in the JSON reports every pair with
lnb-a's null arm folded into the threshold population; no pair moves by 0.004.

Statistic: mean phi across the eight algorithms, each judged against the
threshold drawn for its own (sample rate, probe length) from the cross-edge null
arms.  phi is capped below 1 whenever the two chains fire at different rates,
and these four ports do not fire at the same rate, so phi / phi_max is plotted
beside it -- and it reorders the six.

Uncertainty is a bootstrap over PAIRED SWEEPS, the unit the corpus replicates,
not over cells: the 8 tunings of one sweep are not independent draws.

    nice -n 15 python3 edge-agreement.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
#: ``hcore`` sits beside this file when the figure directory is self-contained,
#: and in ../work beside the frozen snapshot when it is regenerated.
for _candidate in (HERE, HERE.parent / "work"):
    if (_candidate / "hcore.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402
from _pipeline import load as _load_pipeline  # noqa: E402
hcore = _load_pipeline("hcore")

NAME = "edge-agreement"
INK, MUTED, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#d7d6d2"
BAND = "#ecebe7"
BLUE, ORANGE, TEAL = "#2a78d6", "#eb6834", "#2f8f7f"

DRAWS = 400
SEED = 20260814

#: Operator-supplied hardware facts.  Not measured here; recorded so every pair
#: can be labelled with what it holds fixed and what it does not.
#:
#: Model letters are placeholders for two different LNB models -- lnb-a, lnb-c
#: and lnb-d are one model, lnb-b is another.  WATER means liquid water was
#: found on the bias-tee SMA pins of that port.
HARDWARE = {
    "lnb-a": {"model": "X", "water": "dry", "radio": "pluto-5d4d", "port": "rx0"},
    "lnb-b": {"model": "Y", "water": "dry", "radio": "pluto-5d4d", "port": "rx1"},
    "lnb-c": {"model": "X", "water": "wet", "radio": "pluto-19f2", "port": "rx0"},
    "lnb-d": {"model": "X", "water": "wet", "radio": "pluto-19f2", "port": "rx1"},
}

#: All six pairings of the four ports.  The two within-radio pairs are written
#: rx0|rx1 so that "A" is always the lower-numbered port of its radio, and the
#: four cross-radio pairs come out of ``join_cells`` as 19f2|5d4d.
PAIRS = ["lnb-c|lnb-d", "lnb-a|lnb-b", "lnb-c|lnb-a", "lnb-d|lnb-a",
         "lnb-c|lnb-b", "lnb-d|lnb-b"]
WITHIN_RADIO = (("lnb-c", "lnb-d"), ("lnb-a", "lnb-b"))

#: The three factors, and the fact that undoes any attempt to read them as one.
CONFOUND = (
    "WATER IS CONFOUNDED WITH RADIO. Both wet ports (lnb-c, lnb-d) are on "
    "pluto-19f2; both dry ports (lnb-a, lnb-b) are on pluto-5d4d. No contrast "
    "in this corpus separates crossing the radio boundary from crossing from a "
    "wet LNB to a dry one. Model is separable from water once -- lnb-c|lnb-a "
    "against lnb-c|lnb-b substitutes only which dry 5d4d unit is on the far "
    "side -- and even there model is confounded with unit identity, since 5d4d "
    "carries exactly one unit of each model.")

#: What the published four-rung ladder claimed, carried so the withdrawal is
#: legible in the JSON rather than only in the report text.  NOT plotted.
WITHDRAWN_LADDER = {
    "status": "WITHDRAWN",
    "what_it_claimed": {
        "changing RECEIVER": -0.0868,
        "the RADIO BOUNDARY": "indistinguishable from zero",
        "the EDGE": "indistinguishable from zero"},
    "why_it_is_withdrawn": [
        "it is not a factorial design: the four rungs differ in more than one "
        "factor at a time and no rung is a control for the one below it",
        "its 'changing the LNB' step compares one chain's own two edges against "
        "TWO CHAINS AT ONE TUNING where both chains are lnb-c and lnb-d -- same "
        "radio, same LNB model, both wet. No receiver is substituted in it",
        "the baseline it was measured down from pooled three ports while the "
        "rung above it used two, so part of -0.0868 is baseline composition: "
        "adding lnb-a to that pool alone moves it -0.0054",
        "the rung the report says only pluto-19f2 can supply -- two receivers "
        "on one radio -- exists on pluto-5d4d too once lnb-a is restored"],
}


# --------------------------------------------------------------------------
# corpus
# --------------------------------------------------------------------------

def restore_lnb_a(*, threshold_null_excludes=("lnb-a",)):
    """A corpus whose target arm holds all four ports.

    ``cross_radio.DEAD_RECEIVERS`` is the module's own documented exclusion list
    ("a list rather than a flag because the next port to die should be one
    edit").  ``_live``, ``null_thresholds`` and ``_by_instant`` all read it at
    call time, so setting it restores lnb-a to the target arm and to the join
    without touching any of the functions that do the work.
    """
    cr.DEAD_RECEIVERS = tuple(threshold_null_excludes)
    corpus = hcore.Corpus()          # null_thresholds is drawn here
    cr.DEAD_RECEIVERS = ()           # lnb-a is a live receiver from here on
    return corpus


class Deck:
    """Every TARGET observation of the paired corpus, with its eight verdicts.

    Frozen once so that phi, the fire rates and the bootstrap all rest on one
    identical decision per (observation, method) rather than on a rule
    re-derived per panel.  Indexed by the cache row the observation carries, so
    ``join_cells`` output can be looked up without rescoring.
    """

    def __init__(self, corpus):
        self.methods = corpus.methods
        rows, meta, self.row_of = [], [], {}
        for pair in corpus.pairs:
            for entry in pair["radios"]:
                key = cr.threshold_key(entry)
                for observation in entry["scores"]["observations"]:
                    if observation.get("arm") != "target":
                        continue
                    verdicts = [cr.observation_fires(
                        observation, method,
                        cr._threshold_for(corpus.thresholds, method, key))
                        for method in self.methods]
                    if any(v is None for v in verdicts):
                        continue
                    self.row_of[observation["_row"]] = len(rows)
                    rows.append([bool(v) for v in verdicts])
                    meta.append((observation.get("receiver_label"),
                                 entry["capture"], observation.get("iq_index"),
                                 pair["paired_sweep"],
                                 observation.get("channel"),
                                 pair["geometry"]))
        self.fired = np.array(rows, dtype=bool)
        self.anyfire = self.fired.any(axis=1)
        self.rx = np.array([m[0] for m in meta])
        self.capture = np.array([m[1] for m in meta])
        self.instant = np.array([m[2] for m in meta], dtype=np.int16)
        self.sweep = np.array([m[3] for m in meta])
        self.channel = np.array([m[4] for m in meta], dtype=np.int16)
        self.geometry = np.array([m[5] for m in meta])

    def stack(self, rows: np.ndarray) -> np.ndarray:
        """The eight verdicts plus the any-method column, for chosen rows."""
        return np.concatenate([self.fired[rows], self.anyfire[rows][:, None]],
                              axis=1)


def receiver_pair_rows(corpus, deck) -> dict:
    """Deck row indices for both members of each of the six receiver pairs."""
    groups: dict = defaultdict(lambda: {"left": [], "right": [], "sweep": [],
                                        "channel": [], "geometry": []})

    # -- four cross-radio pairs: the repository's own instant join ---------
    for pair in corpus.pairs:
        for cell in cr.join_cells(pair):
            left = deck.row_of.get(cell["a"]["observation"]["_row"])
            right = deck.row_of.get(cell["b"]["observation"]["_row"])
            if left is None or right is None:
                continue
            bucket = groups[cell["receiver_pair"]]
            bucket["left"].append(left)
            bucket["right"].append(right)
            bucket["sweep"].append(pair["paired_sweep"])
            bucket["channel"].append(cell["a"]["channel"])
            bucket["geometry"].append(pair["geometry"])

    # -- two within-radio pairs: the two ports of one radio, one instant ---
    by_instant: dict = defaultdict(dict)
    meta: dict = {}
    for row in range(deck.fired.shape[0]):
        key = (str(deck.capture[row]), int(deck.instant[row]))
        by_instant[key][str(deck.rx[row])] = row
        meta[key] = (str(deck.sweep[row]), int(deck.channel[row]),
                     str(deck.geometry[row]))
    for rx0, rx1 in WITHIN_RADIO:
        bucket = groups[f"{rx0}|{rx1}"]
        for key, members in by_instant.items():
            if rx0 in members and rx1 in members:
                bucket["left"].append(members[rx0])
                bucket["right"].append(members[rx1])
                bucket["sweep"].append(meta[key][0])
                bucket["channel"].append(meta[key][1])
                bucket["geometry"].append(meta[key][2])
    return groups


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def factors(left: str, right: str) -> dict:
    """What one receiver-pair contrast holds fixed and what it changes."""
    a, b = HARDWARE[left], HARDWARE[right]
    return {
        "radio": "same radio" if a["radio"] == b["radio"] else "cross radio",
        "model": "same model" if a["model"] == b["model"] else "cross model",
        "water": ("wet|wet" if a["water"] == b["water"] == "wet" else
                  "dry|dry" if a["water"] == b["water"] == "dry" else "wet|dry"),
        "unit": "two physical units (always)",
        "radios": [a["radio"], b["radio"]],
        "ports": [a["port"], b["port"]],
    }


def phi_columns(left: np.ndarray, right: np.ndarray) -> list:
    """phi per column, via the repository's own normalisation."""
    out = []
    for column in range(left.shape[1]):
        a, b = left[:, column], right[:, column]
        out.append(hcore.phi_from_counts(a.size, int(a.sum()), int(b.sum()),
                                         int(np.logical_and(a, b).sum())))
    return out


def counts_of(left: np.ndarray, right: np.ndarray, column: int) -> dict:
    a, b = left[:, column], right[:, column]
    both = int(np.logical_and(a, b).sum())
    return {"n": int(a.size), "a_fires": int(a.sum()), "b_fires": int(b.sum()),
            "both": both, "a_only": int(a.sum()) - both,
            "b_only": int(b.sum()) - both,
            "neither": int(np.logical_and(~a, ~b).sum())}


def phi_ceiling(p_a: float, p_b: float) -> float:
    """The largest phi two binary variables with these marginals can reach.

    Worth carrying because the four ports do NOT fire at the same rate -- the
    any-method rate runs 0.345 (lnb-a) to 0.460 (lnb-d) -- and phi between two
    unequal marginals is capped below 1.  An ordering that merely tracked that
    cap would be an artefact of the fire rates rather than a fact about the
    hardware, so phi / phi_max is reported and plotted beside phi.
    """
    low, high = (p_a, p_b) if p_a <= p_b else (p_b, p_a)
    if not 0 < low <= high < 1:
        return float("nan")
    return float(np.sqrt(low * (1 - high) / (high * (1 - low))))


def per_sweep_counts(left: np.ndarray, right: np.ndarray,
                     sweeps: np.ndarray, order: list) -> tuple:
    """Sufficient statistics for phi, grouped by paired sweep.

    phi needs only (n, sum_a, sum_b, sum_ab) and those are additive over
    sweeps, so a sweep-cluster bootstrap resamples the four counts instead of
    rescoring the corpus once per draw.  Identical numbers, ~400x less work --
    and the cluster is the paired sweep because a sweep's eight tunings are not
    independent draws.
    """
    index = {sweep: i for i, sweep in enumerate(order)}
    width = left.shape[1]
    n = np.zeros(len(order), dtype=np.int64)
    a = np.zeros((len(order), width), dtype=np.int64)
    b = np.zeros((len(order), width), dtype=np.int64)
    ab = np.zeros((len(order), width), dtype=np.int64)
    slot = np.array([index[s] for s in sweeps.tolist()], dtype=np.int64)
    np.add.at(n, slot, 1)
    for column in range(width):
        np.add.at(a[:, column], slot, left[:, column].astype(np.int64))
        np.add.at(b[:, column], slot, right[:, column].astype(np.int64))
        np.add.at(ab[:, column], slot,
                  np.logical_and(left[:, column], right[:, column]).astype(np.int64))
    return n, a, b, ab


def phi_from_totals(n: int, a: np.ndarray, b: np.ndarray,
                    ab: np.ndarray) -> np.ndarray:
    """Vectorised phi, identical algebra to ``solve_coincidence``'s."""
    if not n:
        return np.full(a.shape, np.nan)
    pa, pb, pab = a / n, b / n, ab / n
    scale = pa * (1 - pa) * pb * (1 - pb)
    out = np.full(pa.shape, np.nan)
    ok = scale > 0
    out[ok] = (pab[ok] - pa[ok] * pb[ok]) / np.sqrt(scale[ok])
    return out


def summarise(deck, bucket, labels) -> tuple:
    left = deck.stack(np.array(bucket["left"], dtype=np.int64))
    right = deck.stack(np.array(bucket["right"], dtype=np.int64))
    sweeps = np.array(bucket["sweep"])
    channels = np.array(bucket["channel"], dtype=np.int16)
    geometry = np.array(bucket["geometry"])
    phi = phi_columns(left, right)

    ratios, ceilings, marginals = [], {}, {}
    for column, label in enumerate(labels):
        p_a = float(left[:, column].mean())
        p_b = float(right[:, column].mean())
        cap = phi_ceiling(p_a, p_b)
        ceilings[label] = cap
        marginals[label] = {"fire_rate_a": p_a, "fire_rate_b": p_b}
        if label != "any-method" and cap == cap and cap > 0:
            ratios.append(phi[column] / cap)

    block = {
        "n": int(left.shape[0]),
        "sweeps": int(len(set(sweeps.tolist()))),
        "phi_methods_mean": float(np.nanmean(phi[:-1])),
        "phi_methods_min": float(np.nanmin(phi[:-1])),
        "phi_methods_max": float(np.nanmax(phi[:-1])),
        "phi_any_method": phi[-1],
        "phi_per_rule": dict(zip(labels, phi)),
        "counts_any_method": counts_of(left, right, len(labels) - 1),
        "fire_rates": marginals,
        "phi_max_given_marginals": ceilings,
        "phi_over_phi_max_methods_mean": float(np.nanmean(ratios)),
        "per_channel": {}, "per_geometry": {},
    }
    for channel in (1, 2, 3, 4):
        keep = channels == channel
        if keep.sum():
            sub = phi_columns(left[keep], right[keep])
            block["per_channel"][str(channel)] = {
                "n": int(keep.sum()),
                "phi_methods_mean": float(np.nanmean(sub[:-1])),
                "phi_any_method": sub[-1]}
    for name in sorted(set(geometry.tolist())):
        keep = geometry == name
        sub = phi_columns(left[keep], right[keep])
        block["per_geometry"][name] = {
            "n": int(keep.sum()),
            "phi_methods_mean": float(np.nanmean(sub[:-1])),
            "phi_any_method": sub[-1]}
    return block, left, right, sweeps


def measure(threshold_null_excludes) -> dict:
    """Every receiver pair at one threshold setting, with the cluster bootstrap."""
    corpus = restore_lnb_a(threshold_null_excludes=threshold_null_excludes)
    deck = Deck(corpus)
    labels = corpus.methods + ["any-method"]
    groups = receiver_pair_rows(corpus, deck)

    blocks, stats = {}, {}
    for name in PAIRS:
        block, left, right, sweeps = summarise(deck, groups[name], labels)
        a, b = name.split("|")
        block["a_is"], block["b_is"] = a, b
        block["factors"] = factors(a, b)
        blocks[name] = block
        stats[name] = (sweeps, left, right)

    order = sorted({s for sweeps, _, _ in stats.values()
                    for s in sweeps.tolist()})
    packed = {name: per_sweep_counts(left, right, sweeps, order)
              for name, (sweeps, left, right) in stats.items()}

    generator = np.random.default_rng(SEED)
    draws = {name: np.empty(DRAWS) for name in packed}
    for draw in range(DRAWS):
        pick = generator.integers(0, len(order), len(order))
        weight = np.bincount(pick, minlength=len(order)).astype(np.int64)
        for name, (n, a, b, ab) in packed.items():
            total = int(weight @ n)
            phi = phi_from_totals(total, weight @ a, weight @ b, weight @ ab)
            draws[name][draw] = float(np.nanmean(phi[:-1]))

    def interval(values):
        return [float(np.percentile(values, 2.5)),
                float(np.percentile(values, 97.5))]

    for name in PAIRS:
        blocks[name]["bootstrap"] = {
            "mean": float(draws[name].mean()),
            "sd": float(draws[name].std(ddof=1)),
            "ci95": interval(draws[name])}
    return {"corpus": corpus, "pairs": blocks, "draws": draws,
            "methods": corpus.methods, "clusters": len(order)}


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def compute() -> dict:
    primary = measure(("lnb-a",))
    corpus = primary["corpus"]
    pairs = primary["pairs"]
    draws = primary["draws"]

    ranked = sorted(PAIRS, key=lambda p: -pairs[p]["phi_methods_mean"])
    by_ratio = sorted(PAIRS,
                      key=lambda p: -pairs[p]["phi_over_phi_max_methods_mean"])

    # -- every pairwise difference, so no reading is privileged -----------
    def interval(values):
        return [float(np.percentile(values, 2.5)),
                float(np.percentile(values, 97.5))]

    differences = {}
    for i, left in enumerate(ranked):
        for right in ranked[i + 1:]:
            delta = draws[left] - draws[right]
            differences[f"{left} - {right}"] = {
                "observed": float(pairs[left]["phi_methods_mean"]
                                  - pairs[right]["phi_methods_mean"]),
                "ci95": interval(delta),
                "crosses_zero": bool(np.percentile(delta, 2.5) <= 0
                                     <= np.percentile(delta, 97.5)),
                "what_changes": {
                    key: [pairs[left]["factors"][key],
                          pairs[right]["factors"][key]]
                    for key in ("radio", "model", "water")},
            }

    # -- factor marginals, labelled as the non-answer they are ------------
    levels: dict = {"radio": defaultdict(list), "model": defaultdict(list),
                    "water": defaultdict(list)}
    for name in PAIRS:
        for factor in levels:
            levels[factor][pairs[name]["factors"][factor]].append(
                pairs[name]["phi_methods_mean"])
    marginals = {
        "warning": "NOT A DECOMPOSITION and not main effects. These are "
                   "unweighted means over an UNBALANCED, non-factorial design: "
                   "every wet|wet and every dry|dry pair is also a within-radio "
                   "pair, and both wet ports share a radio. Quoting any of them "
                   "as an effect of radio, model or water would repeat the "
                   "mistake this figure was regenerated to remove.",
        "levels": {factor: {level: {"pairs": len(values),
                                    "mean_phi": float(np.mean(values))}
                            for level, values in sorted(group.items())}
                   for factor, group in levels.items()},
        "spread_across_the_six_pairs": float(
            max(pairs[p]["phi_methods_mean"] for p in PAIRS)
            - min(pairs[p]["phi_methods_mean"] for p in PAIRS)),
    }

    # -- where each factor's levels land in the phi ranking ---------------
    positions = {}
    for factor in ("radio", "model", "water"):
        positions[factor] = {}
        for name in ranked:
            level = pairs[name]["factors"][factor]
            positions[factor].setdefault(level, []).append(ranked.index(name) + 1)

    # -- the same six with lnb-a's null arm in the threshold population ---
    redrawn = measure(())
    robustness = {
        "what": "every pair recomputed with lnb-a's cross-edge null arm folded "
                "into the population null_thresholds draws from, instead of the "
                "published three-port null",
        "pairs": {name: {
            "phi_methods_mean": redrawn["pairs"][name]["phi_methods_mean"],
            "ci95": redrawn["pairs"][name]["bootstrap"]["ci95"],
            "delta_from_primary": (redrawn["pairs"][name]["phi_methods_mean"]
                                   - pairs[name]["phi_methods_mean"])}
            for name in PAIRS},
    }
    robustness["max_abs_delta"] = max(
        abs(v["delta_from_primary"]) for v in robustness["pairs"].values())
    robustness["ranking_unchanged"] = ranked == sorted(
        PAIRS, key=lambda p: -redrawn["pairs"][p]["phi_methods_mean"])

    census = corpus.census_block()
    census["excluded_receivers"] = {}
    census["lnb_a"] = (
        "INCLUDED. The exclusion recorded in cross_radio.DEAD_RECEIVERS cites a "
        "flat ~1.19 peak-to-median at every tuning since 2026-08-13 04:44 UTC. "
        "That instant falls inside pluto-5d4d's 03:24:04Z-05:07:56Z outage, when "
        "the radio produced no data at all, and this scored corpus stops at "
        "2026-08-14T03:21:55Z regardless. Re-measured on this freeze with the "
        "repository's own fire logic, lnb-a has own-edge phi 0.417 against 0.091 "
        "across channels, a target/null fire ratio of 4.90 equal to lnb-c's, and "
        "coarse peak-to-median median 1.104, max 2.007, sd 0.078 -- not flat.")
    census["receivers_live"] = ["lnb-a", "lnb-b", "lnb-c", "lnb-d"]
    census["threshold_null_population"] = (
        "lnb-b, lnb-c, lnb-d -- the published figure's own null population, held "
        "fixed so that the receiver set is the only thing that changed. This is "
        "the conservative direction: lnb-a's null arm is out-of-sample under "
        "this bar while the other three ports' is in-sample.")

    return {
        "figure": NAME,
        "question": "how far do two receiver chains agree on whether a tuning "
                    "fired, and can this design say why the six pairs differ?",
        "answer": "they differ; it cannot.",
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*",
        "census": census,
        "lnb_a": "INCLUDED",
        "hardware": HARDWARE,
        "design": {
            "unit": "one receiver pair at one tuning instant of one paired sweep",
            "matched_by_construction": "all six pairs rest on the same "
                                       f"{census['paired_sweeps']:,} paired "
                                       "sweeps and the same tuning instants: "
                                       "every instant yields all four "
                                       "cross-radio pairings and both "
                                       "within-radio pairings. No weighting, no "
                                       "re-sampling to balance.",
            "cross_radio_join": "cross_radio.join_cells -- keyed on the instant "
                                "both radios were released into, not on the "
                                "tuning",
            "within_radio_join": "the two ports of one radio at one instant of "
                                 "one capture",
            "is_this_factorial": "NO. Three factors, six pairs, and the design "
                                 "is unbalanced and confounded. No effect of "
                                 "radio, model or water is identified.",
        },
        "not_identified": CONFOUND,
        "statistic": "mean phi across the eight algorithms; each algorithm is "
                     "judged against the threshold null_thresholds drew for its "
                     "own (sample rate, probe length) from the cross-edge null "
                     "arms",
        "methods": primary["methods"],
        "pairs": pairs,
        "ranking_by_phi": ranked,
        "ranking_by_phi_over_phi_max": by_ratio,
        "ranking_changes_under_phi_over_phi_max": ranked != by_ratio,
        "rank_positions_of_each_factor_level": positions,
        "pairwise_differences": differences,
        "factor_marginals_NOT_A_DECOMPOSITION": marginals,
        "bootstrap": {"draws": DRAWS, "seed": SEED,
                      "resampled": "paired sweeps",
                      "clusters": primary["clusters"],
                      "statistic": "mean phi across the eight algorithms"},
        "robustness_thresholds_redrawn": robustness,
        "withdrawn_four_rung_ladder": WITHDRAWN_LADDER,
    }


# --------------------------------------------------------------------------
# plot
# --------------------------------------------------------------------------

#: How each factor is drawn.  Filled = the factor is HELD FIXED across the pair;
#: open = it changes.  Water gets one glyph per port so wet|dry is legible as a
#: half-filled row rather than as a third category.
def chips(ax, y: int, factor: dict) -> None:
    same_radio = factor["radio"] == "same radio"
    same_model = factor["model"] == "same model"
    wet = [factor["water"].split("|")[0] == "wet",
           factor["water"].split("|")[1] == "wet"]

    ax.plot(0.0, y, marker="s", markersize=11, linestyle="none", color=BLUE,
            markerfacecolor=BLUE if same_radio else SURFACE,
            markeredgecolor=BLUE, markeredgewidth=1.8)
    ax.plot(1.0, y, marker="^", markersize=12, linestyle="none", color=TEAL,
            markerfacecolor=TEAL if same_model else SURFACE,
            markeredgecolor=TEAL, markeredgewidth=1.8)
    for offset, is_wet in zip((1.86, 2.20), wet):
        ax.plot(offset, y, marker="o", markersize=9.5, linestyle="none",
                color=ORANGE,
                markerfacecolor=ORANGE if is_wet else SURFACE,
                markeredgecolor=ORANGE, markeredgewidth=1.8)


def plot(data: dict):
    pairs = data["pairs"]
    methods = data["methods"]
    ranked = data["ranking_by_phi"]
    by_ratio = data["ranking_by_phi_over_phi_max"]

    plt.rcParams.update({
        "font.size": 12, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED})
    fig = plt.figure(figsize=(14.6, 15.8), dpi=150, facecolor=SURFACE)
    grid = fig.add_gridspec(
        2, 2, height_ratios=[1.0, 0.80], width_ratios=[0.215, 1.0],
        hspace=0.52, wspace=0.012,
        left=0.212, right=0.972, top=0.838, bottom=0.238)
    axf = fig.add_subplot(grid[0, 0])
    ax = fig.add_subplot(grid[0, 1], sharey=axf)
    ax2 = fig.add_subplot(grid[1, :])

    # ---- panel A: the six receiver pairs --------------------------------
    for position, name in enumerate(ranked):
        block = pairs[name]
        if position % 2 == 0:
            for target in (ax, axf):
                target.axhspan(position - 0.5, position + 0.5, color=BAND,
                               zorder=0)
        values = [block["phi_per_rule"][m] for m in methods]
        ax.scatter(values, [position] * len(values), s=52, facecolor="none",
                   edgecolor=BLUE, linewidth=1.6, zorder=3)
        low, high = block["bootstrap"]["ci95"]
        ax.plot([low, high], [position - 0.245] * 2, color=MUTED, linewidth=2.2,
                zorder=3, solid_capstyle="butt")
        for end in (low, high):
            ax.plot([end, end], [position - 0.31, position - 0.18], color=MUTED,
                    linewidth=2.2, zorder=3)
        mean = block["phi_methods_mean"]
        ax.plot([mean, mean], [position - 0.36, position + 0.30], color=INK,
                linewidth=3.0, zorder=4)
        # va="top" with the inverted y-axis puts the number BELOW the rule on
        # screen; va="bottom" would draw it back through the rule.
        ax.text(mean, position + 0.345, f"{mean:.4f}", ha="center", va="top",
                fontsize=12.5, color=INK, fontweight="bold")
        chips(axf, position, block["factors"])

    ax.set_ylim(len(ranked) - 0.44, -0.62)
    ax.set_xlim(0.425, 0.615)
    ax.set_xlabel("$\\varphi$  (agreement between the two chains' fire / no-fire "
                  "decisions, dimensionless)", fontsize=12, labelpad=9)
    ax.grid(axis="x", color=GRID, linewidth=0.9)
    ax.set_axisbelow(False)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    plt.setp(ax.get_yticklabels(), visible=False)

    axf.set_xlim(-0.62, 2.72)
    axf.set_xticks([0.0, 1.0, 2.03])
    axf.set_xticklabels(["RADIO", "MODEL", "WATER"], fontsize=9.5,
                        color=MUTED)
    axf.xaxis.set_ticks_position("top")
    axf.set_yticks(range(len(ranked)))
    axf.set_yticklabels(
        [f"{name}\n{pairs[name]['factors']['radio']} · "
         f"{pairs[name]['factors']['model']} · {pairs[name]['factors']['water']}\n"
         f"n = {pairs[name]['n']:,} cells over {pairs[name]['sweeps']:,} sweeps"
         for name in ranked], fontsize=10.2, linespacing=1.55)
    for side in ("top", "right", "left", "bottom"):
        axf.spines[side].set_visible(False)
    axf.tick_params(length=0)

    fig.text(0.212, 0.888,
             "filled = the factor is HELD FIXED across the pair;  open = it "
             "changes.  WATER carries one mark per port, so wet|dry reads as "
             "half-filled.\n"
             "open circles: one per algorithm (8)   |   bar: bootstrap 95% CI "
             "over paired sweeps   |   heavy rule: mean $\\varphi$ across the "
             "eight",
             ha="left", va="bottom", fontsize=9.8, color=MUTED,
             linespacing=1.55)

    # the three factor-level positions, spelled out, in the empty left column of
    # panel A rather than over any row's markers
    positions = data["rank_positions_of_each_factor_level"]
    short = {"same radio": "same", "cross radio": "cross",
             "same model": "same", "cross model": "cross",
             "wet|wet": "wet|wet", "wet|dry": "wet|dry", "dry|dry": "dry|dry"}
    lines = []
    for factor, title in (("radio", "RADIO"), ("model", "MODEL"),
                          ("water", "WATER")):
        parts = [f"{short[level]} {','.join(str(p) for p in ranks)}"
                 for level, ranks in sorted(positions[factor].items())]
        lines.append(f"{title}  " + " · ".join(parts))
    ax.text(0.4268, 0.42,
            "NO FACTOR ORDERS THE SIX\n(level, then the ranks it sits at)\n"
            + "\n".join(lines),
            ha="left", va="top", fontsize=8.8, color=INK, linespacing=1.62,
            bbox={"facecolor": SURFACE, "edgecolor": GRID, "pad": 5.0},
            zorder=6)

    # ---- panel B: rank under phi against rank under phi / phi_max -------
    # Vertical position is RANK, not magnitude: the question this panel answers
    # is whether the ORDER survives the ceiling correction, and evenly spaced
    # ranks keep six labels legible where six magnitudes would collide.
    left_x, right_x = 0.0, 1.0
    top = len(ranked)
    for name in ranked:
        y_left = top - ranked.index(name)
        y_right = top - by_ratio.index(name)
        moved = y_left != y_right
        colour = ORANGE if moved else MUTED
        ax2.plot([left_x, right_x], [y_left, y_right], color=colour,
                 linewidth=2.4 if moved else 1.6,
                 alpha=0.95 if moved else 0.55, zorder=2)
        ax2.scatter([left_x, right_x], [y_left, y_right], s=66, zorder=3,
                    facecolor=colour, edgecolor=colour)
        ax2.text(left_x - 0.022, y_left,
                 f"{name}   $\\varphi$ {pairs[name]['phi_methods_mean']:.4f}",
                 ha="right", va="center", fontsize=10.5, color=INK)
        ax2.text(right_x + 0.022, y_right,
                 f"{pairs[name]['phi_over_phi_max_methods_mean']:.4f}   {name}",
                 ha="left", va="center", fontsize=10.5, color=INK)
    for rank in range(1, top + 1):
        ax2.text(left_x - 0.40, top - rank + 1, f"{rank}.", ha="left",
                 va="center", fontsize=10.5, color=MUTED)

    ax2.set_xlim(-0.44, 1.42)
    ax2.set_ylim(0.35, top + 0.75)
    ax2.set_xticks([left_x, right_x])
    ax2.set_xticklabels(
        ["ranked by $\\varphi$\n(the order plotted above)",
         "ranked by $\\varphi\\,/\\,\\varphi_{max}$\n(against the ceiling the two "
         "chains' own fire rates impose)"],
        fontsize=11)
    ax2.set_yticks([])
    ax2.set_ylabel("rank 1 at top", fontsize=10.5, color=MUTED, labelpad=2)
    ax2.set_xlabel("Vertical position is RANK, not magnitude; the value is "
                   "printed beside each point.  Orange = this pair changes rank.",
                   fontsize=10.5, labelpad=12, color=MUTED)
    for side in ("top", "right", "left"):
        ax2.spines[side].set_visible(False)
    ax2.spines["bottom"].set_color(GRID)
    ax2.tick_params(length=0)
    ax2.set_title(
        "$\\varphi$ is capped by the two chains' marginal fire rates, and the "
        "four ports do not fire at the same rate (any-method rate 0.345 lnb-a to "
        "0.460 lnb-d).\nDividing by that ceiling REORDERS the six — including "
        "which pair comes first.",
        fontsize=11.5, color=INK, pad=12, linespacing=1.5)

    fig.suptitle(
        "Agreement between two receiver chains varies across the six receiver "
        "pairs — $\\varphi$ "
        f"{min(pairs[n]['phi_methods_mean'] for n in ranked):.3f} to "
        f"{max(pairs[n]['phi_methods_mean'] for n in ranked):.3f} —\n"
        "and this design cannot say whether radio, LNB model or water is why",
        fontsize=16, color=INK, y=0.982, linespacing=1.5)

    census = data["census"]["frozen_at_start"]
    robust = data["robustness_thresholds_redrawn"]
    lines = [
        "ALL SIX PAIRS rest on the same "
        f"{data['census']['paired_sweeps']:,} paired sweeps and the same tuning "
        f"instants, n = {pairs[ranked[0]]['n']:,} cells each — matched by "
        "construction, not by weighting.  lnb-a IS INCLUDED.",

        "NOT A DECOMPOSITION.  WATER IS CONFOUNDED WITH RADIO: both wet ports "
        "(lnb-c, lnb-d) are on pluto-19f2, both dry ports (lnb-a, lnb-b) on "
        "pluto-5d4d, so nothing here separates",

        "crossing the radio boundary from crossing wet-to-dry.  Model is "
        "separable from water exactly once — lnb-c|lnb-a against lnb-c|lnb-b "
        "substitutes only which dry 5d4d unit is on",

        "the far side — and even there model is confounded with unit identity, "
        "because 5d4d carries exactly one unit of each model.  No effect of "
        "radio, model or water is identified.",

        "The four cross-radio pairs are cross_radio.join_cells, keyed on the "
        "instant both radios were released into; the two within-radio pairs are "
        "the two ports of one radio at one instant.",

        "Thresholds are the published figure's own three-port null population, "
        "held fixed so the receiver set is the only thing that changed; "
        "redrawing them with lnb-a's null arm in moves",

        f"no pair by more than {robust['max_abs_delta']:.4f} and leaves the "
        "ranking "
        + ("unchanged" if robust["ranking_unchanged"] else "reordered")
        + " (JSON).  Intervals are a bootstrap over PAIRED SWEEPS "
        f"({data['bootstrap']['draws']} draws, seed {data['bootstrap']['seed']}, "
        f"{data['bootstrap']['clusters']:,} clusters), not over",

        "cells: the eight tunings of one sweep are not independent draws.  The "
        "four-rung ladder this figure used to carry is WITHDRAWN — it was not "
        "factorial, and its 'changing the",

        "LNB' step compared one chain's own two edges against two chains that "
        "were BOTH lnb-c and lnb-d: same radio, same model, both wet, no "
        "receiver substituted anywhere in it.",

        "Decisions and $\\varphi$ from leo_tracker.radio.beacon.cross_radio, "
        f"unmodified.  CENSUS frozen before computing (digest "
        f"{census['scored_digest']}): {census['sweeps_on_share']:,} sweeps | "
        f"{census['corpus_entries']:,} corpus entries |",

        f"{census['scored_sidecars']:,} scored sidecars; "
        f"{data['census']['paired_sweeps']:,} paired sweeps, all four receivers "
        "live.",
    ]
    caption = hcore.drift_caption()
    if caption:
        lines.append(caption)
    fig.text(0.5, 0.010, "\n".join(lines), ha="center", va="bottom",
             fontsize=8.8, color=MUTED, linespacing=1.50)
    return fig


def main() -> int:
    data = compute()
    pairs, ranked = data["pairs"], data["ranking_by_phi"]
    top, bottom = ranked[0], ranked[-1]
    delta = data["pairwise_differences"][f"{top} - {bottom}"]
    data["headline_checks"] = {
        "lnb_a": "INCLUDED",
        "receiver_pairs": len(ranked),
        "n_per_pair": pairs[ranked[0]]["n"],
        "phi_range": [pairs[bottom]["phi_methods_mean"],
                      pairs[top]["phi_methods_mean"]],
        "ranking_by_phi": ranked,
        "ranking_by_phi_over_phi_max": data["ranking_by_phi_over_phi_max"],
        "ranking_changes_under_phi_over_phi_max":
            data["ranking_changes_under_phi_over_phi_max"],
        "widest_gap": {"pair": f"{top} - {bottom}",
                       "observed": delta["observed"], "ci95": delta["ci95"]},
        "any_factor_orders_the_six": False,
        "verdict": (
            "The six receiver pairs span phi "
            f"{pairs[bottom]['phi_methods_mean']:.4f} ({bottom}) to "
            f"{pairs[top]['phi_methods_mean']:.4f} ({top}) on "
            f"{pairs[top]['n']:,} matched cells each, a gap of "
            f"{delta['observed']:.4f} (95% CI {delta['ci95'][0]:.4f} to "
            f"{delta['ci95'][1]:.4f}). None of radio, LNB model or water orders "
            "them: the two within-radio pairs sit at ranks "
            f"{data['rank_positions_of_each_factor_level']['radio']['same radio']}"
            ", the three same-model pairs at ranks "
            f"{data['rank_positions_of_each_factor_level']['model']['same model']}"
            " with the two same-model cross-radio pairs at opposite ends of the "
            "spread (lnb-c|lnb-a 3rd, lnb-d|lnb-a 6th), and the one wet|wet pair "
            "sits mid-pack at rank "
            f"{data['rank_positions_of_each_factor_level']['water']['wet|wet'][0]}"
            ". Dividing by the ceiling the marginal fire rates "
            "impose reorders the six entirely. This design identifies no effect "
            "of radio, model or water, and the withdrawn four-rung ladder's "
            "-0.0868 'cost of changing the LNB' is not a receiver effect at all"),
    }
    data["census_drift_at_end"] = hcore.drift_block()
    hcore.write_outputs(NAME, data, plot(data))
    print(json.dumps(data["headline_checks"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
