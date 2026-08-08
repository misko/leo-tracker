import json
import numpy as np
import pytest

from leo_tracker.radio import (FakePairedSource, PairedCI16Block, RadioConfig,
                               capture_pair_to_artifacts)
from leo_tracker.radio.artifact import CaptureArtifact
from leo_tracker.radio.cli import main
from leo_tracker.radio.pluto import PairedPlutoSource


def test_paired_artifacts_are_synchronized_and_cross_linked(tmp_path):
    config = RadioConfig(1.575e9, 20_000, 15_000, gain_db=20)
    rx0 = np.arange(1000, dtype=np.float32).astype(np.complex64)
    rx1 = (2j * np.arange(1000, dtype=np.float32)).astype(np.complex64)
    source = FakePairedSource(rx0, rx1, config, block_size=128, start_utc_ns=123456789)
    artifacts = capture_pair_to_artifacts(source, tmp_path / "pair", sample_count=777)
    a0, a1 = (CaptureArtifact.open(item.path) for item in artifacts)
    assert source.closed
    assert np.array_equal(a0.load_samples(), rx0[:777])
    assert np.array_equal(a1.load_samples(), rx1[:777])
    assert a0.manifest["start_utc_ns"] == a1.manifest["start_utc_ns"] == 123456789
    assert a0.manifest["metadata"]["pair_session_id"] == a1.manifest["metadata"]["pair_session_id"]
    assert a0.manifest["metadata"]["paired_capture_id"] == a1.manifest["capture_id"]
    assert a1.manifest["metadata"]["paired_capture_id"] == a0.manifest["capture_id"]
    assert a0.manifest["radio_config"]["channel"] == 0
    assert a1.manifest["radio_config"]["channel"] == 1
    pair = json.loads((tmp_path / "pair" / "pair.json").read_text())
    assert pair["sample_count_per_channel"] == 777


def test_paired_failure_publishes_neither_channel(tmp_path):
    class Broken(FakePairedSource):
        def blocks(self):
            yield next(super().blocks())
            raise RuntimeError("USB loss")
    config = RadioConfig(1e9, 10_000, 8_000)
    source = Broken(np.ones(100), np.ones(100), config, block_size=50)
    destination = tmp_path / "pair"
    with pytest.raises(RuntimeError, match="USB loss"):
        capture_pair_to_artifacts(source, destination, sample_count=100)
    assert source.closed and not destination.exists()
    assert not list(tmp_path.glob("*.incomplete-*"))


def test_injected_paired_pluto_reads_hardware_once_per_block(monkeypatch):
    class Device:
        calls = 0; closed = False
        def rx(self): self.calls += 1; return np.vstack((np.ones(8), np.full(8, 2j)))
        def close(self): self.closed = True
    device = Device(); config = RadioConfig(1e9, 10_000, 8_000)
    clock = iter((1_000, 1_400))
    monkeypatch.setattr("leo_tracker.radio.pluto.time.time_ns", lambda: next(clock))
    source = PairedPlutoSource(config, uri="pluto://test", block_size=8,
                               device_factory=lambda **kwargs: device)
    block = next(source.blocks())
    assert device.calls == 1
    assert np.all(block.rx0 == 1) and np.all(block.rx1 == 2j)
    assert block.utc_ns == 1_200
    assert block.read_duration_ns == 400
    assert "host UTC bracket" in source.identity["timestamp_semantics"]
    source.close(); assert device.closed


def test_injected_paired_pluto_native_ci16_skips_complex_rx(monkeypatch):
    class Device:
        complex_calls = 0
        native_calls = 0
        def rx(self):
            self.complex_calls += 1
            raise AssertionError("complex conversion path was used")
        def rx_ci16(self):
            self.native_calls += 1
            return tuple(np.arange(8, dtype=np.int16) + offset
                         for offset in (0, 100, 200, 300))
        def close(self): pass
    device = Device()
    clock = iter((1_000, 1_400))
    monkeypatch.setattr("leo_tracker.radio.pluto.time.time_ns", lambda: next(clock))
    source = PairedPlutoSource(
        RadioConfig(1e9, 10_000, 8_000), uri="pluto://test", block_size=8,
        sample_format="native-ci16", device_factory=lambda **kwargs: device)
    block = next(source.blocks())
    assert isinstance(block, PairedCI16Block)
    assert block.sample_count == 8
    assert block.components[2][3] == 203
    assert block.utc_ns == 1_200 and block.read_duration_ns == 400
    assert device.native_calls == 1 and device.complex_calls == 0
    assert source.identity["sample_format"] == "native-ci16"


def test_native_backend_validates_component_contract():
    from leo_tracker.radio.pluto import _PyadiPairedRx
    backend = object.__new__(_PyadiPairedRx)
    class SDR:
        def _rx_buffered_data(self):
            return [np.ones(8, dtype=np.int16) for _ in range(4)]
    backend.sdr = SDR()
    assert len(backend.rx_ci16()) == 4
    backend.sdr._rx_buffered_data = lambda: [np.ones(8, dtype=np.int16)] * 3
    with pytest.raises(RuntimeError, match="four components"):
        backend.rx_ci16()


def test_injected_paired_pluto_can_retune_both_receivers():
    class Device:
        center = None
        def rx(self): return np.vstack((np.ones(8), np.ones(8)))
        def retune(self, center): self.center = center
        def close(self): pass
    device = Device(); source = PairedPlutoSource(
        RadioConfig(1e9, 10_000, 8_000), uri="pluto://test", block_size=8,
        device_factory=lambda **kwargs: device)
    source.retune(1_000_500_000)
    assert device.center == 1_000_500_000
    with pytest.raises(ValueError): source.retune(0)


def test_gain_telemetry_transport_failure_is_nonfatal():
    class Device:
        def rx(self): return np.vstack((np.ones(8), np.ones(8)))
        def gain_snapshot(self): raise BrokenPipeError("IIO telemetry loss")
        def close(self): pass
    source = PairedPlutoSource(RadioConfig(1e9, 10_000, 8_000),
        uri="pluto://test", block_size=8, device_factory=lambda **kwargs: Device())
    assert all(np.isnan(source.gain_snapshot()))


def test_paired_pluto_samples_gain_about_once_per_second_and_reads_back_mode(monkeypatch):
    class Device:
        gain_calls = 0
        def rx(self): return np.vstack((np.ones(8), np.ones(8)))
        def gain_snapshot(self):
            self.gain_calls += 1
            return (30 + self.gain_calls, 40 + self.gain_calls)
        def gain_mode_snapshot(self): return ("slow_attack", "slow_attack")
        def close(self): pass
    device = Device()
    clock = iter(range(1_000, 2_000, 100))
    monkeypatch.setattr("leo_tracker.radio.pluto.time.time_ns", lambda: next(clock))
    source = PairedPlutoSource(
        RadioConfig(1e9, 16, 8, gain_mode="slow_attack"), uri="pluto://test",
        block_size=8, device_factory=lambda **kwargs: device)
    blocks = source.blocks()
    first, second, third = next(blocks), next(blocks), next(blocks)
    assert first.gain_db == (31, 41)
    assert second.gain_db is None
    assert third.gain_db == (32, 42)
    assert source.identity["gain_mode_readback"] == ["slow_attack", "slow_attack"]


def test_paired_cli_fake_end_to_end(tmp_path, capsys):
    destination = tmp_path / "session"
    result = main(["paired-capture", str(destination), "--duration-s", ".1",
        "--center-frequency-hz", "1575000000", "--sample-rate-hz", "20000",
        "--bandwidth-hz", "15000", "--block-size", "333", "--fake", "--seed", "9"])
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["samples_per_channel"] == 2000
    assert CaptureArtifact.open(destination / "rx0").manifest["metadata"]["pair_session_id"] == report["pair_session_id"]
