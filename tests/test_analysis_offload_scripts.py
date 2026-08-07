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


def test_server_worker_exits_cleanly_when_once_queue_is_empty(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/starlink-analysis-server.sh"), "--once",
         "--workers", "2", str(tmp_path)],
        env=os.environ | {"LEO_TRACKER_REPO": str(ROOT)}, text=True,
        capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not list((tmp_path / "staging/analysis-queue").glob("*.running.*"))


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
    assert job.read_text().split("\t") == [
        "capture-one", str(shared / "captures/capture-one"), "narrow\n"]


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
