import json
import numpy as np

from leo_tracker.radio import FakeSource, RadioConfig, capture_to_artifact
from leo_tracker.radio.carrier import track_carrier
from leo_tracker.radio.cli import main
from leo_tracker.radio.synthetic import linear_chirp, tone


def test_stationary_carrier(tmp_path):
    rate, center = 20_000, 1_755_000_000
    values = tone(1850, rate, 3, amplitude=.2, noise_std=.05, seed=2)
    path = tmp_path/"iq.c64"; values.astype("<c8").tofile(path)
    result = track_carrier(path, sample_rate_hz=rate, center_frequency_hz=center,
        search_low_hz=center+1200, search_high_hz=center+2400, integration_s=.5,
        fft_size=2048, spectra_per_integration=4)
    assert np.median([abs(p.frequency_hz-(center+1850)) for p in result.points]) < 3
    assert abs(result.fitted_linear_drift_hz_s) < 2
    assert result.residual_rms_hz < 3
    assert min(p.prominence_db for p in result.points) > 10


def test_drifting_carrier_and_memmap_bounded_output(tmp_path, monkeypatch):
    rate, center, duration = 20_000, 1_755_000_000, 4
    values = linear_chirp(1000, 1800, rate, duration, amplitude=.15, noise_std=.04, seed=3)
    path = tmp_path/"iq.c64"; values.astype("<c8").tofile(path)
    original = np.memmap; calls = []
    monkeypatch.setattr("leo_tracker.radio.carrier.np.memmap",
                        lambda *a, **kw: (calls.append(kw.get("mode")), original(*a, **kw))[1])
    result = track_carrier(path, sample_rate_hz=rate, center_frequency_hz=center,
        search_low_hz=center+500, search_high_hz=center+2300, integration_s=.5,
        fft_size=2048, spectra_per_integration=4)
    assert calls == ["r"]
    assert len(result.points) == int(duration/.5)
    assert abs(result.fitted_linear_drift_hz_s-200) < 10
    assert 600 < result.frequency_span_hz < 800
    assert result.residual_rms_hz < 30


def test_carrier_cli_persists_points_and_provenance(tmp_path, capsys):
    rate, center = 20_000, 1_755_000_000
    artifact = capture_to_artifact(FakeSource(tone(1800, rate, 2),
        RadioConfig(center, rate, 15_000), 1000, start_utc_ns=987), tmp_path/"capture")
    output = tmp_path/"carrier.json"; plot = tmp_path/"plots"/"carrier.png"
    code = main(["carrier", str(artifact.path), str(output), "--search-center-hz", str(center+1800),
        "--search-span-hz", "1000", "--integration-s", ".5", "--fft-size", "2048",
        "--spectra-per-integration", "3", "--plot", str(plot)])
    assert code == 0
    value = json.loads(output.read_text())
    assert value["capture_id"] == artifact.manifest["capture_id"]
    assert value["capture_iq_sha256"] == artifact.manifest["files"]["iq.c64"]["sha256"]
    assert value["capture_start_utc_ns"] == 987
    assert value["analysis_claim"].startswith("measurement only")
    assert len(value["points"]) == 4
    assert json.loads(capsys.readouterr().out)["points"] == 4
    assert plot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert plot.stat().st_size > 1000
