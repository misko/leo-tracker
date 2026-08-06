import json

import numpy as np
import pytest

from leo_tracker.radio.cli import main
from leo_tracker.radio.paired import FakePairedSource
from leo_tracker.radio.source import RadioConfig
from leo_tracker.radio.beacon.artifact import BeaconCapture
from leo_tracker.radio.beacon.channels import starlink_edge_pilot_if_hz
from leo_tracker.radio.beacon.hopping import HOP_SESSION_SCHEMA, capture_hop_session


def test_hop_capture_retunes_one_stream_and_excludes_settling_buffers(tmp_path):
    rate = 1_000
    config = RadioConfig(starlink_edge_pilot_if_hz(1, "lower"), rate, 800,
                         gain_db=30)
    source = FakePairedSource(np.arange(800) + 1j, np.arange(800) + 2j,
                              config, block_size=40,
                              start_utc_ns=1_700_000_000_000_000_000)

    report = capture_hop_session(source, tmp_path / "hop", channels=(2, 4),
        dwell_s=.1, sample_rate_hz=rate, bandwidth_hz=800,
        settle_buffers=1, chunk_s=.1, configured_gain_db=30)

    assert report["schema"] == HOP_SESSION_SCHEMA
    assert report["state"] == "complete" and source.closed
    assert source.retune_history == [
        starlink_edge_pilot_if_hz(2, "lower"),
        starlink_edge_pilot_if_hz(4, "lower")]
    assert [item["discarded_settle_samples"] for item in report["segments"]] == [40, 40]
    assert [item["channel_number"] for item in report["segments"]] == [2, 4]
    for segment in report["segments"]:
        capture = BeaconCapture.open(tmp_path / "hop" / segment["capture"], verify=True)
        assert capture.manifest["captured_samples_per_receiver"] == 100
        assert capture.manifest["metadata"]["observation_mode"] == "channel-hop"
        assert capture.manifest["metadata"]["hop_sequence"] == segment["sequence"]


def test_hop_capture_publishes_interrupted_session_on_source_exhaustion(tmp_path):
    config = RadioConfig(starlink_edge_pilot_if_hz(1, "lower"), 1_000, 800)
    source = FakePairedSource(np.ones(80), np.ones(80), config, block_size=40)

    with pytest.raises(RuntimeError, match="radio ended"):
        capture_hop_session(source, tmp_path / "hop", channels=(1, 2), dwell_s=.1,
                            sample_rate_hz=1_000, bandwidth_hz=800,
                            settle_buffers=1)

    session = json.loads((tmp_path / "hop" / "session.json").read_text())
    assert session["state"] == "interrupted" and source.closed


def test_hop_capture_cli_fake_e2e(tmp_path, capsys):
    output = tmp_path / "hop"
    assert main(["starlink-beacon-hop-capture", str(output), "--channels", "1", "3",
        "--dwell-s", ".1", "--sample-rate-hz", "1000", "--bandwidth-hz", "800",
        "--block-size", "40", "--settle-buffers", "1", "--chunk-s", ".1",
        "--fake"]) == 0

    result = json.loads(capsys.readouterr().out)
    session = json.loads((output / "session.json").read_text())
    assert result["state"] == "complete" and result["segment_count"] == 2
    assert session["channel_order"] == [1, 3]


@pytest.mark.parametrize("channels", [(), (1, 1), (0,), (9,)])
def test_hop_capture_rejects_invalid_channel_order(tmp_path, channels):
    source = FakePairedSource(np.ones(100), np.ones(100),
        RadioConfig(1e9, 1_000, 800), block_size=20)
    with pytest.raises(ValueError, match="channels"):
        capture_hop_session(source, tmp_path / "bad", channels=channels,
                            sample_rate_hz=1_000, bandwidth_hz=800)
