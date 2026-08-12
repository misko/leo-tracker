"""Injection is the only hard truth this corpus has, so it is tested hardest.

If injection is wrong, every Pd measured against it is wrong in the same
direction and nothing downstream can detect that. These tests exist to make
specific classes of that error impossible rather than to exercise lines.
"""
import numpy as np
import pytest

from leo_tracker.radio.beacon.injection import (INJECTION_SCHEMA, frame_offsets,
                                                inject, occupancy_mask)
from leo_tracker.radio.beacon.pilots import matched_pilot_score
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S

RATE = 2_500_000.0
PERIOD = RATE * STARLINK_FRAME_DURATION_S      # 3333.333..., deliberately not an int
FRAME = int(round(PERIOD))


def _slots(size, epoch=0):
    """How many frame slots the harness will find, so masks are the right size."""
    return int((size - epoch) // PERIOD)


def _phase(index):
    """Epoch is only defined within a frame: every slot is a valid detection."""
    return index % PERIOD


def _noise(count, seed=0):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(count) + 1j * rng.standard_normal(count))
            / np.sqrt(2)).astype(np.complex64)


def test_the_frame_period_is_not_an_integer(tmp_path=None):
    """3333.333 samples, not 3333. Twenty samples of drift over 60 slots.

    This is the mistake that produced plausible numbers once already, so it is
    pinned rather than trusted to care.
    """
    offsets = frame_offsets(RATE, 60)

    assert offsets[1] == 3333
    assert offsets[3] == 10000            # 3 x 3333.333 rounds up, not to 9999
    assert offsets[59] == 196667
    assert offsets[59] != 59 * 3333       # the naive integer grid, 20 low


def test_an_injected_pilot_is_found_at_exactly_its_epoch():
    """The detector must recover the epoch we put it at, not one nearby."""
    host = _noise(6 * FRAME, seed=1)

    made = inject(host, sample_rate_hz=RATE, epoch_sample=1234, cfo_hz=0.0,
                  snr_db=10.0, occupancy=1.0, frame_phase="coherent", seed=2)
    found = matched_pilot_score(made["samples"], RATE, edge="lower",
                                frequency_offsets_hz=(0.0,))

    assert _phase(found["sample_index"]) == pytest.approx(1234, abs=1.0)


def test_an_injected_offset_is_recovered_in_both_signs():
    """A sign convention only ever tested at zero is not tested.

    This repository carries an unresolved positive-slope sign bug on a quarter
    of qualified Doppler tracks; a detector that silently negated frequency
    would pass every zero-offset test ever written.
    """
    host = _noise(6 * FRAME, seed=3)
    grid = tuple(np.arange(-6000.0, 6001.0, 250.0))

    for offset in (-3000.0, +3000.0):
        made = inject(host, sample_rate_hz=RATE, epoch_sample=700,
                      cfo_hz=offset, snr_db=12.0, occupancy=1.0,
                      frame_phase="coherent", seed=4)
        found = matched_pilot_score(made["samples"], RATE, edge="lower",
                                    frequency_offsets_hz=grid)
        assert found["frequency_offset_hz"] == pytest.approx(offset, abs=250.0)
        assert _phase(found["sample_index"]) == pytest.approx(700, abs=1.0)


def test_the_achieved_strength_matches_what_was_asked_for():
    host = _noise(6 * FRAME, seed=5)

    for wanted in (-12.0, -6.0, 0.0, 6.0):
        made = inject(host, sample_rate_hz=RATE, snr_db=wanted, occupancy=1.0,
                      seed=6)
        assert made["truth"]["achieved_snr_db"] == pytest.approx(wanted, abs=0.01)


def test_strength_is_measured_inside_transmitted_frames_only():
    """Otherwise occupancy and SNR stop being independent axes.

    The same requested SNR must produce the same in-frame strength whether one
    frame in ten is transmitted or all of them.
    """
    host = _noise(20 * FRAME, seed=7)

    dense = inject(host, sample_rate_hz=RATE, snr_db=-6.0, occupancy=1.0, seed=8)
    sparse = inject(host, sample_rate_hz=RATE, snr_db=-6.0, occupancy=0.2, seed=9)

    assert dense["truth"]["achieved_snr_db"] == pytest.approx(-6.0, abs=0.01)
    assert sparse["truth"]["achieved_snr_db"] == pytest.approx(-6.0, abs=0.01)
    assert sparse["truth"]["occupancy"] < dense["truth"]["occupancy"]


def test_only_the_transmitted_slots_carry_signal():
    """Occupancy has to mean something in the samples, not just the record."""
    host = np.zeros(10 * FRAME, np.complex64)
    mask = np.zeros(_slots(host.size), bool)
    mask[2] = True

    made = inject(host + _noise(host.size, seed=10), sample_rate_hz=RATE,
                  epoch_sample=0, snr_db=20.0, occupancy=mask, seed=11)

    difference = np.abs(made["samples"] - _noise(host.size, seed=10))
    offsets = frame_offsets(RATE, mask.size)
    assert difference[offsets[2]:offsets[2] + FRAME].max() > 0
    for quiet in (0, 1, 3, 4, 5, 6):
        window = difference[offsets[quiet]:offsets[quiet] + FRAME - 1]
        assert window.max() == pytest.approx(0.0, abs=1e-6)


def test_zero_frames_selected_is_refused_rather_than_silently_empty():
    """A truth record claiming an injection that never happened is poison."""
    host = _noise(6 * FRAME, seed=12)

    with pytest.raises(ValueError, match="selected no frames"):
        inject(host, sample_rate_hz=RATE,
               occupancy=np.zeros(_slots(host.size), bool))


def test_random_frame_phase_is_the_default():
    """Qin models the per-frame phase as unmodelled; coherent frames would be
    an easier signal than the sky provides, and a detector could learn to
    exploit cross-frame coherence that does not exist."""
    host = _noise(6 * FRAME, seed=13)

    made = inject(host, sample_rate_hz=RATE, snr_db=0.0, seed=14)

    assert made["truth"]["frame_phase"] == "random"


def test_coherent_and_random_phase_differ_in_the_samples():
    host = _noise(6 * FRAME, seed=15)
    kwargs = dict(sample_rate_hz=RATE, epoch_sample=0, snr_db=6.0,
                  occupancy=1.0, seed=16)

    a = inject(host, frame_phase="coherent", **kwargs)["samples"]
    b = inject(host, frame_phase="random", **kwargs)["samples"]

    assert not np.allclose(a, b)


def test_the_truth_record_says_what_was_done():
    host = _noise(6 * FRAME, seed=17)

    truth = inject(host, sample_rate_hz=RATE, epoch_sample=99, cfo_hz=1500.0,
                   snr_db=-3.0, occupancy=0.5, seed=18,
                   host_name="ch4-narrow-x")["truth"]

    assert truth["schema"] == INJECTION_SCHEMA
    assert truth["epoch_sample"] == 99 and truth["cfo_hz"] == 1500.0
    assert truth["host"] == "ch4-narrow-x"
    assert len(truth["transmitted_slots"]) == round(
        truth["occupancy"] * truth["frame_slots"])


def test_a_real_recorded_probe_serves_as_the_background():
    """Synthetic noise is what mis-calibrated the current threshold.

    The background must be able to be a real capture, and injecting into one
    must not change its statistics beyond the signal added.
    """
    rng = np.random.default_rng(19)
    recorded = (rng.integers(-300, 300, 6 * FRAME)
                + 1j * rng.integers(-300, 300, 6 * FRAME)).astype(np.complex64)

    made = inject(recorded, sample_rate_hz=RATE, snr_db=-40.0, occupancy=1.0,
                  seed=20)

    assert np.mean(np.abs(made["samples"]) ** 2) == pytest.approx(
        np.mean(np.abs(recorded) ** 2), rel=0.01)


def test_occupancy_mask_rejects_an_impossible_fraction():
    rng = np.random.default_rng(21)

    with pytest.raises(ValueError, match="occupancy fraction"):
        occupancy_mask(10, fraction=1.5, rng=rng)


def test_a_per_frame_detector_is_indifferent_to_occupancy():
    """Where the folded survey statistic is not.

    `matched_pilot_score` correlates one frame and takes the maximum over
    epochs, so a transmitted frame anywhere in the probe gives the same peak
    whether one slot in four carries signal or all of them. That is precisely
    why it is robust to the sparse duty cycle the folded detector suffers from,
    and it means occupancy must be swept against detection *rate*, not against
    peak score.
    """
    host = _noise(12 * FRAME, seed=22)
    score = lambda **kw: matched_pilot_score(
        inject(host, sample_rate_hz=RATE, epoch_sample=500,
               frame_phase="coherent", seed=23, **kw)["samples"],
        RATE, edge="lower", frequency_offsets_hz=(0.0,))["score"]

    weak = score(snr_db=-12.0, occupancy=1.0)
    strong = score(snr_db=0.0, occupancy=1.0)
    sparse = score(snr_db=0.0, occupancy=0.25)

    assert strong > weak
    assert sparse == pytest.approx(strong, rel=0.05)
