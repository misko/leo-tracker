from pathlib import Path
import os
import subprocess
import time


ROOT = Path(__file__).parents[1]


def test_analysis_export_unit_is_persistent_and_copy_only():
    unit = (ROOT / "deploy/leo-tracker-analysis-export.service").read_text()
    assert "Requires=mnt-leo\\x2dnvme.mount" in unit
    assert "Environment=LEO_OFFLOAD_SOURCE_POLICY=retain" in unit
    assert "ExecStart=/home/satpi01/leo-tracker/scripts/starlink-analysis-export.sh" in unit
    assert "Restart=always" in unit
    assert "WantedBy=multi-user.target" in unit


def test_kalman_service_uses_sixteen_single_thread_workers_in_shadow_mode():
    unit = (ROOT / "deploy/leo-tracker-analysis-server.service").read_text()
    assert "Environment=LEO_ANALYSIS_WORKERS=16" in unit
    assert "Environment=LEO_ANALYSIS_FULL_COVERAGE=1" in unit
    assert "Environment=LEO_ANALYSIS_ARCHIVE_MODE=shadow" in unit
    assert "Environment=LEO_ANALYSIS_RETENTION_MODE=disabled" in unit
    assert "Environment=LEO_ANALYSIS_FULL_EXACT_INTERVAL_S=1" in unit
    assert "Environment=LEO_ANALYSIS_WIDE_ACQUISITION_SPAN_HZ=12000000" in unit
    assert "Environment=OMP_NUM_THREADS=1" in unit
    assert "Environment=OPENBLAS_NUM_THREADS=1" in unit
    assert "Environment=MKL_NUM_THREADS=1" in unit
    assert "TimeoutStopSec=1800" in unit


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
    assert "run_retention" in source
    assert "starlink-beacon-retain" in source
    assert 'flock -n 10' in source
    assert 'workers="${LEO_ANALYSIS_WORKERS:-16}"' in source
    assert "starlink-evidence-archive" in source
    assert "inspect-followup" in source
    assert "retention_skipped" in source
    assert "analysis-server-status.json" in source
    assert source.index("starlink-evidence-archive") < source.index(
        'run_retention "${worker_id}"')
    assert 'trap request_local_drain TERM' in source


def test_backfill_wrapper_uses_uv_existing_venv_and_versioned_enqueue():
    source = (ROOT / "scripts/starlink-analysis-backfill.sh").read_text()
    assert "uv and the existing ${repo_dir}/.venv are required" in source
    assert "run --active --no-sync" in source
    assert "enqueue-backfill" in source
    assert "LEO_ANALYSIS_PIPELINE_ID" in source


def test_source_export_backfill_wrapper_is_bounded_copy_only_uv_workflow():
    source = (ROOT / "scripts/starlink-export-backfill.sh").read_text()
    assert "run --active --no-sync" in source
    assert "enqueue-export-backfill" in source
    assert "LEO_BEACON_STORAGE" in source
    assert "LEO_OFFLOAD_ROOT" in source
    assert "LEO_ANALYSIS_PIPELINE_ID" in source


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
    assert "stage_done worker=0 job=sample-one stage=decode" in result.stdout
    assert "stage_done worker=0 job=sample-one stage=doppler_track" in result.stdout
    assert "stage_done worker=0 job=sample-one stage=evidence_archive" in result.stdout
    assert "retention_skipped worker=0 job=sample-one mode=disabled" in result.stdout
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


def test_server_drain_finishes_claimed_job_without_claiming_next(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    capture = tmp_path / "captures/sample"
    queue.mkdir(parents=True); capture.mkdir(parents=True)
    for index in range(2):
        (queue / f"000{index}.job").write_text(
            f"sample-{index}\t{capture}\tnarrow\n")
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$*\" != *starlink-beacon-analyze* ]] || sleep 1\n"
        "exit 0\n")
    uv_stub.chmod(0o755)
    env = os.environ | {"LEO_TRACKER_REPO": str(ROOT), "UV_BIN": str(uv_stub),
                        "LEO_ANALYSIS_HEARTBEAT_S": "1"}
    server = subprocess.Popen(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"),
         "--workers", "1", str(tmp_path)], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while not list(queue.glob("*.running.*")) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(queue.glob("*.running.*"))

    drained = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"),
         "--drain", str(tmp_path)], env=env, text=True,
        capture_output=True, timeout=5)
    stdout, stderr = server.communicate(timeout=10)

    assert drained.returncode == 0
    assert "drain requested" in drained.stdout
    assert server.returncode == 0, stderr
    assert "worker_drained worker=0" in stdout
    assert "drain_complete" in stdout
    assert len(list((queue / "done").glob("*.job"))) == 1
    assert len(list(queue.glob("*.job"))) == 1
    assert not list(queue.glob("*.running.*"))


def test_server_sigterm_gracefully_finishes_claimed_job(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    capture = tmp_path / "captures/sample"
    queue.mkdir(parents=True); capture.mkdir(parents=True)
    for index in range(2):
        (queue / f"000{index}.job").write_text(
            f"sample-{index}\t{capture}\tnarrow\n")
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$*\" != *starlink-beacon-analyze* ]] || sleep 1\n"
        "exit 0\n")
    uv_stub.chmod(0o755)
    env = os.environ | {"LEO_TRACKER_REPO": str(ROOT), "UV_BIN": str(uv_stub),
                        "LEO_ANALYSIS_HEARTBEAT_S": "1"}
    server = subprocess.Popen(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"),
         "--workers", "1", str(tmp_path)], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while not list(queue.glob("*.running.*")) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(queue.glob("*.running.*"))

    server.terminate()
    stdout, stderr = server.communicate(timeout=15)

    assert server.returncode == 0, stderr
    assert "drain_requested reason=signal" in stdout
    assert "drain_complete" in stdout
    assert len(list((queue / "done").glob("*.job"))) == 1
    assert len(list(queue.glob("*.job"))) == 1
    assert not list(queue.glob("*.running.*"))


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
        "LEO_OFFLOAD_SOURCE_POLICY": "delete",
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


def test_exporter_retain_policy_copies_without_removing_source(tmp_path):
    source = tmp_path / "source"
    shared = tmp_path / "shared"
    capture = source / "captures" / "capture-retained"
    queue = source / "staging" / "analysis-queue"
    capture.mkdir(parents=True); queue.mkdir(parents=True)
    payload = b"abcdefgh"
    (capture / "chunk-000000.ci16").write_bytes(payload)
    (capture / "manifest.json").write_text(
        '{"state":"complete","chunks":[{"path":"chunk-000000.ci16",'
        '"bytes":8,"sha256":"9c56cc51b374c3ba189210d5b6d4bf57790d351c'
        '96c47c02190ecf1e430635ab"}]}'
    )
    (queue / "0001.job").write_text(f"capture-retained\t{capture}\tnarrow\n")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-export.sh"), "--once",
         str(source), str(shared)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT),
                          "LEO_OFFLOAD_SOURCE_POLICY": "retain",
                          "LEO_OFFLOAD_BWLIMIT_KBPS": "0"},
        text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert capture.is_dir()
    assert (capture / "chunk-000000.ci16").read_bytes() == payload
    assert (shared / "captures/capture-retained/chunk-000000.ci16").read_bytes() == payload
    assert "source_policy=retain" in result.stdout


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
