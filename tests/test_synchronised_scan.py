"""The paired scan: two radios on the sky at the same instant, and its record.

Nothing here needs a radio.  What every test is protecting is the claim the
sweep exists to make -- that a difference between the two radios is hardware or
sky rather than configuration, and that two edges of one channel were seen at
one instant rather than 515 ms apart.  A record that stated the intent instead
of the outcome would make both claims unfalsifiable, so most of these assert on
what was *measured* and not on what was asked for.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import tempfile
import threading
import time

import pytest

from leo_tracker.radio.beacon import synchronised_scan as sync

#: Kept off /tmp, which on the capture host is a 2 GB tmpfs holding the
#: compiled kernel cache.
_SCRATCH = os.environ.get("LEO_TEST_SCRATCH") or None


def test_the_arm_grid_is_twelve_arms_of_probe_length_crossed_with_rate():
    names = [arm["name"] for arm in sync.SYNC_ARMS]

    assert len(sync.SYNC_ARMS) == 12
    assert len(set(names)) == 12
    assert {arm["probe_s"] for arm in sync.SYNC_ARMS} == {0.080, 0.160, 0.640}
    assert {arm["sample_rate_hz"] for arm in sync.SYNC_ARMS} == {
        1_250_000.0, 2_500_000.0, 5_000_000.0, 10_000_000.0}


def test_every_arm_is_reachable_and_each_residue_selects_exactly_one():
    selected = [sync.assign_arm(residue)["name"] for residue in range(12)]

    assert sorted(selected) == sorted(arm["name"] for arm in sync.SYNC_ARMS)


def test_the_arm_draw_is_uniform_to_the_bias_the_record_declares():
    """12 does not divide 2**32, so uniformity is a claim with a size.

    Counted exactly rather than sampled: four of the twelve residues take one
    extra draw out of 2**32, and the record has to state that rather than imply
    an exactly uniform split.
    """
    counts = {arm["name"]: 0 for arm in sync.SYNC_ARMS}
    for residue in range(12):
        share = 2 ** 32 // 12 + (1 if residue < 2 ** 32 % 12 else 0)
        counts[sync.assign_arm(residue)["name"]] += share

    assert sum(counts.values()) == 2 ** 32
    assert max(counts.values()) - min(counts.values()) == 1
    excess = max(counts.values()) / min(counts.values()) - 1
    assert excess == pytest.approx(sync.ARM_MODULO_BIAS, rel=1e-6)
    assert 2e-9 < sync.ARM_MODULO_BIAS < 3e-9


def test_a_draw_outside_thirty_two_bits_is_refused():
    with pytest.raises(ValueError):
        sync.assign_arm(2 ** 32)
    with pytest.raises(ValueError):
        sync.assign_arm(-1)


def test_the_plan_records_the_experiment_the_draw_the_probability_and_the_outcome():
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(arm=7))

    record = plan["experiment"]["arm"]
    assert record["experiment_id"] == sync.SYNC_EXPERIMENT_ID
    assert record["random_draw_u32"] == 7
    assert record["assignment_probability"] == pytest.approx(1 / 12)
    assert record["assigned_arm"] == sync.assign_arm(7)["name"]
    assert record["assignment_rule"] == "draw modulo 12"
    assert len(record["arms"]) == 12
    assert "modulo" in record["basis"] and "uniform" in record["basis"]


def test_the_pairing_is_ninety_ten_over_the_whole_draw_space():
    """Counted over all 2**32 draws, not sampled: a 10% arm is worth exactly.

    The mismatched sweeps are the only unconfounded arm comparison this project
    has, so the rate they arrive at is the rate the comparison accumulates at.
    """
    matched = sum(2 ** 32 // 10 + (1 if residue < 2 ** 32 % 10 else 0)
                  for residue in range(10)
                  if sync.assign_pairing(residue) == "matched")

    assert matched / 2 ** 32 == pytest.approx(0.9, abs=1e-8)
    assert sync.MATCHED_PAIRING_PROBABILITY == 0.9
    assert sync.assign_pairing(9) == "mismatched"
    assert {sync.assign_pairing(residue) for residue in range(10)} == {
        "matched", "mismatched"}


def test_the_pairing_draw_is_its_own_thirty_two_bits_and_its_own_record():
    """Recorded separately from the arm so either can be audited alone."""
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(arm=5, pairing=9))

    pairing = plan["experiment"]["pairing"]
    assert pairing["random_draw_u32"] == 9
    assert pairing["random_draw_u32"] != plan["experiment"]["arm"]["random_draw_u32"]
    assert pairing["assignment_probability"] == 0.9
    assert pairing["outcome"] == "mismatched"
    assert pairing["assignment_rule"] == "draw modulo 10 below 9"


def test_a_matched_sweep_holds_probe_length_and_rate_across_both_radios():
    """The 90%: any difference between the radios is hardware or sky."""
    plan = sync.draw_sweep_plan(
        _RADIOS, draws=_draws(arm=5, pairing=0, second_arm=11))

    arms = [entry["arm"] for entry in plan["radios"]]
    assert plan["experiment"]["pairing"]["outcome"] == "matched"
    assert arms[0]["name"] == arms[1]["name"] == sync.assign_arm(5)["name"]
    assert arms[0]["probe_s"] == arms[1]["probe_s"]
    assert arms[0]["sample_rate_hz"] == arms[1]["sample_rate_hz"]


def test_a_mismatched_sweep_gives_each_radio_its_own_independently_drawn_arm():
    """The 10%: two different arms on one sky at one instant.

    Silently handing both radios the first radio's arm would look identical in
    every timing number and would destroy the only unconfounded arm comparison
    the corpus gets, so both arms are asserted against their own draws.
    """
    plan = sync.draw_sweep_plan(
        _RADIOS, draws=_draws(arm=5, pairing=9, second_arm=11))

    arms = [entry["arm"] for entry in plan["radios"]]
    assert plan["experiment"]["pairing"]["outcome"] == "mismatched"
    assert arms[0]["name"] == sync.assign_arm(5)["name"]
    assert arms[1]["name"] == sync.assign_arm(11)["name"]
    assert arms[0]["name"] != arms[1]["name"]
    assert plan["experiment"]["pairing"]["second_arm_draw_u32"] == 11


def test_order_l_is_todays_tuning_list_and_order_u_swaps_the_two_edges():
    from leo_tracker.radio.beacon.presurvey import LOW_BAND_TUNINGS

    assert sync.edge_order_tunings("L") == LOW_BAND_TUNINGS
    assert sync.edge_order_tunings("U") == tuple(
        (channel, edge) for channel in (1, 2, 3, 4)
        for edge in ("upper", "lower"))
    assert {sync.assign_edge_order(0), sync.assign_edge_order(1)} == {"L", "U"}


def test_the_edge_order_is_drawn_per_radio_and_never_shared():
    """The scientific point of the sweep, so it is asserted at the draw.

    One draw shared by both radios would leave them permanently on the same
    tuning, and the opposite-order sweeps are the only observation that puts
    both edges of one channel at one instant.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(edge_orders=(0, 1)))

    orders = [entry["edge_order"] for entry in plan["radios"]]
    assert orders == ["L", "U"]
    assert plan["radios"][0]["tunings"] == [
        list(item) for item in sync.edge_order_tunings("L")]
    assert plan["radios"][1]["tunings"] == [
        list(item) for item in sync.edge_order_tunings("U")]
    assert plan["radios"][0]["tunings"][0] == [1, "lower"]
    assert plan["radios"][1]["tunings"][0] == [1, "upper"]


def test_the_edge_order_is_still_drawn_per_radio_on_a_matched_arm_sweep():
    """Never coupled to the arm draw or to the pairing decision."""
    plan = sync.draw_sweep_plan(
        _RADIOS, draws=_draws(arm=5, pairing=0, edge_orders=(1, 0)))

    assert plan["experiment"]["pairing"]["outcome"] == "matched"
    assert [entry["arm"]["name"] for entry in plan["radios"]] == [
        sync.assign_arm(5)["name"]] * 2
    assert [entry["edge_order"] for entry in plan["radios"]] == ["U", "L"]


def test_both_edge_orders_occur_and_each_radios_draw_is_recorded():
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(edge_orders=(4, 7)))

    record = plan["experiment"]["edge_order"]
    assert record["pluto-19f2"]["random_draw_u32"] == 4
    assert record["pluto-5d4d"]["random_draw_u32"] == 7
    assert record["pluto-19f2"]["assignment_probability"] == 0.5
    assert record["pluto-19f2"]["order"] == "L"
    assert record["pluto-5d4d"]["order"] == "U"
    assert record["drawn_per_radio"] is True
    assert record["shared_between_radios"] is False


def test_opposite_orders_put_both_edges_of_one_channel_at_one_index():
    """What an opposite-order sweep buys, asserted rather than described."""
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(edge_orders=(0, 1)))

    first, second = (entry["tunings"] for entry in plan["radios"])
    for left, right in zip(first, second, strict=True):
        assert left[0] == right[0]
        assert {left[1], right[1]} == {"lower", "upper"}


def test_same_orders_put_the_identical_tuning_on_both_radios_at_one_index():
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(edge_orders=(0, 2)))

    first, second = (entry["tunings"] for entry in plan["radios"])
    assert first == second


def test_both_radios_reach_every_tuning_together_however_unequal_the_work():
    """The lockstep assertion, and the one that fails if the barrier goes.

    Radio A spends 40 ms per tuning and radio B spends 5 ms.  Held at a
    barrier, B waits and the two arrive within a millisecond or so of each
    other at all eight tunings.  Free-running, B finishes its whole sweep while
    A is still on tuning 1 and the eighth tuning is observed about 245 ms
    apart, so this bound is what the barrier is buying.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())
    record, _ = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake,
        collect=_fake_collect({"pluto-19f2": 0.040, "pluto-5d4d": 0.005}))

    skews = [item["skew_ms"] for item in record["tunings"]]
    assert len(skews) == 8
    assert all(item["radios_arrived"] == 2 for item in record["tunings"])
    assert all(item["simultaneous"] for item in record["tunings"])
    assert max(skews) < 25.0
    assert record["skew"]["max_ms"] == pytest.approx(max(skews))
    assert record["skew"]["count"] == 8


def test_the_recorded_skew_is_the_difference_of_two_measured_arrival_stamps():
    """Measured, not asked for.

    Each radio's own arrival time is read in that radio's own thread the
    instant the barrier releases it, and the skew is the difference of those
    two numbers.  A record that stored the tolerance that was asked for --
    0.2 to 0.5 s -- or a nominal zero would satisfy every other assertion in
    this file, so the arrival stamps are pinned and the arithmetic checked.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())
    stamps = {"pluto-19f2": 0, "pluto-5d4d": 7_000_000}

    record, _ = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake, collect=_fake_collect({}),
        clock=_scripted_clock(stamps))

    assert [item["skew_ms"] for item in record["tunings"]] == [7.0] * 8
    for index, item in enumerate(record["tunings"]):
        assert item["arrivals"]["pluto-19f2"]["utc_ns"] == \
            _BASE_NS + index * 1_000_000_000
        assert item["arrivals"]["pluto-5d4d"]["utc_ns"] == \
            _BASE_NS + index * 1_000_000_000 + 7_000_000
        assert item["skew_ms"] == pytest.approx(abs(
            item["arrivals"]["pluto-19f2"]["utc_ns"] -
            item["arrivals"]["pluto-5d4d"]["utc_ns"]) / 1e6)
    assert record["skew"]["measured"] is True
    assert record["skew"]["median_ms"] == 7.0
    assert record["skew"]["operator_requested_tolerance_ms"] == [200.0, 500.0]


def test_the_headline_skew_is_when_sampling_began_not_when_the_barrier_released():
    """The barrier releasing is not the radios observing.

    After the barrier releases, each thread still has work to do before any
    sample exists, and on an opposite-order sweep the two radios are moving to
    *different* frequencies, so that work takes different times.  A record that
    reports the barrier release as the skew therefore reports a number that is
    optimistic by a factor of ten to a hundred and is biased by exactly the
    variable the sweep exists to study.  Measured on the radios: the barrier
    released a median 0.038 ms apart while sampling began about 4 ms apart on
    the opposite-order sweeps.

    So the headline is stamped where sampling begins.  The barrier release is
    kept -- it is how long the rendezvous itself cost -- but it is kept
    somewhere a reader cannot mistake for the answer.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())

    record, _ = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake, collect=_fake_collect({}),
        clock=_scripted_clock({"pluto-19f2": 0, "pluto-5d4d": 0},
                              {"pluto-19f2": 0, "pluto-5d4d": 3_990_000}))

    assert record["skew"]["event"] == "sample_start"
    assert [item["skew_ms"] for item in record["tunings"]] == [3.99] * 8
    assert record["skew"]["median_ms"] == 3.99
    assert record["skew"]["max_ms"] == 3.99
    # The old number survives, demoted, so the two can be compared rather than
    # one of them quietly replacing the other.
    assert [item["barrier_release_skew_ms"]
            for item in record["tunings"]] == [0.0] * 8
    assert record["skew"]["barrier_release"]["median_ms"] == 0.0
    assert record["skew"]["barrier_release"]["event"] == "barrier_release"
    for item in record["tunings"]:
        for radio_id, arrival in item["arrivals"].items():
            assert arrival["sample_start_utc_ns"] == \
                _BASE_NS + item["tuning_index"] * 1_000_000_000 + (
                    3_990_000 if radio_id == "pluto-5d4d" else 0)
            assert arrival["sample_start_utc_ns"] != arrival["utc_ns"] or \
                radio_id == "pluto-19f2"


def test_a_tuning_one_radio_never_sampled_has_no_skew_rather_than_a_zero():
    """"The other radio was not there" and "they started together" are opposites.

    The dying radio reaches the barrier at tuning three and raises before it
    reads a sample, so that tuning has one sample start and not two.  A zero
    there would read as the most perfectly simultaneous tuning in the sweep.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())

    record, _ = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake, barrier_timeout_s=1.0,
        collect=_fake_collect({"pluto-19f2": 0.002, "pluto-5d4d": 0.002},
                              fail_at={"pluto-5d4d": 3}))

    skews = [item["skew_ms"] for item in record["tunings"]]
    assert skews[:3] == [pytest.approx(item, abs=25.0) for item in [0.0] * 3]
    assert skews[3:] == [None] * 5
    assert record["skew"]["count"] == 3
    # Both radios did reach the barrier at three; only one of them sampled.
    assert record["tunings"][3]["radios_arrived"] == 2
    assert record["tunings"][3]["barrier_release_skew_ms"] is not None
    assert record["tunings"][3]["simultaneous"] is False


def test_the_two_radios_are_already_on_frequency_when_the_rendezvous_releases():
    """Tune, settle, THEN meet, THEN sample -- the fix, not the report of it.

    Recording the true offset makes the bias visible; moving the local
    oscillator write out of the critical path removes it.  The write costs
    about 6 ms and costs *different* amounts on the two radios whenever they
    are moving to different frequencies, which on an opposite-order sweep is
    every tuning -- so meeting before the write puts a deterministic,
    edge-order-correlated difference inside the offset the sweep is trying to
    make small.

    One radio here spends 30 ms writing its oscillator and the other spends
    none.  Meeting before the write, that 30 ms lands in the offset between the
    two sample starts.  Meeting after it, the fast radio waits at the barrier
    instead and the two begin sampling together.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(edge_orders=(0, 1)))

    record, _ = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake,
        collect=_fake_collect({}, tune_delays={"pluto-19f2": 0.030,
                                               "pluto-5d4d": 0.0}))

    slow, fast = record["radios"]
    # The expensive retune is real and is still measured...
    assert all(item["tune_ms"] > 25 for item in slow["per_tuning"])
    assert all(item["tune_ms"] < 5 for item in fast["per_tuning"])
    # ...and it is no longer inside the offset between the observations.
    skews = [item["skew_ms"] for item in record["tunings"]]
    assert len(skews) == 8 and all(item is not None for item in skews)
    assert max(skews) < 5.0
    # The fast radio pays for it by waiting, which is the point: the wait is
    # outside the measurement rather than inside it.
    assert fast["barrier_wait_ms"] > 8 * 20
    assert record["barrier"]["meets_at"] == "before_listen"
    assert "on frequency" in record["barrier"]["basis"]


def test_each_radio_keeps_its_own_arrival_time_per_tuning():
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(edge_orders=(0, 1)))
    record, _ = sync.sweep_pair(_RADIOS, plan, open_context=_open_fake,
                                collect=_fake_collect({}))

    for entry in record["radios"]:
        assert len(entry["per_tuning"]) == 8
        stamps = [item["arrival_utc_ns"] for item in entry["per_tuning"]]
        assert stamps == sorted(stamps)
        assert all(item["synchronised"] for item in entry["per_tuning"])
    first, second = record["radios"]
    assert [item["region"] for item in first["per_tuning"]][:2] == [
        "lower-edge", "upper-edge"]
    assert [item["region"] for item in second["per_tuning"]][:2] == [
        "upper-edge", "lower-edge"]


def test_a_mismatched_sweep_completes_with_the_fast_radio_waiting():
    """Unequal work per tuning, both arms recorded, skew still measured.

    The dearest arm against the cheapest is 80 ms at 1.25 MS/s beside 640 ms at
    10 MS/s.  The barrier has to tolerate one side finishing far sooner and
    waiting, the sweep costs the slower arm, and the record has to name both.
    """
    plan = sync.draw_sweep_plan(
        _RADIOS, draws=_draws(arm=0, pairing=9, second_arm=11))
    record, collected = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake,
        collect=_fake_collect({"pluto-19f2": 0.002, "pluto-5d4d": 0.030}))

    fast, slow = record["radios"]
    assert record["experiment"]["pairing"]["outcome"] == "mismatched"
    assert fast["arm"]["name"] == "80ms-1.25MSps"
    assert slow["arm"]["name"] == "640ms-10MSps"
    assert fast["arm"]["name"] != slow["arm"]["name"]
    assert fast["state"] == slow["state"] == "complete"
    assert fast["completed_tuning_count"] == slow["completed_tuning_count"] == 8
    assert record["sweep_mode"] == "paired"
    assert record["solo"] is False
    assert record["skew"]["count"] == 8
    assert record["skew"]["max_ms"] < 25.0
    # The sweep costs the slower arm: eight tunings at 30 ms, not at 2 ms.
    assert record["wall_ms"] > 8 * 30
    # And the record has to *show* that, rather than leave it to be inferred:
    # the fast radio spent the difference waiting at the barrier.
    assert fast["barrier_wait_ms"] > 8 * 20
    assert slow["barrier_wait_ms"] < fast["barrier_wait_ms"] / 4
    assert all(item["barrier_wait_ms"] > 20 for item in fast["per_tuning"][1:])
    assert set(collected) == {"pluto-19f2", "pluto-5d4d"}
    assert collected["pluto-19f2"]["sample_rate_hz"] == 1_250_000.0
    assert collected["pluto-5d4d"]["sample_rate_hz"] == 10_000_000.0


def test_a_radio_that_cannot_be_opened_leaves_the_other_sweeping_solo():
    """Radios were physically unplugged twice in one day; this is that day."""
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())

    def open_context(spec):
        if spec.radio_id == "pluto-5d4d":
            raise OSError("No such device")
        return _FakeContext(spec.radio_id)

    started = time.perf_counter()
    record, collected = sync.sweep_pair(
        _RADIOS, plan, open_context=open_context, collect=_fake_collect({}),
        barrier_timeout_s=1.0)
    elapsed = time.perf_counter() - started

    survivor, absent = record["radios"]
    assert survivor["state"] == "complete"
    assert survivor["completed_tuning_count"] == 8
    assert survivor["solo"] is True
    assert survivor["solo_tuning_count"] == 8
    assert absent["state"] == "failed_to_open"
    assert "No such device" in absent["error"]
    assert absent["completed_tuning_count"] == 0
    assert record["solo"] is True
    # A sweep that never had a partner and one that lost its partner half way
    # are different observations and must not share a value here.
    assert record["sweep_mode"] == "solo"
    assert record["paired_tuning_count"] == 0
    assert survivor["solo_from_tuning_index"] == 0
    assert [item["radios_arrived"] for item in record["tunings"]] == [1] * 8
    assert [item["skew_ms"] for item in record["tunings"]] == [None] * 8
    assert record["skew"]["count"] == 0
    assert set(collected) == {"pluto-19f2"}
    # The survivor must not sit out the barrier timeout eight times over.
    assert elapsed < 1.0


def test_a_radio_that_dies_mid_sweep_leaves_the_other_completing():
    """Recorded per tuning: paired up to the death, solo after it."""
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())

    started = time.perf_counter()
    record, collected = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake, barrier_timeout_s=1.0,
        collect=_fake_collect({"pluto-19f2": 0.002, "pluto-5d4d": 0.002},
                              fail_at={"pluto-5d4d": 3}))
    elapsed = time.perf_counter() - started

    survivor, dead = record["radios"]
    assert survivor["state"] == "complete"
    assert survivor["completed_tuning_count"] == 8
    assert dead["state"] == "failed_mid_sweep"
    assert "disappeared mid-sweep" in dead["error"]
    assert dead["completed_tuning_count"] == 3
    assert [item["tuning_index"] for item in dead["per_tuning"]] == [0, 1, 2]
    assert [item["synchronised"] for item in survivor["per_tuning"]][:3] == \
        [True] * 3
    assert [item["synchronised"] for item in survivor["per_tuning"]][4:] == \
        [False] * 4
    assert [item["radios_arrived"] for item in record["tunings"]] == \
        [2, 2, 2, 2, 1, 1, 1, 1]
    assert [item["simultaneous"] for item in record["tunings"]][:3] == [True] * 3
    assert [item["simultaneous"] for item in record["tunings"]][4:] == [False] * 4
    assert [item["skew_ms"] for item in record["tunings"]][4:] == [None] * 4
    assert record["solo"] is True
    assert record["sweep_mode"] == "partly_paired"
    # Index 3 is where the radio died: whether its barrier release beat the
    # abort is a genuine race, so the record may place the loss at 3 or at 4.
    assert survivor["solo_from_tuning_index"] in (3, 4)
    assert record["paired_tuning_count"] in (3, 4)
    assert set(collected) == {"pluto-19f2"}
    assert "no IQ survives" in dead["partial_iq_note"]
    # The survivor finishes rather than blocking on a radio that is gone.
    assert elapsed < 1.0


def test_a_radio_that_hangs_rather_than_raises_does_not_wedge_the_sweep():
    """A hung radio is not a failed one, and only one of them is handled.

    Every failure path here aborts the rendezvous, which releases the survivor.
    A radio that neither returns nor raises -- a blocked USB read, a firmware
    that stopped answering -- aborts nothing.  The survivor finishes, the join
    never returns, the record is never written and the loop stops making
    observations at all.  Radios were physically unplugged twice in one day,
    so this is a Tuesday rather than a thought experiment.

    Bounded, recorded, and on to the next sweep.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())
    release = threading.Event()

    started = time.perf_counter()
    try:
        record, collected = sync.sweep_pair(
            _RADIOS, plan, open_context=_open_fake, barrier_timeout_s=0.2,
            join_timeout_s=1.5,
            collect=_fake_collect({}, hang={"pluto-5d4d": 3,
                                            "event": release}))
        elapsed = time.perf_counter() - started
    finally:
        release.set()

    survivor, hung = record["radios"]
    assert elapsed < 5.0
    assert survivor["state"] == "complete"
    assert survivor["completed_tuning_count"] == 8
    # Named for what it was, not folded into the failure that it is not.
    assert hung["state"] == "hung"
    assert hung["abandoned"] is True
    assert "did not return" in hung["error"]
    assert record["join"]["timeout_s"] == 1.5
    assert record["join"]["abandoned_radios"] == ["pluto-5d4d"]
    assert record["sweep_mode"] != "paired"
    assert record["solo"] is True
    # And nothing is taken from a radio that is still, as far as this process
    # knows, in the middle of writing into its own buffers.
    assert set(collected) == {"pluto-19f2"}


def test_each_tuning_records_where_the_radio_landed_not_where_it_was_sent():
    """This project's exact historical failure, in a new record.

    ``deployed_shape`` recorded the scorer's configuration rather than the
    capture's.  Every row then agreed with every other row, a heterogeneous
    corpus looked uniform, and a second defect hid underneath that for days.
    The plan and the outcome are the same thing on every healthy sweep, which
    is exactly what makes the substitution invisible -- so the reader is made
    to disagree with the plan and the record has to follow the reader.

    Radio B is sent down order U and reports landing on order L instead.  A
    record built from the plan would label its probes with the frequencies it
    was asked for; the probes are of the other edge.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(edge_orders=(0, 1)))
    landed = list(sync.edge_order_tunings("L"))

    record, _ = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake,
        collect=_fake_collect({}, reports={"pluto-5d4d": landed}))

    sent, arrived = record["radios"]
    # What it was asked for is still recorded, unchanged...
    assert arrived["edge_order"] == "U"
    assert arrived["tunings"][0] == [1, "upper"]
    # ...and what it captured is recorded separately, and is not that.
    assert [(item["channel"], item["region"])
            for item in arrived["per_tuning"]] == [
                (channel, f"{edge}-edge") for channel, edge in landed]
    assert arrived["per_tuning"][0]["region"] == "lower-edge"
    assert sent["per_tuning"][0]["region"] == "lower-edge"
    # And the per-tuning rows the analysis host reads say the same thing: this
    # sweep put both radios on the *same* edge at index 0, whatever the plan
    # said, so it is not the both-edges observation it was drawn to be.
    first = record["tunings"][0]["arrivals"]
    assert first["pluto-19f2"]["region"] == "lower-edge"
    assert first["pluto-5d4d"]["region"] == "lower-edge"
    assert [item["arrivals"]["pluto-5d4d"]["region"]
            for item in record["tunings"]] == [
                f"{edge}-edge" for _, edge in landed]


def test_two_radios_that_both_sampled_but_never_met_are_not_simultaneous():
    """The one property that stops a non-simultaneous sweep being filed as one.

    Both radios reach every tuning and both sample every tuning, so counting
    who turned up says "simultaneous" at all eight.  They were nowhere near
    simultaneous: the barrier timed out, each radio proceeded alone, and the
    two observations are a quarter of a second apart.  ``synchronised`` is the
    only thing separating those two sweeps, and a record that stopped
    consulting it would file this one -- 250 ms apart, the barrier broken --
    as the very thing the experiment claims to produce.
    """
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())

    record, _ = sync.sweep_pair(
        _RADIOS, plan, open_context=_open_fake, barrier_timeout_s=0.05,
        collect=_fake_collect({}, tune_delays={"pluto-19f2": 0.0,
                                               "pluto-5d4d": 0.250}))

    assert all(item["radios_arrived"] == 2 for item in record["tunings"])
    assert all(item["radios_sampled"] == 2 for item in record["tunings"])
    assert all(item["skew_ms"] > 100.0 for item in record["tunings"])
    for item in record["tunings"]:
        assert not all(entry["synchronised"]
                       for entry in item["arrivals"].values())
        assert item["simultaneous"] is False
    assert record["paired_tuning_count"] == 0
    assert record["sweep_mode"] == "solo"
    assert record["skew"]["simultaneous_tuning_count"] == 0


def test_a_sweep_where_both_radios_return_records_no_abandonment():
    record, _ = _run()

    assert record["join"]["abandoned_radios"] == []
    assert record["join"]["bounded"] is True
    assert record["join"]["timeout_s"] > 0
    for entry in record["radios"]:
        assert entry["abandoned"] is False


def test_the_lowest_rate_arm_is_flagged_pilot_band_clipped_on_every_record():
    """1.25 MS/s samples 1.25 MHz of a 1.875 MHz pilot band.

    ``pilot_guard_hz`` returns -312.5 kHz there and roughly a third of the
    eight edge-pilot subcarriers fall outside what was recorded.  The radio
    returns healthy-looking samples regardless, which is exactly why the flag
    has to be stored on every record the arm produces rather than inferred by
    whoever remembers to divide.
    """
    from leo_tracker.radio.beacon.fast_scan import pilot_guard_hz

    clipped = [arm for arm in sync.SYNC_ARMS if arm["pilot_band_clipped"]]
    assert {arm["sample_rate_hz"] for arm in clipped} == {1_250_000.0}
    assert len(clipped) == 3
    for arm in clipped:
        assert pilot_guard_hz(arm["sample_rate_hz"]) == -312_500.0
        assert arm["pilot_guard_hz"] == -312_500.0
        assert arm["pilot_band_outside_fraction"] == pytest.approx(1 / 3, abs=1e-3)
        assert "never recorded" in arm["pilot_band_note"]
    for arm in sync.SYNC_ARMS:
        if arm["sample_rate_hz"] > 1_875_000.0:
            assert arm["pilot_band_clipped"] is False


def test_the_clipped_flag_reaches_the_sweep_record_and_both_of_its_levels():
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(arm=0, pairing=0))
    record, _ = sync.sweep_pair(_RADIOS, plan, open_context=_open_fake,
                                collect=_fake_collect({}))

    assert record["experiment"]["arm"]["assigned_arm"] == "80ms-1.25MSps"
    assert record["pilot_band"]["any_clipped"] is True
    assert record["pilot_band"]["clipped_radios"] == [
        "pluto-19f2", "pluto-5d4d"]
    for entry in record["radios"]:
        assert entry["pilot_band_clipped"] is True
        assert entry["pilot_guard_hz"] == -312_500.0
        assert entry["arm"]["pilot_band_clipped"] is True
    assert all(item["pilot_band_clipped"] for item in record["tunings"])


def test_a_mismatched_sweep_flags_only_the_radio_that_clipped():
    """Pooling one clipped radio with one that fit is the failure to avoid."""
    plan = sync.draw_sweep_plan(
        _RADIOS, draws=_draws(arm=0, pairing=9, second_arm=1))
    record, _ = sync.sweep_pair(_RADIOS, plan, open_context=_open_fake,
                                collect=_fake_collect({}))

    clipped, intact = record["radios"]
    assert clipped["arm"]["sample_rate_hz"] == 1_250_000.0
    assert intact["arm"]["sample_rate_hz"] == 2_500_000.0
    assert clipped["pilot_band_clipped"] is True
    assert intact["pilot_band_clipped"] is False
    assert intact["pilot_guard_hz"] == 312_500.0
    assert record["pilot_band"]["clipped_radios"] == ["pluto-19f2"]
    assert record["pilot_band"]["any_clipped"] is True


def test_the_written_manifest_carries_the_clipped_flag_to_the_analysis_host():
    """The record that leaves this host is the one that must not lose it.

    The sweep record above lives in memory; the manifest beside the IQ is what
    the analysis host reads, so the flag is asserted where it will be read.
    """
    import json

    with _storage() as root:
        record, collected = _run(arm=0, pairing=0)
        written = sync.write_sweep(record, collected, storage_root=root,
                                   sweep_id="20260813T101500Z")

        for entry in written["captures"]:
            manifest = json.loads(
                (root / "captures" / entry["name"] / "manifest.json").read_text())
            assert manifest["pilot_band_clipped"] is True
            assert manifest["pilot_guard_hz"] == -312_500.0
            assert manifest["arm"]["name"] == "80ms-1.25MSps"
            assert manifest["arm"]["pilot_band_clipped"] is True
            assert manifest["sweep"]["pilot_band"]["any_clipped"] is True


def test_the_iq_lands_on_nvme_and_is_queued_for_the_existing_offload():
    import hashlib
    import json

    with _storage() as root:
        record, collected = _run()
        written = sync.write_sweep(record, collected, storage_root=root,
                                   sweep_id="20260813T101500Z")

        assert [entry["name"] for entry in written["captures"]] == [
            "sync-20260813T101500Z-pluto-19f2",
            "sync-20260813T101500Z-pluto-5d4d"]
        jobs = sorted((root / "staging" / "analysis-queue").glob("*.job"))
        assert len(jobs) == 2
        for entry, job in zip(written["captures"], jobs, strict=True):
            capture = root / "captures" / entry["name"]
            assert capture.is_dir()
            name, path, mode = job.read_text().rstrip("\n").split("\t")
            assert name == entry["name"]
            assert Path(path) == capture
            assert mode == "sync-scan"
            manifest = json.loads((capture / "manifest.json").read_text())
            assert manifest["state"] == "complete"
            assert manifest["schema"] == sync.SCAN_IQ_SCHEMA
            assert manifest["metadata"]["observation_mode"] == "sync-scan"
            assert manifest["chunks"], "the exporter verifies declared chunks"
            for chunk in manifest["chunks"]:
                blob = capture / chunk["path"]
                assert blob.stat().st_size == chunk["bytes"]
                assert hashlib.sha256(blob.read_bytes()).hexdigest() == \
                    chunk["sha256"]
            assert entry["bytes"] == sum(item["bytes"]
                                         for item in manifest["chunks"])
        assert written["bytes"] == sum(entry["bytes"]
                                       for entry in written["captures"])


def test_nothing_on_the_capture_path_writes_to_the_network_share():
    """The whole point is that the QNAP copy must not interfere with capture.

    Checked against the module's *code* rather than its text: docstrings say
    "never to /mnt/qnap01" and a substring search cannot tell that apart from a
    path being built.  Every string literal that is not a docstring is
    inspected, so a hardcoded share path anywhere in the writer fails this.
    """
    import ast

    tree = ast.parse(Path(sync.__file__).read_text())
    prose = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and (
                node.body and isinstance(node.body[0], ast.Expr) and
                isinstance(node.body[0].value, ast.Constant) and
                isinstance(node.body[0].value.value, str)):
            prose.add(id(node.body[0].value))
    literals = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and
                isinstance(node.value, str) and id(node) not in prose]

    assert literals, "the writer is expected to contain string literals"
    assert not [item for item in literals if "qnap" in item.lower()]
    assert not [item for item in literals if item.startswith("/mnt/")]

    watch = (Path(__file__).parents[1] /
             "scripts" / "starlink-sync-scan-watch.sh").read_text()
    assert "qnap" not in watch.lower()
    assert "/mnt/leo-nvme/leo-tracker" in watch


def test_every_byte_the_writer_writes_lands_under_the_storage_root():
    with _storage() as root:
        record, collected = _run()
        before = {item for item in root.rglob("*")}
        written = sync.write_sweep(record, collected, storage_root=root,
                                   sweep_id="20260813T101500Z")

        created = {item for item in root.rglob("*")} - before
        assert created, "the sweep is expected to have written something"
        for entry in written["captures"]:
            assert Path(entry["path"]).is_relative_to(root)
            assert Path(entry["job"]).is_relative_to(root)
        for item in created:
            assert item.is_relative_to(root / "captures") or \
                item.is_relative_to(root / "staging")


def test_a_solo_sweep_writes_only_the_radio_that_survived():
    with _storage() as root:
        plan = sync.draw_sweep_plan(_RADIOS, draws=_draws())

        def open_context(spec):
            if spec.radio_id == "pluto-5d4d":
                raise OSError("No such device")
            return _FakeContext(spec.radio_id)

        record, collected = sync.sweep_pair(
            _RADIOS, plan, open_context=open_context,
            collect=_fake_collect({}), barrier_timeout_s=1.0)
        written = sync.write_sweep(record, collected, storage_root=root,
                                   sweep_id="20260813T101500Z")

        assert [entry["name"] for entry in written["captures"]] == [
            "sync-20260813T101500Z-pluto-19f2"]
        assert len(list((root / "staging" / "analysis-queue").glob("*.job"))) == 1
        assert written["skipped"] == [
            {"radio_id": "pluto-5d4d", "state": "failed_to_open",
             "reason": "no IQ was collected"}]


def test_no_dwell_is_performed_and_the_record_says_so():
    """Scans only. A dwell would commit two minutes and gigabytes per channel."""
    record, _ = _run()
    watch = (Path(__file__).parents[1] /
             "scripts" / "starlink-sync-scan-watch.sh").read_text()

    assert record["dwell"] == {"performed": False, "note": record["dwell"]["note"]}
    assert record["dwell"]["performed"] is False
    assert "never dwells" in record["dwell"]["note"]
    assert "starlink-beacon-capture" not in watch
    assert "--duration-s" not in watch
    assert "capture_beacon_iq" not in Path(sync.__file__).read_text()
    # Eight tunings each, and nothing else: a dwell would be a ninth read at a
    # fixed tuning lasting orders of magnitude longer.
    for entry in record["radios"]:
        assert entry["completed_tuning_count"] == 8


def test_collect_radio_takes_a_rendezvous_hook_before_and_after_each_tuning():
    """The production sweep is this same code with the real reader passed in.

    Everything above drives a stand-in, so the one thing that has to be checked
    against the reader itself is that it offers the two hooks the barrier hangs
    off, calls them once per tuning, and calls them in the right order relative
    to the retune.
    """
    from leo_tracker.radio.beacon.fast_scan import collect_radio, survey_profile

    from leo_tracker.radio.beacon.channels import starlink_edge_pilot_if_hz

    context, calls = _fake_context(), []
    tunings = [(1, "lower"), (1, "upper"), (2, "lower")]
    centres = [int(starlink_edge_pilot_if_hz(channel, edge, 9_750_000_000.0))
               for channel, edge in tunings]

    collected = collect_radio(
        context, tunings, profile=survey_profile(0.002, 250_000.0),
        sample_rate_hz=250_000.0,
        before_tuning=lambda index: calls.append(("before", index,
                                                  context.tuned_hz)),
        before_listen=lambda index: calls.append(("listen", index,
                                                  context.tuned_hz)),
        after_tuning=lambda index, entry: calls.append(
            ("after", index, entry["channel"], entry["region"])))

    assert [item[0] for item in calls] == [
        "before", "listen", "after"] * 3
    assert [item[1] for item in calls] == [0, 0, 0, 1, 1, 1, 2, 2, 2]
    # ``before_tuning`` fires with the local oscillator write still to come:
    # it sees the *previous* tuning's centre, and nothing at all on the first.
    assert [item[2] for item in calls if item[0] == "before"] == [
        0, centres[0], centres[1]]
    # ``before_listen`` fires with the radio already parked on this tuning's
    # centre, which is the whole reason a paired sweep meets there and not
    # above: everything that costs a different amount on the two radios has
    # been paid by the time it fires.
    assert [item[2] for item in calls if item[0] == "listen"] == centres
    assert [item[2:] for item in calls if item[0] == "after"] == [
        (1, "lower-edge"), (1, "upper-edge"), (2, "lower-edge")]
    assert [item["tuning_index"] for item in collected["plan"]] == [0, 1, 2]
    assert all(item["tuning_ms"] > 0 for item in collected["plan"])
    assert all(item["sample_start_utc_ns"] > 0 for item in collected["plan"])
    assert all(item["sample_end_utc_ns"] >= item["sample_start_utc_ns"]
               for item in collected["plan"])
    assert collected["samples"].shape[0] == 3


def test_the_reader_never_holds_two_copies_of_the_sweep_it_is_collecting():
    """A 4 GB host, two radios, and the dearest arm is 410 MB of probes each.

    ``np.stack`` at the end of a sweep builds a second copy of every probe in
    it while the first is still held by the list it was appended to, so the
    reader transiently needs twice what it returns.  Both radios do that at
    once, and peak RSS on the dearest matched arm reached 1.4-1.5 GB of the
    host's 4 GB while two other capture services were running.

    Measured here rather than argued: the reader fills one array it allocated
    up front, so the transient is one driver block and not a second sweep.
    """
    import tracemalloc

    from leo_tracker.radio.beacon.fast_scan import collect_radio, survey_profile

    tunings = [(channel, edge) for channel in (1, 2, 3, 4)
               for edge in ("lower", "upper")]
    profile = survey_profile(0.020, 1_000_000.0)

    tracemalloc.start()
    try:
        collected = collect_radio(_fake_context(), tunings, profile=profile,
                                  sample_rate_hz=1_000_000.0)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    retained = collected["samples"].nbytes
    assert collected["samples"].shape == (8, 20_000, 2, 2)
    assert retained == 8 * 20_000 * 2 * 2 * 2
    # One sweep, plus one tuning's worth of driver block in flight, plus slack.
    # Stacking a list of eight would need 2x the sweep and nothing else here
    # is large enough to make up the difference.
    assert peak < retained * 1.45, (
        f"peak {peak} bytes against {retained} bytes retained: the reader is "
        "holding the sweep more than once")


def test_the_sweep_states_the_bound_on_what_it_holds():
    """The number an operator sizes the host against, in the record."""
    record, _ = _run()

    held = record["memory"]["retained_iq_bytes"]
    assert set(held) == {"pluto-19f2", "pluto-5d4d"}
    assert record["memory"]["retained_iq_bytes_total"] == sum(held.values())
    for entry in record["radios"]:
        assert held[entry["radio_id"]] == entry["iq_bytes"]
    assert "preallocat" in record["memory"]["basis"]


def test_collect_radio_still_works_with_no_hooks_at_all():
    """The survey path passes neither, and must be unaffected."""
    from leo_tracker.radio.beacon.fast_scan import collect_radio, survey_profile

    collected = collect_radio(_fake_context(), [(1, "lower"), (2, "upper")],
                              profile=survey_profile(0.002, 250_000.0),
                              sample_rate_hz=250_000.0)

    assert collected["tunings"] == 2
    assert collected["samples"].shape[0] == 2


def test_the_reader_no_longer_claims_radio_time_ignores_sample_rate():
    """It does not, and the sweep's whole cost model depends on which is true.

    An 80 ms probe measured 108 / 137 / 193 / 309 ms of radio time at 1.25 /
    2.5 / 5 / 10 MS/s -- 1.35x to 3.87x real time.  The docstring said rate did
    not matter, which is what a mismatched sweep's barrier timeout would have
    been sized against.
    """
    from leo_tracker.radio.beacon.fast_scan import collect_radio

    text = collect_radio.__doc__
    assert "Radio time depends on probe duration, not on sample rate" not in text
    assert "10.0 MS/s -> 309 ms" in text
    assert "1.25 MS/s -> 108 ms" in text
    assert "3.87" in text

    # The same false claim is restated in the survey that calls it, and a
    # claim wrong in two places is worse than a claim wrong in one.
    from leo_tracker.radio.beacon import presurvey

    assert "both rates alike" not in presurvey.__doc__
    assert "cost follows probe *duration* only" not in presurvey.__doc__


def test_the_offload_queue_recognises_a_sync_scan_recording():
    """The exporter reconciles by walking every preserved recording.

    An observation mode it does not know raises inside that walk, which the
    backfill catches per recording -- so an unregistered mode would not crash
    the exporter, it would quietly leave every sync-scan sweep unqueued and
    accumulate an error per sweep per pass. That is the worst of both.
    """
    from leo_tracker.radio.beacon import offload

    assert sync.SCAN_OBSERVATION_MODE in offload.OBSERVATION_MODES
    assert offload.observation_mode(
        "sync-20260813T101500Z-pluto-19f2",
        {"observation_mode": sync.SCAN_OBSERVATION_MODE}) == "sync-scan"
    # And it resolves from the name alone, which is what a manifest written
    # before this field existed would have to fall back on.
    assert offload.observation_mode(
        "sync-20260813T101500Z-pluto-19f2", {}) == "sync-scan"
    assert offload.observation_mode("ch4-lower-edge-narrow-x", {}) == "narrow"


def _fake_context():
    """The smallest libiio stand-in the reader will accept."""
    import sys
    import types

    import numpy as np

    class _Attr:
        def __init__(self, value=""):
            self.value = value

    class _Channel:
        def __init__(self, identifier, output=False):
            self.id, self.output, self.enabled = identifier, output, False
            self.attrs = {"frequency": _Attr("0"), "sampling_frequency": _Attr("0"),
                          "rf_bandwidth": _Attr("0"), "hardwaregain": _Attr("0"),
                          "gain_control_mode": _Attr("manual")}

    class _Device:
        def __init__(self, channels):
            self.channels = channels

        def set_kernel_buffers_count(self, count):
            self.kernel_buffers = count

    class _Buffer:
        def __init__(self, device, size, cyclic):
            self._size = size

        def refill(self):
            time.sleep(0.001)

        def read(self):
            return np.zeros(self._size * 4, np.int16).tobytes()

    class _Context:
        def __init__(self):
            self._phy = _Device([_Channel("altvoltage0", output=True),
                                 _Channel("voltage0"), _Channel("voltage1")])
            self._stream = _Device([_Channel("voltage0"), _Channel("voltage1")])

        @property
        def tuned_hz(self):
            return int(float(self._phy.channels[0].attrs["frequency"].value or 0))

        def find_device(self, name):
            return {"ad9361-phy": self._phy,
                    "cf-ad9361-lpc": self._stream}[name]

    module = types.ModuleType("iio")
    module.Buffer = _Buffer
    sys.modules.setdefault("iio", module)
    return _Context()


def test_the_unit_an_operator_starts_binds_both_radios_and_neither_dwell():
    unit = (Path(__file__).parents[1] / "deploy" / "systemd" /
            "leo-tracker-sync-scan.service").read_text()

    assert "ExecStart=/home/satpi01/leo-tracker/scripts/" \
           "starlink-sync-scan-watch.sh" in unit
    # One process owns both radios, so it must not run beside anything that
    # claims either of them: a USB context is an exclusive claim.
    assert "Conflicts=leo-tracker-beacon-watch@pluto-new.service " \
           "leo-tracker-beacon-watch@pluto-old.service " \
           "leo-tracker-beacon-watch.service" in unit
    assert "Requires=mnt-leo\\x2dnvme.mount" in unit
    assert "Environment=LEO_SYNC_RADIO_A=" in unit
    assert "Environment=LEO_SYNC_RADIO_B=" in unit
    assert "10400056f695001322002d0010ad1719f2" in unit
    assert "1040005e0b100007100010000bf33a5d4d" in unit
    assert "Restart=always" in unit
    assert "/mnt/qnap01" not in unit


def test_the_loop_refuses_to_start_against_an_exporter_that_cannot_file_it():
    """Starting this loop unmerged took down the offload of everything.

    The exporter's reconciliation walk classifies every preserved recording,
    and until this branch is merged the deployed checkout has never heard of
    ``sync-scan``.  It reported an error per sweep per pass, exited non-zero,
    and ``set -e`` plus ``Restart=always`` turned that into a crash loop that
    stopped ALL offload to QNAP -- narrow dwells, wide, hop, everything --
    over recordings nothing was wrong with.

    The exporter is now tolerant of a recording it cannot classify, which is
    the fix that matters.  This is the second half: the loop that writes them
    checks, before it writes any, that the exporter running beside it can file
    what it is about to produce, and says why in one line rather than filling a
    volume nothing will drain.
    """
    watch = (Path(__file__).parents[1] /
             "scripts" / "starlink-sync-scan-watch.sh").read_text()
    unit = (Path(__file__).parents[1] / "deploy" / "systemd" /
            "leo-tracker-sync-scan.service").read_text()

    # Said plainly, where an operator starting the unit will read it.
    assert "must be merged" in unit.lower()
    assert "must be merged" in watch.lower()
    assert "stops all offload" in unit.lower()
    # And checked, before the first sweep, against the checkout the exporter
    # actually imports rather than against this one.
    assert "OBSERVATION_MODES" in watch
    assert "sync_scan_exporter_unaware" in watch
    guard = watch.split("# --- exporter compatibility gate ---", 1)[1]
    assert "exit 2" in guard.split("\nfi\n", 1)[0]
    # Before anything is drawn or written, not after a volume of sweeps.
    assert watch.index("# --- exporter compatibility gate ---") < \
        watch.index("draw_u32() {")


def test_the_leave_it_alone_beacon_watch_path_is_untouched():
    """This must be reversible: the old loop stays in the tree, unused."""
    root = Path(__file__).parents[1]

    assert (root / "scripts" / "starlink-beacon-watch.sh").is_file()
    assert (root / "deploy" / "systemd" /
            "leo-tracker-beacon-watch@.service").is_file()
    # And the new unit does not disable it, it only refuses to share a radio.
    unit = (root / "deploy" / "systemd" /
            "leo-tracker-sync-scan.service").read_text()
    assert "Conflicts=" in unit


def test_the_command_refuses_a_sweep_that_is_secretly_one_radio_twice(capsys):
    """A sweep filed against one radio twice measures nothing and looks fine."""
    from leo_tracker.radio.cli import main

    with _storage() as root:
        assert main(["starlink-synchronised-scan", "--storage", str(root),
                     "--sweep-id", "20260813T101500Z",
                     "--radio", "a", "pluto://usb:", "same", "lnb-a", "lnb-b",
                     "--radio", "b", "pluto://usb:", "same", "lnb-c", "lnb-d"]) != 0
        assert "distinct serials" in capsys.readouterr().err
        assert main(["starlink-synchronised-scan", "--storage", str(root),
                     "--sweep-id", "20260813T101500Z",
                     "--radio", "a", "pluto://usb:", "one", "lnb-a", "lnb-b"]) != 0
        assert "exactly two radios" in capsys.readouterr().err
        assert not list(root.rglob("manifest.json"))


def _run(**draws):
    plan = sync.draw_sweep_plan(_RADIOS, draws=_draws(**draws))
    return sync.sweep_pair(_RADIOS, plan, open_context=_open_fake,
                           collect=_fake_collect({}))


@contextlib.contextmanager
def _storage():
    with tempfile.TemporaryDirectory(dir=_SCRATCH) as name:
        yield Path(name)


class _FakeContext:
    def __init__(self, radio_id):
        self.radio_id = radio_id


def _open_fake(spec):
    return _FakeContext(spec.radio_id)


def _fake_collect(delays, *, tune_delays=None, fail_at=None, hang=None,
                  reports=None):
    """A ``collect_radio`` stand-in, in the reader's own order.

    Tune, settle, *then* rendezvous, then sample: the same order the real
    reader runs in, because a stand-in that met the barrier somewhere else
    would let the sweep pass a test the radios would fail.  ``delays`` is time
    spent sampling, after the barrier; ``tune_delays`` is time spent writing
    the local oscillator, before it.

    ``reports`` lets a radio say it landed somewhere other than where it was
    asked to go, which is the only way to tell a record that states the
    capture from one that states the plan.
    """
    import numpy as np

    def collect(context, tunings, *, profile, sample_rate_hz, lnb_lo_hz,
                before_tuning=None, before_listen=None, after_tuning=None,
                sample_clock=None):
        pause = delays.get(context.radio_id, 0.0)
        tune_pause = (tune_delays or {}).get(context.radio_id, 0.0)
        stamp = sample_clock or (lambda index, event: time.time_ns())
        reported = (reports or {}).get(context.radio_id)
        plan = []
        for index, (channel, edge) in enumerate(tunings):
            if before_tuning is not None:
                before_tuning(index)
            if tune_pause:
                time.sleep(tune_pause)
            if before_listen is not None:
                before_listen(index)
            if hang is not None and hang.get(context.radio_id) == index:
                hang["event"].wait(30.0)
            started = int(stamp(index, "sample_start"))
            if fail_at is not None and fail_at.get(context.radio_id) == index:
                raise RuntimeError("radio disappeared mid-sweep")
            if pause:
                time.sleep(pause)
            landed = reported[index] if reported is not None else (channel, edge)
            entry = {"channel": landed[0], "region": f"{landed[1]}-edge",
                     "edge": landed[1],
                     "if_center_hz": 1.7e9, "rf_center_hz": 1.15e10,
                     "tuning_index": index,
                     "sample_start_utc_ns": started,
                     "sample_end_utc_ns": int(stamp(index, "sample_end")),
                     "tune_ms": tune_pause * 1000,
                     "settle_ms": 0.0, "listen_ms": pause * 1000,
                     "tuning_ms": (tune_pause + pause) * 1000}
            plan.append(entry)
            if after_tuning is not None:
                after_tuning(index, entry)
        count = max(1, int(round(profile.probe_s * sample_rate_hz / 10_000)))
        samples = np.zeros((len(plan), count, 2, 2), "<i2")
        return {"samples": samples, "plan": plan,
                "sample_order": [(item["channel"], item["region"])
                                 for item in plan],
                "tunings": len(plan),
                "timing_ms": {"tune": 1.0, "settle": 0.0,
                              "listen": pause * 1000 * len(plan)},
                "radio_ms": pause * 1000 * len(plan) + 1.0,
                "radio_ms_per_tuning": pause * 1000,
                "sample_rate_hz": float(sample_rate_hz),
                "probe_s": float(profile.probe_s),
                "samples_per_tuning": int(samples.shape[1]),
                "iq_bytes": int(samples.nbytes),
                "pilot_guard_hz": 0.0,
                "profile": {"block_size": profile.block_size,
                            "probe_s": profile.probe_s}}
    return collect


_BASE_NS = 1_700_000_000_000_000_000


def _scripted_clock(offsets, sample=None):
    """A clock that pins each radio's stamps at each tuning.

    Three arguments rather than none precisely so a test can say what each
    radio measured, at which tuning, and at *which event*; production passes
    host UTC and ignores all three.  ``sample`` defaults to ``offsets``, so a
    test that does not care makes the barrier release and the sample start
    coincide; a test that does care can drive them apart, which is the whole
    difference between the number that was reported and the number that
    matters.
    """
    starts = offsets if sample is None else sample

    def clock(radio_id, tuning_index, event="barrier_release"):
        base = _BASE_NS + tuning_index * 1_000_000_000
        if event == "barrier_release":
            return base + offsets[radio_id]
        return base + starts[radio_id] + (1_000_000 if event == "sample_end" else 0)
    return clock


_RADIOS = (
    sync.RadioSpec("pluto-19f2", uri="pluto://usb:", serial="serial-19f2",
                   receiver_labels=("lnb-c", "lnb-d")),
    sync.RadioSpec("pluto-5d4d", uri="pluto://usb:", serial="serial-5d4d",
                   receiver_labels=("lnb-a", "lnb-b")),
)


def _draws(*, arm=0, pairing=0, second_arm=0, edge_orders=(0, 0)):
    return {"arm": arm, "pairing": pairing, "second_arm": second_arm,
            "edge_order": dict(zip((item.radio_id for item in _RADIOS),
                                   edge_orders, strict=True))}
