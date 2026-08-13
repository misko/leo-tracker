"""The whole family rests on one claim: the unknown frame phase cancels.

If that is wrong, every score here is accumulating evidence that does not add,
and no amount of Pd measurement downstream would say so — a broken detector
just looks like a weak one. So the premise is tested directly rather than
inferred from performance, and beside it are pinned the two things a later
reader must not assume: that the offset estimate is unambiguous, and that its
sign convention was ever checked anywhere but at zero.

These are the slow oracles an optimised implementation gets gated against, so
the tests are about properties of the algebra, not about lines of code.
"""
import numpy as np
import pytest

from leo_tracker.radio.beacon.fast_scan import build_bank
from leo_tracker.radio.beacon.injection import frame_offsets, inject
from leo_tracker.radio.beacon.pilots import edge_pilot_frame
from leo_tracker.radio.beacon.relative_phase import (
    AMBIGUITY_HALF_SPAN_HZ, AMBIGUITY_SPAN_HZ, CONTROL_SYMBOL_ROLL,
    adjacent_differential, anchor_alias_spacing_hz, anchor_relative_phase,
    coherent_ceiling, contiguous_symbols, frame_spectrum, pilot_correlations,
    score_family, survey_anchor_symbols, symbol_glrt)
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S

RATE = 2_500_000.0
PERIOD = RATE * STARLINK_FRAME_DURATION_S      # 3333.333..., deliberately not an int
PROBE = 100_000                                # 40 ms: 30 frame slots, quick enough


def _noise(count, seed):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(count) + 1j * rng.standard_normal(count))
            / np.sqrt(2)).astype(np.complex64)


def _replica(*, count=PROBE, epoch=0, cfo_hz=0.0, period=PERIOD):
    """A noiseless pilot in every slot, placed on the fractional frame grid.

    Built here rather than by ``inject`` because a known answer needs a signal
    with no noise in it at all, and ``inject`` sets its strength against the
    host's noise power.
    """
    frame = edge_pilot_frame(RATE, "lower")
    values = np.zeros(count, np.complex128)
    slots = int((count - epoch - frame.size) // period) + 1
    for slot in range(slots):
        start = epoch + int(round(slot * period))
        values[start:start + frame.size] += frame[:count - start]
    values *= np.exp(2j * np.pi * cfo_hz * np.arange(count) / RATE)
    return values.astype(np.complex64)


def test_the_unknown_frame_phase_cancels_where_a_naive_complex_sum_does_not():
    """This is the premise of the entire family, so it is tested head on.

    Qin models the per-frame carrier phase as unmodelled, which is why the plan
    forbids summing raw frame amplitudes. Randomising it must leave the
    differential and GLRT scores alone — they only ever look at phase
    *differences* inside a frame — while destroying the forbidden statistic,
    the direct complex sum of every correlation in the probe.
    """
    host = _noise(PROBE, seed=1)
    shared = dict(sample_rate_hz=RATE, epoch_sample=640, snr_db=30.0,
                  occupancy=1.0, seed=2)
    coherent = inject(host, frame_phase="coherent", **shared)["samples"]
    scrambled = inject(host, frame_phase="random", **shared)["samples"]

    naive = lambda values: float(
        abs(np.sum(values)) / np.sum(np.abs(values)))
    forbidden_coherent = naive(pilot_correlations(coherent, RATE, 640, 0.0).values)
    forbidden_random = naive(pilot_correlations(scrambled, RATE, 640, 0.0).values)

    for detector in (adjacent_differential, symbol_glrt):
        assert detector(scrambled, RATE, 640, 0.0)["score"] == pytest.approx(
            detector(coherent, RATE, 640, 0.0)["score"], rel=0.01)
    assert forbidden_coherent > 0.95            # frames add, as they must not
    assert forbidden_random < 0.5               # and the same statistic collapses


def test_a_noiseless_replica_at_the_true_epoch_scores_at_the_ceiling():
    """Known answer. Every score here is normalised so perfect alignment is 1.0.

    A number that depended on gain, probe length or symbol count could not be
    compared across the bake-off's candidates, and could not be recognised as
    correct by eye.
    """
    values = _replica()

    for report in score_family(values, RATE, 0, 0.0)["detectors"].values():
        assert report["score"] == pytest.approx(1.0, abs=1e-6)
        assert report["residual_frequency_offset_hz"] == pytest.approx(0.0, abs=1.0)
        assert report["margin"] > 0.7


def test_the_residual_offset_is_recovered_in_both_signs():
    """A sign convention only ever tested at zero is not tested.

    This repository carries an unresolved positive-slope sign bug, and an
    estimator that silently negated frequency would pass every zero-offset
    known-answer test ever written. Both detectors must place a positive
    residual above the coarse hypothesis and a negative one below it.
    """
    # A 512-bin search over one ambiguity period lands on a 444 Hz grid, so the
    # tolerance is half a bin plus the known half-percent bias, not a rounding.
    for offset in (-58_300.0, -5_000.0, +5_000.0, +58_300.0):
        values = _replica(cfo_hz=offset)

        for detector in (adjacent_differential, symbol_glrt):
            report = detector(values, RATE, 0, 0.0)
            estimate = report["residual_frequency_offset_hz"]
            assert np.sign(estimate) == np.sign(offset)
            assert estimate == pytest.approx(offset, rel=0.02, abs=250.0)
            assert report["frequency_offset_hz"] == pytest.approx(
                offset, rel=0.02, abs=250.0)
            assert report["score"] > 0.95


def test_a_coarse_hypothesis_is_subtracted_rather_than_ignored():
    """The residual is measured against the CFO it was handed, not against zero.

    The coarse stage exists precisely to bring the offset inside the ambiguity
    window; a detector that ignored its hypothesis would look identical at zero
    and be useless everywhere else.
    """
    values = _replica(cfo_hz=90_000.0)

    report = adjacent_differential(values, RATE, 0, 75_000.0)

    assert report["residual_frequency_offset_hz"] == pytest.approx(15_000.0, rel=0.02)
    assert report["frequency_offset_hz"] == pytest.approx(90_000.0, rel=0.01)


def test_the_statistic_repeats_exactly_every_ambiguity_period():
    """Nobody may later assume this estimator is unambiguous.

    Adjacent-symbol phase is one sample of the carrier every 4.4 us, so ``S(f)``
    is periodic at 227.3 kHz by construction — not approximately, and not only
    for pilots. Anything asking for a wider search than one period is asking the
    same question twice.
    """
    correlations = pilot_correlations(_replica(cfo_hz=17_000.0), RATE, 0, 0.0,
                                      symbols=contiguous_symbols(32))

    for frequency in (-40_000.0, 0.0, 17_000.0, 61_234.0):
        here = frame_spectrum(correlations, np.array([frequency]))[0]
        period_away = frame_spectrum(
            correlations, np.array([frequency + AMBIGUITY_SPAN_HZ]))[0]
        three_away = frame_spectrum(
            correlations, np.array([frequency - 3 * AMBIGUITY_SPAN_HZ]))[0]
        assert period_away == pytest.approx(here, rel=1e-9)
        assert three_away == pytest.approx(here, rel=1e-9)

    assert AMBIGUITY_SPAN_HZ == pytest.approx(227_272.7, abs=0.1)
    assert AMBIGUITY_HALF_SPAN_HZ == pytest.approx(113_636.4, abs=0.1)


def test_a_residual_outside_the_window_wraps_in_without_announcing_itself():
    """The consequence of that periodicity, and the reason a coarse stage is
    mandatory rather than merely helpful.

    A true residual of +150 kHz is reported as -77 kHz, 227 kHz wrong, while the
    score stays high enough to look like a confident detection. The estimate
    carries no signal that it has wrapped; only the coarse stage can prevent it.
    """
    report = symbol_glrt(_replica(cfo_hz=150_000.0), RATE, 0, 0.0)
    wrapped = 150_000.0 - AMBIGUITY_SPAN_HZ

    assert report["residual_frequency_offset_hz"] == pytest.approx(wrapped, rel=0.02)
    assert abs(report["residual_frequency_offset_hz"] - 150_000.0) > 200_000.0
    assert report["score"] > 0.6                # and it still looks convincing


def test_the_search_covers_exactly_one_ambiguity_period():
    """One period is the whole hypothesis space; two would be a duplicate."""
    report = symbol_glrt(_replica(), RATE, 0, 0.0, transform_size=512)

    low, high = report["searched_span_hz"]
    assert low == pytest.approx(-AMBIGUITY_HALF_SPAN_HZ, rel=1e-9)
    assert high == pytest.approx(AMBIGUITY_HALF_SPAN_HZ - report[
        "frequency_resolution_hz"], rel=1e-9)


def test_detection_survives_the_transform_size_but_precision_does_not():
    """External work claims quality is flat from 256 to 2048 and only the CFO
    sharpens. It holds here, which makes the transform size a cost knob rather
    than a sensitivity one — worth knowing before the bake-off spends on it.
    """
    host = _noise(PROBE, seed=3)
    made = inject(host, sample_rate_hz=RATE, epoch_sample=700, cfo_hz=12_000.0,
                  snr_db=-9.0, occupancy=1.0, seed=4)

    scored = {size: symbol_glrt(made["samples"], RATE, 700, 0.0,
                                transform_size=size)
              for size in (256, 512, 1024, 2048)}

    margins = [report["margin"] for report in scored.values()]
    assert max(margins) - min(margins) < 0.02 * max(margins)
    assert scored[2048]["frequency_resolution_hz"] < (
        scored[256]["frequency_resolution_hz"] / 7)


def test_the_wrong_code_control_collapses_on_a_real_pilot():
    """Asks whether the IQ matches Qin's published sequence specifically.

    A rolled sequence keeps the modulation, the bandwidth, the frame period and
    the interference; it destroys only the identity of the code. Structured
    OFDM-like energy would lift both, so only the difference is evidence.
    """
    host = _noise(PROBE, seed=5)
    made = inject(host, sample_rate_hz=RATE, epoch_sample=512, cfo_hz=0.0,
                  snr_db=12.0, occupancy=1.0, seed=6)

    for report in score_family(made["samples"], RATE, 512, 0.0)["detectors"].values():
        assert report["control_symbol_roll"] == CONTROL_SYMBOL_ROLL
        assert report["control_score"] < 0.35
        assert report["margin"] > 0.55


def test_the_wrong_code_control_matches_the_exact_code_on_noise():
    """The other half of the same claim, and the half that sets the threshold.

    On a probe with no pilot in it the control must be a fair stand-in for the
    exact code — same search, same free parameters, same distribution — so that
    the margin is centred on zero rather than on a code-dependent offset.
    """
    margins = {}
    for seed in range(6):
        for name, report in score_family(
                _noise(PROBE, seed=100 + seed), RATE, 731, 0.0)["detectors"].items():
            margins.setdefault(name, []).append(report["margin"])

    for name, values in margins.items():
        assert abs(float(np.mean(values))) < 0.03, name
        assert max(abs(value) for value in values) < 0.12, name


def test_frames_are_read_off_the_fractional_period_not_a_rounded_one():
    """3333.333 samples, not 3333. Twenty samples of drift over sixty slots.

    Twenty samples is nearly two whole OFDM symbols, so by the end of an 80 ms
    probe a rounded grid correlates each symbol against its neighbour's code and
    the accumulation stops accumulating.
    """
    correlations = pilot_correlations(_replica(), RATE, 0, 0.0)
    expected = frame_offsets(RATE, correlations.frames)

    assert np.array_equal(correlations.frame_starts, expected)
    assert expected[3] == 10_000                   # rounds up, not to 9999
    assert expected[-1] != (correlations.frames - 1) * 3333
    assert frame_offsets(RATE, 60)[59] - 59 * 3333 == 20   # the drift, in samples

    on_grid = adjacent_differential(_replica(), RATE, 0, 0.0)["score"]
    off_grid = adjacent_differential(
        _replica(period=3333.0), RATE, 0, 0.0)["score"]
    assert on_grid > 0.99
    assert off_grid < 0.75


def test_a_frame_the_probe_cuts_short_is_dropped_rather_than_shortened():
    """A truncated correlation is a quietly weaker one, not a missing one.

    Letting a partial final frame through would put a systematically small |z|
    into the amplitude weighting and a systematically wrong phase into the
    accumulation, for no gain at all.
    """
    frames = pilot_correlations(_replica(), RATE, 0, 0.0).frames
    last = int(frame_offsets(RATE, frames)[-1])

    assert last + int(round(301 * RATE * 4.4e-6)) < PROBE
    assert last + int(round(PERIOD)) > PROBE - int(round(PERIOD))
    assert pilot_correlations(_replica(), RATE, 0, 0.0).values.shape == (
        frames, survey_anchor_symbols().size)


def test_every_score_is_invariant_to_receiver_gain_and_a_global_phase():
    """Neither is knowable, so neither may change a verdict.

    Gain is an AGC setting and the global phase is where the LO happened to be
    when the buffer opened. A statistic that moved with either would be
    measuring the receiver.
    """
    values = inject(_noise(PROBE, seed=7), sample_rate_hz=RATE, epoch_sample=300,
                    snr_db=0.0, occupancy=1.0, seed=8)["samples"]
    turned = (values * 137.0 * np.exp(1j * 2.1)).astype(np.complex64)

    plain = score_family(values, RATE, 300, 0.0)["detectors"]
    altered = score_family(turned, RATE, 300, 0.0)["detectors"]

    for name, report in plain.items():
        assert altered[name]["score"] == pytest.approx(report["score"], rel=1e-6)
        assert altered[name]["margin"] == pytest.approx(report["margin"], abs=1e-6)


def test_amplitude_weighting_is_the_default_and_phase_only_is_opt_in():
    """External work found weighting by matched-filter strength beats
    normalising each correlation to unit magnitude, and phase-only is on the
    plan's list of things not worth rediscovering. It stays available and stays
    off.
    """
    values = _replica()

    assert pilot_correlations(values, RATE, 0, 0.0).phase_only is False
    weighted = pilot_correlations(values, RATE, 0, 0.0).values
    unit = pilot_correlations(values, RATE, 0, 0.0, phase_only=True).values

    assert np.abs(weighted).std() > 0
    assert np.allclose(np.abs(unit), 1.0)
    assert adjacent_differential(values, RATE, 0, 0.0, phase_only=True)[
        "phase_only"] is True


def test_the_anchors_are_the_ones_the_deployed_survey_bank_already_correlates():
    """The cheapest candidate's whole claim is that it reuses existing work.

    If these drifted apart from ``fast_scan``'s anchors the candidate would
    silently become a different, more expensive detector wearing the same name.
    """
    bank = build_bank("lower", RATE, shape=(3, 8))

    assert np.array_equal(survey_anchor_symbols(8), bank.anchors)
    assert survey_anchor_symbols(8).tolist() == [2, 45, 87, 130, 173, 216, 258, 301]


def test_the_eight_anchor_score_is_a_comb_needing_a_cfo_good_to_a_few_hundred_hertz():
    """The plan names this candidate cheap because the survey already computes
    its correlations. That is true of the correlations and false of the CFO.

    Spread over 300 symbols, ``S(f)`` at a fixed frequency is a comb: teeth
    about 200 Hz wide repeating every 5.32 kHz. So it needs a coarse CFO as
    accurate as the 300-symbol full-frame detector needs — which is the search
    cost relative phase was supposed to remove. Pinned so the bake-off prices
    it honestly.
    """
    scored = {offset: anchor_relative_phase(
        _replica(cfo_hz=offset), RATE, 0, 0.0)["score"]
        for offset in (0.0, 200.0, 750.0, 5_000.0, anchor_alias_spacing_hz(8))}

    assert scored[0.0] == pytest.approx(1.0, abs=1e-6)
    assert 0.6 < scored[200.0] < 0.85
    assert scored[750.0] < 0.1
    assert scored[5_000.0] < 0.5
    assert scored[anchor_alias_spacing_hz(8)] > 0.95   # a whole tooth away, back at 1
    assert anchor_alias_spacing_hz(8) == pytest.approx(5_320.8, abs=0.5)


def test_searching_the_anchors_recovers_the_score_but_not_the_frequency():
    """The comb's other face. A search always finds a tooth, so it detects at
    any residual — and the tooth it finds is one of about forty-three equally
    good answers, which is why ``refines_cfo`` stays false even when searching.
    """
    report = anchor_relative_phase(_replica(cfo_hz=750.0), RATE, 0, 0.0, search=True)

    assert report["score"] > 0.95
    assert report["refines_cfo"] is False
    assert abs(report["residual_frequency_offset_hz"] - 750.0) > 1_000.0
    assert abs(report["residual_frequency_offset_hz"] - 750.0) == pytest.approx(
        anchor_alias_spacing_hz(8), abs=100.0)


def test_the_offset_estimate_reads_about_half_a_percent_high():
    """A property of the published code, not of this implementation.

    ``T_sym`` assumes each symbol's matched filter has its phase centre in the
    middle of its window. The real ones sit between 2.26 and 7.62 samples into
    an 11-sample window, and across symbols 2..33 the centre drifts +0.055
    samples per symbol — indistinguishable from extra time between symbols.

    It matters because the confirmation stage tolerates about +/-375 Hz: at
    config E's worst 58.3 kHz residual this costs +355 Hz of that budget, and at
    the edge of the ambiguity window it would exceed it. Pinned as a measured
    band; if a later change calibrates it out, this test should be updated to
    pin the corrected behaviour rather than deleted.
    """
    for offset in (5_000.0, 20_000.0, 58_300.0):
        estimate = adjacent_differential(
            _replica(cfo_hz=offset), RATE, 0, 0.0)["residual_frequency_offset_hz"]
        assert 1.004 < estimate / offset < 1.009

    assert adjacent_differential(_replica(cfo_hz=58_300.0), RATE, 0, 0.0)[
        "residual_frequency_offset_hz"] - 58_300.0 == pytest.approx(355.0, abs=25.0)


def test_the_coherent_ceiling_is_what_a_perfectly_aligned_probe_would_reach():
    """The normaliser has to be the achievable maximum or "1.0" means nothing."""
    correlations = pilot_correlations(_replica(), RATE, 0, 0.0)

    assert frame_spectrum(correlations, np.array([0.0]))[0] == pytest.approx(
        coherent_ceiling(correlations), rel=1e-9)


def test_a_probe_holding_no_complete_frame_scores_zero_rather_than_raising():
    """These run over a whole corpus, and one short probe must not stop a sweep.

    Zero is also the right answer: no frame means no evidence, and any nonzero
    score from an empty accumulation would be a false alarm manufactured by the
    normaliser.
    """
    stub = _noise(5_000, seed=9)

    assert pilot_correlations(stub, RATE, 4_900, 0.0).frames == 0
    for report in score_family(stub, RATE, 4_900, 0.0)["detectors"].values():
        assert report["score"] == 0.0
        assert report["margin"] == 0.0
        assert report["residual_frequency_offset_hz"] == 0.0


def test_symbols_outside_the_published_range_are_refused():
    """Qin publishes codes for symbols 2..301. Correlating anything else would
    be correlating against the zeros the template synthesiser leaves behind, and
    would return a confident-looking nothing.
    """
    with pytest.raises(ValueError, match="2..301"):
        pilot_correlations(_replica(), RATE, 0, 0.0, symbols=np.array([0, 1, 2]))
    with pytest.raises(ValueError, match="2..301"):
        contiguous_symbols(32, first=290)
    with pytest.raises(ValueError, match="strictly increasing"):
        pilot_correlations(_replica(), RATE, 0, 0.0, symbols=np.array([5, 5, 6]))
