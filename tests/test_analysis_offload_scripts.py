from pathlib import Path
import json
import os
import shutil
import subprocess
import time


ROOT = Path(__file__).parents[1]


def test_analysis_export_unit_is_persistent_and_copy_only():
    unit = (ROOT / "deploy/leo-tracker-analysis-export.service").read_text()
    script = (ROOT / "scripts/starlink-analysis-export.sh").read_text()
    assert "Requires=mnt-leo\\x2dnvme.mount" in unit
    assert "Environment=LEO_OFFLOAD_SOURCE_POLICY=retain" in unit
    assert "Environment=LEO_OFFLOAD_RECONCILE_S=600" in unit
    assert "Environment=LEO_ANALYSIS_PIPELINE_ID=kalman-full-v1" in unit
    assert "Environment=UV_BIN=/home/satpi01/.local/bin/uv" in unit
    assert "ExecStart=/home/satpi01/leo-tracker/scripts/starlink-analysis-export.sh" in unit
    assert '--archive-root "${archive_root}"' in script
    assert "recover-stale" in script
    assert "LEO_OFFLOAD_STALE_CAPTURE_AGE_S" in script
    assert 'evidence/pilot_symbolwise_v3/' in script
    assert "Restart=always" in unit
    assert "WantedBy=multi-user.target" in unit


def test_local_reclaimer_is_qnap_gated_and_uses_existing_uv_environment():
    unit = (ROOT / "deploy/leo-tracker-local-reclaimer.service").read_text()
    script = (ROOT / "scripts/starlink-local-reclaimer.sh").read_text()
    assert "Requires=mnt-leo\\x2dnvme.mount mnt-qnap01.mount" in unit
    assert "After=network-online.target" in unit
    assert "LEO_LOCAL_RECLAIM_MINIMUM_AGE_S=300" in unit
    assert "/mnt/qnap01/mouse9911/leo-cropped" in unit
    assert '--archive-root "${archive_root}"' in script
    assert "Restart=always" in unit
    assert "starlink-local-reclaimer.sh" in unit
    assert '"${uv_bin}" run \\\n  --active --no-sync leo-radio "${args[@]}"' in script
    assert "starlink-storage-reconcile" in script
    assert "--apply --watch" in script
    assert "--minimum-age-s" in script
    assert "--output" in script


def test_qnap_lifecycle_service_is_six_hour_verified_v2_working_set():
    unit = (ROOT / "deploy/leo-tracker-qnap-lifecycle.service").read_text()
    script = (ROOT / "scripts/starlink-qnap-lifecycle.sh").read_text()
    assert "Environment=LEO_QNAP_RECLAIM_ENABLED=1" in unit
    assert "Environment=LEO_QNAP_RECLAIM_INTERVAL_S=60" in unit
    assert "Environment=LEO_QNAP_RECLAIM_MAXIMUM_TIER=4" in unit
    assert "Environment=LEO_QNAP_RECLAIM_MINIMUM_AGE_HOURS=6" in unit
    assert "Environment=LEO_QNAP_RECLAIM_IGNORE_PRESSURE=1" in unit
    assert "Environment=LEO_QNAP_RECLAIM_TRIGGER_FREE_GB=500" in unit
    assert "Environment=LEO_QNAP_RECLAIM_TARGET_FREE_GB=750" in unit
    assert 'if [[ "${enabled}" == "1" ]]' in script
    assert 'if [[ "${enabled}" != "0" && "${enabled}" != "1" ]]' in script
    assert "--apply --confirm DELETE-QNAP-RAW-IQ" in script
    assert "starlink-qnap-lifecycle" in script
    assert "--maximum-tier" in script
    assert "--ignore-pressure" in script
    assert 'if ! env UV_CACHE_DIR="${repo_dir}/.uv-cache"' in script
    assert "qnap_lifecycle_pass_deferred" in script
    assert 'sleep "${interval_s}"' in script


def test_storage_regime_service_is_bounded_verified_v2_and_uses_existing_uv():
    unit = (ROOT / "deploy/leo-tracker-storage-regime-v2.service").read_text()
    script = (ROOT / "scripts/starlink-storage-regime-v2.sh").read_text()
    assert "Environment=LEO_STORAGE_REGIME_ENABLED=1" in unit
    assert "Environment=LEO_STORAGE_REGIME_LIMIT=32" in unit
    assert "Environment=LEO_STORAGE_REGIME_PLANNING_LIMIT=128" in unit
    assert "Environment=LEO_STORAGE_REGIME_WORKERS=16" in unit
    assert "Environment=LEO_STORAGE_REGIME_ARCHIVE_SLOTS=16" in unit
    assert "Environment=LEO_STORAGE_REGIME_MINIMUM_AGE_HOURS=6" in unit
    assert "Environment=LEO_STORAGE_REGIME_ROLE=primary" in unit
    assert "Environment=LEO_STORAGE_PRIMARY_HEARTBEAT_S=30" in unit
    assert "Environment=LEO_STORAGE_PRIMARY_LEASE_MAX_AGE_S=120" in unit
    assert "UV_BIN=/home/mouse9911/.local/bin/uv" in unit
    assert "starlink-storage-regime-v2" in script
    assert 'scope="${LEO_STORAGE_REGIME_SCOPE:-auto}"' in script
    assert '--scope "${scope}"' in script
    assert '--planning-limit "${planning_limit}"' in script
    assert "starlink-shared-transient-converge" in script
    assert "DELETE-STALE-LEO-TRANSIENTS" in script
    assert '--archive-reserved-slots "${archive_slots}"' in script
    assert '--workers "${workers}"' in script
    assert "--confirm MIGRATE-TO-EVIDENCE-V2" in script
    assert '"${uv_bin}" run --active --no-sync' in script
    assert 'cd "${repo_dir}"' in script
    assert "starlink-storage-normalize-legacy" in script
    assert "legacy_normalization_ready(plan)" in script
    assert "legacy_normalization_complete(plan)" in script
    assert "starlink-storage-audit-v2" in script
    assert "--require-producer-contract" in script
    assert "--confirm NORMALIZE-LEGACY-LAYOUT" in script
    assert "legacy_layout_batch_failed" in script
    assert "trap request_drain TERM INT" in script
    assert "storage_regime_drain_complete" in script
    assert "storage_regime_primary_active" in script
    assert "storage_primary_lease_is_fresh" in script
    assert "Restart=on-failure" in unit
    assert "KillMode=process" in unit
    assert "TimeoutStopSec=1800" in unit


def test_pi_storage_regime_fallback_is_persistent_low_priority_and_bounded():
    unit = (ROOT / "deploy/systemd/leo-tracker-storage-regime-v2-fallback.service").read_text()
    script = (ROOT / "scripts/starlink-storage-regime-v2.sh").read_text()
    assert "User=satpi01" in unit
    assert "LEO_STORAGE_LOCAL_ROOT=/mnt/leo-nvme/leo-tracker" in unit
    assert 'audit_args+=(--local-root "${local_root}")' in script
    assert "Environment=LEO_STORAGE_REGIME_ENABLED=1" in unit
    assert "Environment=LEO_STORAGE_REGIME_LIMIT=12" in unit
    assert "Environment=LEO_STORAGE_REGIME_PLANNING_LIMIT=320" in unit
    assert "Environment=LEO_STORAGE_REGIME_WORKERS=6" in unit
    assert "Environment=LEO_STORAGE_REGIME_ARCHIVE_SLOTS=1" in unit
    assert "Environment=LEO_STORAGE_REGIME_INTERVAL_S=60" in unit
    assert "Environment=LEO_STORAGE_REGIME_ROLE=fallback" in unit
    assert "Environment=LEO_STORAGE_PRIMARY_HEARTBEAT_S=30" in unit
    assert "Environment=LEO_STORAGE_PRIMARY_LEASE_MAX_AGE_S=120" in unit
    assert "Environment=LEO_STORAGE_AUDIT_INTERVAL_S=600" in unit
    assert "Nice=10" in unit
    assert "CPUWeight=20" in unit
    assert "IOWeight=20" in unit
    assert "Requires=mnt-qnap01.mount" in unit
    assert "Restart=on-failure" in unit
    assert "KillMode=process" in unit
    assert "TimeoutStopSec=1800" in unit


def test_kalman_storage_cutover_drains_installs_and_verifies_both_contracts():
    script = (ROOT / "scripts/kalman-storage-cutover.sh").read_text()
    assert "sudo systemctl stop leo-tracker-analysis-server.service" in script
    assert "sudo install -m 0644" in script
    assert "leo-tracker-storage-regime-v2.service" in script
    assert "sudo systemctl enable --now" in script
    assert 'analysis.get("producer_contract_valid") is True' in script
    assert 'analysis.get("evidence_policy") == "tiered-v2"' in script
    assert 'storage.get("state") == "running"' in script
    assert ".venv/bin/python" in script
    assert "uv venv" not in script


def test_pi_storage_fallback_yields_to_fresh_primary_without_inventory(tmp_path):
    shared = tmp_path / "shared"; archive = tmp_path / "archive"
    runtime = shared / "reports/runtime/storage-regime-v2-primary.json"
    runtime.parent.mkdir(parents=True); archive.mkdir()
    runtime.write_text(json.dumps({
        "schema": "leo-tracker.storage-regime-v2-primary/v1",
        "state": "running", "updated_epoch_s": int(time.time()),
    }))
    calls = tmp_path / "calls"
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$UV_STUB_LOG\"\n")
    uv_stub.chmod(0o755)
    process = subprocess.Popen(
        ["bash", str(ROOT / "scripts/starlink-storage-regime-v2.sh"),
         str(shared), str(archive)],
        env=os.environ | {
            "LEO_TRACKER_REPO": str(ROOT), "UV_BIN": str(uv_stub),
            "UV_STUB_LOG": str(calls), "LEO_STORAGE_REGIME_ROLE": "fallback",
            "LEO_STORAGE_REGIME_INTERVAL_S": "1",
            "LEO_STORAGE_PRIMARY_LEASE_MAX_AGE_S": "60",
        }, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1.5); process.terminate()
    output, _ = process.communicate(timeout=10)
    assert process.returncode == 0
    assert "storage_regime_primary_active" in output
    assert "storage_regime_drain_complete" in output
    assert not calls.exists()


def test_storage_regime_sigterm_finishes_active_batch_without_starting_another(
    tmp_path,
):
    shared = tmp_path / "shared"
    archive = tmp_path / "archive"
    shared.mkdir()
    archive.mkdir()
    started = tmp_path / "started"
    completed = tmp_path / "completed"
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf start >> {started!s}\n"
        "sleep 1\n"
        f"printf done >> {completed!s}\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)
    env = os.environ | {
        "LEO_TRACKER_REPO": str(ROOT),
        "UV_BIN": str(uv_stub),
        "LEO_STORAGE_REGIME_ENABLED": "1",
        "LEO_STORAGE_REGIME_INTERVAL_S": "60",
    }
    server = subprocess.Popen(
        [
            "bash",
            str(ROOT / "scripts/starlink-storage-regime-v2.sh"),
            str(shared),
            str(archive),
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert started.exists()

    server.terminate()
    stdout, stderr = server.communicate(timeout=10)

    assert server.returncode == 0, stderr
    assert completed.read_text() == "done"
    assert started.read_text() == "start"
    assert "storage_regime_drain_requested" in stderr
    assert "storage_regime_drain_complete" in stdout


def test_pi_capture_service_delegates_analysis_exclusively_to_kalman():
    unit = (ROOT / "deploy/systemd/leo-tracker-beacon-watch.service").read_text()
    watcher = (ROOT / "scripts/starlink-beacon-watch.sh").read_text()
    assert "Environment=LEO_BEACON_ANALYSIS_MODE=offload" in unit
    assert 'analysis_mode="${LEO_BEACON_ANALYSIS_MODE:-local}"' in watcher
    assert 'if [[ "${analysis_mode}" == "local" ]]' in watcher
    assert '"local_analysis_workers":0' in watcher
    assert '"queue_owner":"starlink-analysis-export"' in watcher
    assert '(( host_temperature_millic_value >= resume_pi_temp_millic ))' in watcher


def test_pi_dashboard_reads_authoritative_kalman_results():
    unit = (ROOT / "deploy/systemd/leo-tracker-dashboard.service").read_text()
    assert "RequiresMountsFor=/mnt/qnap01/mouse9911/leo" in unit
    assert "--beacon-root /mnt/qnap01/mouse9911/leo" in unit
    assert "--beacon-root /mnt/leo-nvme/leo-tracker" not in unit


def test_watcher_rejects_ambiguous_analysis_mode(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-beacon-watch.sh")],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT),
                          "LEO_BEACON_STORAGE": str(tmp_path),
                          "LEO_BEACON_ANALYSIS_MODE": "both"},
        text=True, capture_output=True, timeout=5)
    assert result.returncode == 2
    assert "must be local or offload" in result.stderr


def test_kalman_service_uses_sixteen_single_thread_workers_with_required_v2():
    unit = (ROOT / "deploy/leo-tracker-analysis-server.service").read_text()
    assert "Environment=LEO_ANALYSIS_WORKERS=16" in unit
    assert "Environment=LEO_ANALYSIS_FULL_COVERAGE=1" in unit
    assert "Environment=LEO_ANALYSIS_ARCHIVE_MODE=required" in unit
    assert "Environment=LEO_ANALYSIS_RETENTION_MODE=disabled" in unit
    assert "Environment=LEO_ANALYSIS_FULL_EXACT_INTERVAL_S=1" in unit
    assert "Environment=LEO_ANALYSIS_WIDE_ACQUISITION_SPAN_HZ=3500000" in unit
    assert "Environment=LEO_ANALYSIS_TRACK_MAXIMUM_GAP_S=15" in unit
    assert "Environment=LEO_ANALYSIS_TRACK_MAXIMUM_REACQUISITION_SPAN_HZ=15000" in unit
    script = (ROOT / "scripts/starlink-analysis-server.sh").read_text()
    assert "starlink-evidence-archive-v2" in script
    assert 'catalog/v2/receipts/${name}.json' in script
    assert "starlink-evidence-archive \"" not in script
    assert "Environment=LEO_ANALYSIS_FRAGMENT_MAXIMUM_GAP_S=30" in unit
    assert "Environment=LEO_ANALYSIS_BACKFILL_INTERVAL_S=600" in unit
    assert "Environment=LEO_ANALYSIS_BACKFILL_LIMIT=100" in unit
    assert "run_backfill startup" in script
    assert "run_backfill periodic" in script
    assert "enqueue-backfill" in script
    assert "reconcile_failed startup" in script
    assert "reconcile_failed periodic" in script
    assert "reconcile-failed" in script
    assert "--summary-only" in script
    assert "Environment=UV_BIN=/home/mouse9911/.local/bin/uv" in unit
    assert "Environment=UV_CACHE_DIR=/home/mouse9911/gits/leo-tracker/.uv-cache" in unit
    assert "Environment=OMP_NUM_THREADS=1" in unit
    assert "Environment=OPENBLAS_NUM_THREADS=1" in unit
    assert "Environment=MKL_NUM_THREADS=1" in unit
    assert "TimeoutStopSec=1800" in unit


def test_server_publishes_atomic_v2_runtime_contract(tmp_path):
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    uv_stub.chmod(0o755)
    archive = tmp_path / "archive"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"), "--once",
         "--workers", "1", str(tmp_path)],
        env=os.environ | {
            "LEO_TRACKER_REPO": str(ROOT),
            "UV_BIN": str(uv_stub),
            "LEO_ANALYSIS_ARCHIVE_MODE": "required",
            "LEO_ANALYSIS_ARCHIVE_ROOT": str(archive),
        },
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    state = json.loads(
        (tmp_path / "reports/runtime/analysis-server.json").read_text())
    assert state["schema"] == "leo-tracker.analysis-server-runtime/v1"
    assert state["state"] == "stopped"
    assert state["archive_mode"] == "required"
    assert state["evidence_policy"] == "tiered-v2"
    assert state["archive_command"] == "starlink-evidence-archive-v2"
    assert state["producer_contract_valid"] is False


def test_wide_acquisition_span_fits_the_recorded_wide_capture_bandwidth():
    """The deployed span must be resolvable by the captures the watcher actually writes.

    A 12 MHz span against 10 MS/s wide recordings failed every wide job in the
    `acquire` stage before the library clamped out-of-band tuning requests.
    """
    from leo_tracker.radio.beacon.acquisition import usable_acquisition_span_hz

    watcher = (ROOT / "scripts/starlink-beacon-watch.sh").read_text()
    assert "--sample-rate-hz 10000000" in watcher
    unit = (ROOT / "deploy/leo-tracker-analysis-server.service").read_text()
    server = (ROOT / "scripts/starlink-analysis-server.sh").read_text()
    usable = usable_acquisition_span_hz(10_000_000, 2_500_000)
    deployed = int([line.split("=")[-1] for line in unit.splitlines()
                    if "LEO_ANALYSIS_WIDE_ACQUISITION_SPAN_HZ" in line][0])
    fallback = int([line.split(":-")[-1].split("}")[0] for line in server.splitlines()
                    if line.startswith("wide_acquisition_span_hz=")][0])
    assert 0 < deployed <= usable
    assert 0 < fallback <= usable


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
    assert 'track_maximum_gap_s="${LEO_ANALYSIS_TRACK_MAXIMUM_GAP_S:-15}"' in source
    assert ('frame_maximum_extension_s=' +
            '"${LEO_ANALYSIS_FRAME_MAXIMUM_EXTENSION_S:-60}"') in source
    assert ('fragment_maximum_gap_s=' +
            '"${LEO_ANALYSIS_FRAGMENT_MAXIMUM_GAP_S:-30}"') in source
    assert ('track_maximum_reacquisition_span_hz=' +
            '"${LEO_ANALYSIS_TRACK_MAXIMUM_REACQUISITION_SPAN_HZ:-15000}"') in source
    assert '--maximum-gap-s "${track_maximum_gap_s}"' in source
    assert '--maximum-reacquisition-span-hz "${track_maximum_reacquisition_span_hz}"' in source
    assert '--maximum-extension-s "${frame_maximum_extension_s}"' in source
    assert 'starlink-beacon-channel-link "${linked}" "${track}"' in source
    assert '--maximum-gap-s "${fragment_maximum_gap_s}"' in source
    assert 'stage=fragment_tle_association' in source
    assert 'fragment_component_association orbit associate' in source
    assert 'fragment_diagnostic orbit' in source
    assert 'diagnose-fragments --tracks "${track}" --links "${linked}"' in source
    assert '--minimum-duration-s .5 --minimum-dual-epochs 5' in source
    assert '--observations "${linked}"' in source
    assert source.index('starlink-beacon-channel-link "${linked}" "${track}"') < \
        source.index('--observations "${linked}"')
    assert "starlink-evidence-archive-v2" in source
    assert "inspect-followup" in source
    assert "retention_skipped" in source
    assert "analysis-server-status.json" in source
    assert source.index("starlink-evidence-archive-v2") < source.index(
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
        capture_output=True, timeout=30)
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


def test_server_skips_incompatible_optional_learned_template(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    capture = tmp_path / "captures/upper-edge"
    context = tmp_path / "context/upper-edge-job"
    queue.mkdir(parents=True); capture.mkdir(parents=True); context.mkdir(parents=True)
    (capture / "manifest.json").write_text(json.dumps({
        "sample_rate_hz": 2_500_000,
        "metadata": {"region": "upper-edge"},
    }))
    (context / "learned-beacon.json").write_text(json.dumps({
        "sample_rate_hz": 2_500_000,
        "region": "lower-edge",
        "summary": {"qualified": True},
    }))
    (queue / "0001.job").write_text(
        f"upper-edge\t{capture}\tnarrow\t{context}\n")
    calls = tmp_path / "calls.log"
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$UV_STUB_LOG\"\n"
        "exit 0\n")
    uv_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"), "--once",
         "--workers", "1", str(tmp_path)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT), "UV_BIN": str(uv_stub),
                          "UV_STUB_LOG": str(calls)},
        text=True, capture_output=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert ("template_skipped worker=0 job=upper-edge "
            "reason=incompatible_rate_region_or_qualification") in result.stdout
    analyze_call = next(line for line in calls.read_text().splitlines()
                        if "starlink-beacon-analyze" in line)
    assert "--beacon-template" not in analyze_call
    assert list((queue / "done").glob("*.job"))


def test_server_uses_compatible_qualified_learned_template(tmp_path):
    queue = tmp_path / "staging/analysis-queue"
    capture = tmp_path / "captures/lower-edge"
    context = tmp_path / "context/lower-edge-job"
    queue.mkdir(parents=True); capture.mkdir(parents=True); context.mkdir(parents=True)
    (capture / "manifest.json").write_text(json.dumps({
        "sample_rate_hz": 2_500_000,
        "metadata": {"region": "lower-edge"},
    }))
    template = context / "learned-beacon.json"
    template.write_text(json.dumps({
        "sample_rate_hz": 2_500_000,
        "region": "lower-edge",
        "summary": {"qualified": True},
    }))
    (queue / "0001.job").write_text(
        f"lower-edge\t{capture}\tnarrow\t{context}\n")
    calls = tmp_path / "calls.log"
    uv_stub = tmp_path / "uv-stub"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$UV_STUB_LOG\"\n"
        "exit 0\n")
    uv_stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"), "--once",
         "--workers", "1", str(tmp_path)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT), "UV_BIN": str(uv_stub),
                          "UV_STUB_LOG": str(calls)},
        text=True, capture_output=True, timeout=30)

    assert result.returncode == 0, result.stderr
    analyze_call = next(line for line in calls.read_text().splitlines()
                        if "starlink-beacon-analyze" in line)
    assert f"--beacon-template {template}" in analyze_call
    assert "template_skipped" not in result.stdout
    assert list((queue / "done").glob("*.job"))


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


def test_offload_only_watcher_delivers_every_capture_without_local_dsp(tmp_path):
    source = tmp_path / "source"
    shared = tmp_path / "shared"
    environment = os.environ | {
        "LEO_TRACKER_REPO": str(ROOT), "LEO_BEACON_STORAGE": str(source),
        "LEO_BEACON_ANALYSIS_MODE": "offload", "LEO_BEACON_DWELL_S": ".04",
        "LEO_BEACON_OVERSAMPLE_ON_STARTUP": "0",
        "LEO_BEACON_OVERSAMPLE_EVERY_CYCLES": "0",
        "LEO_BEACON_WIDE_EVERY_CYCLES": "0", "LEO_BEACON_HOP_EVERY_CYCLES": "0",
        "LEO_BEACON_TARGETS": "4:lower-edge", "LEO_BEACON_MAX_CYCLES": "1",
        "LEO_BEACON_FAKE": "1", "LEO_BEACON_MAX_PI_TEMP_MILLIC": "999999",
        "LEO_BEACON_RADIO_ID": "pluto-test",
        "LEO_BEACON_RADIO_SERIAL": "TEST-SERIAL",
        "LEO_BEACON_RECEIVER_LABELS": "lnb-0 lnb-1",
        "UV_CACHE_DIR": str(ROOT / ".uv-cache"), "UV_BIN": shutil.which("uv") or "uv"}

    watched = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-beacon-watch.sh")], cwd=ROOT,
        env=environment, text=True, capture_output=True, timeout=30)

    assert watched.returncode == 0, watched.stderr
    assert '"analysis_mode":"offload"' in watched.stdout
    assert not list((source / "reports").glob("*.json"))
    local_jobs = list((source / "staging/analysis-queue").glob("*.job"))
    assert len(local_jobs) == 1
    name, capture_value, mode = local_jobs[0].read_text().rstrip().split("\t")
    assert mode == "narrow"
    manifest = json.loads((Path(capture_value) / "manifest.json").read_text())
    assert manifest["state"] == "complete"
    assert manifest["identity"]["radio_id"] == "pluto-test"
    assert manifest["identity"]["receiver_labels"] == ["lnb-0", "lnb-1"]
    assert "-pluto-test-" in name

    exported = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-export.sh"), "--once",
         str(source), str(shared)],
        env=environment | {"LEO_OFFLOAD_SOURCE_POLICY": "retain",
                           "LEO_OFFLOAD_BWLIMIT_KBPS": "0"},
        text=True, capture_output=True, timeout=30)

    assert exported.returncode == 0, exported.stderr
    assert not list((source / "staging/analysis-queue").glob("*.job"))
    assert (source / "captures" / name).is_dir()
    assert (shared / "captures" / name / "manifest.json").is_file()
    remote = list((shared / "staging/analysis-queue").glob("*.job"))
    assert len(remote) == 1
    assert remote[0].read_text().split("\t")[:3] == [name, f"captures/{name}", "narrow"]


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


def test_exporter_reconciles_complete_capture_missing_its_queue_marker(tmp_path):
    source = tmp_path / "source"
    shared = tmp_path / "shared"
    capture = source / "captures" / "capture-stranded"
    capture.mkdir(parents=True)
    payload = b"abcdefgh"
    (capture / "chunk-000000.ci16").write_bytes(payload)
    (capture / "manifest.json").write_text(
        '{"state":"complete","metadata":{"observation_mode":"narrow"},'
        '"chunks":[{"path":"chunk-000000.ci16","bytes":8,'
        '"sha256":"9c56cc51b374c3ba189210d5b6d4bf57790d351c'
        '96c47c02190ecf1e430635ab"}]}'
    )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-export.sh"), "--once",
         str(source), str(shared)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT),
                          "LEO_OFFLOAD_SOURCE_POLICY": "retain",
                          "LEO_OFFLOAD_BWLIMIT_KBPS": "0"},
        text=True, capture_output=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert '"queued_count": 1' in result.stdout
    assert capture.is_dir()
    assert (shared / "captures/capture-stranded/chunk-000000.ci16").read_bytes() == payload
    remote = list((shared / "staging/analysis-queue").glob("*.job"))
    assert len(remote) == 1
    assert remote[0].read_text().split("\t")[:3] == [
        "capture-stranded", "captures/capture-stranded", "narrow"]


def test_exporter_carries_a_quarantined_prefix(tmp_path):
    """Quarantined captures stop early but hold verified chunks worth analysing.

    verify_copy independently required state == complete, so widening the path
    allow-list alone would still have failed every quarantine export.
    """
    source = tmp_path / "source"
    shared = tmp_path / "shared"
    capture = source / "quarantine" / "ch4-lower-edge-narrow-20260805T154051Z"
    queue = source / "staging" / "analysis-queue"
    capture.mkdir(parents=True)
    queue.mkdir(parents=True)
    payload = b"abcdefgh"
    (capture / "chunk-000000.ci16").write_bytes(payload)
    (capture / "manifest.json").write_text(
        '{"state":"interrupted","captured_samples_per_receiver":99,'
        '"chunks":[{"path":"chunk-000000.ci16",'
        '"bytes":8,"sha256":"9c56cc51b374c3ba189210d5b6d4bf57790d351c'
        '96c47c02190ecf1e430635ab"}]}'
    )
    (queue / "0001.job").write_text(f"{capture.name}\t{capture}\tnarrow\n")
    env = os.environ | {"LEO_TRACKER_REPO": str(ROOT),
                        "LEO_OFFLOAD_BWLIMIT_KBPS": "0"}

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-export.sh"), "--once",
         str(source), str(shared)], env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    assert capture.exists(), "retain policy must leave the quarantined source"
    assert (shared / "captures" / capture.name /
            "chunk-000000.ci16").read_bytes() == payload
    job = next((shared / "staging/analysis-queue").glob("*.job"))
    assert job.read_text().rstrip().split("\t")[:3] == [
        capture.name, f"captures/{capture.name}", "narrow"]


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
