"""Two radios, one sky: the first measurement here that is not self-referential.

Every detector comparison this site has produced could only say *how often a
method fires*.  With no injection there is no ground truth, so "fires more
often" and "has a worse false-alarm rate" arrive as the same number, and
:mod:`survey_comparison` refuses to call either one sensitivity.  This module
adds the axis that was missing.

Two radios now scan the **same tuning at the same instant** — measured skew
median 0.0155 ms, max 0.054 ms at the barrier, which is a lower bound on the
true sample-start offset.  Two chains observing one sky, firing independently,
make a coincidence model identifiable: three measurable rates and a calibrated
empty-sky rate give back an occupancy and *two detection probabilities*.  ``d``
is the quantity an injected signal would have measured.  It is still inferred
from a model rather than measured against a known input, and the report says so
beside every number.

Why the radios and not the receivers
------------------------------------

The same model was run on the two receivers **inside** one radio.  It returned
an occupancy ``f`` of 0.226 to 0.422 depending on which of the eight algorithms
scored it — a factor of 1.9 spread in a parameter that belongs to the sky and
must therefore be identical for all eight.  That disagreement is not noise; it
is the model detecting its own broken assumption.  Two receivers in one Pluto
share an ADC clock, a USB bus and a power rail, so their firings are correlated
through the hardware and the estimator reads that correlation as sky.

Two radios share nothing but the sky: separate LNBs, separate Plutos, separate
USB controllers on separate buses.  Whether ``f`` now agrees across all eight
algorithms was for a while treated as the internal check on this whole
construction, and :func:`format_review` printed that agreement as its headline.

The agreement check is worthless without its negative controls
---------------------------------------------------------------

It is worthless because it was never shown to be able to fail.  Two independent
adversarial reviews rebuilt the same estimate on joins where the coincidence
model is **definitionally false** and got this:

============================================  ==============  ==========
join                                          f               spread
============================================  ==============  ==========
real pairing                                  0.307 - 0.362   0.050
radio A of one sweep + radio B of ANOTHER     0.637 - 0.713   0.075
same sweep, radio B shifted 2 instants        0.622 - 0.667   0.045
============================================  ==============  ==========

The shifted join — two chains that were never looking at the same instant —
produced a *tighter* agreement than the real one.  The reason is measurable: the
eight algorithms correlate with each other at phi 0.82 to 0.94 over the 2160
target observations in this corpus.  They are near-duplicates of one another,
not eight independent witnesses, so their ``f`` values have to agree whatever
they are fed.  A check that passes on data the model cannot possibly fit is not
evidence that the model fits.

So both of those joins are now **built in**.  :func:`negative_controls` rebuilds
them every run, on the same sweeps, through the same thresholds and the same
empty-sky rate ``p``, and :func:`format_review` prints them in the same table as
the real estimate.  The verdict is a statement about *separation*, never about
tightness on its own: unless every control's resampled spread sits clear above
the real join's, the report says the check is vacuous and refuses to certify
anything.  :data:`CONSISTENT_VERDICT` is the only token that may be read as a
pass, and nothing prints it unless the controls separate.

f moves further along four other axes than it does across algorithms
---------------------------------------------------------------------

``f`` belongs to the sky, so it must be constant along *every* axis, and the
algorithm axis is the one with the least variance available.  Measured on these
same cells:

* receiver pair — f(c|b) against f(d|b) is a mean gap of +0.072 with the same
  sign in 8 of 8 algorithms; a cluster bootstrap over 94 sweeps puts p05..p95
  at +0.042..+0.099 with 0 of 300 draws at or below zero.
* arm — 1.25 MS/s at 160 ms gives f 0.015 against 0.401..0.510 at 5.0 MS/s.
* channel — ch1 gives 0.211..0.291 against ch3's 0.381..0.436.
* skew — inside the 0.054 ms design bound f is 0.310..0.372 (n=1054); beyond it
  0.220..0.288 (n=386), and the two intervals do not overlap.

:func:`axis_movement` recomputes those gaps on whatever the corpus currently
holds and prints them beside the algorithm spread.  **None of them is resolved
here.**  The report labels them unresolved in its own headline rather than
leaving a reader to infer that the one axis that was checked is the only one
that matters.

Two geometries, two different questions
---------------------------------------

============  =============================================================
same edge     both radios on one (channel, edge) at one instant.  Cross-radio
              replication on independent hardware.
opposite      radio A on channel N lower while radio B is on channel N upper,
              at the same instant.  Both edges of one channel simultaneously.
============  =============================================================

The second geometry exists because the two edges of a channel used to be
scanned 515 ms apart by one radio, and their detection correlation capped at
phi 0.73 while cross-method agreement on *identical samples* reached 0.97.
Either the gap was the 515 ms, or the two edges genuinely differ.  Scanned
simultaneously by independent hardware, the question is answerable.

The join is by instant, and getting it backwards is silent
-----------------------------------------------------------

On an opposite-order sweep the two radios are at **different** (channel, edge)
at the same index and at the **same** (channel, edge) at different indexes.  A
join that reaches for the matching tuning therefore compares two dwells apart
and calls it simultaneous, inverting the experiment without changing a single
count.  So the join is keyed on the tuning index — the instant both radios were
released into — and the geometry is *derived from the two sample orders* rather
than read off the ``edge_order`` letter, with the declared letter kept beside it
so a disagreement between manifest and data is visible instead of decisive.

lnb-a is dead and is excluded everywhere
-----------------------------------------

``lnb-a`` (rx0 on pluto-5d4d) has read flat ~1.19 at every tuning since 04:44
UTC: no signal path.  A dead receiver never coincides with anything, so leaving
it in drags every occupancy and every detection probability down while looking
exactly like quiet sky — and its *null* is not a null either, it is silence, so
a false-alarm rate measured on it is optimistic in the same direction.  It is
therefore out of the target population and out of the null population alike,
and the count of what was dropped travels in the report.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import random
import textwrap

from .survey_comparison import (DEFAULT_FALSE_ALARM_RATE, MINIMUM_EXCEEDANCES,
                                _at, describe, threshold_from)
from .survey_scoring import SCORES_FILENAME, SCORES_SCHEMA

#: What this aggregate calls itself.
CROSS_RADIO_SCHEMA = "leo-tracker.cross-radio-occupancy/v1"

#: Receivers with no signal path, excluded from target and null alike.
#:
#: **Empty, and the reason it is empty is the point.**  This held ``lnb-a`` for
#: as long as that port read flat ~1.19 at every tuning after 04:44 UTC, which
#: looked exactly like a receiver with nothing connected to it.  It was not.
#: Its local oscillator moved about +566 kHz across the window that brackets
#: pluto-19f2's LNB swap, and the *narrow* acquisition path -- which searches
#: ``arange(-350_000, 350_001, 25_000) + centre`` -- could no longer reach it,
#: so the port stopped producing candidates and was read as dead.
#:
#: This module scores the **survey** path, whose coarse bank spans +/-700 kHz
#: about raw zero and never consults ``receiver_centers``.  lnb-a is inside that
#: bank in both epochs, and on this corpus it fires on 15.52% of its 60,095
#: target points against lnb-b's 17.98% of 59,604 -- a live port, not a silent
#: one.  Excluding it discarded a working receiver and, because the exclusion
#: took its null as well, pulled every threshold that was drawn beside it.
#:
#: Anything produced before this was emptied carries the old exclusion; see
#: ``hardware/epochs.json`` for the per-epoch record and section 14 of the
#: detector-evaluation report for the withdrawal.  Kept as a list rather than
#: deleted because a port really can die, and because a reader has to be able
#: to see which exclusions were in force when a number was produced.
DEAD_RECEIVERS: tuple[str, ...] = ()

#: Where the manifest sits beside the scores.
MANIFEST_FILENAME = "manifest.json"

#: Width of the pilot band the detectors correlate against.
#:
#: The guard is what is left of the captured band once this has to fit inside
#: it: ``rate / 2 - PILOT_BAND_HZ / 2``.  That is -312.5 kHz at 1.25 MS/s (it
#: does not fit at all), +312.5 kHz at 2.5, +1562.5 kHz at 5.0 and +4062.5 kHz
#: at 10.0.
PILOT_BAND_HZ = 1_875_000.0

#: Resamples behind the sampling-noise scale on the f spread, and the seed.
#:
#: The spread across algorithms was watched move 0.099 -> 0.065 -> 0.047 as the
#: corpus grew 30 -> 55 -> 84 pairs, which is what a statistic dominated by
#: sampling noise looks like — and a bare threshold on it changes verdict as the
#: corpus grows.  So the cells are resampled and all eight ``f`` recomputed
#: together, which preserves the correlation between algorithms scoring the same
#: cells and gives the scale the spread has to be read against.  The seed is
#: fixed because a review that moves when nothing moved cannot be compared
#: against yesterday's.
BOOTSTRAP_DRAWS = 200
BOOTSTRAP_SEED = 20260814

#: The maximum skew, in ms, the synchronised capture path was specified at.
#:
#: The rebuilt path was characterised at a median of 0.0155 ms and a maximum of
#: 0.054 ms at the barrier, and "the same instant" is the claim the whole
#: construction rests on.  The corpus contains tunings well outside that, so the
#: count beyond this bound travels in the report instead of being averaged into
#: a median that looks fine.  It is a lower bound either way: the skew is
#: measured at barrier release, not at the first sample.
DESIGN_MAX_SKEW_MS = 0.054

#: All-sky Doppler p99.9, measured, in Hz.
#:
#: On its own this is already 89% of the 2.5 MS/s guard.  Add an LNB bias —
#: lnb-c is measured 604.2 kHz off its nominal centre in this corpus — and a
#: fast satellite on a biased port is outside the guard before it starts.
ALL_SKY_DOPPLER_P999_HZ = 279_000.0

#: Bin edges for the detection-versus-offset curve, in Hz.
#:
#: 312.5 kHz is an edge rather than a bin interior on purpose: it is exactly the
#: 2.5 MS/s guard, so the bin boundary is the physical boundary and a collapse
#: at the guard cannot be smeared across it by the binning.
OFFSET_BINS_HZ = (0.0, 100e3, 200e3, 312.5e3, 500e3, float("inf"))

#: How far the shifted negative control moves the second radio, in instants.
#:
#: Two is the smallest shift that lands on a different tuning in both scan
#: geometries — on a two-edges-per-channel order a shift of one only swaps the
#: edge, which is a geometry the real join already contains and so would not be
#: a control at all.  At two instants the two chains are looking at different
#: sky at different times through the same hardware, which is precisely the
#: situation the coincidence model cannot fit.
CONTROL_SHIFT = 2

#: Smallest population an axis level needs before its f enters the axis table.
#:
#: A level with a handful of cells produces an f that swings by tenths on one
#: extra coincidence, and the axis table exists to compare gaps between levels;
#: a gap dominated by a four-cell level would be noise wearing the axis's name.
AXIS_MIN_CELLS = 24

#: Algorithms that must solve on a level before its f may set an axis gap.
#:
#: The comparison being drawn is against the *across-algorithm* spread, so a
#: level whose f rests on a single algorithm is not a like-for-like extreme: it
#: carries no information about whether the algorithms agree there, and the sign
#: test degenerates to 1 of 1.  Such levels stay in the table — a level where
#: seven of eight algorithms cannot solve is a finding of its own — but they do
#: not form the gap.
AXIS_MIN_METHODS = 2

#: The axes f is checked along besides the algorithm axis, in reporting order.
#:
#: f is a property of the sky and must be constant along all of them.  The
#: algorithm axis is the one with the least variance available — eight
#: near-duplicate scorers on identical samples — so checking only that one is
#: choosing the easiest test in the building.
AXES = ("receiver pair", "arm", "channel", "skew vs design bound", "geometry")

#: The **only** verdict token that may be read as the agreement check passing.
#:
#: It is a module constant so that a test can assert it is absent, and so that
#: a future edit that wants to print a pass has to come through here and meet
#: the separation requirement in :func:`consistency_verdict`.
CONSISTENT_VERDICT = "CONSISTENT — AND EVERY NEGATIVE CONTROL SEPARATES"

#: The verdicts that are not a pass.  Each names why, because "the check did not
#: pass" and "the check cannot pass on this corpus" are different findings.
VACUOUS_VERDICT = "VACUOUS — THE NEGATIVE CONTROLS AGREE JUST AS CLOSELY"
DISAGREE_VERDICT = "ALGORITHMS DISAGREE"
UNTESTED_VERDICT = "NOT VALIDATED — NO USABLE NEGATIVE CONTROL"
UNESTIMATED_VERDICT = "NOT VALIDATED — f NOT ESTIMATED"

#: The header every printed report opens with.  It travels with the numbers
#: because the misreading it prevents would invert them.
PREAMBLE = (
    "Two radios, one sky, one instant. There is still no injection here: d is "
    "inferred from a\ncoincidence model, not measured against a known input. "
    "The model's own consistency check\nis the spread of f across the eight "
    "algorithms — but a check is only worth something if it\nCAN fail, so every "
    "run rebuilds two joins where the model is definitionally false and prints\n"
    "their spread beside the real one. Read the controls before the estimate.")


# --------------------------------------------------------------------------
# reading the paired corpus
# --------------------------------------------------------------------------

def pilot_guard_hz(sample_rate_hz: float) -> float:
    """Room the pilot band has to slide before it leaves the captured band."""
    return float(sample_rate_hz) / 2.0 - PILOT_BAND_HZ / 2.0


def _live(observation: dict) -> bool:
    return observation.get("receiver_label") not in DEAD_RECEIVERS


def _synchronised(manifest: dict) -> dict | None:
    survey = (manifest.get("metadata") or {}).get("pre_dwell_survey")
    if not isinstance(survey, dict):
        return None
    record = survey.get("synchronised_scan")
    return record if isinstance(record, dict) else None


def _entry(path: Path, scores: dict, manifest: dict) -> dict:
    """One radio of one sweep: what the join and the thresholds need from it."""
    survey = manifest["metadata"]["pre_dwell_survey"]
    record = _synchronised(manifest) or {}
    arm = record.get("arm") or {}
    rate = float(scores.get("sample_rate_hz") or manifest.get("sample_rate_hz") or 0.0)
    probe_ms = scores.get("probe_ms")
    if probe_ms is None:
        probe_ms = float(arm.get("probe_s") or 0.0) * 1e3
    return {"capture": scores.get("capture") or path.name,
            "path": str(path),
            "radio_id": scores.get("radio_id") or manifest["identity"]["radio_id"],
            "receiver_labels": list(manifest["identity"].get("receiver_labels") or []),
            "sample_rate_hz": rate, "probe_ms": float(probe_ms),
            "samples_per_tuning": scores.get("samples_per_tuning"),
            "pilot_guard_hz": scores.get("pilot_guard_hz", pilot_guard_hz(rate)),
            "receiver_centers_hz": list(scores.get("receiver_centers_hz") or []),
            "sample_order": [list(item) for item in survey.get("sample_order") or []],
            "edge_order": record.get("edge_order"),
            "arm_name": arm.get("name"),
            "pilot_band_fits": arm.get("pilot_band_fits"),
            "matched_arm": bool(record.get("matched_arm")),
            "paired_sweep": record.get("paired_sweep"),
            "peer_radio": record.get("peer_radio"),
            "skew_ms": record.get("skew_ms") or {},
            "scores": scores}


def threshold_key(entry: dict) -> tuple:
    """What a threshold belongs to.

    Rate and probe length both, never pooled: rate sets the kernel taps and the
    epoch count, length sets the fold depth, and a clean null reaches p99
    1.310 / 1.189 / 1.137 at 20 / 40 / 80 ms.  A threshold drawn across two of
    those is a threshold for neither.
    """
    return (entry["sample_rate_hz"], entry["probe_ms"])


def sweep_geometry(order_a, order_b) -> str:
    """Which geometry two sample orders describe, read from the orders.

    Derived rather than taken from the ``edge_order`` letter so that a manifest
    that disagrees with its own scan order is visible instead of authoritative.
    ``irregular`` is a real answer: two radios that scanned different channel
    sets, or different lengths, answer neither question and are not silently
    forced into one.
    """
    if not order_a or not order_b or len(order_a) != len(order_b):
        return "irregular"
    steps = list(zip(order_a, order_b))
    if all(tuple(left) == tuple(right) for left, right in steps):
        return "same-edge"
    if all(left[0] == right[0] and left[1] != right[1] for left, right in steps):
        return "opposite-edge"
    return "irregular"


def load_pairs(corpus_root, *, limit: int | None = None) -> tuple[list[dict], dict]:
    """Every synchronised sweep whose two radios are both scored, and a census.

    A sweep with one radio scored is not half a result, it is no result: the
    corpus is continuously imported and scored, so at any moment some sweeps
    have one sidecar and will have two in a few minutes.  They are counted as
    ``unpaired_sweeps`` rather than dropped silently, because "the corpus is
    still catching up" and "the pairing is broken" must not print as the same
    thing.
    """
    root = Path(corpus_root)
    census = {"scanned": 0, "read": 0, "unreadable": 0, "no_manifest": 0,
              "other_schema": {}, "not_synchronised": 0, "unpaired_sweeps": 0,
              "irregular_geometry": 0, "beyond_limit": 0}
    grouped: dict[str, list[dict]] = defaultdict(list)
    if not root.is_dir():
        return [], census
    for directory in sorted(root.iterdir()):
        path = directory / SCORES_FILENAME
        if not path.is_file():
            continue
        census["scanned"] += 1
        try:
            scores = json.loads(path.read_text())
        except (OSError, ValueError):
            census["unreadable"] += 1
            continue
        schema = scores.get("schema")
        if schema != SCORES_SCHEMA:
            name = str(schema) if schema else "no schema declared"
            census["other_schema"][name] = census["other_schema"].get(name, 0) + 1
            continue
        manifest_path = directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            census["no_manifest"] += 1
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            census["unreadable"] += 1
            continue
        record = _synchronised(manifest)
        if not record or not record.get("paired_sweep"):
            census["not_synchronised"] += 1
            continue
        census["read"] += 1
        grouped[str(record["paired_sweep"])].append(_entry(directory, scores, manifest))

    pairs = []
    for sweep in sorted(grouped):
        radios = sorted(grouped[sweep], key=lambda item: item["radio_id"])
        if len(radios) != 2:
            census["unpaired_sweeps"] += 1
            continue
        if limit is not None and len(pairs) >= limit:
            census["beyond_limit"] += 1
            continue
        left, right = radios
        geometry = sweep_geometry(left["sample_order"], right["sample_order"])
        if geometry == "irregular":
            census["irregular_geometry"] += 1
            continue
        declared = ("same-edge" if left["edge_order"] == right["edge_order"]
                    else "opposite-edge")
        dropped: dict[str, int] = {}
        for entry in radios:
            for observation in entry["scores"].get("observations") or []:
                label = observation.get("receiver_label")
                if label in DEAD_RECEIVERS:
                    dropped[label] = dropped.get(label, 0) + 1
        skew = left["skew_ms"] or right["skew_ms"]
        pairs.append({"paired_sweep": sweep, "radios": radios,
                      "geometry": geometry, "geometry_declared": declared,
                      "geometry_agrees": geometry == declared,
                      "matched_arm": left["matched_arm"] and right["matched_arm"],
                      "arms": sorted({entry["arm_name"] for entry in radios}),
                      "skew_ms": skew, "excluded_receivers": dropped})
    return pairs, census


# --------------------------------------------------------------------------
# the join
# --------------------------------------------------------------------------

def _side(entry: dict, observation: dict) -> dict:
    return {"capture": entry["capture"], "radio_id": entry["radio_id"],
            "receiver": observation.get("receiver"),
            "receiver_label": observation.get("receiver_label"),
            "instant": observation.get("iq_index"),
            "channel": observation.get("channel"),
            "region": observation.get("region"),
            "edge": observation.get("edge"),
            "sample_rate_hz": entry["sample_rate_hz"],
            "probe_ms": entry["probe_ms"],
            "key": threshold_key(entry), "observation": observation}


def _by_instant(entry: dict, *, target: bool) -> dict:
    ordered: dict = defaultdict(list)
    for observation in entry["scores"].get("observations") or []:
        is_target = observation.get("arm") == "target"
        if is_target is not target or not _live(observation):
            continue
        ordered[observation.get("iq_index")].append(_side(entry, observation))
    return ordered


def arm_label(side: dict) -> str:
    """The (rate, probe length) pair a cell was taken under, as one string."""
    return (f"{float(side['sample_rate_hz']) / 1e6:g} MS/s "
            f"{float(side['probe_ms']):g} ms")


def _cells_between(left: dict, right: dict, meta: dict, *, target: bool,
                   shift: int = 0, skew=None) -> list[dict]:
    """Join two scored radios on the instant index, optionally offset.

    One function for the real join and for both negative controls, so that a
    control cannot drift away from the thing it is a control for: the cell
    construction, the dead-receiver exclusion and the per-side threshold key are
    literally the same code.  ``shift`` is what makes the shifted control a
    control — radio A's instant ``t`` against radio B's instant ``t + shift``,
    which is two chains that were never released together.
    """
    ahead, behind = _by_instant(left, target=target), _by_instant(right, target=target)
    cells = []
    for instant in sorted(ahead, key=lambda value: (value is None, value)):
        peer = instant
        if shift:
            if not isinstance(instant, int):
                continue
            peer = instant + shift
        if peer not in behind:
            continue
        at_instant = skew(instant) if skew is not None else None
        for side_a in ahead[instant]:
            for side_b in behind[peer]:
                cells.append({**meta, "instant": instant, "peer_instant": peer,
                              "skew_ms": at_instant,
                              "receiver_pair": f"{side_a['receiver_label']}|"
                                               f"{side_b['receiver_label']}",
                              "radio_pair": f"{side_a['radio_id']}|"
                                            f"{side_b['radio_id']}",
                              "a": side_a, "b": side_b})
    return cells


def _join(pair: dict, *, target: bool) -> list[dict]:
    """Cross-radio cells, keyed on the instant both radios were released into.

    Not on the tuning.  On an opposite-order sweep the matching tuning is one
    dwell away on the peer, so a tuning-keyed join silently reports two
    different instants as simultaneous — the one mistake that inverts this
    experiment while leaving every count looking reasonable.
    """
    left, right = pair["radios"]
    per_tuning = (pair.get("skew_ms") or {}).get("per_tuning") or []
    median = (pair.get("skew_ms") or {}).get("median")

    def skew_at(instant):
        return (per_tuning[instant]
                if isinstance(instant, int) and 0 <= instant < len(per_tuning)
                else median)

    return _cells_between(left, right, {
        "join": "real", "paired_sweep": pair["paired_sweep"],
        "a_sweep": pair["paired_sweep"], "b_sweep": pair["paired_sweep"],
        "geometry": pair["geometry"], "matched_arm": pair["matched_arm"]},
        target=target, skew=skew_at)


def join_cells(pair: dict) -> list[dict]:
    """Target-arm cells: two independent chains, one tuning instant."""
    return _join(pair, target=True)


def join_null_cells(pair: dict) -> list[dict]:
    """The same join over the cross-edge null arm.

    The null has to be joined the same way as the target or the conjunction
    false-alarm rate it produces belongs to a different experiment.
    """
    return _join(pair, target=False)


# --------------------------------------------------------------------------
# the negative controls: the same join, made definitionally false
# --------------------------------------------------------------------------

def scrambled_cells(pairs: list[dict], *,
                    matched_arm_only: bool = True) -> tuple[list[dict], dict]:
    """Radio A of one sweep against radio B of a **different** sweep.

    The donor is chosen from the same (A arm, B arm, geometry) group and the
    A side keeps its own radio, so the two chains still differ only in the way
    they always did — separate LNBs, separate Plutos — and every count is drawn
    through the same thresholds.  The single thing destroyed is simultaneity:
    these two observations were taken minutes or hours apart, so no occupancy of
    the sky at "the" instant exists for the model to recover.

    Matching the donor on arm and geometry matters.  A donor at another sample
    rate would make the control differ from the real join in two ways at once,
    and a wider spread could then be read as the arm rather than as the broken
    pairing — which is the same conflation the whole module exists to avoid.
    """
    usable = [pair for pair in pairs
              if pair["matched_arm"] or not matched_arm_only]
    groups: dict = defaultdict(list)
    for pair in usable:
        left, right = pair["radios"]
        groups[(threshold_key(left), threshold_key(right),
                pair["geometry"])].append(pair)
    census = {"donor_groups": 0, "sweeps_joined": 0, "sweeps_without_a_donor": 0,
              "shift": None}
    cells: list[dict] = []
    for key in sorted(groups, key=str):
        group = groups[key]
        if len(group) < 2:
            census["sweeps_without_a_donor"] += len(group)
            continue
        census["donor_groups"] += 1
        for index, pair in enumerate(group):
            donor = group[(index + 1) % len(group)]
            census["sweeps_joined"] += 1
            cells.extend(_cells_between(
                pair["radios"][0], donor["radios"][1], {
                    "join": "scrambled",
                    "paired_sweep": pair["paired_sweep"],
                    "a_sweep": pair["paired_sweep"],
                    "b_sweep": donor["paired_sweep"],
                    "geometry": pair["geometry"],
                    "matched_arm": pair["matched_arm"] and donor["matched_arm"]},
                target=True))
    return cells, census


def shifted_cells(pairs: list[dict], *, shift: int = CONTROL_SHIFT,
                  matched_arm_only: bool = True) -> tuple[list[dict], dict]:
    """One sweep, both its own radios, radio B moved ``shift`` instants along.

    Everything the real join has — one sweep, one pair of radios, one arm, one
    calibration — except that the two chains are no longer looking at the same
    moment.  It is the harsher of the two controls precisely because so little
    changes: whatever the real join measures that survives here is not
    simultaneity.
    """
    census = {"donor_groups": None, "sweeps_joined": 0,
              "sweeps_without_a_donor": 0, "shift": shift}
    cells: list[dict] = []
    for pair in pairs:
        if matched_arm_only and not pair["matched_arm"]:
            continue
        left, right = pair["radios"]
        joined = _cells_between(left, right, {
            "join": f"shifted+{shift}", "paired_sweep": pair["paired_sweep"],
            "a_sweep": pair["paired_sweep"], "b_sweep": pair["paired_sweep"],
            "geometry": pair["geometry"], "matched_arm": pair["matched_arm"]},
            target=True, shift=shift)
        if joined:
            census["sweeps_joined"] += 1
        else:
            census["sweeps_without_a_donor"] += 1
        cells.extend(joined)
    return cells, census


# --------------------------------------------------------------------------
# the decision, and what it costs on empty sky
# --------------------------------------------------------------------------

def methods_in(entries: list[dict]) -> list[str]:
    """Every method the sidecars actually scored, in a stable order."""
    seen = set()
    for entry in entries:
        for observation in entry["scores"].get("observations") or []:
            for point in observation.get("points") or []:
                seen.update((point.get("methods") or {}))
    return sorted(seen)


def null_thresholds(entries: list[dict], *,
                    false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """A per-point threshold for each (method, rate, probe length).

    Drawn from the cross-edge null arm — the same IQ scored against the opposite
    edge's code — which :mod:`survey_comparison` establishes as the primary
    null: a wrong-code control free to choose its own epoch re-finds the real
    signal 187 samples away, and a threshold calibrated on that one would have
    the survey firing on 1.8% of sky instead of 21%.

    Dead receivers are out of this population.  Their null is silence, and a
    threshold drawn partly from silence sits too low for the live ports.
    """
    populations: dict = defaultdict(list)
    for entry in entries:
        key = threshold_key(entry)
        for observation in entry["scores"].get("observations") or []:
            if observation.get("arm") == "target" or not _live(observation):
                continue
            for point in observation.get("points") or []:
                for method, values in (point.get("methods") or {}).items():
                    score = values.get("score")
                    if score is not None:
                        populations[(method, key)].append(float(score))
    return {key: threshold_from(values, false_alarm_rate=false_alarm_rate)
            for key, values in populations.items()}


def _threshold_for(thresholds: dict, method: str, key) -> float | None:
    return (thresholds.get((method, key)) or {}).get("threshold")


def observation_fires(observation: dict, method: str,
                      threshold: float | None) -> bool | None:
    """Whether one chain claims a signal at one tuning at one instant.

    The cell is the observation, not the candidate point: each chain maximises
    over its own candidates, and the two radios never share a candidate list, so
    the only unit both of them observed is the tuning instant itself.

    ``None`` where the method has no score here, which is not the same as not
    firing and must not be counted as a quiet cell.
    """
    if threshold is None:
        return None
    scored = False
    for point in observation.get("points") or []:
        value = (point.get("methods") or {}).get(method, {}).get("score")
        if value is None:
            continue
        scored = True
        if float(value) > threshold:
            return True
    return False if scored else None


def cell_false_alarm(entries: list[dict], thresholds: dict) -> dict:
    """p: how often a chain fires on a null cell, per method.

    Per **cell**, not per point.  A cell holds ~7 candidates and the decision
    maximises over them, so the per-cell rate is several times the per-point
    quantile that set the threshold — using the quantile as p would understate
    empty-sky firing by that factor and inflate every occupancy that follows.

    Measured on the same null realisations the threshold was drawn from, which
    makes it an in-sample rate; with the null population this corpus holds that
    is the best available and it is labelled rather than dressed up.
    """
    calibrated: dict = defaultdict(list)
    for method, key in thresholds or {}:
        calibrated[key].append(method)
    tally: dict = defaultdict(lambda: [0, 0])
    for entry in entries:
        key = threshold_key(entry)
        for observation in entry["scores"].get("observations") or []:
            if observation.get("arm") == "target" or not _live(observation):
                continue
            for method in sorted(calibrated.get(key, ())):
                fired = observation_fires(observation, method,
                                          _threshold_for(thresholds, method, key))
                if fired is None:
                    continue
                tally[method][1] += 1
                tally[method][0] += int(fired)
    measured = {}
    for method, (count, cells) in sorted(tally.items()):
        measured[method] = {
            "count": count, "cells": cells,
            "rate": (count / cells) if cells else None,
            "finest_rate": (MINIMUM_EXCEEDANCES / cells) if cells else None,
            "supported": count >= MINIMUM_EXCEEDANCES,
            "basis": "cross-edge null arm, live receivers only, in-sample"}
    return measured


# --------------------------------------------------------------------------
# the estimator
# --------------------------------------------------------------------------

def solve_coincidence(p_a: float, p_b: float, p_ab: float, p: float) -> dict:
    """Occupancy and both detection probabilities from three rates and p.

    The model, for two chains observing the same cell independently::

        P(A)  = f.dA + (1-f).p
        P(B)  = f.dB + (1-f).p
        P(AB) = f.dA.dB + (1-f).p^2

    Eliminating dA and dB leaves a closed form.  Writing ``C`` for the
    coincidence covariance ``P(AB) - P(A)P(B)`` and ``D`` for the product of the
    two single-chain excesses ``(P(A)-p)(P(B)-p)``::

        f = D / (D + C)

    and then ``dA = (P(A) - (1-f)p) / f``, likewise dB.

    **The unsolvable cases are reported, not clamped.**  Two chains that
    coincide *less* often than independent firing predicts (C < 0 large enough)
    put f outside (0, 1]; a chain firing no more often than its own empty-sky
    rate carries no detections to model at all; and some counts imply a
    detection probability above 1.  Each of those is the model saying it does
    not fit these numbers, and a boundary value printed in its place would read
    as a measurement of a sky that is always, or never, occupied.
    """
    covariance = p_ab - p_a * p_b
    # phi is the covariance normalised by what two binary variables of these
    # marginals could reach, and it is computed here rather than inside the fit
    # because it is a property of the three counts: it survives the model
    # failing to fit, which is exactly when a reader most wants to see it.
    scale = p_a * (1.0 - p_a) * p_b * (1.0 - p_b)
    result = {"p_a": p_a, "p_b": p_b, "p_ab": p_ab, "p": p,
              "covariance": covariance,
              "phi": (covariance / scale ** 0.5) if scale > 0 else None,
              "excess_over_p_squared": p_ab - p * p,
              "independent_coincidence": p_a * p_b,
              "f": None, "d_a": None, "d_b": None,
              "solvable": False, "reason": None}
    excess_a, excess_b = p_a - p, p_b - p
    if excess_a <= 0 or excess_b <= 0:
        result["reason"] = ("a chain fires no more often than its own empty-sky "
                            "rate, so it carries no detections to model")
        return result
    product = excess_a * excess_b
    denominator = product + covariance
    if denominator == 0:
        result["reason"] = ("the coincidence excess exactly cancels the "
                            "single-chain excess: f is not identified")
        return result
    f = product / denominator
    if not 0 < f <= 1:
        result["reason"] = (
            "fewer coincidences than independent firing predicts (covariance "
            f"{covariance:+.4f}): no occupancy in (0, 1] fits these counts"
            if covariance < 0 else
            f"more coincidence than any occupancy in (0, 1] admits (f = {f:.3f})")
        return result
    d_a = (p_a - (1.0 - f) * p) / f
    d_b = (p_b - (1.0 - f) * p) / f
    if not (0.0 <= d_a <= 1.0 and 0.0 <= d_b <= 1.0):
        result["reason"] = ("the counts imply a detection probability outside "
                            f"[0, 1] (dA {d_a:.3f}, dB {d_b:.3f})")
        return result
    result.update(f=f, d_a=d_a, d_b=d_b, solvable=True)
    return result


def _rates(cells: list[dict], method: str, thresholds: dict) -> dict:
    fired_a = fired_b = both = usable = 0
    for cell in cells:
        left = observation_fires(cell["a"]["observation"], method,
                                 _threshold_for(thresholds, method, cell["a"]["key"]))
        right = observation_fires(cell["b"]["observation"], method,
                                  _threshold_for(thresholds, method, cell["b"]["key"]))
        if left is None or right is None:
            continue
        usable += 1
        fired_a += int(left)
        fired_b += int(right)
        both += int(left and right)
    return {"cells": usable, "fired_a": fired_a, "fired_b": fired_b,
            "both": both}


def _estimate(counts: dict, p: float | None) -> dict:
    if not counts["cells"] or p is None:
        return {"cells": counts["cells"], "solvable": False,
                "reason": "no usable cells" if not counts["cells"]
                          else "no calibrated empty-sky rate for this method",
                "f": None, "d_a": None, "d_b": None, "phi": None,
                "p_a": None, "p_b": None, "p_ab": None, "p": p}
    n = counts["cells"]
    solved = solve_coincidence(counts["fired_a"] / n, counts["fired_b"] / n,
                               counts["both"] / n, p)
    return {**solved, **counts}


def occupancy(cells: list[dict], thresholds: dict, false_alarm: dict, *,
              methods: list[str] | None = None) -> dict:
    """f, dA and dB per algorithm, pooled and per receiver pair.

    **Discipline: cross-hardware, not leave-one-out.**  The coincidence model
    needs the same statistic on both sides — dA and dB are that statistic's
    detection probability on each chain — so method M does appear on both sides
    of this estimate.  What it does not do is grade itself on its own data: the
    two sides are different LNBs, different Plutos and different USB buses, and
    the only thing they share is the sky.  The leave-one-out discipline is
    applied where it belongs, in :func:`method_roc`.

    Reported per receiver pair as well as pooled because dA and dB are
    properties of a receiver, and pooling two A-side receivers into one dA
    averages a well-centred port with a biased one.
    """
    chosen = methods or sorted({method for cell in cells
                                for point in cell["a"]["observation"].get("points") or []
                                for method in (point.get("methods") or {})})
    by_pair: dict = defaultdict(list)
    for cell in cells:
        by_pair[cell["receiver_pair"]].append(cell)
    report = {"methods": {}, "receiver_pairs": {
        name: len(group) for name, group in sorted(by_pair.items())}}
    for method in chosen:
        p = (false_alarm.get(method) or {}).get("rate")
        pooled = _estimate(_rates(cells, method, thresholds), p)
        pairs = {name: _estimate(_rates(group, method, thresholds), p)
                 for name, group in sorted(by_pair.items())}
        report["methods"][method] = {"pooled": pooled, "pairs": pairs}
    report["f_spread"] = _spread_with_scale(report["methods"], cells,
                                            thresholds, false_alarm, chosen)
    report["by_geometry"] = {}
    for geometry in sorted({cell["geometry"] for cell in cells}):
        subset = [cell for cell in cells if cell["geometry"] == geometry]
        report["by_geometry"][geometry] = {
            "cells": len(subset),
            "methods": {method: _estimate(
                _rates(subset, method, thresholds),
                (false_alarm.get(method) or {}).get("rate"))
                for method in chosen}}
    return report


def bootstrap_f_spread(cells: list[dict], thresholds: dict, false_alarm: dict,
                       methods: list[str], *, observed: float | None = None,
                       draws: int = BOOTSTRAP_DRAWS,
                       seed: int = BOOTSTRAP_SEED) -> dict:
    """The scale sampling noise alone puts on the spread of f across algorithms.

    The eight algorithms score the *same* cells, so their estimates are strongly
    correlated and the spread between them is far tighter than any single
    estimate's own error.  Resampling the cells jointly — one resample, all
    eight recomputed — is what preserves that correlation; bootstrapping each
    algorithm separately would compare them against a scale several times too
    wide and call any disagreement noise.

    **The interval is optimistic about disagreement and the report says so.**
    ``max - min`` over eight estimates is bounded below by zero and biased
    upward by noise, and the two extremes were selected out of the same cells
    that produce the interval.  It is a scale to read the spread against, not a
    test.
    """
    decisions = []
    for cell in cells:
        row = []
        for method in methods:
            left = observation_fires(
                cell["a"]["observation"], method,
                _threshold_for(thresholds, method, cell["a"]["key"]))
            right = observation_fires(
                cell["b"]["observation"], method,
                _threshold_for(thresholds, method, cell["b"]["key"]))
            row.append(None if left is None or right is None else (left, right))
        decisions.append(row)
    rates = [(false_alarm.get(method) or {}).get("rate") for method in methods]
    if not decisions or all(rate is None for rate in rates):
        return {"draws": 0, "observed": observed, "p05": None, "p50": None,
                "p95": None, "basis": "no cells to resample"}
    dice = random.Random(seed)
    size = len(decisions)
    spreads = []
    for _ in range(draws):
        sample = dice.choices(decisions, k=size)
        counts = [[0, 0, 0, 0] for _ in methods]
        for row in sample:
            for index, verdict in enumerate(row):
                if verdict is None:
                    continue
                slot = counts[index]
                slot[0] += 1
                slot[1] += verdict[0]
                slot[2] += verdict[1]
                slot[3] += verdict[0] and verdict[1]
        drawn = []
        for index, (usable, left, right, both) in enumerate(counts):
            if not usable or rates[index] is None:
                continue
            solved = solve_coincidence(left / usable, right / usable,
                                       both / usable, rates[index])
            if solved["solvable"]:
                drawn.append(solved["f"])
        if len(drawn) > 1:
            spreads.append(max(drawn) - min(drawn))
    if not spreads:
        return {"draws": 0, "observed": observed, "p05": None, "p50": None,
                "p95": None,
                "basis": "no resample produced a solvable estimate for two or "
                         "more algorithms"}
    ordered = sorted(spreads)
    return {"draws": len(ordered), "observed": observed,
            "p05": _at(ordered, 0.05), "p50": _at(ordered, 0.50),
            "p95": _at(ordered, 0.95),
            "basis": f"{len(ordered)} joint resamples of the same "
                     f"{size} cells, seed {seed}"}


def f_spread(per_method: dict) -> dict:
    """How far apart the eight algorithms put one sky parameter.

    ``f`` is a property of the sky: eight algorithms scoring the same cells must
    return the same value, and inside one radio they returned 0.226 to 0.422 — a
    factor of 1.9 — which is how that construction was shown to be measuring its
    own shared hardware.

    On its own this number certifies nothing, and it is not the headline any
    more.  The eight algorithms correlate with each other at phi 0.82 to 0.94 on
    these cells, so a small spread is their default output on any input; the
    negative controls in :func:`negative_controls` are what say whether this
    particular small spread carries information.  Read
    :func:`consistency_verdict`, not this.
    """
    values = {method: item["pooled"]["f"] for method, item in per_method.items()
              if item["pooled"].get("solvable")}
    unsolvable = {method: item["pooled"].get("reason")
                  for method, item in per_method.items()
                  if not item["pooled"].get("solvable")}
    if not values:
        return {"methods": 0, "min": None, "max": None, "spread": None,
                "ratio": None, "values": {}, "unsolvable": unsolvable}
    low, high = min(values.values()), max(values.values())
    return {"methods": len(values), "min": low, "max": high,
            "spread": high - low, "ratio": (high / low) if low else None,
            "values": values, "unsolvable": unsolvable}


def _spread_with_scale(per_method: dict, cells: list[dict], thresholds: dict,
                       false_alarm: dict, methods: list[str], *,
                       draws: int = BOOTSTRAP_DRAWS,
                       seed: int = BOOTSTRAP_SEED) -> dict:
    """A spread and the resampled scale it has to be read against, together.

    One function so the real join and both negative controls carry the same
    statistic computed the same way.  A control measured by a second
    implementation would only be a control for that implementation.
    """
    spread = f_spread(per_method)
    spread["sampling"] = bootstrap_f_spread(
        cells, thresholds, false_alarm, methods,
        observed=spread.get("spread"), draws=draws, seed=seed)
    return spread


def spread_of(cells: list[dict], thresholds: dict, false_alarm: dict,
              methods: list[str], *, draws: int = BOOTSTRAP_DRAWS,
              seed: int = BOOTSTRAP_SEED) -> dict:
    """The f spread across algorithms on one join, with its resampled scale."""
    per_method = {
        method: {"pooled": _estimate(_rates(cells, method, thresholds),
                                     (false_alarm.get(method) or {}).get("rate"))}
        for method in methods}
    return _spread_with_scale(per_method, cells, thresholds, false_alarm,
                              methods, draws=draws, seed=seed)


def negative_controls(pairs: list[dict], thresholds: dict, false_alarm: dict,
                      methods: list[str], *, shift: int = CONTROL_SHIFT,
                      draws: int = BOOTSTRAP_DRAWS,
                      seed: int = BOOTSTRAP_SEED) -> list[dict]:
    """The same estimate, on two joins where the coincidence model is false.

    This exists because the agreement check was never shown to be capable of
    failing.  Both reviews that attacked it rebuilt exactly these two joins and
    found the eight algorithms agreeing as closely as on the real pairing —
    0.075 on the scrambled join and 0.045 on the shifted one against 0.050 on
    the real one, the shifted control agreeing *more* closely than the truth.

    A control has to be run through the identical pipeline or it proves nothing
    about that pipeline, so both use :func:`_cells_between`, the thresholds the
    real estimate uses and the same per-method empty-sky rate ``p``.  Only the
    pairing is broken, and it is broken in a way the model cannot absorb: no
    occupancy of the sky "at the instant" exists when the two chains were never
    at one instant together.
    """
    scrambled, scrambled_census = scrambled_cells(pairs)
    shifted, shifted_census = shifted_cells(pairs, shift=shift)
    built = [
        {"name": "scrambled",
         "join": "radio A of one sweep + radio B of a DIFFERENT sweep",
         "matched_on": "arm and geometry; only the pairing is broken",
         "broken": "the two chains never observed the same sky at the same "
                   "moment, so there is no occupancy at 'the' instant to find",
         "cells": scrambled, "census": scrambled_census},
        {"name": f"shifted +{shift}",
         "join": f"same sweep, same two radios, radio B moved {shift} instants",
         "matched_on": "sweep, radios, arm, calibration; only the instant is "
                       "wrong",
         "broken": f"radio A's instant t is joined to radio B's instant "
                   f"t+{shift}: same hardware, different moment",
         "cells": shifted, "census": shifted_census}]
    controls = []
    for item in built:
        cells = item.pop("cells")
        controls.append({**item, "cell_count": len(cells),
                         "f_spread": spread_of(cells, thresholds, false_alarm,
                                               methods, draws=draws, seed=seed)})
    return controls


def consistency_verdict(real: dict, controls: list[dict]) -> dict:
    """Whether the f-agreement check discriminates at all on this corpus.

    **The verdict is about separation, never about tightness.**  A tight spread
    on the real join is evidence only if the same eight algorithms come apart on
    a join the model cannot fit; if they stay tight there too, the tightness is
    a property of the algorithms — they correlate at phi 0.82 to 0.94 here — and
    says nothing about the sky or about independence.

    A control counts as separating when the whole middle 90% of its resampled
    spread sits above the whole middle 90% of the real join's.  Comparing two
    point estimates would call a 0.075 against 0.050 a separation when both
    intervals span 0.03 to 0.11; comparing the intervals is the weakest claim
    the numbers actually support.  Where a resample could not be formed the
    fallback is the bare comparison, and ``basis`` says which was used so a
    reader is never guessing.

    **Every** control has to separate.  One definitionally-false join that the
    check passes is a demonstration that the check passes on anything, and an
    average over controls would let the other one hide it.
    """
    spread = real.get("spread")
    real_high = (real.get("sampling") or {}).get("p95")
    assessed = []
    for control in controls:
        measured = control["f_spread"]
        control_spread = measured.get("spread")
        control_low = (measured.get("sampling") or {}).get("p05")
        if control_spread is None or spread is None:
            separates, basis = None, ("no f could be estimated on this join, "
                                      "so it cannot act as a control")
        elif control_low is None or real_high is None:
            separates = control_spread > spread
            basis = "point spreads only: no resampled interval on both sides"
        else:
            separates = control_low > real_high
            basis = (f"resampled p05 {control_low:.3f} against the real join's "
                     f"p95 {real_high:.3f}")
        assessed.append({
            "name": control["name"], "spread": control_spread,
            "cells": control["cell_count"], "separates": separates,
            "basis": basis,
            "tighter_than_real": (control_spread is not None and spread is not None
                                  and control_spread <= spread)})
    usable = [item for item in assessed if item["separates"] is not None]
    failed = [item["name"] for item in usable if not item["separates"]]
    # A control that could not be estimated is not a control that passed, and it
    # is not a control that failed either: it leaves the check untested, which
    # is its own answer and must not collapse into either of the other two.
    if not real.get("methods"):
        verdict, vacuous = UNESTIMATED_VERDICT, False
    elif (real.get("ratio") or 1.0) >= 1.5:
        verdict, vacuous = DISAGREE_VERDICT, False
    elif failed:
        verdict, vacuous = VACUOUS_VERDICT, True
    elif not usable or len(usable) != len(assessed):
        verdict, vacuous = UNTESTED_VERDICT, False
    else:
        verdict, vacuous = CONSISTENT_VERDICT, False
    tighter = [item["name"] for item in assessed if item["tighter_than_real"]]
    return {"verdict": verdict, "vacuous": vacuous,
            "certified": verdict == CONSISTENT_VERDICT,
            "controls": assessed, "controls_that_failed": failed,
            "controls_tighter_than_real": tighter,
            "real_spread": spread,
            "rule": "every negative control's resampled f spread must sit "
                    "clear above the real join's before a tight real spread "
                    "may be read as evidence of anything"}


# --------------------------------------------------------------------------
# the axes f is not allowed to move along either
# --------------------------------------------------------------------------

def cell_axes(cell: dict) -> dict:
    """Every axis one cell can be filed under, for the movement table.

    ``None`` where a cell cannot be placed — an unmeasured skew, a missing
    channel — so that absence drops out of the axis rather than becoming a level
    called "None" that then gets compared against real ones.
    """
    skew = cell.get("skew_ms")
    channel = cell["a"].get("channel")
    return {
        "receiver pair": cell.get("receiver_pair"),
        "arm": arm_label(cell["a"]),
        "channel": None if channel is None else f"ch{channel}",
        "skew vs design bound": (
            None if skew is None else
            f"within {DESIGN_MAX_SKEW_MS:g} ms" if skew <= DESIGN_MAX_SKEW_MS
            else f"beyond {DESIGN_MAX_SKEW_MS:g} ms"),
        "geometry": cell.get("geometry")}


def axis_movement(cells: list[dict], thresholds: dict, false_alarm: dict,
                  methods: list[str], *, algorithm_spread: float | None = None,
                  minimum_cells: int = AXIS_MIN_CELLS,
                  minimum_methods: int = AXIS_MIN_METHODS) -> dict:
    """How far f moves along every other axis, measured on the same cells.

    ``f`` is a property of the sky.  It must be identical for every algorithm —
    the check the report used to lead with — but equally for every receiver
    pair, every arm, every channel and either side of the skew bound.  The
    algorithm axis is eight near-duplicate scorers reading identical samples,
    which is the least variance available anywhere in this corpus, so passing
    there and stopping is choosing the easiest test in the building.

    Each level's f is the median over the algorithms that solve on it, and the
    axis's ``gap`` is the distance between the extreme levels' medians.
    ``concordant`` is the reviewers' sign test: of the algorithms that solve on
    both extremes, how many order them the same way.  A gap that 8 of 8
    algorithms agree on cannot be dismissed as the noise the same 8 algorithms'
    disagreement was dismissed as.
    """
    report: dict = {"algorithm_spread": algorithm_spread,
                    "minimum_cells": minimum_cells,
                    "minimum_methods": minimum_methods, "axes": {}}
    # Placed once rather than once per axis: five axes over a corpus-sized cell
    # list is five passes of dict-building for one pass of information.
    placed = [(cell, cell_axes(cell)) for cell in cells]
    for axis in AXES:
        grouped: dict = defaultdict(list)
        for cell, levels_of in placed:
            level = levels_of.get(axis)
            if level is not None:
                grouped[level].append(cell)
        levels = {}
        for name in sorted(grouped, key=str):
            group = grouped[name]
            values = {}
            for method in methods:
                estimate = _estimate(
                    _rates(group, method, thresholds),
                    (false_alarm.get(method) or {}).get("rate"))
                if estimate.get("solvable"):
                    values[method] = estimate["f"]
            ordered = sorted(values.values())
            levels[str(name)] = {
                "cells": len(group), "methods": len(values), "values": values,
                "min": ordered[0] if ordered else None,
                "max": ordered[-1] if ordered else None,
                "f": _at(ordered, 0.50) if ordered else None,
                "counted": (len(values) >= minimum_methods
                            and len(group) >= minimum_cells)}
        counted = {name: item for name, item in levels.items()
                   if item["counted"]}
        entry = {"levels": levels, "counted": len(counted), "gap": None,
                 "low": None, "high": None, "concordant": None,
                 "compared": None, "ratio_to_algorithm": None,
                 "wider_than_algorithm": False, "unanimous": False}
        if len(counted) >= 2:
            # Ordered on (f, name) so the two extremes are always two *different*
            # levels: with every level on the same f, ``min`` and ``max`` both
            # return the first key and the table would compare a level with
            # itself while printing a gap of zero as though it meant something.
            ranked = sorted(counted, key=lambda name: (counted[name]["f"], name))
            bottom, top = counted[ranked[0]], counted[ranked[-1]]
            gap = top["f"] - bottom["f"]
            shared = [method for method in bottom["values"]
                      if method in top["values"]]
            concordant = sum(1 for method in shared
                             if top["values"][method] > bottom["values"][method])
            entry.update(
                gap=gap, low=ranked[0], high=ranked[-1], compared=len(shared),
                concordant=concordant,
                ratio_to_algorithm=(gap / algorithm_spread)
                                   if algorithm_spread else None,
                wider_than_algorithm=(algorithm_spread is not None
                                      and gap > algorithm_spread),
                # A gap smaller than the algorithm spread is not thereby noise.
                # Every algorithm ordering the two extremes the same way is the
                # reviewers' sign test, and it is what separates a systematic
                # movement from a wobble: they measured 8 of 8 on the receiver
                # pair with 0 of 300 cluster-bootstrap draws at or below zero,
                # on a gap of the same size as the across-algorithm spread that
                # was being called agreement.
                unanimous=(len(shared) >= minimum_methods
                           and concordant == len(shared) and gap > 0))
        report["axes"][axis] = entry
    def _by_gap(name):
        return -(report["axes"][name]["gap"] or 0.0)
    wider = sorted((name for name, item in report["axes"].items()
                    if item["wider_than_algorithm"]), key=_by_gap)
    unanimous = sorted((name for name, item in report["axes"].items()
                        if item["unanimous"]), key=_by_gap)
    report["wider_than_algorithm"] = wider
    report["unanimous"] = unanimous
    report["moves"] = sorted(set(wider) | set(unanimous), key=_by_gap)
    report["unresolved"] = bool(report["moves"])
    report["why"] = ("f belongs to the sky and must not move along ANY of these "
                     "axes. Movement along them is measured here — by size "
                     "against the across-algorithm spread, and by whether every "
                     "algorithm orders the extremes the same way — and is NOT "
                     "resolved by anything in this report")
    return report


# --------------------------------------------------------------------------
# leave-one-out, across the hardware boundary
# --------------------------------------------------------------------------

def confirmed_by_others(side: dict, method: str, thresholds: dict,
                        methods: list[str]) -> bool | None:
    """Whether anything *other than* ``method`` fired on this observation.

    A cell only M fires on is M's claim, not evidence for M.  Leaving M in its
    own confirmation is what lets a method that agrees with nobody report
    perfect recall, and that is precisely the circularity the cross-radio
    construction exists to escape.
    """
    verdicts = [observation_fires(side["observation"], other,
                                  _threshold_for(thresholds, other, side["key"]))
                for other in methods if other != method]
    usable = [item for item in verdicts if item is not None]
    if not usable:
        return None
    return any(usable)


def method_roc(cells: list[dict], thresholds: dict, *,
               methods: list[str] | None = None) -> dict:
    """Recall and false-alarm against the peer radio's leave-one-out verdict.

    **Discipline: cross-hardware and leave-one-out, both.**  Method M decides on
    radio A; the cell counts as a positive only if at least one of the *other
    seven* methods fired on radio B's independent observation of the same tuning
    at the same instant.  Both directions are scored, so every cell contributes
    twice with the roles swapped.

    Cost is carried but the ranking is on detection: the operator has said
    plainly that computational cost is not what this question is about.  The
    fire-rate ranking has already inverted once under an edge-correlation test —
    differential-32 fires 2.6x more often than anchor-8 and confirms the same
    ~1,050 channels — so a ranking by how often a method speaks is not a ranking
    by what it knows.
    """
    chosen = methods or sorted({method for cell in cells
                                for point in cell["a"]["observation"].get("points") or []
                                for method in (point.get("methods") or {})})
    tally = {method: {"positives": 0, "hits": 0, "negatives": 0,
                      "false_alarms": 0, "cells": 0, "cost_ms": []}
             for method in chosen}
    radio_pairs: dict = defaultdict(set)
    for cell in cells:
        for decider, judge in (("a", "b"), ("b", "a")):
            side, peer = cell[decider], cell[judge]
            for method in chosen:
                fired = observation_fires(
                    side["observation"], method,
                    _threshold_for(thresholds, method, side["key"]))
                proxy = confirmed_by_others(peer, method, thresholds, chosen)
                if fired is None or proxy is None:
                    continue
                slot = tally[method]
                slot["cells"] += 1
                radio_pairs[method].add(cell["radio_pair"])
                cost = _cost_ms(side["observation"], method)
                if cost is not None:
                    slot["cost_ms"].append(cost)
                if proxy:
                    slot["positives"] += 1
                    slot["hits"] += int(fired)
                else:
                    slot["negatives"] += 1
                    slot["false_alarms"] += int(fired)
    report = {}
    for method, slot in sorted(tally.items()):
        costs = sorted(slot["cost_ms"])
        report[method] = {
            "cells": slot["cells"], "positives": slot["positives"],
            "negatives": slot["negatives"], "hits": slot["hits"],
            "false_alarm_count": slot["false_alarms"],
            "recall": (slot["hits"] / slot["positives"]) if slot["positives"] else None,
            "false_alarm": (slot["false_alarms"] / slot["negatives"])
                           if slot["negatives"] else None,
            "ms_per_cell": _at(costs, 0.50) if costs else None,
            "cost_basis": ("scorer's own per-point elapsed_ms, summed over the "
                           "cell's candidates" if costs else
                           "not timed by the scorer"),
            "discipline": "cross-hardware + leave-one-out",
            "radio_pairs": sorted(radio_pairs.get(method, ()))}
    return report


def _cost_ms(observation: dict, method: str) -> float | None:
    total, seen = 0.0, False
    for point in observation.get("points") or []:
        value = (point.get("methods") or {}).get(method, {}).get("elapsed_ms")
        if value is None:
            continue
        seen = True
        total += float(value)
    return total if seen else None


# --------------------------------------------------------------------------
# the guard band
# --------------------------------------------------------------------------

def guard_band_curve(entries: list[dict], thresholds: dict, *,
                     methods: list[str] | None = None) -> dict:
    """Detection rate against measured frequency offset, split by sample rate.

    **By rate, never by sample count.**  The two alias badly — 80 ms at
    2.5 MS/s and 160 ms at 1.25 MS/s are both 200,000 samples, and 160 ms at
    10 MS/s and 640 ms at 2.5 MS/s are both 1,600,000 — while the guard depends
    only on the rate: -312.5 kHz at 1.25 MS/s, +312.5 at 2.5, +1562.5 at 5.0,
    +4062.5 at 10.0.  Binned by count, a rate whose pilot band never fits at all
    averages with one that has 1.5 MHz of headroom.

    The x-axis is the candidate's offset **within its own receiver's captured
    band**, which is what the band edge clips: the LNB bias physically moves the
    signal inside the ADC band, so a biased port spends its guard before Doppler
    does.  The bias-corrected offset travels beside it as the sky-Doppler
    reading, since those are different quantities and only one of them is what
    the guard sees.
    """
    chosen = methods or methods_in(entries)
    buckets: dict = defaultdict(lambda: {
        "n": 0, "fired": defaultdict(int), "offsets": [], "corrected": []})
    ports: dict = defaultdict(lambda: {
        "n": 0, "fired": defaultdict(int), "offsets": [], "corrected": []})
    centre_of: dict = {}
    shape: dict = defaultdict(lambda: {"samples": set(), "probe_ms": set(),
                                       "fits": set(), "n": 0,
                                       "receivers": set()})
    for entry in entries:
        rate, key = entry["sample_rate_hz"], threshold_key(entry)
        guard = pilot_guard_hz(rate)
        shape[rate]["samples"].add(entry.get("samples_per_tuning"))
        shape[rate]["probe_ms"].add(entry["probe_ms"])
        shape[rate]["fits"].add(bool(entry.get("pilot_band_fits")))
        centres = entry.get("receiver_centers_hz") or []
        for observation in entry["scores"].get("observations") or []:
            if observation.get("arm") != "target" or not _live(observation):
                continue
            index = observation.get("receiver")
            bias = centres[index] if isinstance(index, int) and index < len(centres) else 0.0
            label = observation.get("receiver_label")
            shape[rate]["receivers"].add(label)
            centre_of[(rate, label)] = float(bias or 0.0)
            for point in observation.get("points") or []:
                offset = point.get("cfo_hz")
                if offset is None:
                    continue
                raw = abs(float(offset))
                low, high = _bin_of(raw)
                band = "beyond" if raw >= guard else "inside"
                targets = (buckets[(rate, low, high)], ports[(rate, label, band)])
                for bucket in targets:
                    bucket["n"] += 1
                    bucket["offsets"].append(raw)
                    bucket["corrected"].append(abs(float(offset) - float(bias or 0.0)))
                shape[rate]["n"] += 1
                for method in chosen:
                    threshold = _threshold_for(thresholds, method, key)
                    if threshold is None:
                        continue
                    score = (point.get("methods") or {}).get(method, {}).get("score")
                    if score is not None and float(score) > threshold:
                        for bucket in targets:
                            bucket["fired"][method] += 1
    by_rate = {}
    for rate in sorted(shape):
        guard = pilot_guard_hz(rate)
        bins = []
        for low, high in zip(OFFSET_BINS_HZ, OFFSET_BINS_HZ[1:]):
            bucket = buckets.get((rate, low, high))
            count = bucket["n"] if bucket else 0
            fired = dict(bucket["fired"]) if bucket else {}
            bins.append({
                # The open-ended bin's edge is null rather than infinity: JSON
                # has no infinity, and Python writing ``Infinity`` produces a
                # document that fails in every strict reader there is.
                "low": low, "high": None if high == float("inf") else high,
                "n": count,
                "beyond_guard": low >= guard,
                "median_offset_hz": (_at(sorted(bucket["offsets"]), 0.5)
                                     if count else None),
                "median_corrected_hz": (_at(sorted(bucket["corrected"]), 0.5)
                                        if count else None),
                "fired": {method: fired.get(method, 0) for method in chosen},
                "rate": {method: (fired.get(method, 0) / count) if count else None
                         for method in chosen}})
        by_receiver = {}
        for label in sorted(shape[rate]["receivers"]):
            by_receiver[label] = {
                "centre_hz": centre_of.get((rate, label), 0.0),
                **{band: _band_row(ports.get((rate, label, band)), chosen)
                   for band in ("inside", "beyond")}}
        by_rate[rate] = {
            "guard_hz": guard,
            "pilot_band_fits": guard >= 0,
            "samples_per_tuning": sorted(
                value for value in shape[rate]["samples"] if value is not None),
            "probe_ms": sorted(shape[rate]["probe_ms"]),
            "points": shape[rate]["n"], "bins": bins,
            "by_receiver": by_receiver,
            "beyond_guard_receivers": {
                label: item["beyond"]["n"] for label, item in by_receiver.items()
                if item["beyond"]["n"]}}
    return {"by_rate": by_rate,
            "bins_hz": [None if edge == float("inf") else edge
                        for edge in OFFSET_BINS_HZ],
            "doppler_p999_hz": ALL_SKY_DOPPLER_P999_HZ,
            "x_axis": "candidate offset within the receiver's own captured band, "
                      "which is what the band edge clips",
            "confound": "raw offset is confounded with the receiver: an LNB bias "
                        "moves the signal inside the ADC band, so a biased port "
                        "populates the far bins on its own. by_receiver is what "
                        "separates a guard effect from a port effect"}


def _band_row(bucket: dict | None, methods: list[str]) -> dict:
    """One receiver's points on one side of its own rate's guard."""
    count = bucket["n"] if bucket else 0
    fired = dict(bucket["fired"]) if bucket else {}
    return {"n": count,
            "median_offset_hz": (_at(sorted(bucket["offsets"]), 0.5)
                                 if count else None),
            "median_corrected_hz": (_at(sorted(bucket["corrected"]), 0.5)
                                    if count else None),
            "fired": {method: fired.get(method, 0) for method in methods},
            "rate": {method: (fired.get(method, 0) / count) if count else None
                     for method in methods}}


def _bin_of(offset: float) -> tuple:
    for low, high in zip(OFFSET_BINS_HZ, OFFSET_BINS_HZ[1:]):
        if low <= offset < high:
            return low, high
    return OFFSET_BINS_HZ[-2], OFFSET_BINS_HZ[-1]


# --------------------------------------------------------------------------
# the channel-instant verdict
# --------------------------------------------------------------------------

def _any_method_fires(side: dict, thresholds: dict, methods: list[str]) -> bool | None:
    verdicts = [observation_fires(side["observation"], method,
                                  _threshold_for(thresholds, method, side["key"]))
                for method in methods]
    usable = [item for item in verdicts if item is not None]
    if not usable:
        return None
    return any(usable)


def channel_instant_verdicts(pairs: list[dict], thresholds: dict, *,
                             methods: list[str] | None = None) -> dict:
    """Was channel N broadcasting at instant T — a channel question, not a cell one.

    Opposite-order sweeps answer it directly: one radio on the lower edge and
    the other on the upper edge of the *same channel* at the *same instant*, on
    hardware that shares nothing.  The verdict is the conjunction of the two
    radios' independent decisions, and its false-alarm rate is **measured on
    null cells joined the same way** rather than taken as the product of two
    per-cell rates, because that product assumes exactly the independence this
    whole exercise is trying to establish rather than assume.
    """
    chosen = methods or sorted({
        method for pair in pairs for entry in pair["radios"]
        for observation in entry["scores"].get("observations") or []
        for point in observation.get("points") or []
        for method in (point.get("methods") or {})})
    opposite = [pair for pair in pairs if pair["geometry"] == "opposite-edge"]
    verdicts = []
    for pair in opposite:
        grouped: dict = defaultdict(list)
        for cell in join_cells(pair):
            grouped[cell["instant"]].append(cell)
        for instant in sorted(grouped, key=lambda value: (value is None, value)):
            group = grouped[instant]
            confirmed = 0
            usable = 0
            for cell in group:
                left = _any_method_fires(cell["a"], thresholds, chosen)
                right = _any_method_fires(cell["b"], thresholds, chosen)
                if left is None or right is None:
                    continue
                usable += 1
                confirmed += int(left and right)
            first = group[0]
            verdicts.append({
                "paired_sweep": pair["paired_sweep"], "instant": instant,
                "channel": first["a"]["channel"],
                "edges": sorted({first["a"]["edge"], first["b"]["edge"]}),
                "radios": sorted({first["a"]["radio_id"], first["b"]["radio_id"]}),
                "geometry": pair["geometry"], "skew_ms": first["skew_ms"],
                "receiver_pairs": usable, "pairs_confirmed": confirmed,
                "confirmed": confirmed > 0})
    count = cells = 0
    for pair in opposite:
        grouped = defaultdict(list)
        for cell in join_null_cells(pair):
            grouped[cell["instant"]].append(cell)
        for instant, group in grouped.items():
            hits = 0
            usable = 0
            for cell in group:
                left = _any_method_fires(cell["a"], thresholds, chosen)
                right = _any_method_fires(cell["b"], thresholds, chosen)
                if left is None or right is None:
                    continue
                usable += 1
                hits += int(left and right)
            if usable:
                cells += 1
                count += int(hits > 0)
    return {"geometry": "opposite-edge", "sweeps": len(opposite),
            "rule": "any of the scored methods on each radio, both radios",
            "verdicts": verdicts,
            "confirmed": sum(1 for item in verdicts if item["confirmed"]),
            "false_alarm": {
                "count": count, "cells": cells,
                "rate": (count / cells) if cells else None,
                "finest_rate": (MINIMUM_EXCEEDANCES / cells) if cells else None,
                "supported": count >= MINIMUM_EXCEEDANCES,
                "basis": "measured on cross-edge null cells"}}


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def review(corpus_root, *, limit: int | None = None,
           false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """Everything above, assembled over whatever the corpus currently holds."""
    pairs, census = load_pairs(corpus_root, limit=limit)
    entries = [entry for pair in pairs for entry in pair["radios"]]
    methods = methods_in(entries)
    thresholds = null_thresholds(entries, false_alarm_rate=false_alarm_rate)
    false_alarm = cell_false_alarm(entries, thresholds)
    cells = [cell for pair in pairs for cell in join_cells(pair)]
    matched = [cell for cell in cells if cell["matched_arm"]]
    dropped: dict = {}
    for pair in pairs:
        for label, count in (pair["excluded_receivers"] or {}).items():
            dropped[label] = dropped.get(label, 0) + count
    skews = [value for pair in pairs
             for value in ((pair.get("skew_ms") or {}).get("per_tuning") or [])]
    report = {
        "schema": CROSS_RADIO_SCHEMA,
        "corpus": str(corpus_root),
        "false_alarm_rate": false_alarm_rate,
        "census": census,
        "methods": methods,
        "excluded_receivers": {"labels": list(DEAD_RECEIVERS),
                               "observations_dropped": dropped,
                               "reason": ("no receiver is excluded; lnb-a's "
                                          "exclusion was withdrawn once its "
                                          "flat reading was traced to a "
                                          "+566 kHz oscillator move that only "
                                          "blinds the narrow path")
                                         if not DEAD_RECEIVERS else
                                         "no signal path"},
        "pairs": {
            "joined": len(pairs),
            "same_edge": sum(1 for pair in pairs if pair["geometry"] == "same-edge"),
            "opposite_edge": sum(1 for pair in pairs
                                 if pair["geometry"] == "opposite-edge"),
            "matched_arm": sum(1 for pair in pairs if pair["matched_arm"]),
            "geometry_disagrees": sum(1 for pair in pairs
                                      if not pair["geometry_agrees"]),
            "arms": sorted({name for pair in pairs for name in pair["arms"]
                            if name}),
            "skew_ms": describe(skews),
            "skew_beyond_design": {
                "design_max_ms": DESIGN_MAX_SKEW_MS,
                "tunings": sum(1 for value in skews
                               if value is not None and value > DESIGN_MAX_SKEW_MS),
                "of": len(skews),
                "sweeps": sum(1 for pair in pairs
                              if any(value > DESIGN_MAX_SKEW_MS for value in
                                     ((pair.get("skew_ms") or {}).get("per_tuning") or [])))}},
        "cells": {
            "joined": len(cells),
            "matched_arm": len(matched),
            "same_edge": sum(1 for cell in cells if cell["geometry"] == "same-edge"),
            "opposite_edge": sum(1 for cell in cells
                                 if cell["geometry"] == "opposite-edge"),
            "receiver_pairs": {name: sum(1 for cell in cells
                                         if cell["receiver_pair"] == name)
                               for name in sorted({cell["receiver_pair"]
                                                   for cell in cells})}},
        "false_alarm": {
            "per_cell": false_alarm,
            "per_point_rate": false_alarm_rate,
            "thresholds": [{"method": method, "sample_rate_hz": key[0],
                            "probe_ms": key[1], **value}
                           for (method, key), value in sorted(thresholds.items())]},
        "occupancy": occupancy(matched, thresholds, false_alarm, methods=methods),
        "roc": method_roc(matched, thresholds, methods=methods),
        "guard_band": guard_band_curve(entries, thresholds, methods=methods),
        "channel_instants": channel_instant_verdicts(pairs, thresholds,
                                                     methods=methods)}
    report["occupancy"]["population"] = {
        "cells": len(matched), "excluded_unmatched_arm": len(cells) - len(matched),
        "why": "the model carries one p, so the two chains have to have been "
               "running the same arm; unmatched-arm sweeps are joined and "
               "counted but left out of the estimate"}
    # The controls are not optional and are not a flag.  The agreement check
    # above passes on data the model cannot fit, so it is only ever reported
    # alongside two joins that prove whether it can fail at all.
    spread = report["occupancy"]["f_spread"]
    controls = negative_controls(pairs, thresholds, false_alarm, methods)
    report["occupancy"]["controls"] = controls
    report["occupancy"]["consistency"] = consistency_verdict(spread, controls)
    report["occupancy"]["axes"] = axis_movement(
        matched, thresholds, false_alarm, methods,
        algorithm_spread=spread.get("spread"))
    return report


def _cell(value, spec: str = "8.3f", missing: str = "       -") -> str:
    if value is None:
        return missing
    return format(value, spec)


def _range_of(spread: dict) -> str:
    """One algorithm-spread as ``low..high``, or why there is none."""
    if not spread.get("methods"):
        return "not estimable"
    return f"{spread['min']:.3f}..{spread['max']:.3f}"


def _interval_of(spread: dict) -> str:
    sampling = spread.get("sampling") or {}
    if not sampling.get("draws"):
        return "no resample"
    return f"{sampling['p05']:.3f}..{sampling['p95']:.3f}"


def _paragraph(text: str, *, indent: str = "  ", hanging: str | None = None,
               width: int = 94) -> list[str]:
    """Wrap one sentence-run to the report's column, keeping the indent.

    ``hanging`` indents every line after the first, which is what keeps a
    ``<--`` marker pointing at the row above it while its sentences line up
    under each other rather than under the arrow.
    """
    return textwrap.wrap(" ".join(text.split()), width=width,
                         initial_indent=indent,
                         subsequent_indent=hanging or indent) or []


def headline(report: dict) -> list[str]:
    """The one thing a reader must not be able to miss.

    It is the *verdict on the check*, not the check's own number.  The spread of
    f across algorithms used to lead this report and was read as validation; it
    is a number the negative controls reproduce on data the model cannot fit, so
    leading with it is how a reader was misled in the first place.
    """
    consistency = (report.get("occupancy") or {}).get("consistency") or {}
    axes = (report.get("occupancy") or {}).get("axes") or {}
    verdict = consistency.get("verdict")
    if not verdict:
        return []
    lines = [f"HEADLINE — f-AGREEMENT CHECK: {verdict}"]
    failed = consistency.get("controls_that_failed") or []
    tighter = consistency.get("controls_tighter_than_real") or []
    if consistency.get("vacuous"):
        lines += _paragraph(
            "The check passes on joins where the coincidence model is "
            f"definitionally false ({', '.join(failed)}), so it is not testing "
            "the model. It CANNOT be read as evidence that the independence "
            "assumption holds.")
        if tighter:
            lines += _paragraph(
                f"{', '.join(tighter)} "
                + ("agrees" if len(tighter) == 1 else "agree")
                + " MORE closely than the real pairing does.")
    elif verdict == CONSISTENT_VERDICT:
        lines += _paragraph(
            "Every definitionally-false join came apart and the real one did "
            "not, so the check can fail and did not. Necessary, not sufficient: "
            "d below is still a model output with no injection behind it.")
    elif verdict == DISAGREE_VERDICT:
        lines += _paragraph(
            "The eight algorithms do not agree on one sky parameter, so the "
            "model's independence assumption is not met and every d below is a "
            "model output, not a measurement.")
    else:
        lines += _paragraph(
            "Nothing here shows the check is capable of failing, so the spread "
            "of f certifies nothing.")
    if axes.get("unresolved"):
        moves = axes.get("moves") or []
        wider = axes.get("wider_than_algorithm") or []
        # Only the axes the size test missed are worth naming twice: an axis
        # already called wider does not become a second finding by also being
        # unanimous, and repeating it reads as two axes where there is one.
        only_unanimous = [name for name in (axes.get("unanimous") or [])
                          if name not in wider]
        lines += _paragraph(
            "UNRESOLVED, and separately: f moves along "
            + (f"{len(moves)} other axis" if len(moves) == 1
               else f"{len(moves)} other axes")
            + f" ({', '.join(moves)}) — "
            + (f"further than across algorithms on {', '.join(wider)}"
               if wider else "none of them further than across algorithms")
            + (f"; ordered the same way by every algorithm on "
               f"{', '.join(only_unanimous)}" if only_unanimous else "")
            + ". f belongs to the sky and must not move along any of them. "
              "Nothing in this report resolves that.", hanging="      ")
    return lines


def _control_block(report: dict) -> list[str]:
    """The real estimate and both negative controls, in one table.

    Side by side and in the same units, because the whole failure this section
    exists to prevent was a number being read on its own.
    """
    occupancy_report = report.get("occupancy") or {}
    controls = occupancy_report.get("controls") or []
    consistency = occupancy_report.get("consistency") or {}
    spread = occupancy_report.get("f_spread") or {}
    estimated = (occupancy_report.get("population") or {}).get("cells", 0)
    lines = ["",
             "  NEGATIVE CONTROLS — the same sweeps, the same thresholds, the "
             "same p, joined so that the",
             "  coincidence model is DEFINITIONALLY FALSE. A tight f spread on "
             "the real join is evidence only if",
             "  these come apart from it. Rebuilt every run; there is no flag "
             "that turns them off.",
             f"    {'join':<14} {'cells':>7} {'f range':>15} {'spread':>8}"
             f" {'resample p05..p95':>18}   separates?"]
    lines.append(
        f"    {'real':<14} {estimated:>7}"
        f" {_range_of(spread):>15}"
        f" {_cell(spread.get('spread'), '8.3f', '       -')}"
        f" {_interval_of(spread):>18}   --")
    assessed = {item["name"]: item for item in consistency.get("controls") or []}
    for control in controls:
        measured = control["f_spread"]
        judged = assessed.get(control["name"]) or {}
        separates = judged.get("separates")
        note = ("--" if separates is None else "yes" if separates else "NO")
        if judged.get("tighter_than_real"):
            note += "  (TIGHTER than the real join)"
        lines.append(
            f"    {control['name']:<14} {control['cell_count']:>7}"
            f" {_range_of(measured):>15}"
            f" {_cell(measured.get('spread'), '8.3f', '       -')}"
            f" {_interval_of(measured):>18}   {note}")
    for control in controls:
        census = control.get("census") or {}
        orphaned = census.get("sweeps_without_a_donor") or 0
        lines += _paragraph(
            f"{control['name']}: {control['join']} — matched on "
            f"{control['matched_on']}. {census.get('sweeps_joined', 0)} sweep(s) "
            "joined"
            + (f", {orphaned} left out for want of a donor" if orphaned else "")
            + ".", indent="      ")
    for item in consistency.get("controls") or []:
        lines.append(f"      {item['name']} separation basis: {item['basis']}")
    lines.append(f"  >>> VERDICT: {consistency.get('verdict', UNTESTED_VERDICT)}")
    failed = consistency.get("controls_that_failed") or []
    scorers = spread.get("methods") or len(spread.get("values") or {})
    if consistency.get("vacuous"):
        # Named rather than summarised as "neither": one control separating and
        # the other not is a different, and much more interesting, state than
        # both failing, and a fixed sentence would print the wrong one of them.
        lines += _paragraph(
            f"{', '.join(failed)} destroy{'s' if len(failed) == 1 else ''} the "
            "simultaneity the model is built on, and the "
            f"{scorers} algorithms agree on f just as closely there as here. "
            "The agreement above is therefore a property of the algorithms, not "
            "of the sky: they correlate with one another at phi 0.82-0.94 over "
            "these observations, so they are near-duplicates rather than "
            f"{scorers} independent witnesses and their f values have to agree "
            "whatever they are fed.", indent="      ")
        lines += _paragraph(
            "NOTHING BELOW IS VALIDATED BY THE SPREAD ABOVE. d stays what it "
            "was: a model output, with no injection behind it and now no "
            "working check either.", indent="      ")
    elif consistency.get("certified"):
        lines += _paragraph(
            "Every definitionally-false join produced a wider f spread than the "
            "real pairing, with resampled intervals clear of it. The check can "
            "fail and did not fail. That is necessary and not sufficient: d is "
            "still inferred from the model, not measured against a known input.",
            indent="      ")
    elif consistency.get("verdict") == UNTESTED_VERDICT:
        lines += _paragraph(
            "Not every definitionally-false join could be formed and estimated "
            "from this corpus, so there is no evidence the check is able to "
            "fail. The spread above certifies nothing until every control "
            "separates.", indent="      ")
    lines += _paragraph(f"rule: {consistency.get('rule', '')}", indent="      ")
    return lines


def _axis_block(report: dict) -> list[str]:
    """Where f moves, next to the one axis the check was run on."""
    axes = (report.get("occupancy") or {}).get("axes") or {}
    if not axes.get("axes"):
        return []
    spread = (report.get("occupancy") or {}).get("f_spread") or {}
    lines = ["",
             "  WHERE f ACTUALLY MOVES — f is a property of the sky, so it must "
             "be constant along EVERY axis.",
             "  The algorithm axis checked above is the one with the LEAST "
             "variance available: eight scorers",
             "  reading identical samples, correlated with each other at phi "
             "0.82-0.94.",
             f"    {'axis':<22} {'levels':>7} {'cells':>7} {'f low..high':>15}"
             f" {'gap':>7} {'vs algos':>9} {'same sign':>10}"]
    solved = spread.get("methods", 0)
    scored = solved + len(spread.get("unsolvable") or {})
    estimated = ((report.get("occupancy") or {}).get("population") or {}).get(
        "cells", 0)
    lines.append(
        f"    {'algorithm':<22} {f'{solved}/{scored}':>7} {estimated:>7}"
        f" {_range_of(spread):>15}"
        f" {_cell(spread.get('spread'), '7.3f', '      -')}"
        f" {'checked':>9} {'--':>10}")
    ordered = sorted(axes["axes"].items(),
                     key=lambda item: -(item[1]["gap"] or -1.0))
    for axis, item in ordered:
        levels = item["levels"]
        counted = {name: level for name, level in levels.items()
                   if level["counted"]}
        cells = sum(level["cells"] for level in counted.values())
        span = ("too few levels" if item["gap"] is None else
                f"{counted[item['low']]['f']:.3f}..{counted[item['high']]['f']:.3f}")
        ratio = ("-" if item["ratio_to_algorithm"] is None
                 else f"{item['ratio_to_algorithm']:.1f}x")
        sign = ("-" if item["compared"] in (None, 0)
                else f"{item['concordant']}/{item['compared']}")
        flags = []
        if item["wider_than_algorithm"]:
            flags.append("WIDER THAN THE ALGORITHM AXIS")
        if item["unanimous"]:
            flags.append("EVERY ALGORITHM ORDERS IT THE SAME WAY")
        lines.append(
            f"    {axis:<22} {f'{len(counted)}/{len(levels)}':>7} {cells:>7}"
            f" {span:>15} {_cell(item['gap'], '7.3f', '      -')} {ratio:>9}"
            f" {sign:>10}"
            + ("   " + ", ".join(flags) if flags else ""))
        if item["gap"] is None:
            continue
        for role, name in (("lowest ", item["low"]), ("highest", item["high"])):
            level = counted[name]
            lines.append(
                f"        {role} {name:<24} n={level['cells']:<6} f "
                f"{level['f']:.3f}   algorithms "
                f"{level['min']:.3f}..{level['max']:.3f} over "
                f"{level['methods']}")
    moves = axes.get("moves") or []
    if moves:
        lines += _paragraph(
            "<-- UNRESOLVED: f moves along "
            + (f"{len(moves)} of these axes" if len(moves) > 1
               else "one of these axes")
            + f" ({', '.join(moves)}). The consistency check was run on the "
              "axis with the least variance available.", hanging="      ")
        lines += _paragraph(
            "'same sign' counts the algorithms that order the two extreme "
            "levels the same way. A gap SMALLER than the across-algorithm "
            "spread is not thereby noise: one that every algorithm orders the "
            "same way is systematic, and it is exactly the pattern the "
            "reviewers pinned on the receiver pair — mean +0.072, 8 of 8 "
            "algorithms, 0 of 300 cluster-bootstrap draws at or below zero.",
            indent="      ")
        lines += _paragraph(
            "NOTHING IN THIS REPORT RESOLVES ANY OF THIS MOVEMENT.",
            indent="      ")
    else:
        lines.append("  no other axis moves f on the cells now in the corpus: "
                     "none is wider than the algorithm axis and none is ordered "
                     "unanimously.")
    lines += _paragraph(
        f"levels shows counted/found: a level under {axes.get('minimum_cells')} "
        "cells, or one that fewer than "
        f"{axes.get('minimum_methods')} algorithms can solve, is found and "
        "listed but left out of the gap — the first swings by tenths on one "
        "extra coincidence and the second has no across-algorithm reading to "
        "compare against the axis above. A gap is only formed where two levels "
        "survive both cuts.", indent="      ")
    if not spread.get("spread"):
        lines += _paragraph(
            "the algorithm spread is zero on these cells, so 'vs algos' has no "
            "denominator and every other axis is wider than it by construction; "
            "the gaps themselves are still the numbers to read.",
            indent="      ")
    return lines


def format_review(report: dict) -> str:
    """The cross-radio result as text, with every caveat beside its number."""
    lines = [PREAMBLE, ""]
    lines.extend(headline(report))
    if len(lines) > 2:
        lines.append("")
    pairs, cells = report["pairs"], report["cells"]
    if not pairs["joined"]:
        census = report["census"]
        lines.append("no paired sweep has both radios scored yet: "
                     f"{census['read']} synchronised sidecars read, "
                     f"{census['unpaired_sweeps']} sweeps still waiting for "
                     "their peer to be scored")
        if census["other_schema"]:
            lines.append("  " + "; ".join(
                f"{count} sidecar(s) at {name}, another schema and not counted"
                for name, count in sorted(census["other_schema"].items())))
        return "\n".join(lines)

    skew = pairs["skew_ms"]
    lines.append(
        f"pairs {pairs['joined']} (same-edge {pairs['same_edge']}, "
        f"opposite-edge {pairs['opposite_edge']}; matched arms "
        f"{pairs['matched_arm']})   cells {cells['joined']} "
        f"(same-edge {cells['same_edge']}, opposite-edge "
        f"{cells['opposite_edge']})")
    lines.append(
        f"receiver pairs " + ", ".join(
            f"{name} n={count}" for name, count in cells["receiver_pairs"].items())
        + f"   arms {len(pairs['arms'])}")
    dropped = report["excluded_receivers"]["observations_dropped"]
    if dropped or report["excluded_receivers"]["labels"]:
        lines.append(
            "excluded " + (", ".join(f"{label} ({count} observations)"
                                     for label, count in sorted(dropped.items()))
                           or ", ".join(report["excluded_receivers"]["labels"]))
            + f": {report['excluded_receivers']['reason']}. Out of target AND "
              "null, because a dead port's null is silence and would pull every "
              "threshold down with it")
    else:
        # Said rather than omitted: a run with no exclusions and a run whose
        # exclusion line was dropped look identical in a log otherwise, and this
        # corpus has already been read both ways.
        lines.append("no receivers excluded: "
                     f"{report['excluded_receivers']['reason']}")
    lines.append(
        f"skew median {_cell(skew.get('p50'), '.4f', 'n/a')} ms, max "
        f"{_cell(skew.get('max'), '.4f', 'n/a')} ms over {skew.get('count', 0)} "
        "tunings  <-- measured at barrier release, a LOWER BOUND on the true "
        "sample-start offset")
    beyond = pairs.get("skew_beyond_design") or {}
    if beyond.get("tunings"):
        lines.append(
            f"  <-- {beyond['tunings']} of {beyond['of']} tunings, in "
            f"{beyond['sweeps']} sweep(s), exceed the "
            f"{beyond['design_max_ms']:g} ms the synchronised path was "
            "specified at. 'Same instant' is the")
        lines.append("      claim everything below rests on, and on those "
                     "tunings it is weaker than the design says.")
    if pairs["geometry_disagrees"]:
        lines.append(f"  <-- {pairs['geometry_disagrees']} sweep(s) whose "
                     "declared edge_order disagrees with their own scan order; "
                     "the scan order is what was used")
    lines.append("")

    # ---- the headline -------------------------------------------------
    spread = report["occupancy"]["f_spread"]
    population = report["occupancy"]["population"]
    lines.append("OCCUPANCY f — one sky parameter, eight algorithms, and they "
                 "must agree")
    lines.append(f"  estimated on {population['cells']} matched-arm cells"
                 + (f" ({population['excluded_unmatched_arm']} unmatched-arm "
                    "cells joined but left out: the model carries one p)"
                    if population["excluded_unmatched_arm"] else ""))
    lines.append("  discipline: cross-hardware. The same method decides on both "
                 "radios, which share only the sky —")
    lines.append("  not leave-one-out, because the model needs one statistic on "
                 "both sides. See ROC below for that.")
    lines.append("  this pools both geometries, and they do not mean quite the "
                 "same thing: on a same-edge cell the")
    lines.append("  two chains observe ONE tuning, while on an opposite-edge "
                 "cell a coincidence means both edges of")
    lines.append("  the channel were live at once. Per-geometry values are "
                 "below; they are what to read if the two")
    lines.append("  disagree.")
    consistency = report["occupancy"].get("consistency") or {}
    if spread["methods"]:
        sampling = spread.get("sampling") or {}
        lines.append(
            f"  f spans {spread['min']:.3f} to {spread['max']:.3f} across "
            f"{spread['methods']} algorithms: spread {spread['spread']:.3f}, "
            f"ratio {spread['ratio']:.2f}x")
        if sampling.get("draws"):
            lines.append(
                f"  resampling the same cells {sampling['draws']}x puts the "
                f"spread at {sampling['p05']:.3f}..{sampling['p95']:.3f} "
                f"(median {sampling['p50']:.3f}) from sampling alone. The eight")
            lines.append(
                "  are resampled together, so the scale keeps the correlation "
                "of scoring one set of cells. It is")
            lines.append(
                "  optimistic about disagreement: max-minus-min cannot go below "
                "zero and its two extremes were")
            lines.append(
                "  picked out of the same cells. A scale to read the spread "
                "against, not a test.")
        else:
            lines.append("  no resampled scale for the spread: "
                         + str(sampling.get("basis", "not computed")))
    else:
        lines.append("  f could not be estimated for any algorithm; the reasons "
                     "are per method below")
    for method, reason in sorted((spread.get("unsolvable") or {}).items()):
        lines.append(f"      {method}: unsolvable — {reason}")
    lines.extend(_control_block(report))
    lines.extend(_axis_block(report))
    if consistency.get("verdict") == DISAGREE_VERDICT:
        lines.append(
            "  f belongs to the SKY. Algorithms disagreeing about it means the "
            "model's independence assumption")
        lines.append(
            "  is not met, so every d below is a model output, not a "
            "measurement. For scale: the two receivers")
        lines.append(
            "  inside one radio spanned 0.226 to 0.422, a ratio of 1.87x.")
    lines.append("")
    lines.append(f"{'method':>19} {'cells':>6} {'P(A)':>7} {'P(B)':>7}"
                 f" {'P(AB)':>7} {'p':>7} {'P(AB)-p^2':>10} {'f':>7}"
                 f" {'dA':>7} {'dB':>7}")
    for method, item in sorted(report["occupancy"]["methods"].items()):
        pooled = item["pooled"]
        lines.append(
            f"{method:>19} {pooled.get('cells', 0):>6}"
            f" {_cell(pooled.get('p_a'), '7.3f', '      -')}"
            f" {_cell(pooled.get('p_b'), '7.3f', '      -')}"
            f" {_cell(pooled.get('p_ab'), '7.3f', '      -')}"
            f" {_cell(pooled.get('p'), '7.3f', '      -')}"
            f" {_cell(pooled.get('excess_over_p_squared'), '10.4f', '         -')}"
            f" {_cell(pooled.get('f'), '7.3f', '      -')}"
            f" {_cell(pooled.get('d_a'), '7.3f', '      -')}"
            f" {_cell(pooled.get('d_b'), '7.3f', '      -')}")
    lines.append("  p is the per-CELL empty-sky rate measured on the cross-edge "
                 "null arm at the same decision")
    lines.append("  the targets get (max over the cell's candidates), not the "
                 "per-point quantile that set the")
    lines.append("  threshold — and it is in-sample, drawn from the same null "
                 "realisations. N per method:")
    lines.append("    " + "; ".join(
        f"{method} {item['count']}/{item['cells']}"
        for method, item in sorted(report["false_alarm"]["per_cell"].items())))
    lines.append("  dA and dB are DETECTION PROBABILITIES — the quantity "
                 "injection would have measured —")
    lines.append("  but inferred from the model above rather than from a known "
                 "input. If f disagrees, so do they.")
    lines.append("")

    # ---- per receiver -------------------------------------------------
    lines.append("DETECTION PROBABILITY per receiver pair — dA and dB belong to "
                 "a port, not to a corpus")
    lines.append(f"{'method':>19} {'pair':>15} {'cells':>6} {'f':>7} {'dA':>7}"
                 f" {'dB':>7}   note")
    for method, item in sorted(report["occupancy"]["methods"].items()):
        for name, estimate in sorted(item["pairs"].items()):
            note = "" if estimate.get("solvable") else (estimate.get("reason") or "")
            lines.append(
                f"{method:>19} {name:>15} {estimate.get('cells', 0):>6}"
                f" {_cell(estimate.get('f'), '7.3f', '      -')}"
                f" {_cell(estimate.get('d_a'), '7.3f', '      -')}"
                f" {_cell(estimate.get('d_b'), '7.3f', '      -')}   {note}")
    lines.append("")

    # ---- geometry -----------------------------------------------------
    lines.append("BY GEOMETRY — same-edge is replication; opposite-edge is both "
                 "edges of one channel at one instant")
    phis = {}
    for geometry, item in sorted(report["occupancy"]["by_geometry"].items()):
        solved = {method: value["f"] for method, value in item["methods"].items()
                  if value.get("solvable")}
        agreed = [value["phi"] for value in item["methods"].values()
                  if value.get("phi") is not None]
        phis[geometry] = agreed
        span = (f"f {min(solved.values()):.3f}..{max(solved.values()):.3f} over "
                f"{len(solved)} algorithms" if solved else
                "f not estimable on this geometry alone")
        lines.append(
            f"  {geometry:>14}  cells {item['cells']:>4}  {span}"
            + (f"   phi {min(agreed):.3f}..{max(agreed):.3f}" if agreed else
               "   phi not computable"))
    lines.append("  The 515 ms question: the two edges of one channel used to be "
                 "scanned two dwells apart and")
    lines.append("  their detection correlation capped at phi 0.73 while "
                 "cross-method agreement on identical")
    lines.append("  samples reached 0.97. Both were measured INSIDE one radio "
                 "and are not directly comparable to")
    lines.append("  anything here: a shared Pluto, ADC clock and USB bus "
                 "inflate agreement, which is")
    lines.append("  the whole reason this module crossed the hardware boundary. "
                 "The like-for-like comparison is the")
    lines.append("  two rows above — same hardware boundary, same instant, "
                 "differing only in geometry.")
    both = [geometry for geometry in ("same-edge", "opposite-edge")
            if phis.get(geometry)]
    if len(both) == 2:
        same = sum(phis["same-edge"]) / len(phis["same-edge"])
        opposite = sum(phis["opposite-edge"]) / len(phis["opposite-edge"])
        lines.append(
            f"  Simultaneous cross-radio phi averages {opposite:.3f} across "
            f"edges against {same:.3f} on one edge:")
        lines.append(
            f"  the edge costs {same - opposite:+.3f} with the scan gap removed "
            "entirely, so the edge gap is sky or"
            if opposite < same else
            f"  the edge costs {same - opposite:+.3f} with the scan gap removed "
            "entirely, so the geometry is not what")
        lines.append(
            "  hardware rather than scan latency."
            if opposite < same else "  was capping agreement.")
        lines.append(
            "  Neither reaches 1.0, and it cannot: with dA and dB near 0.8 the "
            "two chains miss different cells,")
        lines.append(
            "  so phi is bounded well below 1 by detection probability alone "
            "before any sky effect enters.")
    elif both:
        lines.append(f"  only {both[0]} sweeps are scored on both radios, so the "
                     "two geometries cannot be compared yet.")
    lines.append("")

    # ---- guard band ---------------------------------------------------
    lines.append("GUARD BAND — detection rate against measured frequency "
                 "offset, split by SAMPLE RATE")
    lines.append("  the guard is rate/2 - 937.5 kHz, so it is a property of the "
                 "rate alone; sample COUNT aliases")
    lines.append("  across rates (200,000 samples is both 80 ms at 2.5 MS/s and "
                 "160 ms at 1.25) and must not be")
    lines.append("  used to bin this. x is the candidate's offset inside its own "
                 "receiver's captured band, which")
    lines.append("  is what the band edge clips; all-sky Doppler p99.9 is "
                 f"{ALL_SKY_DOPPLER_P999_HZ / 1e3:.0f} kHz on its own.")
    for rate, item in sorted(report["guard_band"]["by_rate"].items()):
        fits = ("pilot band does NOT fit at all" if not item["pilot_band_fits"]
                else f"guard {item['guard_hz'] / 1e3:+.1f} kHz")
        lines.append(
            f"  {rate / 1e6:>5.2f} MS/s  {fits}   probe "
            + "/".join(f"{value:g}ms" for value in item["probe_ms"])
            + "   samples " + "/".join(f"{value:,}" for value in
                                       item["samples_per_tuning"])
            + f"   points {item['points']}")
        header = f"      {'offset kHz':>16} {'n':>6}  " + " ".join(
            f"{_abbreviate(method):>6}" for method in report["methods"])
        lines.append(header)
        for entry in item["bins"]:
            high = ("inf" if entry["high"] is None
                    else f"{entry['high'] / 1e3:g}")
            label = f"{entry['low'] / 1e3:g}-{high}"
            flag = " *" if entry["beyond_guard"] else "  "
            row = f"      {label:>14}{flag} {entry['n']:>6}  " + " ".join(
                _cell(entry["rate"].get(method), '6.1%', '     -')
                for method in report["methods"])
            lines.append(row)
        for label, port in sorted(item["by_receiver"].items()):
            for band in ("inside", "beyond"):
                if not port[band]["n"]:
                    continue
                lines.append(
                    f"      {label} {band:>6} guard, centre "
                    f"{port['centre_hz'] / 1e3:+7.1f} kHz  n={port[band]['n']:>4}"
                    "  " + " ".join(
                        _cell(port[band]["rate"].get(method), '6.1%', '     -')
                        for method in report["methods"]))
        share = item.get("beyond_guard_receivers") or {}
        if share:
            total = sum(share.values())
            top, count = max(share.items(), key=lambda pair: pair[1])
            if count / total >= 0.5:
                lines.append(
                    f"      <-- {count} of {total} beyond-guard candidates at "
                    f"this rate are on {top} alone: at this rate the far bins "
                    "are")
                lines.append("          mostly one port, so they read as a "
                             "PORT effect at least as much as a guard effect.")
    lines.append("  * bin lies entirely beyond this rate's guard: the pilot band "
                 "is clipped there.")
    lines.append("  Prediction under test: the curve collapses past ~312 kHz at "
                 "2.5 MS/s and stays flat at 5 and 10.")
    lines.append("  Read the per-receiver rows before the bins: raw offset is "
                 "confounded with the port, because an")
    lines.append("  LNB bias moves the signal inside the ADC band. A biased port "
                 "spends its guard before Doppler does,")
    lines.append("  and it also populates the far bins on its own.")
    lines.append("")

    # ---- ROC ----------------------------------------------------------
    lines.append("PER-ALGORITHM ROC against the peer radio's LEAVE-ONE-OUT "
                 "verdict")
    lines.append("  discipline: cross-hardware AND leave-one-out. M decides on "
                 "one radio; a cell counts as")
    lines.append("  positive only if one of the OTHER SEVEN fired on the other "
                 "radio's own observation of the")
    lines.append("  same tuning at the same instant. Ranked on detection: cost "
                 "is carried, not ranked on.")
    lines.append(f"{'method':>19} {'cells':>6} {'pos':>6} {'recall':>8}"
                 f" {'neg':>6} {'false alarm':>12} {'ms/cell':>9}   cost basis")
    # A measured 0% and "nothing to grade against" are different findings and
    # rank differently: ``recall or -1`` reads a real zero as absent and buries
    # a method that was tested and failed among the ones that were never tested.
    ranked = sorted(report["roc"].items(),
                    key=lambda item: (item[1]["recall"] is None,
                                      -(item[1]["recall"] or 0.0), item[0]))
    for method, item in ranked:
        lines.append(
            f"{method:>19} {item['cells']:>6} {item['positives']:>6}"
            f" {_cell(item['recall'], '8.1%', '       -')}"
            f" {item['negatives']:>6}"
            f" {_cell(item['false_alarm'], '12.1%', '           -')}"
            f" {_cell(item['ms_per_cell'], '9.2f', '        -')}"
            f"   {item['cost_basis']}")
    lines.append("  a method with 0 positives has no recall to report and prints "
                 "'-' rather than 0%: nothing")
    lines.append("  else fired on the peer, so there was nothing for it to be "
                 "graded against.")
    lines.append("  fire rate is not a ranking: differential-32 fires 2.6x more "
                 "often than anchor-8 and confirms")
    lines.append("  the same ~1,050 channels.")
    lines.append("")

    # ---- channel-instant ----------------------------------------------
    verdicts = report["channel_instants"]
    lines.append("CHANNEL-INSTANT VERDICT — 'was channel N broadcasting at "
                 "instant T', from opposite-edge sweeps")
    if not verdicts["verdicts"]:
        lines.append("  no opposite-edge sweep is scored on both radios yet, so "
                     "this question is unanswered rather")
        lines.append("  than answered negatively.")
    else:
        false_alarm = verdicts["false_alarm"]
        lines.append(
            f"  {verdicts['confirmed']} of {len(verdicts['verdicts'])} "
            f"channel-instants confirmed by both radios over "
            f"{verdicts['sweeps']} sweeps; rule: {verdicts['rule']}")
        lines.append(
            "  conjunction false-alarm rate "
            + _cell(false_alarm["rate"], ".1%", "n/a")
            + f" measured on {false_alarm['cells']} null channel-instants "
            f"({false_alarm['count']} fired)")
        lines.append("  <-- measured, not assumed: the product of two per-cell "
                     "rates would assume the very")
        lines.append("      independence this construction exists to test.")
        if not false_alarm["supported"]:
            lines.append(
                "  <-- the null holds too few firings to pin this rate: "
                f"{false_alarm['count']} exceedance(s) over "
                f"{false_alarm['cells']} cells bounds "
                + _cell(false_alarm["finest_rate"], ".1%", "n/a")
                + " at best. More sweeps is the only fix.")
        lines.append(f"{'sweep':>18} {'inst':>5} {'ch':>3} {'edges':>13}"
                     f" {'pairs':>6} {'skew ms':>8}   verdict")
        for item in verdicts["verdicts"]:
            lines.append(
                f"{item['paired_sweep']:>18} {item['instant']:>5}"
                f" {item['channel']:>3} {'+'.join(item['edges']):>13}"
                f" {item['pairs_confirmed']}/{item['receiver_pairs']:<4}"
                f" {_cell(item['skew_ms'], '8.4f', '       -')}"
                f"   {'BROADCASTING' if item['confirmed'] else 'not confirmed'}")
    return "\n".join(lines)


def _abbreviate(method: str) -> str:
    """Six characters that still tell the eight methods apart."""
    parts = method.split("-")
    if len(parts) == 1:
        return method[:6]
    head, tail = parts[0], parts[-1]
    return f"{head[:3]}{tail[:3]}"
