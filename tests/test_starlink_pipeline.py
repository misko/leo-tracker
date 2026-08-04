from pathlib import Path
import os
import subprocess


def test_pipeline_dry_run_uses_uv_and_all_compact_stages(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "starlink_pipeline.sh"
    env = {**os.environ, "DRY_RUN": "1", "START_UTC": "2026-08-02T00:00:00Z",
           "END_UTC": "2026-08-02T12:00:00Z"}
    result = subprocess.run(["bash", str(script), str(tmp_path / "run")], env=env,
                            check=True, text=True, capture_output=True)
    assert "uv run --active --no-sync leo-orbit" in result.stdout
    assert "leo-radio monitor" in result.stdout
    assert "leo-radio rank-regions" in result.stdout
    assert "stare_center.json" in result.stdout
    assert "--offset 0" in result.stdout
    assert "--max-cycle-lag 7" in result.stdout
    assert "--max-cycle-lag 20" in result.stdout


def test_watch_and_status_scripts_have_valid_shell_syntax(tmp_path):
    scripts = Path(__file__).parents[1] / "scripts"
    subprocess.run(["bash", "-n", str(scripts/"starlink_watch.sh")], check=True)
    subprocess.run(["bash", "-n", str(scripts/"starlink_status.sh")], check=True)
    result = subprocess.run([str(scripts/"starlink_status.sh"), str(tmp_path)],
                            check=True, text=True, capture_output=True)
    assert '"stage":"not_started"' in result.stdout


def test_hybrid_watch_dry_run_has_survey_dwell_and_fallback(tmp_path):
    script = Path(__file__).parents[1]/"scripts/starlink_hybrid_watch.sh"
    env = {**os.environ, "DRY_RUN": "1", "MAX_CYCLES": "1",
           "DWELL_SECONDS": "60"}
    result = subprocess.run(["bash", str(script), str(tmp_path/"hybrid")],
        env=env, check=True, text=True, capture_output=True)
    assert "uv run --active --no-sync leo-radio starlink-hybrid-plan" in result.stdout
    assert "leo-radio monitor" in result.stdout
    assert "--sample-rate-hz 30720000" in result.stdout
    assert "leo-radio rank-regions" in result.stdout
    assert "starlink-measurement-capture" in result.stdout
    assert "--sample-rate-hz 4000000" in result.stdout
    assert "starlink-measurement-analyze" in result.stdout
    source = script.read_text()
    assert "dwell_rate=2500000" in source
    assert "use-fallback-rate" in source
    assert "status.json" in source
    assert "10#$number + 1" in source
    assert "--interleaved-dither-hz" in result.stdout
    assert "--dither-segment-s" in result.stdout
    assert "--dither-segment-s 30" in result.stdout
    assert "leo-radio doppler-observations" in result.stdout
    assert "--assume-all-shifts-doppler" in result.stdout
    assert "--observation-mode retune-validation" in result.stdout
    assert "--snapshots 2747" in result.stdout
    assert "/observations/" in source
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_hybrid_watch_supports_agc_without_manual_gain_argument(tmp_path):
    script = Path(__file__).parents[1]/"scripts/starlink_hybrid_watch.sh"
    env = {**os.environ, "DRY_RUN": "1", "MAX_CYCLES": "1",
           "DWELL_SECONDS": "60", "GAIN_MODE": "slow_attack"}
    result = subprocess.run(["bash", str(script), str(tmp_path/"agc")],
        env=env, check=True, text=True, capture_output=True)
    capture = next(line for line in result.stdout.splitlines()
                   if "starlink-measurement-capture" in line)
    assert "--gain-mode slow_attack" in capture
    assert "--gain-db" not in capture
    assert "capture_attempt in 1 2 3" in script.read_text()


def test_hybrid_watch_fixed_mode_has_no_in_capture_retune(tmp_path):
    script = Path(__file__).parents[1]/"scripts/starlink_hybrid_watch.sh"
    env = {**os.environ, "DRY_RUN": "1", "MAX_CYCLES": "1",
           "DWELL_SECONDS": "60", "VALIDATION_EVERY_CYCLES": "0"}
    result = subprocess.run(["bash", str(script), str(tmp_path/"fixed")],
        env=env, check=True, text=True, capture_output=True)
    capture = next(line for line in result.stdout.splitlines()
                   if "starlink-measurement-capture" in line)
    assert "--observation-mode fixed" in capture
    assert "--interleaved-dither-hz" not in capture
