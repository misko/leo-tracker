from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).parents[1]


def test_server_worker_uses_atomic_claims_uv_and_existing_venv():
    source = (ROOT / "scripts/starlink-analysis-server.sh").read_text()
    assert 'mv "${marker}" "${claim}"' in source
    assert "uv and an existing ${venv} are required" in source
    assert "run --active --no-sync leo-radio" in source
    assert "--workers" in source
    assert "starlink-beacon-frame-track" in source
    assert "associate --observations" in source
    assert 'emit "server_start' in source
    assert 'emit "job_start' in source
    assert 'emit "stage_start' in source
    assert 'emit "stage_done' in source
    assert 'print_progress heartbeat' in source
    assert "average_job=" in source
    assert "eta=" in source


def test_server_worker_exits_cleanly_when_once_queue_is_empty(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"), "--once",
         "--workers", "2", str(tmp_path)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT)}, text=True,
        capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not list((tmp_path / "staging/analysis-queue").glob("*.running.*"))
    assert "server_start" in result.stdout
    assert "progress reason=startup" in result.stdout
    assert "complete=0/0" in result.stdout
    assert "server_stop status=0" in result.stdout


def test_server_worker_reports_job_stage_progress_and_eta(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    capture = tmp_path / "captures/sample-one"
    queue.mkdir(parents=True)
    capture.mkdir(parents=True)
    (queue / "0001.job").write_text(f"sample-one\t{capture}\tnarrow\n")
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    uv_stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"), "--once",
         "--workers", "1", str(tmp_path)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT), "UV_BIN": str(uv_stub)},
        text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "job_start worker=0 job=sample-one mode=narrow" in result.stdout
    assert "stage_start worker=0 job=sample-one stage=acquire" in result.stdout
    assert "stage_done worker=0 job=sample-one stage=followup" in result.stdout
    assert "signal_result worker=0 job=sample-one confirmed=false" in result.stdout
    assert "job_done worker=0 job=sample-one" in result.stdout
    assert "complete=1/1 percent=100.0%" in result.stdout
    assert "average_job=" in result.stdout
    assert "eta=" in result.stdout
    assert list((queue / "done").glob("*.job"))
    assert list((queue / "metrics").glob("*.tsv"))


def test_server_worker_stops_pipeline_and_marks_failed_when_analysis_fails(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    capture = tmp_path / "captures/sample-bad"
    queue.mkdir(parents=True); capture.mkdir(parents=True)
    (queue / "0001.job").write_text(f"sample-bad\t{capture}\tnarrow\n")
    calls = tmp_path / "calls.log"
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$UV_STUB_LOG\"\n"
        "[[ \"$*\" != *starlink-beacon-analyze* ]]\n")
    uv_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"), "--once",
         "--workers", "1", str(tmp_path)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT), "UV_BIN": str(uv_stub),
                          "UV_STUB_LOG": str(calls)},
        text=True, capture_output=True, timeout=10)

    assert result.returncode == 0
    assert "stage_failed worker=0 job=sample-bad stage=acquire" in result.stdout
    assert "job_failed worker=0 job=sample-bad" in result.stdout
    invoked = calls.read_text()
    assert "starlink-beacon-analyze" in invoked
    assert "starlink-beacon-followup" not in invoked
    assert list((queue / "failed").glob("*.job"))
    assert not list((queue / "done").glob("*.job"))


def test_exporter_moves_complete_bundle_then_queues_it(tmp_path):
    source = tmp_path / "source"
    shared = tmp_path / "shared"
    capture = source / "captures" / "capture-one"
    queue = source / "staging" / "analysis-queue"
    capture.mkdir(parents=True)
    queue.mkdir(parents=True)
    payload = b"abcdefgh"
    (capture / "chunk-000000.ci16").write_bytes(payload)
    (capture / "manifest.json").write_text(
        '{"state":"complete","chunks":[{"path":"chunk-000000.ci16",'
        '"bytes":8,"sha256":"9c56cc51b374c3ba189210d5b6d4bf57790d351c'
        '96c47c02190ecf1e430635ab"}]}'
    )
    (queue / "0001.job").write_text(f"capture-one\t{capture}\tnarrow\n")
    env = os.environ | {
        "LEO_TRACKER_REPO": str(ROOT),
        "LEO_OFFLOAD_BWLIMIT_KBPS": "0",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-export.sh"), "--once",
         str(source), str(shared)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not capture.exists()
    assert (shared / "captures/capture-one/chunk-000000.ci16").read_bytes() == payload
    job = next((shared / "staging/analysis-queue").glob("*.job"))
    fields = job.read_text().rstrip().split("\t")
    assert fields[:3] == ["capture-one", "captures/capture-one", "narrow"]
    assert fields[3].startswith("context/bundles/")
    assert not list((shared / "staging/incoming").glob("*.partial"))


def test_exporter_refuses_incomplete_capture(tmp_path):
    source = tmp_path / "source"
    shared = tmp_path / "shared"
    capture = source / "captures" / "bad"
    queue = source / "staging" / "analysis-queue"
    capture.mkdir(parents=True)
    queue.mkdir(parents=True)
    (capture / "manifest.json").write_text('{"state":"interrupted","chunks":[]}')
    (queue / "0001.job").write_text(f"bad\t{capture}\tnarrow\n")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-export.sh"), "--once",
         str(source), str(shared)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT)}, text=True,
        capture_output=True)
    assert result.returncode == 0
    assert capture.exists()
    assert not list((shared / "staging/analysis-queue").glob("*.job"))
