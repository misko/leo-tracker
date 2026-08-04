import json
import numpy as np

from leo_tracker.radio import FakePairedSource, RadioConfig, capture_pair_to_artifacts
from leo_tracker.radio.cli import main
from leo_tracker.radio.qualification import qualify_paired_comb


def _comb(rate, duration, spacing, *, amplitude=.075, seed=1):
    t = np.arange(round(rate * duration)) / rate
    center = -300 + 140 * t
    result = np.zeros(t.size, np.complex64)
    for offset in range(-2, 3):
        result += amplitude * np.exp(1j * 2*np.pi*np.cumsum(center + offset*spacing)/rate)
    rng = np.random.default_rng(seed)
    result += (.07/np.sqrt(2)*(rng.standard_normal(t.size)+1j*rng.standard_normal(t.size))).astype(np.complex64)
    return result


def _session(tmp_path, rx0, rx1, rate=20_000):
    config = RadioConfig(1.575e9, rate, rate*.8)
    capture_pair_to_artifacts(FakePairedSource(rx0, rx1, config, block_size=777,
                                              start_utc_ns=123), tmp_path / "session")
    return tmp_path / "session"


def _settings():
    return dict(true_spacing_hz=900, wrong_spacings_hz=(650, 1200), fft_size=1024,
                integration_s=.25, spectra_per_integration=6, tone_count=5,
                search_hz=(-1500, 1500), max_drift_hz_s=300)


def test_positive_paired_comb_qualification(tmp_path):
    rate, duration = 20_000, 5
    rx0 = _comb(rate, duration, 900)
    rng = np.random.default_rng(8)
    rx1 = (.07/np.sqrt(2)*(rng.standard_normal(rx0.size)+1j*rng.standard_normal(rx0.size))).astype(np.complex64)
    summary, _ = qualify_paired_comb(_session(tmp_path, rx0, rx1), score_margin=.5,
                                     rx_margin=.5, min_positive_tone_fraction=.7, **_settings())
    assert summary["radio_qualified"] is True
    assert all(gate["passed"] for gate in summary["gates"].values())
    assert min(summary["deltas"]["rx0_true_minus_wrong_z"].values()) >= .5
    assert summary["metrics"]["rx0"]["900.0"]["median_positive_tone_fraction"] >= .7


def test_noise_and_control_comb_are_rejected(tmp_path):
    rng = np.random.default_rng(10); n = 100_000
    noise0 = (rng.standard_normal(n)+1j*rng.standard_normal(n)).astype(np.complex64)*.07
    noise1 = (rng.standard_normal(n)+1j*rng.standard_normal(n)).astype(np.complex64)*.07
    summary, _ = qualify_paired_comb(_session(tmp_path, noise0, noise1), score_margin=.5,
                                     rx_margin=.5, min_positive_tone_fraction=.7, **_settings())
    assert summary["radio_qualified"] is False
    assert summary["reasons"]
    assert not summary["gates"]["rx0_tone_support"]["passed"]
    control_root = tmp_path / "control"; control_root.mkdir()
    control = _comb(20_000, 5, 650, seed=12)
    control_summary, _ = qualify_paired_comb(_session(control_root, control, noise1), score_margin=.5,
                                             rx_margin=.5, min_positive_tone_fraction=.7, **_settings())
    assert control_summary["radio_qualified"] is False
    assert not control_summary["gates"]["rx0_beats_wrong_spacings"]["passed"]


def test_qualify_pair_cli_writes_explicit_rejection(tmp_path, capsys):
    rng = np.random.default_rng(2); values = (rng.standard_normal(80_000)+1j*rng.standard_normal(80_000)).astype(np.complex64)
    session = _session(tmp_path, values, values.copy()); output = tmp_path / "qualification.json"
    code = main(["qualify-pair", str(session), str(output), "--true-spacing-hz", "900",
        "--wrong-spacings-hz", "650", "1200", "--score-margin", ".5", "--rx-margin", ".5",
        "--fft-size", "1024", "--integration-s", ".25", "--spectra-per-integration", "4",
        "--tone-count", "5", "--search-hz", "-1500", "1500", "--max-drift-hz-s", "300"])
    assert code == 3
    summary = json.loads(output.read_text())
    assert summary["radio_qualified"] is False
    assert set(summary["gates"]) == {"rx0_beats_wrong_spacings", "rx0_beats_rx1", "rx0_tone_support"}
    assert json.loads(capsys.readouterr().out)["radio_qualified"] is False
