import json

import numpy as np

from leo_tracker.radio import cli
from leo_tracker.radio.wide_doppler import (analyze_compact_waterfall,
                                            _offset_mhz_to_rf_ghz,
                                            _rf_ghz_to_offset_mhz,
                                            plot_compact_waterfall)


def test_stationary_suppressed_tracker_recovers_moving_depression(tmp_path):
    rng = np.random.default_rng(12)
    receivers, times, bins = 2, 40, 512
    spectra = rng.normal(0, .08, (receivers, times, bins)).astype(np.float32)
    path = 160 + np.arange(times)
    for receiver in range(receivers):
        for index, center in enumerate(path):
            spectra[receiver, index, center-3:center+4] -= 2.5
    offsets = np.linspace(-2_000_000, 2_000_000, bins, endpoint=False)
    utc = 1_700_000_000_000_000_000 + np.arange(times, dtype=np.int64)*1_000_000_000
    artifact = tmp_path / "moving.npz"
    np.savez(artifact, spectra_db=spectra, utc_ns=utc,
             frequency_offsets_hz=offsets, sample_rate_hz=4_000_000,
             center_frequency_hz=1_825_000_000, fft_size=512,
             identity_json=np.array("{}"))

    report = analyze_compact_waterfall(artifact, integration_s=2.1,
                                       max_drift_hz_s=12_000,
                                       permutations=4, seed=2)

    expected_drift = offsets[1] - offsets[0]
    for track in report["tracks"]:
        assert track["fitted_drift_hz_s"] == pytest.approx(expected_drift, rel=.08)
        assert track["frequency_span_hz"] > 250_000
        assert track["median_depression_db"] > 1
    assert report["receiver_agreement"]["median_absolute_difference_hz"] < 4*expected_drift
    assert report["receiver_agreement"]["correlation"] > .95
    assert all(item["false_alarm_probability"] <= .2 for item in report["significance"])


def test_plot_labels_approximate_ku_rf_from_lnb_lo(tmp_path):
    artifact = tmp_path / "waterfall.npz"
    output = tmp_path / "waterfall.png"
    spectra = np.zeros((1, 5, 64), dtype=np.float32)
    np.savez(artifact, spectra_db=spectra,
             utc_ns=np.arange(5, dtype=np.int64) * 1_000_000_000,
             frequency_offsets_hz=np.linspace(-500_000, 500_000, 64, endpoint=False),
             sample_rate_hz=1_000_000, center_frequency_hz=1_830_000_000,
             lnb_lo_hz=9_750_000_000, fft_size=64,
             identity_json=np.array("{}"))
    analysis = {"frequency_offsets_hz": [[0] * 5], "time_s": list(range(5))}

    plot_compact_waterfall(artifact, analysis, output)

    assert output.is_file() and output.stat().st_size > 0
    rf = _offset_mhz_to_rf_ghz(
        np.array([-15.0, 0.0, 15.0]), if_center_hz=1_830_000_000,
        lnb_lo_hz=9_750_000_000)
    assert rf.tolist() == pytest.approx([11.565, 11.58, 11.595])
    assert _rf_ghz_to_offset_mhz(
        rf, if_center_hz=1_830_000_000,
        lnb_lo_hz=9_750_000_000).tolist() == pytest.approx([-15, 0, 15])


def test_wide_waterfall_cli_e2e(tmp_path, capsys):
    waterfall, analysis = tmp_path / "wide.npz", tmp_path / "analysis.json"
    assert cli.main(["starlink-waterfall-capture", str(waterfall),
        "--channel-number", "3", "--channels", "0,1", "--fake",
        "--sample-rate-hz", "1000000", "--bandwidth-hz", "900000",
        "--block-size", "16384", "--snapshots", "200", "--fft-size", "4096",
        "--output-bins", "1024", "--fake-start-offset-hz", "-200000",
        "--fake-drift-hz-s", "50000"]) == 0
    capture = json.loads(capsys.readouterr().out)
    assert capture["receiver_count"] == 2 and capture["snapshots"] == 200
    assert cli.main(["starlink-waterfall-analyze", str(waterfall), str(analysis),
                     "--integration-s", ".25", "--max-drift-hz-s", "80000"]) == 0
    result = json.loads(analysis.read_text())
    assert len(result["tracks"]) == 2
    assert result["receiver_agreement"] is not None


def test_long_watch_cli_writes_incremental_status_plot_and_detection(tmp_path, capsys):
    root = tmp_path / "watch"
    assert cli.main(["starlink-waterfall-watch", str(root), "--fake",
        "--hours", "1", "--max-chunks", "1", "--channel-number", "3",
        "--sample-rate-hz", "1000000", "--bandwidth-hz", "900000",
        "--block-size", "16384", "--chunk-snapshots", "200",
        "--fft-size", "4096", "--output-bins", "1024",
        "--fake-start-offset-hz", "-200000", "--fake-drift-hz-s", "50000",
        "--integration-s", ".25", "--max-drift-hz-s", "80000",
        "--permutations", "4", "--max-false-alarm-probability", ".2",
        "--min-receiver-correlation", ".1", "--max-receiver-difference-hz", "500000"]) == 0
    summary = json.loads((root / "summary.json").read_text())
    rows = [json.loads(row) for row in (root / "index.jsonl").read_text().splitlines()]
    assert summary["state"] == "complete" and summary["completed_chunks"] == 1
    assert len(rows) == 1 and (root / "plots/chunk-00000.png").exists()
    assert (root / "chunks/chunk-00000.npz").exists()
    assert json.loads(capsys.readouterr().out)["chunk"] == 0


def test_long_watch_resume_preserves_deadline_and_chunk_numbering(tmp_path, capsys):
    root = tmp_path / "watch"
    arguments = ["starlink-waterfall-watch", str(root), "--fake",
        "--hours", "1", "--max-chunks", "1", "--channel-number", "3",
        "--sample-rate-hz", "1000000", "--bandwidth-hz", "900000",
        "--block-size", "16384", "--chunk-snapshots", "80",
        "--fft-size", "4096", "--output-bins", "1024",
        "--fake-start-offset-hz", "-200000", "--fake-drift-hz-s", "50000",
        "--integration-s", ".25", "--max-drift-hz-s", "80000",
        "--permutations", "0"]
    assert cli.main(arguments) == 0
    first = json.loads((root / "summary.json").read_text())
    capsys.readouterr()

    assert cli.main(arguments) == 0
    resumed = json.loads((root / "summary.json").read_text())
    rows = [json.loads(row) for row in (root / "index.jsonl").read_text().splitlines()]

    assert resumed["started_utc"] == first["started_utc"]
    assert resumed["requested_hours"] == 1
    assert [row["chunk"] for row in rows] == [0, 1]
    assert (root / "plots/chunk-00001.png").exists()


def test_long_watch_resume_rejects_changed_duration(tmp_path, capsys):
    root = tmp_path / "watch"
    base = ["starlink-waterfall-watch", str(root), "--fake", "--max-chunks", "1",
        "--channel-number", "3", "--sample-rate-hz", "1000000",
        "--bandwidth-hz", "900000", "--block-size", "16384",
        "--chunk-snapshots", "80", "--fft-size", "4096", "--output-bins", "1024",
        "--integration-s", ".25", "--permutations", "0"]
    assert cli.main(base + ["--hours", "1"]) == 0
    capsys.readouterr()

    assert cli.main(base + ["--hours", "2"]) == 1
    assert "cannot resume" in capsys.readouterr().err


import pytest
