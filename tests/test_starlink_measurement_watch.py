from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def test_watcher_keeps_population_summary_current() -> None:
    repository = Path(__file__).resolve().parents[1]
    source = (repository / "scripts/starlink_measurement_watch.sh").read_text()
    assert "starlink-wide-feature-summary" in source
    assert '"$watch_root/wide/population-summary.json"' in source
    assert "--iq-evidence-output" in source
    assert "starlink-waveform-iq-analyze" in source
    assert 'rm -f "$iq_candidate"' in source
    assert "/dev/shm/leo-tracker-iq" in source
    assert "--discard-buffers 8" in source
    assert "--host-temperature-c" in source
    assert "--radio-temperature-c" in source
    assert "starlink-confound-analyze" in source
    assert '--psd-quantization-db "$psd_quantization_db"' in source
    assert 'channel-${channel}-${output_bins}bins.npz' in source
    assert "starlink-rf-baseline" in source
    assert "other_bins == bins" in source
    assert 'for backfill_measurement in "${backfill_inputs[@]}"' in source
    assert 'flock -n 8' in source
    assert 'if [[ "$fake" == "1" ]]' in source


def test_measurement_watch_fake_end_to_end(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "watch"
    passes = tmp_path / "passes.json"
    passes.write_text(json.dumps({"generated_at": "fixture", "carrier_hz": 11_330_117_187.5,
                                  "satellites": []}))
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE": "1",
            "MAX_CHUNKS": "1",
            "SNAPSHOTS": "8",
            "UV_CACHE_DIR": "/tmp/leo-tracker-uv-cache",
            "PASSES_CH3": str(passes),
            "PASSES_CH4": str(passes),
        }
    )
    result = subprocess.run(
        ["bash", str(repository / "scripts/starlink_measurement_watch.sh"), str(output)],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list((output / "chunks").glob("*.npz"))) == 1
    assert len(list((output / "analysis").glob("*.json"))) == 1
    assert len(list((output / "plots").glob("*.png"))) == 1
    analysis = json.loads(next((output / "analysis").glob("*.json")).read_text())
    assert analysis["pass_catalog"]["path"] == str(passes)
    assert analysis["pass_catalog"]["carrier_hz"] == 11_330_117_187.5
    status = json.loads((output / "status.json").read_text())
    assert status["stage"] == "stopped"

    second = subprocess.run(
        ["bash", str(repository / "scripts/starlink_measurement_watch.sh"), str(output)],
        cwd=repository, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert sorted(path.name for path in (output / "analysis").glob("*.json")) == [
        next((output / "analysis").glob("chunk-00000-*.json")).name,
        next((output / "analysis").glob("chunk-00001-*.json")).name,
    ]


def test_measurement_watch_preserves_capture_failure_status(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "failed-watch"
    environment = os.environ.copy()
    environment.update({"UV_BIN": "/bin/false", "MAX_FAILURES": "1", "PASS_REFRESH": "0"})

    result = subprocess.run(
        ["bash", str(repository / "scripts/starlink_measurement_watch.sh"), str(output)],
        cwd=repository, env=environment, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 1
    status = json.loads((output / "status.json").read_text())
    assert status["stage"] == "failed"
    assert "pipeline_exit=1" in status["detail"]


def test_measurement_watch_bootstraps_resolution_specific_baselines(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "bootstrap-watch"
    passes = tmp_path / "passes.json"
    passes.write_text(json.dumps({"generated_at": "fixture",
                                  "carrier_hz": 11_330_117_187.5,
                                  "satellites": []}))
    environment = os.environ.copy()
    environment.update({
        "FAKE": "1", "MAX_CHUNKS": "8", "SNAPSHOTS": "50",
        "BLOCK_SIZE": "4096", "FFT_SIZE": "1024", "OUTPUT_BINS": "256",
        "PSD_QUANTIZATION_DB": ".01", "BASELINE_MIN_CAPTURES": "4",
        "WIDE_INTEGRATION_S": ".25",
        "UV_CACHE_DIR": "/tmp/leo-tracker-uv-cache",
        "PASSES_CH3": str(passes), "PASSES_CH4": str(passes),
    })

    result = subprocess.run(
        ["bash", str(repository / "scripts/starlink_measurement_watch.sh"), str(output)],
        cwd=repository, env=environment, capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (output / "baseline/channel-3-256bins.npz").is_file()
    assert (output / "baseline/channel-4-256bins.npz").is_file()
    assert len(list((output / "wide").glob("chunk-*.json"))) == 8
    assert not list((output / "iq-staging").glob("*.npz"))
