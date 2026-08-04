import json
from types import SimpleNamespace

import numpy as np

from leo_tracker.radio import cli, monitor


RATE = 1_000_000
CENTER = 100_000_000


def _textured_noise(count=32768, seed=1):
    rng = np.random.default_rng(seed)
    spectrum = (rng.standard_normal(count) + 1j*rng.standard_normal(count))
    shape = .2 + np.exp(-.5*((np.arange(count)-count*.43)/(count*.035))**2)
    return np.fft.ifft(np.fft.ifftshift(spectrum*shape)).astype(np.complex64)*1000


def _frame(psd, cycle, channel, bin_width=10_000):
    return monitor.SpectrumFrame(cycle, channel, CENTER, cycle*1_000_000_000,
        -40, 12, 0, bin_width, tuple(np.asarray(psd, np.float32)))


def test_compact_psd_and_shift_recover_broadband_motion():
    samples = _textured_noise()
    reference, width = monitor.compact_psd(samples, sample_rate_hz=RATE,
                                            bandwidth_hz=800_000, output_bins=512)
    current = np.roll(reference, 40)
    shift, correlation = monitor.estimate_spectral_shift(
        reference, current, bin_width_hz=width, max_shift_hz=100_000)
    assert abs(shift-40*width) < width
    assert correlation > .99


def test_dual_motion_requires_agreement_and_rejects_stationary():
    rng = np.random.default_rng(3); base = rng.standard_normal(256)
    moved0 = np.roll(base, 6); moved1 = np.roll(base, 7)
    prior = {ch: _frame(base, 0, ch) for ch in (0, 1)}
    current = {0: _frame(moved0, 1, 0), 1: _frame(moved1, 1, 1)}
    candidate = monitor.promote_dual_motion(prior, current, min_shift_hz=30_000,
        max_shift_hz=100_000, min_correlation=.8, channel_tolerance_hz=20_000)
    assert candidate is not None
    assert candidate.shift_hz == 65_000
    assert monitor.promote_dual_motion(prior, prior, min_shift_hz=30_000) is None
    disagree = {0: _frame(np.roll(base, 6), 1, 0), 1: _frame(np.roll(base, -6), 1, 1)}
    assert monitor.promote_dual_motion(prior, disagree, min_shift_hz=30_000,
                                       channel_tolerance_hz=20_000) is None


def test_multi_lag_finds_slow_motion_below_adjacent_threshold():
    rng = np.random.default_rng(8); base = rng.standard_normal(256)
    cycles = tuple(tuple(_frame(np.roll(base, cycle), cycle, channel)
                         for channel in (0, 1)) for cycle in range(4))
    assert not monitor.find_motion_candidates(cycles, min_shift_hz=25_000,
                                              min_correlation=.9, max_cycle_lag=1)
    found = monitor.find_motion_candidates(cycles, min_shift_hz=25_000,
                                           min_correlation=.9, max_cycle_lag=3)
    assert found and found[0].shift_hz == 30_000
    assert found[0].current_cycle - found[0].previous_cycle == 3


class _FakeAd9361:
    def __init__(self, uri):
        self.uri = uri; self.rx_enabled_channels = []; self.rx_lo = 0; self.calls = 0
    def rx_destroy_buffer(self): pass
    def rx(self):
        self.calls += 1
        base = _textured_noise(self.rx_buffer_size, seed=round(self.rx_lo)+self.calls//2)
        if self.calls > 4: base = np.roll(base, 4)
        return np.vstack((base, base))


def test_monitor_hardware_path_and_cli_report(tmp_path, monkeypatch, capsys):
    device = _FakeAd9361("test")
    monkeypatch.setattr(monitor.importlib, "import_module",
                        lambda name: SimpleNamespace(ad9361=lambda uri: device))
    monkeypatch.setattr(monitor.time, "sleep", lambda seconds: None)
    output = tmp_path/"monitor"
    code = cli.main(["monitor", str(output), "--uri", "pluto://test",
        "--start-hz", str(CENTER), "--stop-hz", str(CENTER+1_000_000),
        "--step-hz", "1000000", "--cycles", "2", "--sample-rate-hz", str(RATE),
        "--bandwidth-hz", "800000", "--samples-per-tuning", "32768",
        "--fft-size", "4096", "--psd-bins", "512", "--settle-seconds", "0",
        "--min-shift-hz", "3000", "--max-shift-hz", "100000",
        "--channel-tolerance-hz", "5000"])
    assert code == 0
    payload = json.loads((output/"monitor.json").read_text())
    arrays = np.load(output/"spectra.npz")
    assert payload["cycles"] == 2 and payload["channels"] == [0, 1]
    assert arrays["psd_db"].shape == (2, 2, 2, 512)
    assert json.loads(capsys.readouterr().out)["report"].endswith("monitor.json")
    reprocessed = tmp_path / "reprocessed"
    assert cli.main(["reanalyze-monitor", str(output/"monitor.json"), str(reprocessed),
                     "--max-cycle-lag", "1"]) == 0
    assert json.loads((reprocessed/"monitor.json").read_text())["metadata"]["max_cycle_lag"] == 1
