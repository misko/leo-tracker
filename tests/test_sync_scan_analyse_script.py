"""The one command the operator runs on the analysis host over the sweeps.

The sweeps are on the capture host's NVMe and the analysis host reads the QNAP
share, so the first thing this has to be honest about is that the data is not
where the script runs.  The second is the worker count: a scoring process is
three OpenMP threads because the kernel calls
``omp_set_num_threads(fast_scan.DEFAULT_THREADS)``, which overrides
``OMP_NUM_THREADS``, so twenty cores is six or seven scoring workers and not
twenty.  Import is I/O and can be wider.

The scoring half is not re-implemented here: ``starlink-survey-score-parallel.sh``
already deals a corpus into shards of symlinks and already sizes itself at
cores/3, so this hands it the sync corpus and stays out of its way.  What is
pinned below is that it really does hand off, that it names the missing pieces
instead of reporting no work, and that it cannot be talked into oversubscribing
the host it runs on.
"""
import os
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "starlink-sync-scan-analyse.sh"
SCORE_SCRIPT = REPO / "scripts" / "starlink-survey-score-parallel.sh"


def _run(args, root=None, extra_env=None, timeout=120):
    env = dict(os.environ)
    env["LEO_TRACKER_REPO"] = str(REPO)
    env["PYTHONPATH"] = str(REPO / "src")
    env.update(extra_env or {})
    return subprocess.run([str(SCRIPT), *args, *([str(root)] if root else [])],
                          capture_output=True, text=True, env=env,
                          timeout=timeout)


def _sweeps(root, count=2):
    import json

    import numpy as np

    sweeps = root / "sync-scans"
    sweeps.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        sweep = sweeps / f"sync-2026081{index}T000315Z"
        sweep.mkdir(exist_ok=True)
        shape = [2, 15_000, 2, 2]
        radios = {}
        for radio_id, labels in (("pluto-19f2", ["lnb-c", "lnb-d"]),
                                 ("pluto-5d4d", ["lnb-a", "lnb-b"])):
            (sweep / f"{radio_id}.ci16").write_bytes(
                np.zeros(tuple(shape), "<i2").tobytes())
            radios[radio_id] = {
                "arm": {"name": "6ms-2.50MSps", "probe_s": 0.006,
                        "sample_rate_hz": 2_500_000.0,
                        "pilot_band_fits": True},
                "edge_order": "U", "order_draw_u32": 1,
                "receiver_labels": labels,
                "tunings": [[1, "upper"], [1, "lower"]],
                "iq": {"path": f"{radio_id}.ci16",
                       "bytes": 2 * 15_000 * 2 * 2 * 2, "shape": shape},
                "error": None}
        (sweep / "sweep.json").write_text(json.dumps({
            "schema": "leo-tracker.interim-synchronised-scan/v1",
            "sweep": index, "utc": f"2026081{index}T000315Z", "wall_s": 1.0,
            "matched_arm": True, "arm_draw_u32": 1, "pairing_draw_u32": 2,
            "radios": radios,
            "skew_ms": {"per_tuning": [0.1, 0.2], "median": 0.15,
                        "max": 0.2}}))
    return sweeps


def test_the_script_is_executable():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_it_parses_as_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_the_usage_says_how_the_sweeps_reach_the_share():
    """The sweeps are on the capture host and this runs on the analysis host.

    An operator who runs it before rsyncing gets an empty corpus and no reason,
    so the transfer is in the usage text rather than in somebody's memory.
    """
    text = _run(["--help"]).stdout
    assert "rsync" in text
    assert "--sweep-root" in text


def test_it_can_be_pointed_at_any_root_rather_than_only_the_share():
    text = _run(["--help"]).stdout
    assert "--corpus-root" in text
    assert "--sweep-root" in text


def test_a_missing_sweep_root_is_named_rather_than_reported_as_no_work(tmp_path):
    """An un-rsynced share and a finished import must not look alike."""
    result = _run([], root=tmp_path / "absent")
    assert result.returncode == 2
    assert "absent" in result.stderr


def test_a_flattened_sweep_root_is_named_rather_than_called_empty(tmp_path):
    """The sweeps have to arrive as sync-<UTC>/ directories, and may not.

    Observed on the share while this was written: a drain process was copying
    every sweep's three files into one flat directory, so the path existed, held
    102 MB of IQ, and contained not one sweep.  "no finished sweeps" would send
    the operator to look for a transfer that had already run.
    """
    sweeps = tmp_path / "shared" / "sync-scans"
    sweeps.mkdir(parents=True)
    (sweeps / "pluto-19f2.ci16").write_bytes(b"\0" * 64)
    (sweeps / "sweep.json").write_text("{}")
    result = _run([], root=tmp_path / "shared")
    assert result.returncode == 2
    assert "sync-<UTC>" in result.stderr
    assert "loose" in result.stderr


def test_a_scoring_worker_is_three_threads_so_it_is_not_one_per_core(tmp_path):
    """Twenty cores is six or seven scoring workers, and the plan must say so."""
    root = tmp_path / "shared"
    _sweeps(root)
    result = _run(["--plan"], root=root,
                  extra_env={"LEO_SYNC_ANALYSE_CORES": "24"})
    assert result.returncode == 0, result.stderr
    assert "score workers:  8" in result.stdout
    assert "3 OpenMP threads" in result.stdout


def test_import_is_wider_than_scoring_because_it_is_io_bound(tmp_path):
    root = tmp_path / "shared"
    _sweeps(root)
    result = _run(["--plan"], root=root,
                  extra_env={"LEO_SYNC_ANALYSE_CORES": "24"})
    assert "import workers: 24" in result.stdout


def test_a_nonsense_worker_count_is_refused_rather_than_clamped(tmp_path):
    root = tmp_path / "shared"
    _sweeps(root)
    for flag in ("--score-workers", "--import-workers"):
        for bad in ("0", "-4", "many"):
            result = _run([flag, bad, "--plan"], root=root)
            assert result.returncode == 2, (flag, bad)
            assert "positive integer" in result.stderr, (flag, bad)


def test_it_hands_the_scoring_half_to_the_existing_sharder():
    """One sharding scheme, not two.

    ``starlink-survey-score-parallel.sh`` already deals a corpus into shards of
    symlinks, already warms the kernel once before fanning out and already
    kills its workers on the way out.  A second implementation here would be a
    second place for those to be wrong.
    """
    text = SCRIPT.read_text()
    assert SCORE_SCRIPT.name in text
    assert "LEO_SURVEY_CORPUS_ROOT" in text


def test_it_keeps_the_sync_corpus_out_of_the_live_survey_corpus(tmp_path):
    """The share's own corpus is being scored by the analysis service now.

    Twelve arms of a different experiment landing in it would swamp that queue
    and pool with the four-arm pre-dwell draw in every aggregate that reads the
    corpus as one population.
    """
    root = tmp_path / "shared"
    _sweeps(root)
    result = _run(["--plan"], root=root)
    assert "surveys/sync-corpus" in result.stdout
    assert "surveys/corpus\n" not in result.stdout


def test_the_import_half_actually_imports_and_then_resumes(tmp_path):
    """Two passes, four entries, and the second pass writes nothing new."""
    root = tmp_path / "shared"
    _sweeps(root, count=2)
    first = _run(["--import-only"], root=root, timeout=300)
    assert first.returncode == 0, first.stderr
    corpus = root / "surveys" / "sync-corpus"
    entries = sorted(path.name for path in corpus.iterdir())
    assert len(entries) == 4, entries
    stamps = {path: os.stat(path).st_mtime_ns
              for path in corpus.glob("*/manifest.json")}
    second = _run(["--import-only"], root=root, timeout=300)
    assert second.returncode == 0, second.stderr
    assert sorted(path.name for path in corpus.iterdir()) == entries
    assert {path: os.stat(path).st_mtime_ns
            for path in corpus.glob("*/manifest.json")} == stamps
