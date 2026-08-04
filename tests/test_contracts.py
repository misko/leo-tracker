from datetime import datetime, timezone

import pytest

from leo_tracker.contracts import FrequencyObservation, require_utc, to_json_dict


def test_require_utc_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        require_utc(datetime(2026, 7, 31, 12, 0))


def test_frequency_observation_serializes_utc_and_flags():
    observation = FrequencyObservation(
        timestamp=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        frequency_hz=1_000.25,
        uncertainty_hz=0.5,
        quality_flags=("low_snr",),
    )

    encoded = to_json_dict(observation)

    assert encoded["timestamp"] == "2026-07-31T12:00:00Z"
    assert encoded["quality_flags"] == ["low_snr"]


def test_frequency_uncertainty_cannot_be_negative():
    with pytest.raises(ValueError, match="non-negative"):
        FrequencyObservation(
            timestamp=datetime.now(timezone.utc),
            frequency_hz=1.0,
            uncertainty_hz=-1.0,
        )
