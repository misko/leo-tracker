import json
from types import SimpleNamespace

import numpy as np
import pytest

from leo_tracker.radio.artifact import CaptureArtifact
from leo_tracker.radio import cli, scout


class _FakeAd9361:
    def __init__(self, uri):
        self.uri = uri
        self.rx_enabled_channels = []
        self.rx_lo = 0
        self.calls = 0
        self.destroy_calls = 0

    def rx_destroy_buffer(self):
        self.destroy_calls += 1

    def rx(self):
        self.calls += 1
        size = self.rx_buffer_size
        if len(self.rx_enabled_channels) == 1:
            return np.full(size, 100 + 1j * self.rx_enabled_channels[0], np.complex64)
        return np.vstack((np.full(size, 100 + 0j, np.complex64),
                          np.full(size, 200 + 1j, np.complex64)))


def test_parse_channels_and_legacy_conflict():
    assert cli.parse_channels("0") == (0,)
    assert cli.parse_channels("1") == (1,)
    assert cli.parse_channels("1,0") == (0, 1)
    for invalid in ("", "2", "0,0", "x"):
        with pytest.raises(Exception):
            cli.parse_channels(invalid)
    with pytest.raises(ValueError, match="either --channel or --channels"):
        cli._selected_channels(SimpleNamespace(channel=0, channels=(0, 1)))


def test_dual_scan_reads_hardware_once_per_refill(monkeypatch):
    device = _FakeAd9361("test")
    monkeypatch.setattr(scout.importlib, "import_module",
                        lambda name: SimpleNamespace(ad9361=lambda uri: device))
    monkeypatch.setattr(scout.time, "sleep", lambda seconds: None)
    result = scout.scan_channels_pyadi(
        uri="pluto://test", frequencies_hz=[1e9, 1.1e9], sample_rate_hz=4e6,
        bandwidth_hz=3e6, gain_db=40, samples_per_frequency=64,
        channels=(0, 1),
    )
    assert device.rx_enabled_channels == [0, 1]
    assert device.calls == 4  # one discarded and one retained refill per center
    assert device.destroy_calls == 2
    assert set(result) == {0, 1}
    assert all(len(points) == 2 for points in result.values())
    assert [p.center_frequency_hz for p in result[0]] == [1e9, 1.1e9]
    assert [p.center_frequency_hz for p in result[1]] == [1e9, 1.1e9]


def test_single_channel_scan_wrapper_preserves_rx1(monkeypatch):
    device = _FakeAd9361("test")
    monkeypatch.setattr(scout.importlib, "import_module",
                        lambda name: SimpleNamespace(ad9361=lambda uri: device))
    monkeypatch.setattr(scout.time, "sleep", lambda seconds: None)
    result = scout.scan_pyadi(
        uri="test", frequencies_hz=[1e9], sample_rate_hz=4e6,
        bandwidth_hz=3e6, gain_db=20, samples_per_frequency=64, channel=1,
    )
    assert device.rx_enabled_channels == [1]
    assert len(result) == 1


def test_multi_scan_report_links_equal_channel_grids(tmp_path):
    values = np.exp(2j * np.pi * np.arange(64) / 8).astype(np.complex64) * 100
    points = [scout.summarize_samples(values, center, 4e6)
              for center in (1e9, 1.005e9)]
    output = tmp_path / "dual"
    scout.write_multi_scan_report({0: points, 1: points}, output, {"gain_db": 40})
    session = json.loads((output / "scan.json").read_text())
    assert session["metadata"]["channels"] == [0, 1]
    assert session["metadata"]["simultaneous"] is True
    assert session["point_count_per_channel"] == 2
    assert (output / "scan.png").read_bytes().startswith(b"\x89PNG")
    for channel in (0, 1):
        assert (output / f"rx{channel}" / "scan.json").exists()
        assert (output / f"rx{channel}" / "scan.png").exists()


def test_capture_channels_dual_fake_and_single_rx1(tmp_path, capsys):
    common = ["--duration-s", ".05", "--center-frequency-hz", "1575000000",
              "--sample-rate-hz", "20000", "--bandwidth-hz", "15000",
              "--block-size", "128", "--fake"]
    dual = tmp_path / "dual"
    assert cli.main(["capture", str(dual), *common, "--channels", "0,1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["channels"] == [0, 1]
    assert report["samples_per_channel"] == 1000
    assert CaptureArtifact.open(dual / "rx0").manifest["radio_config"]["channel"] == 0
    assert CaptureArtifact.open(dual / "rx1").manifest["radio_config"]["channel"] == 1

    rx1 = tmp_path / "rx1"
    assert cli.main(["capture", str(rx1), *common, "--channels", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["channel"] == 1
    assert CaptureArtifact.open(rx1).manifest["radio_config"]["channel"] == 1
