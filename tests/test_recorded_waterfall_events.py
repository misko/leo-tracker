from pathlib import Path

import numpy as np
import pytest

from leo_tracker.radio.events import detect_spectral_events
from leo_tracker.radio.joint_tracking import associate_receiver_events
from leo_tracker.radio.wide_doppler import _integrate


ROOT = Path("artifacts/starlink_watch_12h_20260802/chunks")


@pytest.mark.recorded
@pytest.mark.parametrize("chunk", [24, 40, 41])
def test_visible_frozen_events_are_finite_and_jointly_detected(chunk):
    path = ROOT / f"chunk-{chunk:05d}.npz"
    if not path.exists():
        pytest.skip("local frozen observation is not available")
    with np.load(path, allow_pickle=False) as artifact:
        spectra = artifact["spectra_db"]
        utc = artifact["utc_ns"]
        frequencies = artifact["frequency_offsets_hz"]
    receiver_events = []
    for receiver in range(2):
        integrated, times = _integrate(spectra[receiver], utc, 1)
        receiver_events.append(detect_spectral_events(integrated, times, frequencies,
            receiver=receiver, threshold_db=.3, min_time_bins=5,
            min_frequency_bins=5, min_support_pixels=100))
    major = [[event for event in events if event.support_pixels >= 1000]
             for events in receiver_events]
    pairs = associate_receiver_events(*receiver_events, min_score=.5)

    assert major[0] and major[1]
    assert all(event.duration_s < 150 for events in major for event in events)
    assert any(receiver_events[0][pair.rx0_index].support_pixels >= 1000 and
               receiver_events[1][pair.rx1_index].support_pixels >= 1000
               for pair in pairs)
