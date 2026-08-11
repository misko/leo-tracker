import numpy as np
import pytest

from leo_tracker.radio.beacon.fast_scan import (FULL_BANK, SURVEY_BANK,
                                                build_bank, detection_threshold,
                                                dwell_verifier, probe,
                                                scan_tunings, verify_presence)
from leo_tracker.radio.beacon.pilots import (_edge_pilot_frame_cached,
                                             acquire_pilot_epoch)
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S

SAMPLE_RATE_HZ = 2_500_000.0
PERIOD = SAMPLE_RATE_HZ * STARLINK_FRAME_DURATION_S
EPOCH = 1234
OFFSET_HZ = 100_000.0


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

    survey = probe(samples, build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK))
    full = probe(samples, build_bank("lower", SAMPLE_RATE_HZ, FULL_BANK))

    assert survey["epoch_sample"] == full["epoch_sample"] == EPOCH
    assert survey["kernel_count"] < full["kernel_count"]


def test_thresholds_sit_above_each_bank_noise_floor():
    """A smaller bank folds fewer scores, so its noise floor is higher.

    Sharing one threshold across bank sizes would either drown the survey pass
    in false alarms or make the full pass needlessly deaf.
    """
    survey = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)
    full = build_bank("lower", SAMPLE_RATE_HZ, FULL_BANK)

    survey_noise = [probe(_noise(50_000, 900 + i), survey)["peak_to_median"]
                    for i in range(6)]
    full_noise = [probe(_noise(50_000, 900 + i), full)["peak_to_median"]
                  for i in range(6)]

    assert max(survey_noise) < detection_threshold(SURVEY_BANK)
    assert max(full_noise) < detection_threshold(FULL_BANK)
    assert detection_threshold(SURVEY_BANK) > detection_threshold(FULL_BANK)


def test_presence_rejects_noise_and_accepts_a_beacon():
    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)

    assert verify_presence(_noise(50_000, 77), bank)["present"] is False
    assert verify_presence(_beacon(-5, seed=5), bank)["present"] is True


def test_an_uncharacterised_bank_shape_gets_a_strict_threshold():
    """An unmeasured shape must not silently inherit a permissive bar."""
    assert detection_threshold((4, 9)) > detection_threshold(SURVEY_BANK)


def test_probe_requires_at_least_four_frames():
    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)
    with pytest.raises(ValueError, match="four frames"):
        probe(_noise(round(3 * PERIOD), 1), bank)


def test_probe_rejects_two_dimensional_input():
    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)
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
    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)
    observe = dwell_verifier(bank, analyse_every=4)
    samples = _beacon(-3, seed=21)

    reports = [observe(samples) for _ in range(12)]

    assert sum(r["analysed"] for r in reports) == 3
    assert all(r["continue"] for r in reports)


def test_verifier_waits_for_patience_before_declaring_loss():
    """A single quiet chunk is normal; only a run of them ends a dwell."""
    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)
    observe = dwell_verifier(bank, analyse_every=1, patience=3)

    first = observe(_noise(50_000, 31))
    second = observe(_noise(50_000, 32))
    third = observe(_noise(50_000, 33))

    assert first["continue"] and second["continue"]
    assert third["continue"] is False
    assert third["consecutive_misses"] == 3


def test_a_returning_signal_clears_the_miss_run():
    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)
    observe = dwell_verifier(bank, analyse_every=1, patience=3)

    observe(_noise(50_000, 41))
    observe(_noise(50_000, 42))
    recovered = observe(_beacon(-3, seed=43))

    assert recovered["present"] and recovered["consecutive_misses"] == 0
    assert recovered["continue"]


def test_verifier_rejects_a_nonpositive_cadence():
    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK)
    with pytest.raises(ValueError, match="must be positive"):
        dwell_verifier(bank, analyse_every=0)


def test_the_survey_profile_is_not_the_dwell_profile():
    """A survey inheriting the capture path's block size is the whole bug.

    A 120 s dwell is indifferent to a 105 ms block; a survey pays it three
    times per tuning to look at 20 ms.
    """
    from leo_tracker.radio.beacon.fast_scan import DWELL_PROFILE, SURVEY_PROFILE
    assert SURVEY_PROFILE.block_size < DWELL_PROFILE.block_size
    assert SURVEY_PROFILE.sweep_ms() < DWELL_PROFILE.sweep_ms() / 4


def test_cost_model_reproduces_the_measured_dwell_profile():
    """Measured 314 ms per tuning on a Pi 5; the model must not drift from it."""
    from leo_tracker.radio.beacon.fast_scan import DWELL_PROFILE
    cost = DWELL_PROFILE.cost_ms(2_500_000.0)
    assert cost["total_ms"] == pytest.approx(324.0, abs=15.0)
    assert cost["settle_ms"] == pytest.approx(209.7, abs=1.0)


def test_cost_model_reproduces_the_measured_survey_profile():
    """Measured 39 ms per tuning for the same eight tunings."""
    from leo_tracker.radio.beacon.fast_scan import SURVEY_PROFILE
    cost = SURVEY_PROFILE.cost_ms(2_500_000.0)
    assert cost["total_ms"] == pytest.approx(44.0, abs=12.0)


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
    from leo_tracker.radio.beacon.fast_scan import ScanProfile
    coarse = ScanProfile(block_size=262_144).cost_ms()
    fine = ScanProfile(block_size=32_768).cost_ms()

    assert fine["signal_used_fraction"] > coarse["signal_used_fraction"] * 3
    # Like for like, at one settle buffer each, the finer block is 4.5x cheaper.
    # The 8x measured in the field also folds in the dwell profile's second
    # settle buffer, which is a separate saving.
    assert fine["total_ms"] < coarse["total_ms"] / 4


@pytest.mark.parametrize("kwargs", [
    {"block_size": 0}, {"settle_buffers": -1}, {"probe_s": 0.0},
])
def test_an_impossible_profile_is_refused(kwargs):
    from leo_tracker.radio.beacon.fast_scan import ScanProfile
    with pytest.raises(ValueError, match="must be positive"):
        ScanProfile(**kwargs)


def test_warming_the_kernel_moves_the_compile_out_of_the_survey():
    """The first probe compiles the kernel; a survey must not pay that."""
    from leo_tracker.radio.beacon.fast_scan import (SURVEY_PROFILE, build_bank,
                                                    probe, warm_kernel)
    import time as _time

    warm_kernel(SURVEY_PROFILE)

    bank = build_bank("lower", SAMPLE_RATE_HZ, SURVEY_PROFILE.shape)
    started = _time.perf_counter()
    probe(_noise(50_000, 5), bank)
    # Post-warm probes ran at 9 ms on a Pi 5; the compile alone was 833 ms.
    assert _time.perf_counter() - started < 0.4
