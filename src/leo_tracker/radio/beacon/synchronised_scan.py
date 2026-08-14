"""Two radios scanning the same sky at the same instant.

Every arm comparison this project has made was confounded by time.  One radio
scans eight tunings in sequence, so the two edges of a channel are 515 ms
apart, and two probe lengths are compared across whatever the sky did in
between.  The measured lower/upper detection correlation caps at phi 0.73 while
cross-method agreement on identical samples reaches 0.97, and the arm
comparison reversed three times.

This module removes time from both comparisons by driving both Plutos from one
process, one thread each, meeting at a :class:`threading.Barrier` before every
tuning:

  - **matched arms** (90% of sweeps) hold probe length and sample rate fixed
    across the two radios, so a difference between them is hardware or sky;
  - **mismatched arms** (10%) put two different arms on the *same* sky at the
    *same* instant, which is the only way to compare arms without the sky
    varying between them;
  - the **edge order** is drawn independently per radio on every sweep, so the
    two radios are sometimes on the identical tuning at one instant (direct
    cross-radio replication) and sometimes on the two edges of one channel at
    one instant (both edges, no 515 ms).

Cross-process coordination was built and discarded: marker files and POSIX
semaphores solve a problem that does not exist once one process owns both
radios.  A stdlib ``threading.Barrier(2)`` measured median 0.037 ms and max
0.180 ms skew across an eight-tuning sweep, against the 0.2--0.5 s the operator
asked for.

**The record states the outcome, never the request.**  Per tuning it carries
each radio's own measured arrival time and the skew between them, so a sweep
where one radio fell behind or ran alone is a different row from a genuinely
simultaneous one.  ``deployed_shape`` once recorded the scorer's configuration
rather than the capture's, which made a heterogeneous corpus look uniform and
hid a second defect underneath it; nothing here is allowed to do that again.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import numpy as np

from .fast_scan import (PILOT_BANDWIDTH_HZ, collect_radio, pilot_guard_hz,
                        survey_profile)

#: What the sweep record calls itself.
SWEEP_SCHEMA = "leo-tracker.synchronised-scan-sweep/v1"
#: What the plan the sweep was drawn from calls itself.
PLAN_SCHEMA = "leo-tracker.synchronised-scan-plan/v1"
#: What one radio's stored probes call themselves.
#:
#: Deliberately **not** ``leo-tracker.beacon-iq/v1``.  The layout carries a
#: tuning axis in front, so it is not a dwell recording however similar the
#: bytes look, and ``BeaconCapture.open`` refuses an unknown schema rather than
#: reading eight tunings as one continuous stream.
SCAN_IQ_SCHEMA = "leo-tracker.synchronised-scan-iq/v1"
#: The pipeline mode the offload queue files these under.  New rather than
#: reused: a sync-scan recording is not a narrow dwell and must not be analysed
#: as one.
SCAN_OBSERVATION_MODE = "sync-scan"

#: What the randomisation calls itself.  Named rather than implied so a later
#: analysis can select exactly the sweeps that belong to this experiment, and
#: so that changing the arm set, the pairing rate or the edge orders forces a
#: new id rather than silently pooling two different experiments into one
#: column.  This follows ``presurvey.SURVEY_EXPERIMENT_ID``.
SYNC_EXPERIMENT_ID = "synchronised-paired-scan-v1"

#: The frame-folding axis.  The fold is incoherent across frames, so
#: sensitivity grows roughly as their square root: 80 ms holds about 60 frame
#: slots, 160 ms about 120 and 640 ms about 480.
PROBE_LENGTHS_S: tuple[float, ...] = (0.080, 0.160, 0.640)
#: The bandwidth axis.  1.25 MS/s is below the pilot band and is kept anyway as
#: a degradation measurement; see :data:`SYNC_ARMS`.
SAMPLE_RATES_HZ: tuple[float, ...] = (1_250_000.0, 2_500_000.0, 5_000_000.0,
                                      10_000_000.0)


def _arm(probe_s: float, sample_rate_hz: float) -> dict:
    """One point of the grid, with the consequences of its rate attached.

    The pilot-band verdict is computed here rather than at the point of use so
    that no record can carry the arm without carrying the verdict.  The eight
    published edge-pilot subcarriers span
    :data:`~.fast_scan.PILOT_BANDWIDTH_HZ` = 1.875 MHz; at 1.25 MS/s the
    sampled band is 1.25 MHz and :func:`~.fast_scan.pilot_guard_hz` returns
    -312.5 kHz, so a third of the pilots are outside what was recorded.  The
    radio returns healthy-looking samples regardless, which is exactly why this
    has to be a stored flag and not an inference a later reader may forget to
    make.
    """
    guard_hz = pilot_guard_hz(sample_rate_hz)
    outside_hz = max(0.0, PILOT_BANDWIDTH_HZ - float(sample_rate_hz))
    return {
        "name": f"{round(probe_s * 1000)}ms-{sample_rate_hz / 1e6:g}MSps",
        "probe_s": float(probe_s),
        "sample_rate_hz": float(sample_rate_hz),
        "samples_per_tuning": int(round(probe_s * sample_rate_hz)),
        "pilot_bandwidth_hz": PILOT_BANDWIDTH_HZ,
        "pilot_guard_hz": guard_hz,
        "pilot_band_clipped": bool(guard_hz < 0),
        "pilot_band_outside_hz": outside_hz,
        "pilot_band_outside_fraction": outside_hz / PILOT_BANDWIDTH_HZ,
        "pilot_band_note": (
            "the eight edge-pilot subcarriers span "
            f"{PILOT_BANDWIDTH_HZ / 1e6:g} MHz and the sampled band is "
            f"{sample_rate_hz / 1e6:g} MHz, so "
            f"{outside_hz / PILOT_BANDWIDTH_HZ:.1%} of the pilot band was "
            "never recorded. The radio returns healthy-looking samples "
            "regardless. Do not pool this arm with arms whose pilot band fit"
            if guard_hz < 0 else
            f"the whole {PILOT_BANDWIDTH_HZ / 1e6:g} MHz pilot band fits, with "
            f"{guard_hz / 1e3:g} kHz of guard either side"),
    }


#: The twelve arms: three probe lengths crossed with four sample rates.
#:
#: 1.25 MS/s is kept deliberately even though it clips the pilot band, because
#: how much detection survives losing a third of the pilots is a measurement
#: nobody has taken.  Every record it produces carries
#: ``pilot_band_clipped: true`` so no later analysis can pool it with the arms
#: whose band fit.
SYNC_ARMS: tuple[dict, ...] = tuple(
    _arm(probe_s, rate)
    for probe_s in PROBE_LENGTHS_S for rate in SAMPLE_RATES_HZ)

#: Nominal probability of any one arm.  See :data:`ARM_MODULO_BIAS` for what
#: separates this from the realised probability.
ARM_ASSIGNMENT_PROBABILITY = 1.0 / len(SYNC_ARMS)

#: How far ``draw % 12`` is from exactly uniform.
#:
#: ``presurvey`` could say its split was exact because 2**32 is divisible by
#: four.  Twelve does not divide 2**32: 2**32 = 12 * 357,913,941 + 4, so four
#: of the twelve arms take 357,913,942 draws and eight take 357,913,941.  The
#: heavier arms are therefore about **one part in 3.6e8** more likely than the
#: lighter ones.  That is negligible against any effect this corpus can
#: resolve, and it is stated rather than rounded away, because "uniform" and
#: "uniform to one part in 3.6e8" are different claims and only one of them is
#: true here.
ARM_MODULO_BIAS = ((2 ** 32 // len(SYNC_ARMS) + 1) /
                   (2 ** 32 // len(SYNC_ARMS)) - 1)

ARM_ASSIGNMENT_BASIS = (
    f"uniform over {len(SYNC_ARMS)} arms by draw modulo {len(SYNC_ARMS)}. "
    f"2**32 is NOT divisible by {len(SYNC_ARMS)} -- it leaves "
    f"{2 ** 32 % len(SYNC_ARMS)} -- so {2 ** 32 % len(SYNC_ARMS)} arms are "
    f"about {ARM_MODULO_BIAS:.2g} more likely than the rest, one part in "
    f"{1 / ARM_MODULO_BIAS:.3g}. Negligible, and stated rather than implied. "
    "The draw is kept so a later analysis can verify the split from the "
    "corpus instead of trusting this claim")


def _checked_u32(value: int, label: str) -> int:
    number = int(value)
    if not 0 <= number < 2 ** 32:
        raise ValueError(f"{label} must be an unsigned 32-bit integer")
    return number


def assign_arm(draw_u32: int) -> dict:
    """Which of the twelve arms a raw 32-bit draw selects.

    Kept in Python rather than in the shell that draws the number, for the
    reason :func:`presurvey.assign_config` gives: the mapping is the part of a
    randomised assignment that can be wrong, and a mapping written in bash is a
    mapping with no test.  The shell draws and logs; this decides.
    """
    return SYNC_ARMS[_checked_u32(draw_u32, "random draw") % len(SYNC_ARMS)]


#: How often both radios take the *same* arm.
#:
#: The matched sweeps are the cross-radio replication measurement: probe length
#: and rate are held fixed across the two radios, so any difference between
#: them is hardware or sky and not configuration.  The mismatched tenth is the
#: only unconfounded *arm* comparison there is -- two different arms on one sky
#: at one instant -- and it is a tenth rather than a half because replication
#: is the measurement that has to run continuously while arm comparison
#: accumulates.
MATCHED_PAIRING_PROBABILITY = 0.9
#: Buckets the pairing draw is reduced into.  Ten, not 10,000, because the
#: split is exactly nine in ten and a coarser modulo carries less bias:
#: 2**32 = 10 * 429,496,729 + 6, so the realised matched probability is
#: 3,865,470,567 / 2**32 = 0.9000000003, about one part in 2.6e9 high.
PAIRING_BUCKETS = 10

PAIRING_ASSIGNMENT_BASIS = (
    f"matched with probability {MATCHED_PAIRING_PROBABILITY} by draw modulo "
    f"{PAIRING_BUCKETS} below {round(MATCHED_PAIRING_PROBABILITY * PAIRING_BUCKETS)}. "
    f"2**32 is not divisible by {PAIRING_BUCKETS}, so the realised matched "
    "probability is 0.9000000003 rather than exactly 0.9. Drawn from its own "
    "32 bits, independent of the arm draw and of both edge-order draws, so "
    "each can be audited from the corpus on its own")


def assign_pairing(draw_u32: int) -> str:
    """Whether both radios take one arm this sweep, from its own 32 bits.

    Separate bits from the arm draw so the two are independent and so a later
    analysis can check each split without the other's outcome in the way.
    """
    matched_buckets = round(MATCHED_PAIRING_PROBABILITY * PAIRING_BUCKETS)
    residue = _checked_u32(draw_u32, "pairing draw") % PAIRING_BUCKETS
    return "matched" if residue < matched_buckets else "mismatched"


def arm_by_name(name: str) -> dict:
    """The arm with this name, for an operator pinning one deliberately."""
    for arm in SYNC_ARMS:
        if arm["name"] == name:
            return arm
    raise ValueError(f"unknown sweep arm {name!r}; expected one of "
                     f"{[item['name'] for item in SYNC_ARMS]}")


#: The two orders one radio may sweep the eight low-band edge tunings in.
#:
#: ``L`` is today's :data:`presurvey.LOW_BAND_TUNINGS` exactly; ``U`` visits the
#: same eight tunings with the two edges of each channel swapped.  There is no
#: third order and there is deliberately no shuffle: the whole value is in what
#: the *pair* of orders does at one instant, and only these two make the two
#: radios either exactly aligned or exactly opposed at every index.
EDGE_ORDERS: dict[str, tuple[tuple[int, str], ...]] = {
    "L": tuple((channel, edge) for channel in (1, 2, 3, 4)
               for edge in ("lower", "upper")),
    "U": tuple((channel, edge) for channel in (1, 2, 3, 4)
               for edge in ("upper", "lower")),
}
#: Exactly uniform: 2**32 is divisible by two, so this split carries no modulo
#: bias at all, unlike the twelve-arm draw above.
EDGE_ORDER_PROBABILITY = 0.5

EDGE_ORDER_BASIS = (
    "drawn independently per radio on every sweep, from its own 32 bits, and "
    "never shared between the radios or coupled to the arm draw or the "
    "pairing decision. Same order on both radios means the two radios observe "
    "the identical tuning at the same instant, which is direct cross-radio "
    "replication on one frequency. Opposite orders mean one radio is on "
    "CH1-lower while the other is on CH1-upper at the same instant, which is "
    "the only observation of both edges of a channel with no time between "
    "them; today one radio scans them 515 ms apart. 2**32 is divisible by 2, "
    "so this split is exactly uniform")


def assign_edge_order(draw_u32: int) -> str:
    """Which of the two edge orders a raw 32-bit draw selects."""
    return sorted(EDGE_ORDERS)[_checked_u32(draw_u32, "edge order draw") % 2]


def edge_order_tunings(order: str) -> tuple[tuple[int, str], ...]:
    """The eight ``(channel, edge)`` tunings this order sweeps, in order."""
    try:
        return EDGE_ORDERS[order]
    except KeyError as exc:
        raise ValueError(f"unknown edge order {order!r}; expected one of "
                         f"{sorted(EDGE_ORDERS)}") from exc


@dataclass(frozen=True)
class RadioSpec:
    """One radio, named the way every record downstream names it."""

    radio_id: str
    uri: str
    serial: str | None = None
    receiver_labels: tuple[str, ...] = ("rx0", "rx1")


def draw_sweep_plan(radios, *, draws: dict) -> dict:
    """Resolve one sweep's assignment, and the record that audits it.

    Four independent 32-bit draws: the arm, the pairing, a second arm used only
    when the pairing came out mismatched, and one edge order per radio.  They
    are separate numbers on purpose -- a single draw reused across two
    decisions correlates them, and a correlation between "which arm" and "which
    edge order" is exactly the kind of confound this sweep exists to remove.
    """
    specs = tuple(radios)
    if len(specs) != 2:
        raise ValueError("a synchronised sweep pairs exactly two radios")
    arm_draw = _checked_u32(draws["arm"], "arm draw")
    pairing_draw = _checked_u32(draws["pairing"], "pairing draw")
    second_draw = _checked_u32(draws["second_arm"], "second arm draw")
    arm = assign_arm(arm_draw)
    pairing = assign_pairing(pairing_draw)
    # A mismatched sweep draws the second radio's arm from its own bits; a
    # matched one hands both radios the same arm and records the second draw as
    # unused rather than dropping it, so the corpus can still see that the
    # pairing decision was the thing that made them equal.
    second_arm = arm if pairing == "matched" else assign_arm(second_draw)
    assigned = (arm, second_arm)
    # Drawn per radio, always, on every sweep -- including the matched-arm
    # sweeps.  Coupling this to the pairing decision would leave the radios
    # permanently aligned exactly when the arms were equal, which is precisely
    # the sweep where an opposite-order observation is cleanest.
    order_draws = {spec.radio_id: _checked_u32(
        draws["edge_order"][spec.radio_id], f"edge order draw for {spec.radio_id}")
        for spec in specs}
    orders = {radio_id: assign_edge_order(value)
              for radio_id, value in order_draws.items()}
    return {
        "schema": PLAN_SCHEMA,
        "experiment": {
            "experiment_id": SYNC_EXPERIMENT_ID,
            "arm": {
                "experiment_id": SYNC_EXPERIMENT_ID,
                "random_draw_u32": arm_draw,
                "assignment_probability": ARM_ASSIGNMENT_PROBABILITY,
                "assignment_rule": f"draw modulo {len(SYNC_ARMS)}",
                "assigned_arm": arm["name"],
                "arms": [item["name"] for item in SYNC_ARMS],
                "modulo_bias": ARM_MODULO_BIAS,
                "basis": ARM_ASSIGNMENT_BASIS,
            },
            "pairing": {
                "experiment_id": SYNC_EXPERIMENT_ID,
                "random_draw_u32": pairing_draw,
                "assignment_probability": MATCHED_PAIRING_PROBABILITY,
                "assignment_rule": (
                    f"draw modulo {PAIRING_BUCKETS} below "
                    f"{round(MATCHED_PAIRING_PROBABILITY * PAIRING_BUCKETS)}"),
                "outcome": pairing,
                "second_arm_draw_u32": second_draw,
                "second_arm_draw_used": pairing == "mismatched",
                "assigned_arms": [item["name"] for item in assigned],
                "basis": PAIRING_ASSIGNMENT_BASIS,
            },
            "edge_order": {
                spec.radio_id: {
                    "experiment_id": SYNC_EXPERIMENT_ID,
                    "random_draw_u32": order_draws[spec.radio_id],
                    "assignment_probability": EDGE_ORDER_PROBABILITY,
                    "assignment_rule": "draw modulo 2",
                    "order": orders[spec.radio_id],
                    "orders": sorted(EDGE_ORDERS),
                } for spec in specs} | {
                "drawn_per_radio": True,
                "shared_between_radios": False,
                "coupled_to_arm_or_pairing": False,
                "aligned": len(set(orders.values())) == 1,
                "relationship": ("identical tuning on both radios at every "
                                 "index" if len(set(orders.values())) == 1 else
                                 "both edges of one channel at every index"),
                "basis": EDGE_ORDER_BASIS,
            },
        },
        "radios": [{"radio_id": spec.radio_id, "arm": dict(item),
                    "edge_order": orders[spec.radio_id],
                    "edge_order_draw_u32": order_draws[spec.radio_id],
                    "tunings": [list(tuning) for tuning in
                                edge_order_tunings(orders[spec.radio_id])]}
                   for spec, item in zip(specs, assigned, strict=True)],
    }


#: How long one radio waits at a tuning barrier before giving up on the other.
#:
#: Bounded, and generous.  A mismatched sweep can put an 80 ms probe at
#: 1.25 MS/s against a 640 ms probe at 10 MS/s, and radio time scales with rate
#: as well as with duration -- an 80 ms probe measured 108 / 137 / 193 / 309 ms
#: at 1.25 / 2.5 / 5 / 10 MS/s -- so the dearest arm costs a few seconds per
#: tuning and the fast side simply waits.  This has to clear that with room and
#: still be short enough that a radio somebody unplugged cannot hold the
#: survivor for a minute.
SWEEP_BARRIER_TIMEOUT_S = 30.0

#: What the operator asked for, kept where it cannot be mistaken for what was
#: measured.  ``threading.Barrier`` beats it by about 2,000x; the number is
#: recorded so a later reader can see the requirement the design was judged
#: against, in a field named for the request rather than for the outcome.
REQUESTED_SKEW_TOLERANCE_MS = (200.0, 500.0)

SKEW_BASIS = (
    "measured, not requested: each radio reads host UTC in its own thread the "
    "instant threading.Barrier.wait() returns for that tuning, and the skew is "
    "the difference between those two stamps. A tuning only one radio reached "
    "has no skew at all and is recorded with skew_ms null rather than zero, "
    "because 'the other radio was not there' and 'the two arrived together' "
    "are opposite facts and must not share a column")


def _host_clock(radio_id: str, tuning_index: int) -> int:
    """Host UTC in nanoseconds, read in the calling radio's own thread.

    Both arguments are ignored.  They exist so a test can pin what each radio
    measured at each tuning and check that the recorded skew is the difference
    of two stamps rather than a constant; a seam is cheaper than trusting that
    a number labelled "measured" was.
    """
    return time.time_ns()


class TuningRendezvous:
    """Where the two radios meet before every tuning.

    A plain :class:`threading.Barrier` with the arrival stamps kept.  Two
    properties matter and both are the barrier's own:

    - ``wait`` returns to both threads at the same instant, which is what makes
      the two radios observe their tunings simultaneously;
    - ``abort`` and a bounded ``timeout`` mean a radio that died, or one that
      never opened, releases the survivor instead of holding it.  A survivor
      that hangs is worse than a solo sweep, because the solo sweep is data.
    """

    def __init__(self, parties: int = 2, *,
                 timeout_s: float = SWEEP_BARRIER_TIMEOUT_S,
                 clock=_host_clock) -> None:
        if parties < 1:
            raise ValueError("a rendezvous needs at least one party")
        if timeout_s <= 0:
            raise ValueError("the barrier wait must be bounded and positive")
        self.parties = int(parties)
        self.timeout_s = float(timeout_s)
        self._barrier = threading.Barrier(self.parties)
        self._clock = clock
        self._lock = threading.Lock()
        self.arrivals: dict[int, dict[str, dict]] = {}

    def arrive(self, radio_id: str, tuning_index: int) -> dict:
        """Wait for the other radio, then stamp this radio's own arrival.

        The time spent inside the wait is kept as well as the moment it ended.
        On a mismatched sweep it is the whole evidence that the barrier
        tolerated one side finishing far sooner: the fast radio's waiting is
        the slow radio's extra work, measured rather than argued.  It is a
        monotonic duration, not a difference of UTC stamps, because the two
        answer different questions and only one of them is immune to the host
        clock stepping.
        """
        synchronised = True
        entered = time.perf_counter()
        try:
            self._barrier.wait(timeout=self.timeout_s)
        except threading.BrokenBarrierError:
            # Either the other radio aborted, or nobody came inside the
            # timeout.  Both mean this radio is alone from here on; it proceeds
            # rather than raising, and the record says it was alone.
            synchronised = False
        arrival = {"radio_id": radio_id, "tuning_index": int(tuning_index),
                   "utc_ns": int(self._clock(radio_id, tuning_index)),
                   "waited_ms": (time.perf_counter() - entered) * 1000,
                   "synchronised": bool(synchronised)}
        with self._lock:
            self.arrivals.setdefault(int(tuning_index), {})[radio_id] = arrival
        return arrival

    def abort(self) -> None:
        """Release whoever is waiting, and everyone who arrives after."""
        self._barrier.abort()


def _open_radio_context(spec: RadioSpec):
    """Open one libiio context on this radio, resolved by stable serial."""
    from .presurvey import _open_context, _verify_serial

    context = _open_context(spec.uri, spec.serial)
    _verify_serial(context, spec.serial)
    return context


def _receiver_state(context) -> dict:
    """What the radio says its receivers are set to, read back rather than assumed.

    Diagnostic, and deliberately fail-open.  A cross-radio comparison whose two
    sides ran at different gains is a different measurement from one that did
    not, and nothing else in the record would show it.
    """
    try:
        phy = context.find_device("ad9361-phy")
        state = {}
        for channel in phy.channels:
            if channel.output or not str(channel.id).startswith("voltage"):
                continue
            attrs = getattr(channel, "attrs", {}) or {}
            state[channel.id] = {
                key: str(attrs[key].value) for key in
                ("hardwaregain", "gain_control_mode", "sampling_frequency",
                 "rf_bandwidth") if key in attrs}
        return state
    except Exception as exc:                       # noqa: BLE001 - diagnostic
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _sweep_one_radio(spec: RadioSpec, entry: dict, rendezvous: TuningRendezvous,
                     *, open_context, collect, lnb_lo_hz: float,
                     outcomes: dict, collected: dict) -> None:
    """Sweep one radio's eight tunings, and never hold the other one up.

    Every *failing* exit aborts the rendezvous, so a radio that cannot be
    opened and one that dies on its third tuning look the same to the survivor:
    the barrier breaks and it carries on alone.

    A radio that finishes normally must **not** abort.  Both radios always
    sweep the same eight indexes, so a completed sweep leaves no wait for the
    other to be released from -- and aborting anyway loses a race with
    :meth:`threading.Barrier.wait`.  The last party to arrive releases the
    barrier and returns while the other is still waking; an abort issued in
    that window sets the broken state before the woken thread rechecks it, and
    the survivor records its final tuning as unsynchronised when it was not.
    Which way that error runs matters: an abort that arrives while a radio is
    genuinely dying can only make a row *understate* simultaneity, never
    overstate it, so the record stays conservative.
    """
    arm = entry["arm"]
    tunings = [tuple(item) for item in entry["tunings"]]
    per_tuning: list[dict] = []
    outcome = {"radio_id": spec.radio_id, "serial": spec.serial,
               "uri": spec.uri, "receiver_labels": list(spec.receiver_labels),
               "arm": dict(arm), "edge_order": entry["edge_order"],
               "edge_order_draw_u32": entry["edge_order_draw_u32"],
               "tunings": [list(item) for item in tunings],
               "state": "failed_to_open", "error": None,
               "per_tuning": per_tuning,
               "pilot_band_clipped": bool(arm["pilot_band_clipped"]),
               "pilot_guard_hz": arm["pilot_guard_hz"],
               "pilot_band_outside_fraction": arm["pilot_band_outside_fraction"],
               "pilot_band_note": arm["pilot_band_note"],
               "measured_receiver_state": None,
               "iq_bytes": 0}
    outcomes[spec.radio_id] = outcome

    def before(index: int) -> dict:
        return rendezvous.arrive(spec.radio_id, index)

    def after(index: int, plan_entry: dict) -> None:
        arrival = rendezvous.arrivals.get(index, {}).get(spec.radio_id, {})
        per_tuning.append({
            "tuning_index": int(index),
            "channel": plan_entry["channel"], "region": plan_entry["region"],
            "if_center_hz": plan_entry.get("if_center_hz"),
            "rf_center_hz": plan_entry.get("rf_center_hz"),
            "arrival_utc_ns": arrival.get("utc_ns"),
            "barrier_wait_ms": arrival.get("waited_ms"),
            "synchronised": bool(arrival.get("synchronised")),
            "tuning_ms": plan_entry.get("tuning_ms")})

    context = None
    try:
        context = open_context(spec)
    except Exception as exc:                       # noqa: BLE001 - fail open
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        rendezvous.abort()
        return
    try:
        outcome["measured_receiver_state"] = _receiver_state(context)
        result = collect(context, tunings,
                         profile=survey_profile(arm["probe_s"],
                                                arm["sample_rate_hz"]),
                         sample_rate_hz=arm["sample_rate_hz"],
                         lnb_lo_hz=lnb_lo_hz,
                         before_tuning=before, after_tuning=after)
        collected[spec.radio_id] = result
        outcome["state"] = "complete"
        outcome["radio_ms"] = result.get("radio_ms")
        outcome["timing_ms"] = result.get("timing_ms")
        outcome["iq_bytes"] = int(result.get("iq_bytes") or 0)
        outcome["samples_per_tuning"] = int(result.get("samples_per_tuning") or 0)
        outcome["sample_order"] = [list(item) for item in
                                   result.get("sample_order") or []]
    except Exception as exc:                       # noqa: BLE001 - fail open
        outcome["state"] = "failed_mid_sweep"
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        outcome["partial_iq_note"] = (
            "the radio raised part way through its sweep; the reader holds its "
            "probes until the sweep ends, so no IQ survives. The tunings it "
            "did complete are recorded above")
    finally:
        if outcome["state"] != "complete":
            # Stop the other radio waiting on a radio that is not coming.
            rendezvous.abort()
        # The binding has no explicit close: dropping the last reference is
        # what hands the radio back.
        del context


def _tuning_records(rendezvous: TuningRendezvous, outcomes: dict,
                    order: tuple[str, ...]) -> list[dict]:
    """One row per tuning index, saying who arrived and how far apart.

    Built from the arrival stamps rather than from what was planned, so a
    tuning that only one radio reached is a visibly different row from one they
    reached together.
    """
    where = {radio_id: {int(item["tuning_index"]): item
                        for item in outcomes[radio_id]["per_tuning"]}
             for radio_id in order}
    records = []
    for index in sorted(rendezvous.arrivals):
        arrivals = rendezvous.arrivals[index]
        present = [radio_id for radio_id in order if radio_id in arrivals]
        row = {"tuning_index": int(index),
               "radios_arrived": len(present),
               "simultaneous": bool(len(present) == len(order) and all(
                   arrivals[radio_id]["synchronised"] for radio_id in present)),
               "arrivals": {}}
        for radio_id in present:
            completed = where[radio_id].get(int(index), {})
            row["arrivals"][radio_id] = {
                "utc_ns": arrivals[radio_id]["utc_ns"],
                "synchronised": arrivals[radio_id]["synchronised"],
                "channel": completed.get("channel"),
                "region": completed.get("region"),
                "completed": bool(completed)}
        if len(present) == 2:
            first, second = (arrivals[radio_id]["utc_ns"] for radio_id in present)
            row["skew_ms"] = abs(first - second) / 1e6
        else:
            # Not zero.  A tuning one radio reached alone has no skew at all,
            # and a zero here would read as a perfectly simultaneous pair.
            row["skew_ms"] = None
        row["pilot_band_clipped"] = any(
            outcomes[radio_id]["pilot_band_clipped"] for radio_id in present)
        records.append(row)
    return records


def sweep_pair(radios, plan: dict, *, open_context=_open_radio_context,
               collect=collect_radio,
               barrier_timeout_s: float = SWEEP_BARRIER_TIMEOUT_S,
               lnb_lo_hz: float = 9_750_000_000.0,
               clock=_host_clock) -> tuple[dict, dict]:
    """Sweep both radios in lockstep; return the record and the collected IQ.

    One thread per radio, one barrier per tuning.  ``open_context`` and
    ``collect`` are parameters so the whole failure surface -- a radio that
    never opens, one that dies half way, two radios doing unequal work -- can be
    exercised without unplugging anything, and so that the production path is
    this same code with :func:`fast_scan.collect_radio` passed in.
    """
    specs = tuple(radios)
    entries = {item["radio_id"]: item for item in plan["radios"]}
    if set(entries) != {spec.radio_id for spec in specs}:
        raise ValueError("the plan and the radios disagree about which radios ran")
    order = tuple(spec.radio_id for spec in specs)
    rendezvous = TuningRendezvous(len(specs), timeout_s=barrier_timeout_s,
                                  clock=clock)
    outcomes: dict[str, dict] = {}
    collected: dict[str, dict] = {}
    started_ns = time.time_ns()
    started = time.perf_counter()
    threads = [threading.Thread(
        target=_sweep_one_radio, name=f"sweep-{spec.radio_id}",
        args=(spec, entries[spec.radio_id], rendezvous),
        kwargs={"open_context": open_context, "collect": collect,
                "lnb_lo_hz": lnb_lo_hz, "outcomes": outcomes,
                "collected": collected}) for spec in specs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall_ms = (time.perf_counter() - started) * 1000
    finished_ns = time.time_ns()

    tunings = _tuning_records(rendezvous, outcomes, order)
    for radio_id in order:
        outcome = outcomes[radio_id]
        outcome["completed_tuning_count"] = len(outcome["per_tuning"])
        outcome["arrived_tuning_count"] = sum(
            1 for index in rendezvous.arrivals
            if radio_id in rendezvous.arrivals[index])
        outcome["solo_tuning_count"] = sum(
            1 for item in outcome["per_tuning"] if not item["synchronised"])
        # What this radio spent waiting for the other one. On a matched sweep
        # it is milliseconds; on a mismatched one it is the difference between
        # the two arms, and it is what shows the sweep cost the slower arm.
        outcome["barrier_wait_ms"] = sum(
            float(item["waited_ms"])
            for index in rendezvous.arrivals
            for item in [rendezvous.arrivals[index].get(radio_id)]
            if item is not None)
        outcome["solo"] = bool(outcome["solo_tuning_count"] or
                               outcome["state"] != "complete")
        # Where the pairing was lost, so "ran alone from the start" and "lost
        # its partner on tuning five" are separable without re-deriving them
        # from the per-tuning list.
        alone = [item["tuning_index"] for item in outcome["per_tuning"]
                 if not item["synchronised"]]
        outcome["solo_from_tuning_index"] = min(alone) if alone else None
    skews = [item["skew_ms"] for item in tunings if item["skew_ms"] is not None]
    ordered = sorted(skews)
    median = (None if not ordered else
              ordered[len(ordered) // 2] if len(ordered) % 2 else
              (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2)
    simultaneous = [item for item in tunings if item["simultaneous"]]
    clipped = [radio_id for radio_id in order
               if outcomes[radio_id]["pilot_band_clipped"]]
    # Three outcomes, not two.  "Never had a partner" and "lost its partner on
    # tuning five" are different observations of the sky and pooling them into
    # one degraded bucket is how a heterogeneous corpus comes to look uniform.
    if bool(tunings) and len(simultaneous) == len(tunings) and all(
            outcomes[radio_id]["state"] == "complete" for radio_id in order):
        sweep_mode = "paired"
    elif simultaneous:
        sweep_mode = "partly_paired"
    else:
        sweep_mode = "solo"
    return {
        "schema": SWEEP_SCHEMA,
        "started_utc_ns": started_ns,
        "finished_utc_ns": finished_ns,
        "wall_ms": wall_ms,
        "experiment": plan["experiment"],
        "barrier": {
            "mechanism": "threading.Barrier, two parties, one wait per tuning",
            "parties": rendezvous.parties,
            "timeout_s": rendezvous.timeout_s,
            "basis": ("one process owns both radios, so coordination is a "
                      "barrier rather than a protocol. Marker files and POSIX "
                      "semaphores were built and discarded: they solve a "
                      "cross-process problem that does not exist here"),
        },
        "radios": [outcomes[radio_id] for radio_id in order],
        "tunings": tunings,
        "skew": {
            "measured": True,
            "basis": SKEW_BASIS,
            "count": len(skews),
            "median_ms": median,
            "max_ms": max(skews) if skews else None,
            "min_ms": min(skews) if skews else None,
            "per_tuning_ms": [item["skew_ms"] for item in tunings],
            "simultaneous_tuning_count": len(simultaneous),
            "operator_requested_tolerance_ms": list(REQUESTED_SKEW_TOLERANCE_MS),
        },
        "pilot_band": {
            "any_clipped": bool(clipped),
            "clipped_radios": clipped,
            "pilot_bandwidth_hz": PILOT_BANDWIDTH_HZ,
            "basis": ("a rate below the 1.875 MHz pilot band leaves part of it "
                      "outside what was sampled, and the radio returns "
                      "healthy-looking samples regardless, so the flag is "
                      "stored on every record the arm produces rather than "
                      "left to be inferred from the rate"),
        },
        "sweep_mode": sweep_mode,
        "paired_tuning_count": len(simultaneous),
        "solo": any(outcomes[radio_id]["solo"] for radio_id in order),
        "dwell": {
            "performed": False,
            "note": ("scans only. This loop never dwells: it sweeps eight edge "
                     "tunings on each radio and hands the probes to the "
                     "offload queue"),
        },
    }, collected


#: One file per radio per sweep, named so it can never be mistaken for a dwell
#: chunk while still satisfying the exporter, which verifies whatever the
#: manifest declares under ``chunks``.
SCAN_IQ_FILENAME = "chunk-000000.ci16"

#: Where a capture goes and where the exporter is willing to find one.  The
#: capture host writes here and nowhere else; the network copy to QNAP is
#: :file:`leo-tracker-analysis-export.service`'s job precisely so that it
#: cannot interfere with live capture.
CAPTURES_DIRECTORY = "captures"
ANALYSIS_QUEUE_DIRECTORY = "staging/analysis-queue"


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.next.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _write_iq(destination: Path, samples: np.ndarray) -> dict:
    """Commit one radio's probes with a digest the exporter can verify.

    ``(tuning, sample, receiver, component)`` little-endian int16 -- the
    capture path's own ci16 layout with a tuning axis in front, kept as the
    driver delivered it rather than as the complex64 a detector works in, so a
    later analysis re-decides from the signal rather than from this one's
    conversion of it.
    """
    values = np.ascontiguousarray(samples, dtype="<i2")
    if values.ndim != 4 or values.shape[2:] != (2, 2):
        raise ValueError("scan IQ must be (tuning, sample, receiver, component), "
                         f"got {values.shape}")
    partial = destination / (SCAN_IQ_FILENAME + ".partial")
    final = destination / SCAN_IQ_FILENAME
    digest = hashlib.sha256()
    with partial.open("wb", buffering=0) as stream:
        payload = memoryview(values).cast("B")
        stream.write(payload)
        digest.update(payload)
        os.fsync(stream.fileno())
    os.replace(partial, final)
    return {"path": SCAN_IQ_FILENAME, "dtype": "ci16_le",
            "layout": "tuning,sample,receiver,component; i0 q0 i1 q1",
            "tunings": int(values.shape[0]),
            "samples_per_tuning": int(values.shape[1]),
            "first_sample_index": 0,
            "sample_count": int(values.shape[0]) * int(values.shape[1]),
            "sha256": digest.hexdigest(),
            "bytes": final.stat().st_size}


def _capture_manifest(record: dict, radio: dict, collected: dict,
                      chunk: dict, *, name: str, sweep_id: str) -> dict:
    """What travels with the bytes, and what the analysis host reads.

    The sweep record is embedded whole.  Everything an analysis needs to avoid
    pooling incomparable rows -- which arm, which edge order, whether the pilot
    band fit, how far apart the two radios actually were at each tuning -- has
    to survive the copy to shared storage, and the manifest is the only thing
    that does.
    """
    arm = radio["arm"]
    return {
        "schema": SCAN_IQ_SCHEMA,
        "state": "complete",
        "capture_id": name,
        "sweep_id": sweep_id,
        "dtype": "ci16_le",
        "layout": chunk["layout"],
        "receiver_count": 2,
        "sample_rate_hz": arm["sample_rate_hz"],
        "probe_s": arm["probe_s"],
        "samples_per_tuning": chunk["samples_per_tuning"],
        "tuning_count": chunk["tunings"],
        # Retained IQ is in the order it was collected; the mapping is stored
        # rather than inferred, because a reader that indexes it by anything
        # else scores one tuning's samples against another tuning's label.
        "sample_order": radio.get("sample_order"),
        "edge_order": radio["edge_order"],
        "arm": dict(arm),
        # Repeated at the top level as well as inside the arm: an analysis that
        # groups by rate would find it, and one that groups by capture would
        # not, and the whole point of the flag is that it cannot be missed.
        "pilot_band_clipped": bool(arm["pilot_band_clipped"]),
        "pilot_guard_hz": arm["pilot_guard_hz"],
        "pilot_bandwidth_hz": PILOT_BANDWIDTH_HZ,
        "identity": {"radio_id": radio["radio_id"], "serial": radio["serial"],
                     "uri": radio["uri"],
                     "receiver_labels": radio["receiver_labels"]},
        "radio_ms": radio.get("radio_ms"),
        "timing_ms": radio.get("timing_ms"),
        "created_utc_ns": record["finished_utc_ns"],
        "chunks": [chunk],
        "metadata": {
            "command": "leo-radio starlink-synchronised-scan",
            "observation_mode": SCAN_OBSERVATION_MODE,
            "dwell": record["dwell"],
        },
        # The whole outcome, not this radio's half of it: whether the other
        # radio was there, and how far apart the two were at every tuning, is a
        # property of this recording and cannot be recovered from it alone.
        "sweep": record,
    }


def write_sweep(record: dict, collected: dict, *, storage_root,
                sweep_id: str, enqueue: bool = True) -> dict:
    """Write both radios' probes to NVMe and hand them to the offload queue.

    Never to :file:`/mnt/qnap01`.  The network copy is
    :file:`leo-tracker-analysis-export.service`'s job and runs in the
    background at a bandwidth limit, which is the whole reason the capture loop
    does not do it: a stalled NFS write inside the sweep would cost radio time
    that no amount of retrying recovers.
    """
    root = Path(storage_root)
    captures = root / CAPTURES_DIRECTORY
    queue = root / ANALYSIS_QUEUE_DIRECTORY
    captures.mkdir(parents=True, exist_ok=True)
    queue.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for radio in record["radios"]:
        radio_id = radio["radio_id"]
        result = collected.get(radio_id)
        if result is None or result.get("samples") is None:
            # A radio that never opened, or died before its first tuning
            # completed, contributes a recorded absence rather than a file.
            skipped.append({"radio_id": radio_id, "state": radio["state"],
                            "reason": "no IQ was collected"})
            continue
        name = f"sync-{sweep_id}-{radio_id}"
        destination = captures / name
        destination.mkdir(parents=True, exist_ok=False)
        chunk = _write_iq(destination, result["samples"])
        manifest = _capture_manifest(record, radio, result, chunk,
                                     name=name, sweep_id=sweep_id)
        _atomic_json(destination / "manifest.json", manifest)
        entry = {"radio_id": radio_id, "name": name,
                 "path": str(destination), "bytes": int(chunk["bytes"]),
                 "job": None}
        if enqueue:
            entry["job"] = str(_enqueue(queue, name, destination))
        written.append(entry)
    return {"schema": "leo-tracker.synchronised-scan-write/v1",
            "sweep_id": sweep_id, "storage_root": str(root),
            "captures": written, "skipped": skipped,
            "bytes": sum(entry["bytes"] for entry in written),
            "queue": str(queue)}


def draw_u32() -> int:
    """Thirty-two fresh bits from the kernel pool.

    The same source ``presurvey`` and the AGC assignment draw from.  Kept here
    so a caller that did not draw in the shell still gets an auditable number
    rather than a seeded pseudo-random one.
    """
    return int.from_bytes(os.urandom(4), "big")


def run_sweep(radios, *, storage_root, sweep_id: str, draws: dict | None = None,
              open_context=_open_radio_context, collect=collect_radio,
              barrier_timeout_s: float = SWEEP_BARRIER_TIMEOUT_S,
              lnb_lo_hz: float = 9_750_000_000.0, enqueue: bool = True) -> dict:
    """Draw, sweep, write and queue: one whole cycle of the loop."""
    resolved = dict(draws or {})
    specs = tuple(radios)
    for key in ("arm", "pairing", "second_arm"):
        if resolved.get(key) is None:
            resolved[key] = draw_u32()
    orders = dict(resolved.get("edge_order") or {})
    for spec in specs:
        if orders.get(spec.radio_id) is None:
            orders[spec.radio_id] = draw_u32()
    resolved["edge_order"] = orders
    plan = draw_sweep_plan(specs, draws=resolved)
    record, collected = sweep_pair(
        specs, plan, open_context=open_context, collect=collect,
        barrier_timeout_s=barrier_timeout_s, lnb_lo_hz=lnb_lo_hz)
    written = write_sweep(record, collected, storage_root=storage_root,
                          sweep_id=sweep_id, enqueue=enqueue)
    record["written"] = written
    return record


def _enqueue(queue: Path, name: str, capture: Path) -> Path:
    """Publish one durable job the existing exporter already understands.

    Same three tab-separated fields and the same rename-into-place the beacon
    watcher uses, so the running exporter needs no change to drain these.
    """
    marker = queue / f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}" \
                     f"{time.time_ns() % 1_000_000_000:09d}Z-{name}.job"
    temporary = marker.with_suffix(".job.next")
    temporary.write_text(f"{name}\t{capture}\t{SCAN_OBSERVATION_MODE}\n")
    os.replace(temporary, marker)
    return marker
