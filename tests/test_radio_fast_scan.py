import numpy as np
import pytest

from leo_tracker.radio.beacon.fast_scan import (DEFAULT_OFFSET_SPAN_HZ,
                                                FULL_BANK, SURVEY_BANK,
                                                SURVEY_OFFSET_SPAN_HZ,
                                                build_bank, detection_threshold,
                                                dwell_verifier, probe,
                                                scan_tunings, survey_bank,
                                                verify_presence)
from leo_tracker.radio.beacon.pilots import (_edge_pilot_frame_cached,
                                             acquire_pilot_epoch)
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S

SAMPLE_RATE_HZ = 2_500_000.0
PERIOD = SAMPLE_RATE_HZ * STARLINK_FRAME_DURATION_S
EPOCH = 1234
OFFSET_HZ = 100_000.0
#: 80 ms. Anything judged against ``detection_threshold`` uses this, because
#: that threshold was measured on 80 ms probes and the statistic is strongly
#: length-dependent: on synthetic Gaussian noise this bank's 99th percentile
#: runs 1.310 at 20 ms and 1.137 at 80 ms. Applying an 80 ms bar to a 20 ms
#: window tests a configuration nothing runs, and lets the verdict turn on
#: which random seed was drawn.
PROBE_SAMPLES = 200_000


def _noise(count, seed):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(count) + 1j * rng.standard_normal(count))
            / np.sqrt(2)).astype(np.complex64)


def _beacon(snr_db, *, count=50_000, seed=1, epoch=EPOCH, offset_hz=OFFSET_HZ):
    """A pilot frame repeated on the frame grid, buried in noise at a given SNR."""
    template = _edge_pilot_frame_cached(SAMPLE_RATE_HZ, "lower", 0)
    background = _noise(count, seed)
    signal = np.zeros(count, np.complex64)
    frame = 0
    while True:
        start = epoch + round(frame * PERIOD)
        if start >= count:
            break
        piece = template[:min(len(template), count - start)]
        signal[start:start + len(piece)] += piece
        frame += 1
    time_s = np.arange(count) / SAMPLE_RATE_HZ
    signal = (signal * np.exp(-2j * np.pi * offset_hz * time_s)).astype(np.complex64)
    gain = np.sqrt(10 ** (snr_db / 10) * np.mean(np.abs(background) ** 2)
                   / max(np.mean(np.abs(signal) ** 2), 1e-30))
    return (gain * signal + background).astype(np.complex64)


@pytest.mark.parametrize("snr_db", [0, -5, -10, -15])
def test_full_bank_reproduces_the_reference_acquisition(snr_db):
    """The fast path is an optimisation, not a different detector.

    It must agree with the reference epoch and statistic, otherwise a speedup
    would be silently trading away sensitivity.
    """
    samples = _beacon(snr_db)
    reference = acquire_pilot_epoch(samples, SAMPLE_RATE_HZ, edge="lower")

    scored = probe(samples, build_bank("lower", SAMPLE_RATE_HZ, FULL_BANK))

    assert scored["epoch_sample"] == reference["epoch_sample"]
    assert scored["frequency_offset_hz"] == reference["frequency_offset_hz"]
    assert scored["peak_to_median"] == pytest.approx(
        reference["peak_to_median"], rel=1e-3)


def test_survey_bank_finds_the_same_epoch_where_it_is_sensitive():
    """The cheap bank is only useful if it agrees down to its stated limit."""
    samples = _beacon(-8)

    survey = probe(samples, survey_bank("lower", SAMPLE_RATE_HZ))
    full = probe(samples, build_bank("lower", SAMPLE_RATE_HZ, FULL_BANK))

    assert survey["epoch_sample"] == full["epoch_sample"] == EPOCH
    assert survey["kernel_count"] < full["kernel_count"]


def test_thresholds_sit_above_each_bank_noise_floor():
    """A bank folding fewer anchors has a higher noise floor and needs a higher bar.

    Anchors, not kernels, are what set the floor: each anchor is another
    independent look averaged into the same epoch, so the survey's eight sit
    above the full bank's twenty-four however many frequency hypotheses either
    carries.  Sharing one threshold across bank sizes would either drown the
    survey pass in false alarms or make the full pass needlessly deaf.

    Six realisations cannot see a 1% tail and this does not claim to: it is a
    sanity bound that catches a threshold set below the *middle* of the noise
    distribution.  The 1% point itself is measured on the sky, on windows that
    hold no target pilot by construction, and lives in ``SURVEY_NOISE_CEILING``.
    """
    survey = survey_bank("lower", SAMPLE_RATE_HZ)
    full = build_bank("lower", SAMPLE_RATE_HZ, FULL_BANK)

    survey_noise = [probe(_noise(50_000, 900 + i), survey)["peak_to_median"]
                    for i in range(6)]
    full_noise = [probe(_noise(50_000, 900 + i), full)["peak_to_median"]
                  for i in range(6)]

    assert max(survey_noise) < detection_threshold(SURVEY_BANK)
    assert max(full_noise) < detection_threshold(FULL_BANK)
    assert detection_threshold(SURVEY_BANK) > detection_threshold(FULL_BANK)
    assert SURVEY_BANK[1] < FULL_BANK[1]


def test_presence_rejects_noise_and_accepts_a_beacon():
    """Judged on 80 ms windows, which is what the threshold was measured on."""
    bank = survey_bank("lower", SAMPLE_RATE_HZ)

    quiet = _noise(PROBE_SAMPLES, 77)
    loud = _beacon(-5, seed=5, count=PROBE_SAMPLES)

    assert verify_presence(quiet, bank)["present"] is False
    assert verify_presence(loud, bank)["present"] is True


def test_the_statistic_tightens_with_the_frames_it_folds():
    """Which is why a threshold belongs to a probe length, not just to a bank.

    The fold is incoherent across frames, so averaging more of them narrows
    the null distribution: 15 frames against 60 moves the 99th percentile of
    synthetic noise from 1.31 to 1.14. One fixed bar therefore realises quite
    different false-alarm rates at different lengths -- the deployed 1.33
    realised 8.11% at 20 ms and 2.11% at 80 ms -- and ``verify_presence``
    cannot see the difference, so it is pinned here.
    """
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    short = [probe(_noise(50_000, 1000 + i), bank)["peak_to_median"]
             for i in range(8)]
    long = [probe(_noise(PROBE_SAMPLES, 1000 + i), bank)["peak_to_median"]
            for i in range(8)]

    assert max(long) < min(short)
    assert np.median(long) < np.median(short) - 0.05


def test_the_survey_bank_spaces_hypotheses_closer_than_the_weakest_signal_needs():
    """Spacing is what the bank is bought for, and it has a measured requirement.

    An injection sweep of 14,608 trials over CFO 0-900 kHz by SNR -16..-4 dB
    put the largest hypothesis-to-hypothesis spacing still holding Pd >= 0.9
    everywhere at 150 kHz for -12 dB, and -12 dB is where this detector stops
    working at all.  Three hypotheses over +/-300 kHz left 300 kHz between
    neighbours -- twice what the weakest signal it claimed to find required.

    Pinned as an inequality rather than as 116,667 Hz because the requirement
    is a bound; a future bank may be finer and must not have to edit a test to
    say so.
    """
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    spacing = float(np.diff(bank.offsets_hz).max())

    assert spacing <= 150_000.0
    assert bank.size == 104
    # Doppler p99.9 279,059 Hz + 19,346 Hz LNB bias + 21,595 Hz margin.
    assert bank.offsets_hz.max() >= 320_000.0


def test_the_survey_bank_no_longer_goes_quiet_on_an_offset_port():
    """The caveat the survey record has carried since it was written.

    A port whose LNB sits 436 kHz from its twin used to score 1.36 at -6 dB
    against a 1.33 bar while the same beacon on-centre scored 2.61, so a quiet
    verdict on such a port said nothing about the sky.  Thirteen hypotheses put
    that port 31 kHz from a hypothesis instead of 136 kHz from one, and the
    score stops depending on where the port sits: 2.46 on centre against 2.37
    at 436 kHz.  This is the property, not the ratio -- if it regresses the
    caveat has to go back into the record.
    """
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    scores = {offset: np.median(
        [probe(_beacon(-6, seed=200 + s, offset_hz=offset), bank)["peak_to_median"]
         for s in range(4)])
        for offset in (0.0, 436_000.0, 700_000.0)}

    assert min(scores.values()) > 0.85 * scores[0.0]
    assert min(scores.values()) > detection_threshold(SURVEY_BANK)


def test_widening_the_default_span_would_break_the_full_bank():
    """Why the survey's span is its own constant instead of the module default.

    ``FULL_BANK`` spreads seven hypotheses across ``DEFAULT_OFFSET_SPAN_HZ``,
    which reproduces the reference acquisition's own 100 kHz grid exactly --
    that agreement is what makes the fast path an optimisation rather than a
    different detector.  Moving the default to follow the survey would coarsen
    it to 233 kHz and nothing would fail loudly.
    """
    reference = acquire_pilot_epoch(_beacon(-5), SAMPLE_RATE_HZ, edge="lower")
    full = build_bank("lower", SAMPLE_RATE_HZ, FULL_BANK)

    assert sorted(full.offsets_hz.tolist()) == pytest.approx(
        sorted(reference["searched_frequency_offsets_hz"]))
    assert DEFAULT_OFFSET_SPAN_HZ != SURVEY_OFFSET_SPAN_HZ


def test_the_survey_shape_alone_does_not_name_a_configuration():
    """A shape without a span is half a bank, and the wrong half.

    ``build_bank`` defaults the span, so asking it for the survey's shape and
    nothing else builds thirteen hypotheses across +/-300 kHz: a 50 kHz grid no
    deployment runs and no threshold describes.  ``survey_bank`` exists so that
    cannot be typed by accident.
    """
    paired = survey_bank("lower", SAMPLE_RATE_HZ)
    shape_only = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)

    assert paired.size == shape_only.size == 104
    assert paired.offsets_hz.max() > 2 * shape_only.offsets_hz.max()


@pytest.mark.parametrize("roll", [1, 5, 17, 29])
def test_a_symbol_rolled_bank_is_a_time_shift_and_not_a_wrong_code(roll):
    """Rolling the code by r symbols shifts the waveform by r symbol periods.

    This is the trap in using ``symbol_roll`` as a null for a detector that
    searches the epoch.  Every symbol occupies exactly one symbol period, so
    the rolled template *is* the true template moved along: coherence 0.909
    between roll 17 and the plain frame shifted 187 samples, which is 17 x 11.
    An epoch search re-finds the real pilot at the shifted epoch and scores
    2.80 where the matched bank scores 3.44 -- a 19% dent, not a null.

    The reference path's control is conditioned on a fixed epoch, where the
    same roll separates 0.585 from 0.019, and that is the construction it is
    valid for.  ``NOISE_CEILING`` uses cross-edge windows for exactly this
    reason: their codes are unrelated rather than shifted.
    """
    from leo_tracker.radio.beacon.pilots import OFDM_SYMBOL_DURATION_S

    samples = _beacon(-3, seed=61, offset_hz=0.0)
    matched = probe(samples, survey_bank("lower", SAMPLE_RATE_HZ))
    rolled = probe(samples, survey_bank("lower", SAMPLE_RATE_HZ, symbol_roll=roll))
    symbol_samples = SAMPLE_RATE_HZ * OFDM_SYMBOL_DURATION_S

    assert matched["epoch_sample"] == EPOCH
    assert rolled["epoch_sample"] == EPOCH - round(roll * symbol_samples)
    # It tracks the matched score rather than falling to the noise floor.
    assert rolled["peak_to_median"] > 0.7 * matched["peak_to_median"]


def test_a_rolled_bank_still_says_it_is_a_control():
    """A control's scores share a shape with the real bank's and must not be mixed.

    ``detection_threshold`` is keyed by shape, so a rolled bank's score would
    be judged against the real bank's bar by anything downstream that only had
    the shape to go on.
    """
    real = survey_bank("lower", SAMPLE_RATE_HZ)
    control = survey_bank("lower", SAMPLE_RATE_HZ, symbol_roll=17)

    assert real.symbol_roll == 0 and control.symbol_roll == 17
    assert real.size == control.size
    assert not np.array_equal(real.real, control.real)


def test_an_uncharacterised_bank_shape_gets_a_strict_threshold():
    """An unmeasured shape must not silently inherit a permissive bar."""
    assert detection_threshold((4, 9)) > detection_threshold(SURVEY_BANK)


def test_probe_requires_at_least_four_frames():
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    with pytest.raises(ValueError, match="four frames"):
        probe(_noise(round(3 * PERIOD), 1), bank)


def test_probe_rejects_two_dimensional_input():
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    with pytest.raises(ValueError, match="one dimensional"):
        probe(np.zeros((50_000, 2), np.complex64), bank)


def test_scan_ranks_tunings_and_reports_published_geometry():
    """The survey must name where it looked, not only how strong it was."""
    loud, quiet = _beacon(-3, seed=11), _noise(50_000, 12)
    seen = []

    def read(if_hz):
        seen.append(if_hz)
        return loud if abs(if_hz - 1_709_687.5 - 0) < 1 or len(seen) == 4 else quiet

    results = scan_tunings(read, [(c, e) for c in (1, 2, 3, 4)
                                  for e in ("lower", "upper")],
                           sample_rate_hz=SAMPLE_RATE_HZ)

    assert len(results) == 8 and len(seen) == 8
    assert results == sorted(results, key=lambda r: r["peak_to_median"],
                             reverse=True)
    top = results[0]
    assert top["rf_center_hz"] == pytest.approx(top["if_center_hz"] + 9.75e9)
    assert top["region"] in {"lower-edge", "upper-edge"}


def test_verifier_analyses_only_a_sampled_subset():
    """Chunks arrive faster than they need checking; most must cost nothing."""
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    observe = dwell_verifier(bank, analyse_every=4)
    samples = _beacon(-3, seed=21, count=PROBE_SAMPLES)

    reports = [observe(samples) for _ in range(12)]

    assert sum(r["analysed"] for r in reports) == 3
    assert all(r["continue"] for r in reports)


def test_verifier_waits_for_patience_before_declaring_loss():
    """A single quiet chunk is normal; only a run of them ends a dwell.

    On 80 ms windows, because the verdict is taken against a threshold
    measured on 80 ms windows. At 20 ms one seed in fifteen of pure noise
    clears the bar, so this used to end a dwell or not depending on the draw
    rather than on the state machine it is testing.
    """
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    observe = dwell_verifier(bank, analyse_every=1, patience=3)

    first = observe(_noise(PROBE_SAMPLES, 31))
    second = observe(_noise(PROBE_SAMPLES, 32))
    third = observe(_noise(PROBE_SAMPLES, 33))

    assert first["continue"] and second["continue"]
    assert third["continue"] is False
    assert third["consecutive_misses"] == 3


def test_a_returning_signal_clears_the_miss_run():
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    observe = dwell_verifier(bank, analyse_every=1, patience=3)

    observe(_noise(PROBE_SAMPLES, 41))
    observe(_noise(PROBE_SAMPLES, 42))
    recovered = observe(_beacon(-3, seed=43, count=PROBE_SAMPLES))

    assert recovered["present"] and recovered["consecutive_misses"] == 0
    assert recovered["continue"]


def test_verifier_rejects_a_nonpositive_cadence():
    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    with pytest.raises(ValueError, match="must be positive"):
        dwell_verifier(bank, analyse_every=0)


def test_the_survey_profile_is_not_the_dwell_profile():
    """A survey inheriting the capture path's block size is the whole bug.

    The invariant is that a survey uses what it pays for, not that it is
    cheap: a longer probe legitimately costs more radio time, while reading a
    fifth of a block it paid for in full is waste at any probe length.
    """
    from leo_tracker.radio.beacon.fast_scan import DWELL_PROFILE, SURVEY_PROFILE
    assert SURVEY_PROFILE.block_size < DWELL_PROFILE.block_size
    assert SURVEY_PROFILE.cost_ms()["signal_used_fraction"] == pytest.approx(1.0)
    assert (SURVEY_PROFILE.cost_ms()["signal_used_fraction"]
            > 4 * DWELL_PROFILE.cost_ms()["signal_used_fraction"])
    assert SURVEY_PROFILE.cost_ms()["settle_ms"] == 0.0


def test_cost_model_reproduces_the_measured_dwell_profile():
    """Measured 314 ms per tuning discarding two buffers on a Pi 5.

    The profile declares three, because two does not drain a depth-four queue,
    so the model is correspondingly one buffer dearer than that measurement.
    The field figure was cheap precisely because it was reading stale samples.
    """
    from leo_tracker.radio.beacon.fast_scan import DWELL_PROFILE
    cost = DWELL_PROFILE.cost_ms(2_500_000.0)
    buffer_ms = 1000 * DWELL_PROFILE.block_size / 2_500_000.0
    assert cost["settle_ms"] == pytest.approx(3 * buffer_ms, abs=1.0)
    assert cost["total_ms"] == pytest.approx(427.6, abs=25.0)


def test_cost_model_reproduces_the_measured_survey_profile():
    """Measured 43.5 ms per tuning for the same eight tunings.

    Pinned to the profile that measurement was taken on rather than to
    whichever profile is deployed, so lengthening the probe cannot quietly
    invalidate a field number by moving what it refers to.

    That now covers the *bank* as well as the probe length.  The measurement
    was taken on 24 kernels; the survey searches 104, so a profile that
    inherited today's bank would restate a 43.5 ms field reading as a claim
    about hardware never run in that configuration.
    """
    from leo_tracker.radio.beacon.fast_scan import (MEASURED_COST_BANK,
                                                    MEASURED_PROFILE_20MS)
    cost = MEASURED_PROFILE_20MS.cost_ms(2_500_000.0)
    assert MEASURED_PROFILE_20MS.shape == MEASURED_COST_BANK
    assert cost["kernel_count"] == 24
    assert cost["total_ms"] == pytest.approx(46.6, abs=6.0)


def test_scoring_knows_how_large_the_bank_is():
    """A constant that does not know the bank size is a constant that lies.

    ``compute_ms_per_probe_s`` read 400.0 for the 24-kernel bank and would
    have gone on reading 400.0 for a 104-kernel one.  The level comes from the
    field: 234 deployed surveys of the 24-kernel bank report a median 512.5 ms
    of scoring for 16 probes of 80 ms, which is 400.4 ms per probe-second, and
    the model must still reproduce that.
    """
    from leo_tracker.radio.beacon.fast_scan import (MEASURED_PROFILE_20MS,
                                                    SURVEY_PROFILE)

    assert MEASURED_PROFILE_20MS.compute_ms_per_probe_s == pytest.approx(
        400.4, abs=1.0)
    assert SURVEY_PROFILE.kernel_count == 104
    assert (SURVEY_PROFILE.compute_ms_per_probe_s
            > 3 * MEASURED_PROFILE_20MS.compute_ms_per_probe_s)


def test_scoring_cost_is_affine_in_the_bank_and_not_proportional():
    """Some of a probe is paid whatever it searches, and the model says so.

    The energy normaliser and the running-power cumsum run over the whole
    window before any kernel is touched.  Measured on a Pi 5 at three threads,
    80 ms probes, best of nine: 14.7 ms at 8 kernels, 28.2 at 24, 60.2 at 56,
    106.2 at 104.  That is 3.76x from 24 to 104, not the 4.33x the kernel
    count alone gives, because a quarter of the 24-kernel probe is fixed cost
    being amortised.  A proportional model would overstate the new bank by 15%
    -- in the safe direction, which is exactly why it would never be noticed.
    """
    from leo_tracker.radio.beacon.fast_scan import ScanProfile

    def per_probe_s(kernels):
        return ScanProfile(shape=(kernels, 1)).compute_ms_per_probe_s

    assert per_probe_s(104) / per_probe_s(24) == pytest.approx(3.76, abs=0.05)
    assert per_probe_s(104) / per_probe_s(24) < 104 / 24
    # Halving the bank does not halve the probe.
    assert per_probe_s(12) > per_probe_s(24) / 2


def test_the_wider_bank_costs_host_time_and_not_radio_time():
    """What the survey pays for thirteen hypotheses, and what it does not.

    Retuning, settling and listening are radio time that no arithmetic can
    recover; scoring is host time that can be overlapped with the next tuning.
    A four-fold bank must land entirely in the second, or the survey would be
    holding the radio longer to look at the same 80 ms of sky.
    """
    from leo_tracker.radio.beacon.fast_scan import (MEASURED_COST_BANK,
                                                    SURVEY_PROFILE)
    wide = SURVEY_PROFILE.cost_ms(2_500_000.0)
    narrow = SURVEY_PROFILE.__class__(
        probe_s=SURVEY_PROFILE.probe_s, block_size=SURVEY_PROFILE.block_size,
        shape=MEASURED_COST_BANK).cost_ms(2_500_000.0)

    for term in ("tune_ms", "settle_ms", "listen_ms"):
        assert wide[term] == narrow[term]
    assert wide["compute_ms"] == pytest.approx(narrow["compute_ms"] * 3.76,
                                               rel=0.02)
    # 8 tunings before a 120 s dwell. The model charges one receiver and gives
    # 1.76 s; the field scores both and measured 1.70 s for the old bank, which
    # this projects to about 3.1 s. Either way it is preamble, not the dwell.
    assert SURVEY_PROFILE.sweep_ms(8) < 2_500.0


def test_settle_must_drain_the_kernel_queue():
    """The driver pre-fills its queue, so a shallow settle reads stale samples.

    Measured with a 44 dB gain step: a depth-4 queue leaves three buffers
    holding the previous tuning's samples. Discarding one of them and keeping
    the rest reports the old tuning's signal as the new one's, which is a
    wrong answer rather than a slow one.
    """
    from leo_tracker.radio.beacon.fast_scan import ScanProfile
    with pytest.raises(ValueError, match="cannot drain"):
        ScanProfile(block_size=32_768, settle_buffers=1, kernel_buffers=4)


def test_a_shallow_queue_needs_no_settle_at_all():
    """At depth one nothing is pre-filled, so there is nothing to discard."""
    from leo_tracker.radio.beacon.fast_scan import ScanProfile
    profile = ScanProfile(kernel_buffers=1, settle_buffers=0)
    assert profile.cost_ms()["settle_ms"] == 0.0


def test_the_survey_reads_exactly_the_signal_it_wants():
    """Sizing the block to the probe removes the quantisation waste entirely."""
    from leo_tracker.radio.beacon.fast_scan import SURVEY_PROFILE
    cost = SURVEY_PROFILE.cost_ms(2_500_000.0)
    assert cost["buffers_listened"] == 1
    assert cost["signal_used_fraction"] == pytest.approx(1.0, abs=0.01)


def test_the_dwell_profile_declares_a_settle_that_drains_its_own_queue():
    """The capture path keeps a deep queue for throughput and must pay for it."""
    from leo_tracker.radio.beacon.fast_scan import DWELL_PROFILE
    assert DWELL_PROFILE.settle_buffers >= DWELL_PROFILE.kernel_buffers - 1


def test_listening_is_quantised_by_the_buffer():
    """A probe shorter than a buffer still costs a whole buffer.

    This quantisation, not arithmetic, is what dominates a survey.
    """
    from leo_tracker.radio.beacon.fast_scan import ScanProfile
    coarse = ScanProfile(block_size=262_144, settle_buffers=0, probe_s=0.020)
    cost = coarse.cost_ms(2_500_000.0)

    assert cost["buffers_listened"] == 1
    # 20 ms wanted out of a 104.9 ms buffer
    assert cost["signal_used_fraction"] == pytest.approx(0.191, abs=0.01)


def test_a_finer_block_wastes_less_of_what_it_pays_for():
    """Block size governs listening, and listening is what a survey mostly is.

    The saving is stated on ``listen_ms`` because that is the term block size
    controls.  It used to be stated on ``total_ms`` at 2.8x, and that number
    moved to 2.15x when the bank went from 24 kernels to 104 -- not because a
    fine block got worse but because both profiles now carry a scoring cost
    four times larger, which dilutes any ratio taken over the total.  A
    denominator that quietly grows is the wrong place to pin a claim about
    block size.
    """
    from leo_tracker.radio.beacon.fast_scan import ScanProfile
    # Same shallow queue and same bank on both, so only the block size differs.
    coarse = ScanProfile(block_size=262_144, kernel_buffers=1,
                         settle_buffers=0).cost_ms()
    fine = ScanProfile(block_size=50_000, kernel_buffers=1,
                       settle_buffers=0).cost_ms()

    assert fine["signal_used_fraction"] > coarse["signal_used_fraction"] * 3
    # 32.8 ms against 117.7 ms of listening.
    assert fine["listen_ms"] < coarse["listen_ms"] / 3
    assert fine["total_ms"] < coarse["total_ms"] / 2


@pytest.mark.parametrize("kwargs", [
    {"block_size": 0}, {"settle_buffers": -1}, {"probe_s": 0.0},
])
def test_an_impossible_profile_is_refused(kwargs):
    from leo_tracker.radio.beacon.fast_scan import ScanProfile
    with pytest.raises(ValueError, match="must be positive"):
        ScanProfile(**kwargs)


def test_warming_the_kernel_moves_the_compile_out_of_the_survey():
    """The first probe compiles the kernel; a survey must not pay that.

    Timed against the bank the profile actually names -- shape *and* span.
    Warming one configuration and timing another would still have passed while
    measuring nothing, because the compile is shared and the bank build is not
    what is being bounded.
    """
    from leo_tracker.radio.beacon.fast_scan import (SURVEY_PROFILE, build_bank,
                                                    probe, warm_kernel)
    import time as _time

    warm_kernel(SURVEY_PROFILE)

    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_PROFILE.shape,
                      SURVEY_PROFILE.offset_span_hz)
    started = _time.perf_counter()
    probe(_noise(50_000, 5), bank)
    # A 20 ms probe of the 104-kernel bank measured 35 ms on a Pi 5; the
    # compile alone was 833 ms.
    assert _time.perf_counter() - started < 0.4
