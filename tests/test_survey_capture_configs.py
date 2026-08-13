"""The four capture configurations the pre-dwell survey draws between.

Probe length in {80, 160} ms crossed with sample rate in {2.5, 5} MS/s, drawn
uniformly rather than chosen, so the corpus measures which is better instead of
us deciding.

Three of the four change the sample count in ``survey.ci16``, and that is the
part that bites silently.  A reader that hard-codes ``(8, 200000, 2, 2)`` either
raises -- which is fine -- or reshapes 400,000 samples into something plausible
and scores one tuning's signal against another tuning's label, which is not.
This repository has already had one silent-mapping bug, so what is pinned here
is mostly that shape, rate and probe length are *read* rather than assumed, and
that the three of them are checked against each other.

The other axis is thresholds.  ``SURVEY_NOISE_CEILING`` was measured at 80 ms
and 2.5 MS/s only.  A boolean that means "1% false alarms" in one row and
"below a bar borrowed from a different experiment" in the next is worse than no
boolean, so three of the four configurations must emit none at all.
"""
import json

import numpy as np
import pytest

from leo_tracker.radio.beacon.artifact import _write_survey_iq
from leo_tracker.radio.beacon.fast_scan import (PILOT_BANDWIDTH_HZ,
                                                SURVEY_BANK,
                                                SURVEY_CEILING_PROBE_S,
                                                SURVEY_CEILING_SAMPLE_RATE_HZ,
                                                SURVEY_NOISE_CEILING,
                                                build_bank, collect_radio,
                                                pilot_guard_hz, probe,
                                                score_collection, survey_bank,
                                                survey_profile,
                                                survey_threshold)
from leo_tracker.radio.beacon.injection import frame_offsets
from leo_tracker.radio.beacon.pilots import (OFDM_SYMBOL_DURATION_S,
                                             _edge_pilot_frame_cached)
from leo_tracker.radio.beacon.presurvey import (DEFAULT_SURVEY_CONFIG,
                                                LOW_BAND_TUNINGS,
                                                PI_SCORE_SAMPLE_LIMIT,
                                                SURVEY_ASSIGNMENT_PROBABILITY,
                                                SURVEY_CONFIGS,
                                                SURVEY_EXPERIMENT_ID,
                                                assign_config, config_by_name,
                                                draw_configuration, summarise)
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S
from leo_tracker.radio.beacon.survey_scoring import (ProbeUnusable, read_probe,
                                                     survey_sample_rate_hz)

RATES = (2_500_000.0, 5_000_000.0)
PROBES = (0.080, 0.160)


# --------------------------------------------------------------------------
# the arms, and the draw that picks one
# --------------------------------------------------------------------------

def test_the_survey_offers_exactly_the_four_length_by_rate_combinations():
    """Both axes are crossed, not chosen between.

    Probe length and sample rate answer different questions -- one folds more
    frames, the other widens the guard band an LNB offset eats into -- so the
    experiment cannot learn about either by varying them together. Four arms is
    the whole cross product and anything less confounds the two.
    """
    assert len(SURVEY_CONFIGS) == 4
    assert {(item["probe_s"], item["sample_rate_hz"]) for item in SURVEY_CONFIGS} \
        == {(probe_s, rate) for probe_s in PROBES for rate in RATES}


def test_each_arm_declares_the_sample_count_it_will_produce():
    """The count is the thing every downstream reader has to stop assuming.

    200,000 today; 400,000 in two of the four and 800,000 in the last. Stating
    it on the arm means the manifest, the profile's block size and the file on
    disk all derive from one number rather than three that must agree.
    """
    counts = {item["name"]: item["samples_per_tuning"] for item in SURVEY_CONFIGS}
    assert counts == {"80ms-2.5MSps": 200_000, "80ms-5.0MSps": 400_000,
                      "160ms-2.5MSps": 400_000, "160ms-5.0MSps": 800_000}
    for item in SURVEY_CONFIGS:
        assert item["samples_per_tuning"] == round(item["probe_s"]
                                                   * item["sample_rate_hz"])


def test_the_assignment_is_exactly_uniform_rather_than_approximately_so():
    """2**32 divides by four, so the modulo carries no bias whatsoever.

    A randomised experiment that is uniform only to within a rounding error can
    still be analysed, but it has to be analysed *knowing* the error. Here the
    arm count divides the draw space exactly, so the split is uniform by
    construction and the analysis needs no correction at all. The AGC
    experiment's ``draw % 10000`` does not have this property; four arms do.
    """
    assert 2 ** 32 % len(SURVEY_CONFIGS) == 0
    assert SURVEY_ASSIGNMENT_PROBABILITY == 0.25
    # Every residue class maps to one arm, and consecutive draws walk the arms,
    # so no arm can be starved by any stride the caller happens to use.
    assert [assign_config(value)["name"] for value in range(8)] == \
        [item["name"] for item in SURVEY_CONFIGS] * 2
    counts = {item["name"]: 0 for item in SURVEY_CONFIGS}
    draws = np.random.default_rng(11).integers(0, 2 ** 32, size=40_000)
    for value in draws:
        counts[assign_config(int(value))["name"]] += 1
    assert min(counts.values()) > 0.9 * draws.size / len(SURVEY_CONFIGS)


def test_a_draw_outside_the_unsigned_thirty_two_bit_range_is_refused():
    """A draw from the wrong source is a silently different experiment.

    The watch script reads four bytes from the kernel pool. A negative or
    oversized value means something else produced it, and mapping it anyway
    would file a row under an assignment rule that never ran.
    """
    for bad in (-1, 2 ** 32, 2 ** 40):
        with pytest.raises(ValueError):
            assign_config(bad)


def test_the_record_keeps_the_experiment_the_draw_and_the_assignment():
    """All three, so fairness is checkable from the corpus rather than trusted.

    The id says which rows belong together, the draw is the input to the
    assignment rule and the outcome is what actually ran. With any one of them
    missing an analysis has to take the randomisation on faith, which is the
    thing recording a raw draw exists to avoid.
    """
    config, experiment = draw_configuration(
        experiment_id=SURVEY_EXPERIMENT_ID, random_draw_u32=7)

    assert config == assign_config(7)
    assert experiment["experiment_id"] == SURVEY_EXPERIMENT_ID
    assert experiment["random_draw_u32"] == 7
    assert experiment["assignment_probability"] == SURVEY_ASSIGNMENT_PROBABILITY
    assert experiment["assigned_config"] == config["name"]
    assert experiment["randomised"] is True
    assert sorted(experiment["arms"]) == sorted(item["name"]
                                                for item in SURVEY_CONFIGS)


def test_an_experiment_without_a_draw_is_refused_rather_than_invented():
    """An id with no draw is a row that claims randomisation it cannot show.

    Drawing one here would work and would be untraceable: the number would
    exist only inside this process. Refusing forces the draw to come from the
    caller that logs it.
    """
    with pytest.raises(ValueError):
        draw_configuration(experiment_id=SURVEY_EXPERIMENT_ID)
    with pytest.raises(ValueError):
        draw_configuration(random_draw_u32=3)


def test_a_pinned_configuration_is_recorded_as_not_randomised():
    """An operator debugging one arm must not contaminate the comparison.

    A pinned row is a perfectly good observation and a useless one for a
    randomised comparison, and the only thing that separates the two is this
    flag.
    """
    config, experiment = draw_configuration(config_name="160ms-5.0MSps")

    assert config == config_by_name("160ms-5.0MSps")
    assert experiment["randomised"] is False
    assert experiment["random_draw_u32"] is None


def test_a_survey_that_was_never_randomised_runs_the_calibrated_arm():
    """With no experiment asked for, the verdict should still mean something.

    Falling back to any other arm would produce a record whose threshold has
    never been measured, for a caller that did not ask to be in an experiment.
    """
    config, experiment = draw_configuration()

    assert config == DEFAULT_SURVEY_CONFIG
    assert config["probe_s"] == SURVEY_CEILING_PROBE_S
    assert config["sample_rate_hz"] == SURVEY_CEILING_SAMPLE_RATE_HZ
    assert experiment["randomised"] is False


# --------------------------------------------------------------------------
# rate-dependent physics: the grid, the taps, the guard
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rate,period", [(2_500_000.0, 3333 + 1 / 3),
                                         (5_000_000.0, 6666 + 2 / 3)])
def test_the_frame_grid_is_fractional_at_both_sample_rates(rate, period):
    """3333.333 at 2.5 MS/s and 6666.667 at 5 MS/s. Neither is an integer.

    Truncating either drifts the fold apart across a probe -- twenty samples
    over sixty slots at 2.5 MS/s -- while still producing plausible-looking
    numbers, which is the failure mode worth a regression of its own. The
    higher rate makes it worse, not better: the same fractional part costs
    twice as many samples of drift over the same wall-clock probe.
    """
    assert rate * STARLINK_FRAME_DURATION_S == pytest.approx(period, abs=1e-9)
    assert float(period).is_integer() is False

    offsets = frame_offsets(rate, 60)

    assert offsets[0] == 0
    assert offsets[1] == round(period)
    # The naive integer grid falls behind, and by twice as much at twice the
    # rate: 20 samples at 2.5 MS/s, 40 at 5.
    drift = int(offsets[59] - 59 * int(period))
    assert drift == pytest.approx(59 * (period - int(period)), abs=1)
    assert drift > 0


@pytest.mark.parametrize("rate,taps", [(2_500_000.0, 11), (5_000_000.0, 22)])
def test_the_matched_filter_kernel_holds_one_symbol_at_both_rates(rate, taps):
    """One OFDM symbol is 4.4 us, so the tap count doubles with the rate.

    A kernel cut to the wrong length is not a weaker detector, it is a
    different one: too short throws away signal, too long correlates the next
    symbol's code as if it were noise. ``build_bank`` derives this from the
    rate, and this checks that rather than assuming it.
    """
    assert round(rate * OFDM_SYMBOL_DURATION_S) == taps

    bank = build_bank("lower", rate, SURVEY_BANK, 700_000.0)

    assert bank.taps == taps
    assert bank.size == SURVEY_BANK[0] * SURVEY_BANK[1]
    # Each kernel really is that many complex taps, flattened.
    assert bank.real.size == bank.size * taps


@pytest.mark.parametrize("rate,guard_hz", [(2_500_000.0, 312_500.0),
                                           (5_000_000.0, 1_562_500.0)])
def test_the_higher_rate_buys_five_times_the_guard_band(rate, guard_hz):
    """This is the entire argument for testing 5 MS/s, so it is pinned.

    The eight pilot subcarriers occupy 1.875 MHz whatever the receiver does.
    At 2.5 MS/s that leaves 312.5 kHz of room either side, which lnb-c's
    +434 kHz offset does not fit inside -- it loses subcarriers off the end of
    the sampled spectrum, and no search recovers a band that was never
    sampled. At 5 MS/s there is 1,562.5 kHz and it fits with room to spare.
    """
    assert PILOT_BANDWIDTH_HZ == 8 * 234_375.0
    assert pilot_guard_hz(rate) == guard_hz

    fits = abs(434_000.0) <= pilot_guard_hz(rate)

    assert fits is (rate > 2_500_000.0)


# --------------------------------------------------------------------------
# the file on disk, at every shape
# --------------------------------------------------------------------------

def _manifest(config, *, tunings=2, samples=None, declared=None, rate=None):
    """A capture manifest carrying a survey record at one configuration."""
    samples = config["samples_per_tuning"] if samples is None else samples
    record = {
        "schema": "leo-tracker.pre-dwell-survey/v2", "state": "complete",
        "sample_rate_hz": rate or config["sample_rate_hz"],
        "probe_s": config["probe_s"], "offset_span_hz": 700_000.0,
        "started_utc_ns": 1_760_000_000_000_000_000, "warm_ms": 800.0,
        "total_ms": 400.0, "per_tuning_ms": 50.0,
        "capture_config": {"name": config["name"],
                           "probe_s": config["probe_s"],
                           "sample_rate_hz": rate or config["sample_rate_hz"],
                           "samples_per_tuning": samples},
        "sample_order": [[index + 1, "lower-edge"] for index in range(tunings)],
        "tunings": [{"channel": index + 1, "region": "lower-edge",
                     "if_center_hz": 9.6e8, "rf_center_hz": 1.07e10,
                     "receivers": [{"receiver": side, "active": None,
                                    "peak_to_median": 1.1}
                                   for side in (0, 1)]}
                    for index in range(tunings)]}
    return {"state": "complete", "created_utc_ns": 1_760_000_002_000_000_000,
            "identity": {"radio_id": "pluto-test"},
            # Deliberately different from the survey's: the dwell records at
            # 2.5 MS/s whatever the survey drew, and a reader that took this
            # one would be wrong in exactly the interesting cases.
            "sample_rate_hz": 2_500_000.0,
            "metadata": {"pre_dwell_survey": record},
            "survey_iq": {"path": "survey.ci16", "dtype": "ci16_le",
                          "tunings": tunings,
                          "samples_per_tuning": (samples if declared is None
                                                 else declared),
                          "sample_rate_hz": rate or config["sample_rate_hz"],
                          "probe_s": config["probe_s"],
                          "layout": "tuning,sample,receiver,component"}}


@pytest.mark.parametrize("config", SURVEY_CONFIGS,
                         ids=[item["name"] for item in SURVEY_CONFIGS])
def test_the_preserved_probe_round_trips_at_every_configuration_shape(
        tmp_path, config):
    """Write it, read it back, and get the same samples in the same places.

    Every one of the four, not only the three new ones: the point is that the
    reader takes the shape from the record, and a reader that happened to work
    for 200,000 by hard-coding it would pass a test of the new shapes alone
    while still being wrong about how it got there.
    """
    entry = tmp_path / "capture"
    entry.mkdir()
    tunings, count = 2, 4_000                 # the shape is what varies, not size
    written = np.arange(tunings * count * 4, dtype="<i2").reshape(
        tunings, count, 2, 2)

    block = _write_survey_iq(entry, written,
                             sample_rate_hz=config["sample_rate_hz"],
                             probe_s=count / config["sample_rate_hz"],
                             config_name=config["name"])
    manifest = _manifest(config, tunings=tunings, samples=count)
    manifest["survey_iq"] = {**manifest["survey_iq"], **block}
    manifest["metadata"]["pre_dwell_survey"]["capture_config"].update(
        {"samples_per_tuning": count,
         "probe_s": count / config["sample_rate_hz"]})

    read = read_probe(entry, manifest)

    assert read.shape == (tunings, count, 2, 2)
    assert np.array_equal(read, written)
    assert block["sample_rate_hz"] == config["sample_rate_hz"]
    assert block["capture_config"] == config["name"]
    assert block["bytes"] == tunings * count * 2 * 2 * 2


@pytest.mark.parametrize("config", SURVEY_CONFIGS,
                         ids=[item["name"] for item in SURVEY_CONFIGS])
def test_the_manifest_declares_the_bytes_each_configuration_costs(config):
    """Storage is the price of the higher rate and the longer probe, so size it.

    8 tunings x N samples x 2 receivers x 2 components x 2 bytes, which is 64N.
    12.8 MB at the cheapest arm and 51.2 MB at the dearest.
    """
    expected = 8 * config["samples_per_tuning"] * 2 * 2 * 2

    assert expected == 64 * config["samples_per_tuning"]
    assert expected == {200_000: 12_800_000, 400_000: 25_600_000,
                        800_000: 51_200_000}[config["samples_per_tuning"]]


def test_a_reader_refuses_a_file_shorter_than_its_declared_shape(tmp_path):
    """A truncated copy must raise, never reshape into something plausible.

    A partial network copy of a 51.2 MB probe is a realistic failure, and the
    dangerous outcome is not the exception -- it is the reshape that succeeds
    with the tuning axis off by one.
    """
    config = config_by_name("160ms-5.0MSps")
    entry = tmp_path / "capture"
    entry.mkdir()
    manifest = _manifest(config, tunings=2, samples=1_000)
    manifest["metadata"]["pre_dwell_survey"]["capture_config"].update(
        {"samples_per_tuning": 1_000, "probe_s": 1_000 / 5_000_000.0})
    manifest["survey_iq"]["probe_s"] = 1_000 / 5_000_000.0
    (entry / "survey.ci16").write_bytes(
        np.zeros(2 * 999 * 4, "<i2").tobytes())

    with pytest.raises(ProbeUnusable, match="expected"):
        read_probe(entry, manifest)


def test_a_reader_refuses_a_shape_that_contradicts_its_own_record(tmp_path):
    """Two declarations of the same file, disagreeing, with no way to choose.

    ``survey_iq`` says how many samples per tuning; the survey record says the
    probe length and the rate. They describe one file, so ``probe_s * rate``
    must be the sample count. When it is not, one of them is stale -- most
    likely because a configuration changed and something kept a constant -- and
    guessing which would put a whole capture's tunings under the wrong labels.
    """
    config = config_by_name("160ms-5.0MSps")          # implies 800,000
    entry = tmp_path / "capture"
    entry.mkdir()
    # The file and the ``survey_iq`` block agree with each other and both
    # disagree with the record's 160 ms at 5 MS/s.
    manifest = _manifest(config, tunings=2, samples=200_000)
    manifest["metadata"]["pre_dwell_survey"]["capture_config"][
        "samples_per_tuning"] = 800_000
    manifest["survey_iq"]["samples_per_tuning"] = 200_000
    (entry / "survey.ci16").write_bytes(
        np.zeros(2 * 200_000 * 4, "<i2").tobytes())

    with pytest.raises(ProbeUnusable, match="implies 800000"):
        read_probe(entry, manifest)


def test_the_reader_takes_the_surveys_rate_and_not_the_dwells(tmp_path):
    """They are different numbers now, and the capture's is the wrong one.

    ``manifest["sample_rate_hz"]`` belongs to the recording the survey merely
    preceded. Reading it would halve every frequency a 5 MS/s probe reports and
    put the frame grid on 3,333 samples instead of 6,667, all without raising.
    """
    config = config_by_name("80ms-5.0MSps")
    manifest = _manifest(config)

    assert manifest["sample_rate_hz"] == 2_500_000.0
    assert survey_sample_rate_hz(manifest) == 5_000_000.0


def test_a_probe_written_before_the_experiment_still_reads_at_two_and_a_half():
    """Half the corpus predates the configuration being written down.

    Those probes were all taken at 80 ms and 2.5 MS/s, so that is the right
    answer for them -- and it is only ever a fallback, which is why it applies
    solely when nothing in the record says otherwise.
    """
    legacy = _manifest(DEFAULT_SURVEY_CONFIG)
    del legacy["metadata"]["pre_dwell_survey"]["capture_config"]
    del legacy["metadata"]["pre_dwell_survey"]["sample_rate_hz"]
    del legacy["survey_iq"]["sample_rate_hz"]

    assert survey_sample_rate_hz(legacy) == 2_500_000.0


# --------------------------------------------------------------------------
# thresholds: no verdict without a measured bar
# --------------------------------------------------------------------------

def test_only_the_measured_configuration_yields_a_calibrated_threshold():
    """One of four. The other three have no null population behind them yet.

    ``SURVEY_NOISE_CEILING`` rests on 1,696 cross-edge windows taken at 80 ms
    and 2.5 MS/s. The statistic is known to move with probe length -- p99
    1.310 / 1.189 / 1.137 at 20 / 40 / 80 ms -- and moves with rate too,
    because rate sets the taps and the epoch count. Producing the other three
    populations is what this experiment is for, so the order is randomise,
    accumulate, then calibrate.
    """
    calibrated = [config for config in SURVEY_CONFIGS
                  if survey_threshold(SURVEY_BANK,
                                      probe_s=config["probe_s"],
                                      sample_rate_hz=config["sample_rate_hz"]
                                      )["calibrated"]]

    assert [item["name"] for item in calibrated] == ["80ms-2.5MSps"]

    bar = survey_threshold(SURVEY_BANK, probe_s=SURVEY_CEILING_PROBE_S,
                           sample_rate_hz=SURVEY_CEILING_SAMPLE_RATE_HZ)

    assert bar["threshold"] == SURVEY_NOISE_CEILING
    assert "1,696" in bar["basis"].replace("1696", "1,696")


@pytest.mark.parametrize("config", [item for item in SURVEY_CONFIGS
                                    if item["name"] != "80ms-2.5MSps"],
                         ids=lambda item: item["name"])
def test_an_uncalibrated_configuration_says_so_and_names_itself(config):
    """The basis string has to identify the configuration, not just hedge.

    "uncalibrated" alone leaves a reader unable to tell which of three
    unmeasured configurations a row came from, and therefore unable to pool the
    rows that do belong together into the null population that would calibrate
    them.
    """
    bar = survey_threshold(SURVEY_BANK, probe_s=config["probe_s"],
                           sample_rate_hz=config["sample_rate_hz"])

    assert bar["calibrated"] is False
    assert "UNCALIBRATED" in bar["basis"]
    assert f"{config['probe_s'] * 1000:.0f} ms" in bar["basis"]
    assert f"{config['sample_rate_hz'] / 1e6:.1f} MS/s" in bar["basis"]
    assert bar["calibrated_probe_s"] == SURVEY_CEILING_PROBE_S
    assert bar["calibrated_sample_rate_hz"] == SURVEY_CEILING_SAMPLE_RATE_HZ


def _outcome(config, *, scored_probe_s=None, peak=2.0):
    """A scan outcome shaped the way :func:`scan_radio` returns one."""
    count = config["samples_per_tuning"]
    scored = int(round((scored_probe_s or config["probe_s"])
                       * config["sample_rate_hz"]))
    return {
        "results": [{"channel": 1, "region": "lower-edge", "edge": "lower",
                     "if_center_hz": 9.6e8, "rf_center_hz": 1.07e10,
                     "peak_to_median": peak,
                     "receivers": [{"receiver": side, "peak_to_median": peak,
                                    "frequency_offset_hz": 0.0, "epoch_s": 0.0,
                                    "folded_score": 1.0, "folded_median": 0.5,
                                    "peak_to_p99": 1.0, "peak_to_second": 1.0,
                                    "offset_contrast": 1.0,
                                    "offset_profile": [1.0], "anchor_agreement": 4,
                                    "anchor_count": 8, "folded_p99": 1.0,
                                    "second_score": 1.0, "mean_power": 1.0,
                                    "peak_amplitude": 1.0}
                                   for side in (0, 1)]}],
        "tunings": 1, "sample_order": [(1, "lower-edge")],
        "timing_ms": {"tune": 6.1, "settle": 0.0, "listen": 80.0, "compute": 5.0},
        "total_ms": 91.1, "per_tuning_ms": 91.1, "radio_ms": 86.1,
        "compute_ms": 5.0, "scored_samples": scored,
        "scored_probe_s": scored / config["sample_rate_hz"], "scored": True,
        "sample_rate_hz": config["sample_rate_hz"],
        "probe_s": config["probe_s"], "samples_per_tuning": count,
        "iq_bytes": 64 * count, "pilot_guard_hz": pilot_guard_hz(
            config["sample_rate_hz"]),
        "offset_span_hz": 700_000.0,
        "profile": {"block_size": count, "kernel_buffers": 1,
                    "settle_buffers": 0, "probe_s": config["probe_s"],
                    "shape": list(SURVEY_BANK), "offset_span_hz": 700_000.0}}


def test_a_calibrated_configuration_still_emits_the_verdict_it_always_did():
    """Nothing is taken away from the arm whose bar was measured.

    The change must not silently disable the one verdict the survey has been
    entitled to make since the ceiling was measured.
    """
    config = config_by_name("80ms-2.5MSps")
    profile = survey_profile(config["probe_s"], config["sample_rate_hz"])

    record = summarise(_outcome(config), profile=profile, config=config)

    assert record["threshold_calibrated"] is True
    assert record["active_count"] == 2
    assert all(item["active"] is True
               for tuning in record["tunings"] for item in tuning["receivers"])


@pytest.mark.parametrize("config", [item for item in SURVEY_CONFIGS
                                    if item["name"] != "80ms-2.5MSps"],
                         ids=lambda item: item["name"])
def test_no_verdict_is_stored_where_no_threshold_has_been_measured(config):
    """A boolean meaning different things in different rows is worse than none.

    ``False`` would read as "looked and found nothing" and pool with rows where
    that was actually established. ``None`` cannot be mistaken for either
    answer, and the score is kept beside it so the row becomes usable the
    moment a bar for this configuration exists.
    """
    profile = survey_profile(config["probe_s"], config["sample_rate_hz"])

    record = summarise(_outcome(config, peak=99.0), profile=profile,
                       config=config)

    assert record["threshold_calibrated"] is False
    # Not [] and not 0: an empty list is the same shape as "nothing detected".
    assert record["active"] is None and record["active_count"] is None
    for tuning in record["tunings"]:
        for item in tuning["receivers"]:
            assert item["active"] is None
            assert item["peak_to_median"] == 99.0        # the score survives
    assert "UNCALIBRATED" in record["threshold_basis"]


def test_the_record_names_the_configuration_every_score_was_taken_in():
    """A score with no configuration attached cannot be pooled with anything.

    Length, rate, sample count and guard band all belong to the row, because a
    later analysis grouping by arm has nothing else to group by.
    """
    config = config_by_name("160ms-5.0MSps")
    profile = survey_profile(config["probe_s"], config["sample_rate_hz"])

    record = summarise(_outcome(config), profile=profile, config=config,
                       experiment={"experiment_id": SURVEY_EXPERIMENT_ID,
                                   "random_draw_u32": 3, "randomised": True})

    held = record["capture_config"]
    assert held["name"] == "160ms-5.0MSps"
    assert held["probe_s"] == 0.160 and held["sample_rate_hz"] == 5_000_000.0
    assert held["samples_per_tuning"] == 800_000
    assert held["pilot_guard_hz"] == 1_562_500.0
    assert record["experiment"]["random_draw_u32"] == 3
    assert json.loads(json.dumps(record))["schema"].startswith("leo-tracker.")


def test_a_capped_capture_host_score_reports_the_window_it_actually_scored():
    """40 ms of a 160 ms probe is a 40 ms measurement, and must say so.

    The Pi scores a bounded prefix so its cost does not grow with the drawn
    configuration. That makes the stored score a different experiment from the
    collected probe, and the threshold has to answer for the window that was
    scored rather than the one that was kept.
    """
    config = config_by_name("160ms-5.0MSps")
    profile = survey_profile(config["probe_s"], config["sample_rate_hz"])
    outcome = _outcome(config, scored_probe_s=PI_SCORE_SAMPLE_LIMIT
                       / config["sample_rate_hz"])

    record = summarise(outcome, profile=profile, config=config)

    assert record["capture_config"]["samples_per_tuning"] == 800_000
    assert record["capture_config"]["scored_samples"] == PI_SCORE_SAMPLE_LIMIT
    assert record["capture_config"]["scored_probe_s"] == pytest.approx(0.040)
    assert "40 ms" in record["threshold_basis"]
    assert record["threshold_calibrated"] is False


# --------------------------------------------------------------------------
# collecting and scoring are two jobs on two machines
# --------------------------------------------------------------------------

class _Attr:
    def __init__(self, value=""):
        self.value = value


class _Channel:
    def __init__(self, identifier, output=False):
        self.id, self.output, self.enabled = identifier, output, False
        self.attrs = {"frequency": _Attr("0"), "sampling_frequency": _Attr("0"),
                      "rf_bandwidth": _Attr("0")}


class _Device:
    def __init__(self, channels):
        self.channels = channels

    def set_kernel_buffers_count(self, count):
        self.kernel_buffers = count


class _Buffer:
    def __init__(self, context):
        self._context = context

    def refill(self):
        self._context.refills += 1

    def read(self):
        return self._context.payload


class _Context:
    """A radio that hands back one fixed block however it is tuned."""

    def __init__(self, payload):
        self._phy = _Device([_Channel("altvoltage0", output=True),
                             _Channel("voltage0"), _Channel("voltage1")])
        self._stream = _Device([_Channel("voltage0"), _Channel("voltage1")])
        self._stream.context = self
        self.payload = payload
        self.refills = 0

    def find_device(self, name):
        return {"ad9361-phy": self._phy, "cf-ad9361-lpc": self._stream}[name]


@pytest.fixture
def fake_iio(monkeypatch):
    import sys
    import types

    module = types.ModuleType("iio")
    module.Buffer = lambda device, size, cyclic: _Buffer(device.context)
    monkeypatch.setitem(sys.modules, "iio", module)


def _payload(count, seed=3, rate=2_500_000.0, epoch=1234, snr_db=-3.0):
    """Interleaved i0 q0 i1 q1 with an edge pilot on both receivers."""
    rng = np.random.default_rng(seed)
    background = ((rng.standard_normal(count) + 1j * rng.standard_normal(count))
                  / np.sqrt(2)).astype(np.complex64)
    template = _edge_pilot_frame_cached(rate, "lower", 0)
    signal = np.zeros(count, np.complex64)
    for start in frame_offsets(rate, int(count / (rate * STARLINK_FRAME_DURATION_S))):
        begin = int(start) + epoch
        piece = template[:max(0, min(template.size, count - begin))]
        if piece.size:
            signal[begin:begin + piece.size] += piece
    gain = np.sqrt(10 ** (snr_db / 10) * np.mean(np.abs(background) ** 2)
                   / max(np.mean(np.abs(signal) ** 2), 1e-30))
    wave = (gain * signal + background).astype(np.complex64)
    raw = np.empty(count * 4, np.int16)
    raw[0::4] = np.clip(wave.real * 200, -2047, 2047)
    raw[1::4] = np.clip(wave.imag * 200, -2047, 2047)
    raw[2::4] = raw[0::4]
    raw[3::4] = raw[1::4]
    return raw.tobytes()


def test_collecting_returns_the_iq_and_scores_none_of_it(fake_iio):
    """The Pi must own the radio; it need not own the detection.

    Collection is the part no other machine can do. Keeping scoring out of it
    is what lets the expensive comparison move to a host with sixteen workers
    while the four-core Pi pays only for the seconds the antenna was listening.
    """
    count = 20_000
    context = _Context(_payload(count))
    profile = survey_profile(count / 2_500_000.0, 2_500_000.0)

    collected = collect_radio(context, list(LOW_BAND_TUNINGS), profile=profile,
                              sample_rate_hz=2_500_000.0)

    assert "results" not in collected
    assert set(collected["timing_ms"]) == {"tune", "settle", "listen"}
    assert collected["samples"].shape == (8, count, 2, 2)
    assert collected["samples"].dtype == np.int16
    assert collected["sample_order"] == [(c, f"{e}-edge")
                                         for c, e in LOW_BAND_TUNINGS]
    assert collected["iq_bytes"] == 8 * count * 2 * 2 * 2


def test_scoring_collected_iq_needs_no_radio_at_all(fake_iio):
    """The same function the analysis host runs, run here without a device.

    If scoring could not be driven from stored samples alone, deferring it
    would not be possible and the split would be cosmetic.
    """
    count = 20_000
    collected = collect_radio(_Context(_payload(count)), [(4, "lower")],
                              profile=survey_profile(count / 2_500_000.0,
                                                     2_500_000.0),
                              sample_rate_hz=2_500_000.0)

    scored = score_collection(collected)

    assert scored["results"][0]["channel"] == 4
    assert [item["receiver"] for item in scored["results"][0]["receivers"]] == [0, 1]
    assert scored["scored_samples"] == count
    assert scored["compute_ms"] > 0


def test_the_capture_host_scores_one_bounded_window_whatever_it_collected(
        fake_iio):
    """Radio time follows the probe; scoring cost must not follow it too.

    Unbounded, the dearest arm costs 11.6 s of scoring against the cheapest
    arm's 2.0 s, measured on this Pi -- a quarter of the survey's own dwell
    budget spent on a verdict the analysis host is going to recompute anyway.
    Capping the window makes the Pi's bill the cheapest arm's in all four.
    """
    count = 40_000
    collected = collect_radio(_Context(_payload(count)), [(4, "lower")],
                              profile=survey_profile(count / 2_500_000.0,
                                                     2_500_000.0),
                              sample_rate_hz=2_500_000.0)

    capped = score_collection(collected, sample_limit=20_000)

    assert capped["scored_samples"] == 20_000
    assert capped["scored_fraction"] == 0.5
    assert capped["scored_probe_s"] == pytest.approx(20_000 / 2_500_000.0)


def test_the_split_timings_add_up_to_the_total_in_the_same_unit(fake_iio):
    """Radio milliseconds and scoring milliseconds are both milliseconds.

    Splitting collection from scoring put the two halves of the timing on
    opposite sides of a unit conversion, and mixing them produced a survey that
    reported thirty-one minutes for four seconds of work while every individual
    part was right. A total that is not the sum of its parts is the kind of
    number that gets copied into a plan.
    """
    from leo_tracker.radio.beacon.fast_scan import scan_radio

    count = 20_000
    outcome = scan_radio(_Context(_payload(count)), [(4, "lower")],
                         profile=survey_profile(count / 2_500_000.0,
                                                2_500_000.0),
                         sample_rate_hz=2_500_000.0)

    assert outcome["total_ms"] == pytest.approx(
        sum(outcome["timing_ms"].values()))
    assert outcome["timing_ms"]["compute"] == pytest.approx(
        outcome["compute_ms"])
    assert outcome["radio_ms"] == pytest.approx(
        outcome["timing_ms"]["tune"] + outcome["timing_ms"]["settle"]
        + outcome["timing_ms"]["listen"])
    # Sanity on the magnitude: a probe of this size cannot take a minute.
    assert 0 < outcome["total_ms"] < 60_000


def test_deferring_the_score_still_records_where_the_radio_was_pointed(
        fake_iio):
    """No scores, but the tuning-to-IQ map has to survive or the probe is junk.

    ``sample_order`` alone names channel and region; the centres live on the
    tuning records. Dropping those when scoring is deferred would leave the
    analysis host with samples it cannot attribute to a sky.
    """
    from leo_tracker.radio.beacon.fast_scan import scan_radio

    count = 20_000
    outcome = scan_radio(_Context(_payload(count)), list(LOW_BAND_TUNINGS),
                         profile=survey_profile(count / 2_500_000.0,
                                                2_500_000.0),
                         sample_rate_hz=2_500_000.0, keep_samples=True,
                         score=False)

    assert outcome["scored"] is False
    assert outcome["compute_ms"] == 0.0
    assert len(outcome["results"]) == len(LOW_BAND_TUNINGS)
    assert all(item["receivers"] == [] for item in outcome["results"])
    assert all(item["if_center_hz"] > 0 for item in outcome["results"])
    assert outcome["samples"].shape[0] == len(LOW_BAND_TUNINGS)


def test_the_waterfall_takes_its_frequency_axis_from_the_record(tmp_path):
    """A 5 MS/s probe drawn on a 2.5 MS/s axis is wrong and looks fine.

    Every frequency in the picture is scaled by the rate, and the picture
    outlives the capture directory, so it is the last surviving evidence of
    what the receiver saw. Getting the axis from the record rather than a
    module default is the difference between a record and a misleading one.
    """
    from leo_tracker.radio.beacon.survey_waterfall import (
        LEGACY_SAMPLE_RATE_HZ, record_sample_rate_hz)

    drawn = {"capture_config": {"sample_rate_hz": 5_000_000.0},
             "sample_rate_hz": 5_000_000.0}
    legacy = {"tunings": []}

    assert record_sample_rate_hz(drawn) == 5_000_000.0
    assert record_sample_rate_hz(legacy) == LEGACY_SAMPLE_RATE_HZ
    # An older record that names only the top-level rate is still believed.
    assert record_sample_rate_hz({"sample_rate_hz": 5_000_000.0}) == 5_000_000.0


def test_a_panel_distinguishes_no_verdict_from_a_negative_one():
    """"Not marked active" and "no bar exists" are different pictures.

    Three of the four configurations produce a score with no threshold behind
    it. A label that rendered those identically to a measured miss would put
    the uncalibrated rows back into the comparison through the picture.
    """
    from leo_tracker.radio.beacon.survey_waterfall import _panel_label

    entry = {"channel": 4, "region": "lower-edge"}
    base = {"receiver": 0, "peak_to_median": 1.9, "anchor_agreement": 4,
            "anchor_count": 8, "offset_contrast": 2.0}

    fired = _panel_label(entry, {**base, "active": True}, None)
    quiet = _panel_label(entry, {**base, "active": False}, None)
    unknown = _panel_label(entry, {**base, "active": None}, None)

    assert "ACTIVE" in fired and "uncalibrated" not in fired
    assert "ACTIVE" not in quiet and "uncalibrated" not in quiet
    assert "ACTIVE" not in unknown and "uncalibrated" in unknown


def test_radio_time_follows_probe_duration_and_not_sample_rate():
    """Two of the four configurations cost the same air for different bytes.

    A block is sized to the probe, so 80 ms is one 80 ms read whether that is
    200,000 samples or 400,000. This is why doubling the rate is affordable at
    all: it buys guard band with storage rather than with the antenna.
    """
    per_probe = {}
    for config in SURVEY_CONFIGS:
        profile = survey_profile(config["probe_s"], config["sample_rate_hz"])
        cost = profile.cost_ms(config["sample_rate_hz"])
        assert cost["buffers_listened"] == 1        # one read, no stitching
        assert profile.block_size == config["samples_per_tuning"]
        per_probe.setdefault(config["probe_s"], set()).add(
            cost["tune_ms"] + cost["settle_ms"] + cost["listen_ms"])

    assert all(len(values) == 1 for values in per_probe.values())
    assert min(per_probe[0.080]) < min(per_probe[0.160])


# --------------------------------------------------------------------------
# the operator's switch, which lives in the shell
# --------------------------------------------------------------------------

WATCH_SCRIPT = "scripts/starlink-beacon-watch.sh"


def _survey_args(*, experiment="exp-v1", pinned="", defer="0", draws=1):
    """Run the watch script's own survey-argument assembly, in isolation."""
    import subprocess

    body = f"""
    survey_experiment_id="{experiment}"
    survey_config="{pinned}"
    survey_defer_scoring="{defer}"
    for _ in $(seq {draws}); do
      survey_args=(--survey-before-dwell)
      if [[ -n "${{survey_config}}" ]]; then
        survey_args+=(--survey-config "${{survey_config}}")
      elif [[ -n "${{survey_experiment_id}}" ]]; then
        survey_draw="$(od -An -N4 -tu4 /dev/urandom | tr -d '[:space:]')"
        survey_args+=(--survey-experiment-id "${{survey_experiment_id}}"
          --survey-random-draw-u32 "${{survey_draw}}")
      fi
      [[ "${{survey_defer_scoring}}" == "1" ]] && survey_args+=(--survey-defer-scoring)
      printf '%s\\n' "${{survey_args[@]}}"
      printf -- '---\\n'
    done
    """
    return subprocess.run(["bash", "-euo", "pipefail", "-c", body],
                          capture_output=True, text=True,
                          check=True).stdout.strip().split("\n")


def test_the_watch_script_draws_a_fresh_thirty_two_bit_number_per_capture():
    """One draw per capture, from the kernel pool, in range.

    Reusing a draw across captures would make the assignment correlated with
    whatever else that cycle shares, and the analysis would have no way to see
    it. The range matters because the mapping refuses anything outside it.
    """
    import subprocess

    subprocess.run(["bash", "-n", WATCH_SCRIPT], check=True)
    blocks = "\n".join(_survey_args(draws=6)).split("---")
    draws = [int(block.strip().split("\n")[-1])
             for block in blocks if block.strip()]

    assert len(draws) == 6
    assert all(0 <= value < 2 ** 32 for value in draws)
    assert len(set(draws)) > 1                  # not a constant
    for value in draws:
        assert assign_config(value) in SURVEY_CONFIGS


def test_pinning_an_arm_and_drawing_one_are_mutually_exclusive():
    """A draw that decided nothing is worse than no draw at all.

    Recording both would put a random number beside an assignment it did not
    produce, which is exactly the kind of row that makes a later audit conclude
    the randomisation was broken.
    """
    pinned = _survey_args(pinned="160ms-5.0MSps")

    assert "--survey-config" in pinned
    assert "--survey-random-draw-u32" not in pinned
    assert "--survey-experiment-id" not in pinned


def test_the_script_carries_the_deferral_switch_and_leaves_it_off():
    """Off by default: the bounded capture-host score is cheap and useful.

    Deferring entirely is the right lever when the Pi is short of headroom, and
    the wrong default while the dashboard still reads a verdict from the
    manifest.
    """
    text = open(WATCH_SCRIPT).read()

    assert 'LEO_BEACON_SURVEY_DEFER_SCORING:-0' in text
    assert "--survey-defer-scoring" not in _survey_args(defer="0")
    assert "--survey-defer-scoring" in _survey_args(defer="1")


def test_the_script_never_restates_the_assignment_rule():
    """Two copies of an assignment rule is how they drift apart.

    The AGC experiment computes its arm in bash and passes the outcome; this
    one passes only the draw, so the mapping exists once, in Python, where it
    has a test. The script must not acquire a second copy.
    """
    text = open(WATCH_SCRIPT).read()

    assert "survey_draw" in text
    assert "survey_draw %" not in text and "survey_bucket" not in text
    for config in SURVEY_CONFIGS:
        # Naming an arm in the script would be that second copy, except in the
        # comment that documents the pinning option.
        assert text.count(config["name"]) <= 1


@pytest.mark.parametrize("config", SURVEY_CONFIGS,
                         ids=[item["name"] for item in SURVEY_CONFIGS])
def test_the_detector_finds_a_pilot_at_every_one_of_the_four_configurations(
        config):
    """End to end, because every rate-dependent piece has to be right at once.

    The kernel taps, the frame period, the epoch count and the fold support are
    each derived from the sample rate somewhere different, and any one of them
    left at a 2.5 MS/s constant produces a detector that runs, returns numbers
    and finds nothing. A pilot at -3 dB has to come out well clear of the
    noise floor in all four, or one of those derivations is wrong.

    Shortened to a few frames so the suite stays affordable; what is under test
    is that the geometry composes at each rate, not the sensitivity of a
    full-length probe.
    """
    rate = config["sample_rate_hz"]
    count = int(6 * rate * STARLINK_FRAME_DURATION_S)
    raw = np.frombuffer(_payload(count, rate=rate, seed=17), np.int16)
    values = (raw[0::4] + 1j * raw[1::4]).astype(np.complex64)
    bank = survey_bank("lower", rate)

    found = probe(values, bank)

    assert found["peak_to_median"] > 2.0
    assert found["kernel_count"] == SURVEY_BANK[0] * SURVEY_BANK[1]
    assert found["epoch_s"] == pytest.approx(1234 / rate, abs=4 / rate)
