"""Filing the interim synchronised sweeps into the survey corpus.

The sweeps are the only data on this site nothing has ever analysed, and the
one way to file them that destroys the experiment silently is to get
``sample_order`` wrong.  Half the sweeps are ``edge_order`` "U" -- the radio
scanned (1, upper), (1, lower), (2, upper) -- and the two radios of one sweep
disagree about the order in 632 of 1,205 sweeps, so the order cannot be taken
from the pair, from a constant, or from the peer.  It has to come from *this*
radio's own tunings list.  An entry imported with the canonical lower-first
order files every probe under the wrong sky while every other number in the
sidecar looks perfect, and the corpus has already been bitten once by position
agreeing with the record list only by luck.

So most of what is pinned here is the mapping, and the rest is what has to
survive with it: the arm, the peer, the skew, and the flag that says the pilot
band did not fit in the sampled spectrum.
"""
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from leo_tracker.radio.beacon.survey_scoring import (ProbeUnusable, read_probe,
                                                     tuning_plan)
from leo_tracker.radio.beacon.sync_import import (SweepUnusable, entry_name,
                                                  import_status, import_sweep,
                                                  run)

#: The order the collector calls "L" and the one it calls "U".
L_ORDER = [[channel, edge] for channel in (1, 2, 3, 4)
           for edge in ("lower", "upper")]
U_ORDER = [[channel, edge] for channel in (1, 2, 3, 4)
           for edge in ("upper", "lower")]

#: What the corpus spells those as.
CANONICAL_REGIONS = [[channel, f"{edge}-edge"] for channel, edge in L_ORDER]
UPPER_FIRST_REGIONS = [[channel, f"{edge}-edge"] for channel, edge in U_ORDER]


def _arm(probe_s, rate):
    return {"name": f"{round(probe_s * 1000)}ms-{rate / 1e6:.2f}MSps",
            "probe_s": probe_s, "sample_rate_hz": rate,
            "pilot_band_fits": rate > 1_875_000.0}


def _radio(order, arm, labels, *, error=None, tunings=8, samples=None,
           order_draw=7):
    per_tuning = samples if samples is not None else int(
        round(arm["probe_s"] * arm["sample_rate_hz"]))
    listed = (U_ORDER if order == "U" else L_ORDER)[:tunings]
    return {"arm": dict(arm), "edge_order": order, "order_draw_u32": order_draw,
            "receiver_labels": list(labels), "tunings": listed,
            "iq": {"path": None, "bytes": tunings * per_tuning * 2 * 2 * 2,
                   "shape": [tunings, per_tuning, 2, 2]},
            "error": error}


def _sweep(root, utc, radios, *, matched_arm=True, wall_s=6.0, sparse=False,
           marker=True, skew=None):
    """One sweep directory shaped the way the interim collector writes one.

    ``marker`` stamps each tuning's I component with its own collection index,
    which is what lets a test ask whether a block of IQ was filed under the sky
    it was actually collected from rather than merely whether a string in the
    manifest reads the way it should.
    """
    sweep = Path(root) / f"sync-{utc}"
    sweep.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "leo-tracker.interim-synchronised-scan/v1",
               "sweep": 1, "utc": utc, "wall_s": wall_s,
               "matched_arm": matched_arm, "arm_draw_u32": 2398922215,
               "pairing_draw_u32": 3926988771, "radios": {},
               "skew_ms": skew or {"per_tuning": [0.18, 0.07, 0.08, 0.07,
                                                  0.05, 1.22, 0.05, 0.06],
                                   "median": 0.0765, "max": 1.2248}}
    for radio_id, block in radios.items():
        block = json.loads(json.dumps(block))
        block["iq"]["path"] = f"{radio_id}.ci16"
        payload["radios"][radio_id] = block
        if block.get("error"):
            continue
        shape = block["iq"]["shape"]
        path = sweep / block["iq"]["path"]
        if sparse:
            with path.open("wb") as stream:
                stream.truncate(block["iq"]["bytes"])
        else:
            values = np.zeros(tuple(shape), "<i2")
            if marker:
                values[..., 0] = np.arange(shape[0], dtype="<i2")[
                    :, None, None]
            path.write_bytes(values.tobytes())
    (sweep / "sweep.json").write_text(json.dumps(payload))
    return sweep


def _pair(root, utc, *, orders=("U", "U"), arm=None, matched_arm=True,
          errors=(None, None), skew=None, **kw):
    arm = arm or _arm(0.080, 2_500_000.0)
    radios = {
        "pluto-19f2": _radio(orders[0], arm, ["lnb-c", "lnb-d"],
                             error=errors[0], **kw),
        "pluto-5d4d": _radio(orders[1], arm, ["lnb-a", "lnb-b"],
                             error=errors[1], order_draw=9, **kw)}
    return _sweep(root, utc, radios, matched_arm=matched_arm, skew=skew)


def _manifest(entry):
    return json.loads((Path(entry) / "manifest.json").read_text())


def _record(entry):
    return _manifest(entry)["metadata"]["pre_dwell_survey"]


def _sync(entry):
    return _manifest(entry)["metadata"]["synchronised_sweep"]


# --------------------------------------------------------------------------
# the mapping, which is the whole experiment
# --------------------------------------------------------------------------

def test_a_u_order_sweep_imports_upper_first(tmp_path):
    """The order the radio scanned, taken from that radio's own tunings list.

    This is the failure the project has hit before and the one that leaves no
    trace: the canonical list is a valid eight-tuning order and every score
    computed against it is a real number about the wrong sky.
    """
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z", orders=("U", "U"))
    outcome = import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    order = _record(entry)["sample_order"]
    assert outcome["imported"] == 2
    assert [list(pair) for pair in order] == UPPER_FIRST_REGIONS
    assert [list(pair) for pair in order] != CANONICAL_REGIONS


def test_a_u_order_sweep_files_each_block_under_the_sky_it_came_from(tmp_path):
    """The mapping checked through the IQ, not through the manifest string.

    Each tuning's I component carries its own collection index, so asking
    ``tuning_plan`` for (1, lower-edge) and reading the block it names is a
    question the canonical order gets wrong: on a "U" sweep the lower edge of
    channel 1 was collected *second*, and an entry imported lower-first would
    hand back the block collected first.
    """
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z", orders=("U", "L"))
    import_sweep(sweep, tmp_path / "corpus")
    for radio_id, expected in (("pluto-19f2", 1), ("pluto-5d4d", 0)):
        entry = tmp_path / "corpus" / entry_name(sweep.name, radio_id)
        manifest = _manifest(entry)
        block = read_probe(entry, manifest)
        item = next(row for row in tuning_plan(manifest)
                    if (row["channel"], row["region"]) == (1, "lower-edge"))
        assert item["iq_index"] == expected
        assert int(block[item["iq_index"], 0, 0, 0]) == expected


def test_an_l_order_sweep_keeps_the_lower_first_order(tmp_path):
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z", orders=("L", "L"))
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    assert [list(pair) for pair in _record(entry)["sample_order"]] == \
        CANONICAL_REGIONS


def test_the_two_radios_of_one_sweep_keep_their_own_orders(tmp_path):
    """632 of 1,205 real sweeps have the two radios disagreeing about order.

    So the order can never be lifted from the sweep, only from the radio.
    """
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z", orders=("U", "L"))
    import_sweep(sweep, tmp_path / "corpus")
    first = _record(tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2"))
    second = _record(tmp_path / "corpus" / entry_name(sweep.name, "pluto-5d4d"))
    assert [list(p) for p in first["sample_order"]] == UPPER_FIRST_REGIONS
    assert [list(p) for p in second["sample_order"]] == CANONICAL_REGIONS


def test_regions_carry_the_edge_suffix_the_corpus_keys_on(tmp_path):
    """``tuning_plan`` keys on (channel, region) and refuses rather than guess.

    The sweep says "lower"; the corpus says "lower-edge".  Drop the suffix on
    either side of that translation and every lookup misses.
    """
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z")
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    manifest = _manifest(entry)
    regions = {row["region"] for row in _record(entry)["tunings"]}
    assert regions == {"lower-edge", "upper-edge"}
    plan = tuning_plan(manifest)
    assert len(plan) == 8
    assert {row["edge"] for row in plan} == {"lower", "upper"}


# --------------------------------------------------------------------------
# every arm, and nothing assumed about its shape
# --------------------------------------------------------------------------

@pytest.mark.parametrize("probe_s,rate,per_tuning", [
    (0.080, 1_250_000.0, 100_000),
    (0.080, 2_500_000.0, 200_000),
    (0.160, 5_000_000.0, 800_000),
    (0.640, 10_000_000.0, 6_400_000),
])
def test_every_arm_round_trips_through_read_probe(tmp_path, probe_s, rate,
                                                  per_tuning):
    """100,000 to 6,400,000 samples per tuning, and 200,000 is not the default.

    ``read_probe`` refuses a file that contradicts the declared shape and also
    refuses a shape that contradicts the declared probe length and rate, so a
    hard-coded sample count fails loudly here rather than quietly downstream.
    The two largest arms are written sparse: the bytes are real to every reader
    and the digest still covers all of them, but the pages are not.
    """
    sweep = _sweep(tmp_path / "sweeps", "20260814T000315Z",
                   {"pluto-19f2": _radio("U", _arm(probe_s, rate),
                                         ["lnb-c", "lnb-d"])},
                   sparse=per_tuning > 200_000)
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    manifest = _manifest(entry)
    assert manifest["survey_iq"]["samples_per_tuning"] == per_tuning
    assert read_probe(entry, manifest).shape == (8, per_tuning, 2, 2)
    assert len(tuning_plan(manifest)) == 8


def test_the_pilot_band_flag_survives_for_an_arm_that_does_not_fit(tmp_path):
    """1.25 MS/s leaves a guard of -312.5 kHz: subcarriers fall off the end.

    Pooling those with arms that fit would average a measurement over probes
    that never sampled the band being measured, so the flag has to arrive with
    the entry rather than be recomputed by whoever remembers to.
    """
    sweep = _sweep(tmp_path / "sweeps", "20260814T000315Z",
                   {"pluto-19f2": _radio("L", _arm(0.080, 1_250_000.0),
                                         ["lnb-c", "lnb-d"])})
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    record = _record(entry)
    assert record["capture_config"]["pilot_band_fits"] is False
    assert record["capture_config"]["pilot_guard_hz"] == pytest.approx(-312_500.0)
    assert _sync(entry)["arm"]["pilot_band_fits"] is False


def test_the_pilot_band_flag_survives_for_an_arm_that_does_fit(tmp_path):
    sweep = _sweep(tmp_path / "sweeps", "20260814T000315Z",
                   {"pluto-19f2": _radio("L", _arm(0.080, 5_000_000.0),
                                         ["lnb-c", "lnb-d"])})
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    assert _record(entry)["capture_config"]["pilot_band_fits"] is True


# --------------------------------------------------------------------------
# the synchronisation facts
# --------------------------------------------------------------------------

def test_the_synchronisation_facts_survive_the_import(tmp_path):
    """What makes a paired sweep worth more than two unrelated surveys.

    Without the peer and the skew the two entries are two surveys that happen
    to share a minute, and every cross-radio question the sweeps were collected
    to answer becomes unanswerable from the corpus.
    """
    skew = {"per_tuning": [0.18, 0.07, 0.08, 0.07, 0.05, 1.22, 0.05, 0.06],
            "median": 0.0765, "max": 1.2248}
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z", orders=("U", "L"),
                  matched_arm=False, skew=skew)
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    facts = _sync(entry)
    assert facts["sweep_id"] == "sync-20260814T000315Z"
    assert facts["radio_id"] == "pluto-19f2"
    assert facts["peer_radio_id"] == "pluto-5d4d"
    assert facts["peer_entry"] == entry_name(sweep.name, "pluto-5d4d")
    assert facts["skew_ms"]["per_tuning"] == skew["per_tuning"]
    assert facts["skew_ms"]["max"] == 1.2248
    assert facts["edge_order"] == "U"
    assert facts["peer_edge_order"] == "L"
    assert facts["arm"]["name"] == "80ms-2.50MSps"
    assert facts["matched_arm"] is False
    assert facts["arm"]["pilot_band_fits"] is True


def test_the_skew_is_keyed_to_this_radios_own_tunings(tmp_path):
    """A skew is a property of the slot, and the slots are not the same sky.

    The two radios scan slot ``i`` at the same instant, but when their edge
    orders differ slot ``i`` is a different tuning on each, so a consumer that
    keys skew by (channel, region) needs it mapped through the radio's own
    order rather than through the canonical one.
    """
    skew = {"per_tuning": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
            "median": 13.5, "max": 17.0}
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z", orders=("U", "L"),
                  skew=skew)
    import_sweep(sweep, tmp_path / "corpus")
    upper_first = _sync(tmp_path / "corpus"
                        / entry_name(sweep.name, "pluto-19f2"))
    lower_first = _sync(tmp_path / "corpus"
                        / entry_name(sweep.name, "pluto-5d4d"))
    by_tuning = {(row["channel"], row["region"]): row["skew_ms"]
                 for row in upper_first["skew_ms_by_tuning"]}
    assert by_tuning[(1, "upper-edge")] == 10.0
    assert by_tuning[(1, "lower-edge")] == 11.0
    peer = {(row["channel"], row["region"]): row["skew_ms"]
            for row in lower_first["skew_ms_by_tuning"]}
    assert peer[(1, "lower-edge")] == 10.0
    assert peer[(1, "upper-edge")] == 11.0


def test_the_peer_radio_is_named_even_when_the_peer_failed(tmp_path):
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z",
                  errors=(None, "iio: device busy"))
    import_sweep(sweep, tmp_path / "corpus")
    facts = _sync(tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2"))
    assert facts["peer_radio_id"] == "pluto-5d4d"
    assert facts["peer_imported"] is False
    assert facts["peer_error"] == "iio: device busy"


# --------------------------------------------------------------------------
# refusing, resuming, and the digest
# --------------------------------------------------------------------------

def test_a_sweep_with_one_radio_errored_imports_the_other(tmp_path):
    """One dead radio is half a sweep, not no sweep.

    The pair is the interesting unit but each radio's eight tunings are a
    complete survey on their own, and discarding the good half would throw away
    a probe nothing else holds.
    """
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z",
                  errors=(None, "iio: device busy"))
    outcome = import_sweep(sweep, tmp_path / "corpus")
    assert outcome["imported"] == 1
    assert outcome["skipped"] == 1
    assert (tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")).is_dir()
    assert not (tmp_path / "corpus"
                / entry_name(sweep.name, "pluto-5d4d")).exists()
    assert any("pluto-5d4d" in reason["radio_id"]
               for reason in outcome["reasons"])


def test_a_truncated_iq_file_is_refused_rather_than_reshaped(tmp_path):
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z")
    path = sweep / "pluto-19f2.ci16"
    path.write_bytes(path.read_bytes()[:-8])
    outcome = import_sweep(sweep, tmp_path / "corpus")
    assert outcome["imported"] == 1
    assert not (tmp_path / "corpus"
                / entry_name(sweep.name, "pluto-19f2")).exists()


def test_the_declared_digest_matches_the_bytes_actually_written(tmp_path):
    sweep = _pair(tmp_path / "sweeps", "20260814T000315Z")
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    block = _manifest(entry)["survey_iq"]
    written = (entry / "survey.ci16").read_bytes()
    assert block["sha256"] == hashlib.sha256(written).hexdigest()
    assert block["bytes"] == len(written)


def test_importing_twice_changes_nothing(tmp_path):
    """An interrupted import has to resume, not duplicate or rewrite.

    180 GB is hours of copying; a second pass that re-copied everything it
    already had would make an interrupted run more expensive than a fresh one.
    """
    sweeps = tmp_path / "sweeps"
    _pair(sweeps, "20260814T000315Z")
    _pair(sweeps, "20260814T000321Z")
    first = run(sweeps, tmp_path / "corpus")
    entries = sorted(p.name for p in (tmp_path / "corpus").iterdir())
    stamps = {p: os.stat(p).st_mtime_ns
              for p in (tmp_path / "corpus").glob("*/survey.ci16")}
    second = run(sweeps, tmp_path / "corpus")
    assert first["imported"] == 4 and first["already_imported"] == 0
    assert second["imported"] == 0 and second["already_imported"] == 4
    assert sorted(p.name for p in (tmp_path / "corpus").iterdir()) == entries
    assert {p: os.stat(p).st_mtime_ns
            for p in (tmp_path / "corpus").glob("*/survey.ci16")} == stamps


def test_a_half_written_entry_is_finished_rather_than_counted_as_done(tmp_path):
    """The manifest is what marks an entry complete, and it is written last."""
    sweeps = tmp_path / "sweeps"
    _pair(sweeps, "20260814T000315Z")
    run(sweeps, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name("sync-20260814T000315Z",
                                             "pluto-19f2")
    (entry / "manifest.json").unlink()
    outcome = run(sweeps, tmp_path / "corpus")
    assert outcome["imported"] == 1 and outcome["already_imported"] == 1
    assert (entry / "manifest.json").is_file()


def test_status_counts_what_is_left(tmp_path):
    sweeps = tmp_path / "sweeps"
    _pair(sweeps, "20260814T000315Z")
    _pair(sweeps, "20260814T000321Z")
    before = import_status(sweeps, tmp_path / "corpus")
    run(sweeps, tmp_path / "corpus", limit=1)
    after = import_status(sweeps, tmp_path / "corpus")
    assert before["sweeps"] == 2 and before["imported_entries"] == 0
    assert after["imported_entries"] == 2


def test_a_sweep_still_being_written_is_left_alone_and_counted(tmp_path):
    """The collector is live: the newest directory has no sweep.json yet.

    Reporting that as broken would put an error in every pass forever, and
    reporting it as nothing at all would hide a collector that had stopped
    halfway.  It is neither imported nor an error; it is counted under its own
    name.
    """
    _pair(tmp_path / "sweeps", "20260814T000315Z")
    (tmp_path / "sweeps" / "sync-20260814T000321Z").mkdir()
    outcome = run(tmp_path / "sweeps", tmp_path / "corpus")
    assert outcome["imported"] == 2 and outcome["unusable"] == 0
    assert outcome["in_progress"] == 1
    assert import_status(tmp_path / "sweeps",
                         tmp_path / "corpus")["in_progress"] == 1


def test_a_corrupt_sweep_json_is_named_rather_than_crashing(tmp_path):
    """A directory that has a record and cannot be read is a real failure."""
    sweep = tmp_path / "sweeps" / "sync-20260814T000315Z"
    sweep.mkdir(parents=True)
    (sweep / "sweep.json").write_text("{not json")
    with pytest.raises(SweepUnusable):
        import_sweep(sweep, tmp_path / "corpus")
    outcome = run(tmp_path / "sweeps", tmp_path / "corpus")
    assert outcome["unusable"] == 1 and outcome["imported"] == 0
    assert outcome["errors"][0]["sweep"] == "sync-20260814T000315Z"


# --------------------------------------------------------------------------
# and what the scorer then does with it
# --------------------------------------------------------------------------

def test_a_scored_entry_is_skipped_on_the_next_pass(tmp_path):
    """Re-running the driver must resume, not re-score.

    Scoring an entry costs minutes; a second pass that re-scored everything
    would make interrupting the run the most expensive thing an operator could
    do.
    """
    from leo_tracker.radio.beacon import survey_scoring

    sweeps = tmp_path / "sweeps"
    # 15,000 samples is 4.5 frames, the fold's own minimum, and the cheapest
    # thing the real scorer will accept; the arms themselves are proven on
    # real sweeps rather than here.
    sweep = _sweep(sweeps, "20260814T000315Z",
                   {"pluto-19f2": _radio("U", _arm(0.006, 2_500_000.0),
                                         ["lnb-c", "lnb-d"], tunings=2)})
    import_sweep(sweep, tmp_path / "corpus")
    first = survey_scoring.run(tmp_path / "corpus")
    second = survey_scoring.run(tmp_path / "corpus")
    assert first["scored"] == 1, first
    assert second["scored"] == 0 and second["already_scored"] == 1
    payload = json.loads(
        (tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
         / "scores.json").read_text())
    assert payload["observations"]
    assert {row["region"] for row in payload["observations"]} == \
        {"lower-edge", "upper-edge"}


def test_the_scorer_reads_the_arms_rate_not_the_corpus_default(tmp_path):
    """``survey_sample_rate_hz`` reads the record, and the record must say."""
    from leo_tracker.radio.beacon import survey_scoring

    sweep = _sweep(tmp_path / "sweeps", "20260814T000315Z",
                   {"pluto-19f2": _radio("U", _arm(0.006, 1_250_000.0),
                                         ["lnb-c", "lnb-d"], tunings=2)})
    import_sweep(sweep, tmp_path / "corpus")
    entry = tmp_path / "corpus" / entry_name(sweep.name, "pluto-19f2")
    assert survey_scoring.survey_sample_rate_hz(_manifest(entry)) == 1_250_000.0
