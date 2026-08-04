import json

from leo_tracker.radio import cli


def test_gain_experiment_cli_e2e_covers_manual_and_both_agc_modes(tmp_path, capsys):
    root = tmp_path / "gain"
    assert cli.main(["starlink-gain-experiment", str(root), "--fake",
        "--manual-gains-db", "20", "40", "--snapshots", "6",
        "--block-size", "4096", "--fft-size", "1024", "--output-bins", "256",
        "--sample-rate-hz", "1000000", "--bandwidth-hz", "900000"]) == 0
    output = json.loads(capsys.readouterr().out)
    report = json.loads((root / "report.json").read_text())

    assert output["profiles"] == 4
    assert [item["gain_mode"] for item in report["profiles"]] == [
        "manual", "manual", "slow_attack", "fast_attack"]
    assert all((root / item["artifact"]).exists() if not str(item["artifact"]).startswith(str(root))
               else __import__('pathlib').Path(item["artifact"]).exists()
               for item in report["profiles"])
    assert all(item["capture"]["samples_per_snapshot"] == 4096 for item in report["profiles"])
