import numpy as np
import pytest

from leo_tracker.radio.events import detect_spectral_events
from leo_tracker.radio.joint_tracking import (associate_receiver_events,
                                               estimate_lnb_offset_hz)


def _events(offset_bins=0, second_start=20):
    rng = np.random.default_rng(12)
    times = np.arange(80, dtype=float); frequencies = np.arange(512)*10_000.0
    outputs = []
    for receiver, offset in enumerate((0, offset_bins)):
        values = rng.normal(0, .025, (80, 512))
        for row in range(second_start, 51):
            center = 140 + (row-second_start)//2 + offset
            values[row, center-3:center+4] += 1
        outputs.append(detect_spectral_events(values, times, frequencies, receiver=receiver,
            threshold_db=.2, min_support_pixels=20))
    return outputs


def test_pairs_shared_event_and_recovers_independent_lnb_offset():
    rx0, rx1 = _events(offset_bins=120)
    pairs = associate_receiver_events(rx0, rx1)
    assert len(pairs) == 1
    assert pairs[0].lnb_offset_hz == pytest.approx(1_200_000, abs=15_000)
    assert pairs[0].time_iou > .95
    assert pairs[0].drift_difference_hz_s < 200
    assert estimate_lnb_offset_hz(pairs) == pytest.approx(1_200_000, abs=15_000)


def test_does_not_pair_nonoverlapping_events():
    rx0, _ = _events(second_start=10)
    _, rx1 = _events(second_start=55)
    assert associate_receiver_events(rx0, rx1) == []
