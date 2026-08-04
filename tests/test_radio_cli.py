import json
from pathlib import Path

import pytest

from leo_tracker.radio.artifact import CaptureArtifact
from leo_tracker.radio import cli


def test_fake_preflight(capsys):
    assert cli.main(["preflight", "--fake-serial", "TEST-PLUTO"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mode": "fake", "pluto_serials": ["TEST-PLUTO"], "ready": True,
    }


def test_sysfs_discovery(tmp_path):
    device = tmp_path / "1-1"; device.mkdir()
    (device / "idVendor").write_text("0456\n"); (device / "idProduct").write_text("b673\n")
    (device / "serial").write_text("PLUTO-42\n")
    assert cli.discover_pluto_serials(tmp_path) == ["PLUTO-42"]


def test_deterministic_fake_capture_analysis_and_verify(tmp_path, monkeypatch, capsys):
    capture = tmp_path / "capture"
    common = [str(capture), "--duration-s", "0.5", "--center-frequency-hz", "1500000000",
              "--sample-rate-hz", "20000", "--bandwidth-hz", "15000", "--block-size", "777",
              "--fake", "--fake-start-hz", "-1000", "--fake-stop-hz", "1200", "--seed", "7"]
    assert cli.main(["capture", *common]) == 0
    artifact = CaptureArtifact.open(capture)
    assert artifact.manifest["sample_count"] == 10_000
    assert artifact.manifest["radio_identity"] == {"kind": "fake", "seed": 7}
    assert cli.main(["verify", str(capture)]) == 0

    def fake_plot(samples, rate, destination, fft_size, hop):
        destination.write_bytes(b"deterministic png fixture")
    monkeypatch.setattr(cli, "_write_waterfall", fake_plot)
    analysis = tmp_path / "analysis"
    assert cli.main(["analyze", str(capture), str(analysis), "--fft-size", "512",
                     "--hop-size", "128", "--search-hz", "-3000", "3000", "--max-step-hz", "200"]) == 0
    ridge = json.loads((analysis / "ridge.json").read_text())
    assert ridge["capture_id"] == artifact.manifest["capture_id"]
    assert ridge["blind"] is True
    assert len(ridge["points"]) > 20
    assert (analysis / "waterfall.png").read_bytes() == b"deterministic png fixture"


def test_cli_module_has_no_orbit_dependencies():
    source = Path(cli.__file__).read_text()
    assert "leo_tracker.orbit" not in source
    assert "leo_tracker.passes" not in source
    assert "leo_tracker.fusion" not in source
