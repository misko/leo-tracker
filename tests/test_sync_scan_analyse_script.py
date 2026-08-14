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
import re
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "starlink-sync-scan-analyse.sh"
SCORE_SCRIPT = REPO / "scripts" / "starlink-survey-score-parallel.sh"

#: The live drain's own destination, and the script that decides it.  These are
#: the only authority on where the sweeps are: the analysis host reads whatever
#: ``syncdrain.sh`` wrote, so a default that disagrees with ``DST`` is wrong no
#: matter how tidy it looks next to the shared root.
DRAIN_SCRIPT = Path("/mnt/leo-nvme/leo-tracker/bin/syncdrain.sh")
SHARE = Path("/mnt/qnap01/mouse9911")


def _run(args, root=None, sweep_root=None, extra_env=None, timeout=120):
    """Run the script.  ``sweep_root`` is separate from ``root`` on purpose.

    The sweeps are not under the shared root on the real share -- the drain
    puts them in a sibling dataset -- so a test that wants the script to read a
    fixture has to say where that fixture is instead of assuming the script
    will look inside the root it was handed.
    """
    env = dict(os.environ)
    env["LEO_TRACKER_REPO"] = str(REPO)
    env["PYTHONPATH"] = str(REPO / "src")
    env.update(extra_env or {})
    argv = [str(SCRIPT), *args]
    if sweep_root is not None:
        argv += ["--sweep-root", str(sweep_root)]
    if root:
        argv.append(str(root))
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          timeout=timeout)


def _planned(result, field):
    """One labelled line out of the plan, e.g. ``sweeps:`` or ``corpus:``."""
    for line in result.stdout.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no {field!r} line in plan:\n{result.stdout}")


def _first_finished_sweep(root):
    """The first sweep in ``root`` that has its commit marker, or ``None``.

    Stops at the first hit rather than listing the directory: the real share
    holds thousands of sweeps on NFS, and the question here is only whether
    there is any work at all under the default.
    """
    return next((path for path in Path(root).glob("sync-*")
                 if (path / "sweep.json").is_file()), None)


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
    result = _run([], root=tmp_path / "absent",
                  sweep_root=tmp_path / "absent" / "sync-scans")
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
    result = _run([], root=tmp_path / "shared", sweep_root=sweeps)
    assert result.returncode == 2
    assert "sync-<UTC>" in result.stderr
    assert "loose" in result.stderr


def test_a_scoring_worker_is_three_threads_so_it_is_not_one_per_core(tmp_path):
    """Twenty cores is six or seven scoring workers, and the plan must say so."""
    root = tmp_path / "shared"
    sweeps = _sweeps(root)
    result = _run(["--plan"], root=root, sweep_root=sweeps,
                  extra_env={"LEO_SYNC_ANALYSE_CORES": "24"})
    assert result.returncode == 0, result.stderr
    assert "score workers:  8" in result.stdout
    assert "3 OpenMP threads" in result.stdout


def test_import_is_wider_than_scoring_because_it_is_io_bound(tmp_path):
    root = tmp_path / "shared"
    sweeps = _sweeps(root)
    result = _run(["--plan"], root=root, sweep_root=sweeps,
                  extra_env={"LEO_SYNC_ANALYSE_CORES": "24"})
    assert "import workers: 24" in result.stdout


def test_a_nonsense_worker_count_is_refused_rather_than_clamped(tmp_path):
    root = tmp_path / "shared"
    sweeps = _sweeps(root)
    for flag in ("--score-workers", "--import-workers"):
        for bad in ("0", "-4", "many"):
            result = _run([flag, bad, "--plan"], root=root, sweep_root=sweeps)
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
    sweeps = _sweeps(root)
    result = _run(["--plan"], root=root, sweep_root=sweeps)
    assert "surveys/sync-corpus" in result.stdout
    assert "surveys/corpus\n" not in result.stdout


def test_the_import_half_actually_imports_and_then_resumes(tmp_path):
    """Two passes, four entries, and the second pass writes nothing new."""
    root = tmp_path / "shared"
    sweeps = _sweeps(root, count=2)
    first = _run(["--import-only"], root=root, sweep_root=sweeps, timeout=300)
    assert first.returncode == 0, first.stderr
    corpus = root / "surveys" / "sync-corpus"
    entries = sorted(path.name for path in corpus.iterdir())
    assert len(entries) == 4, entries
    stamps = {path: os.stat(path).st_mtime_ns
              for path in corpus.glob("*/manifest.json")}
    second = _run(["--import-only"], root=root, sweep_root=sweeps, timeout=300)
    assert second.returncode == 0, second.stderr
    assert sorted(path.name for path in corpus.iterdir()) == entries
    assert {path: os.stat(path).st_mtime_ns
            for path in corpus.glob("*/manifest.json")} == stamps


@pytest.mark.skipif(not DRAIN_SCRIPT.is_file(),
                    reason="not the analysis host: no syncdrain.sh to read")
def test_the_default_sweep_root_is_the_drains_own_destination():
    """The drain decides where the sweeps are; this script only reads them.

    ``syncdrain.sh`` is the process that puts sweeps on the share, so its
    ``DST`` is the address.  A default derived from the shared root instead is
    a second, independent opinion about the same path -- and when the two
    disagree the script reports no work while thousands of sweeps sit unread.
    """
    destination = re.search(r'^DST=(\S+)', DRAIN_SCRIPT.read_text(), re.M)
    assert destination, f"no DST= in {DRAIN_SCRIPT}"
    assert _planned(_run(["--plan"]), "sweeps") == destination.group(1)


@pytest.mark.skipif(not SHARE.is_dir(), reason="the QNAP share is not mounted")
def test_the_documented_defaults_find_the_sweeps_that_are_really_there():
    """Run with nothing but ``--plan`` and the default must name real work.

    Deliberately not a fixture: a fixture that builds sweeps wherever the
    script happens to point proves only that the script agrees with itself.
    This asks the live share whether the advertised default has anything in it.
    """
    result = _run(["--plan"])
    assert result.returncode == 0, result.stderr
    root = _planned(result, "sweeps")
    assert _first_finished_sweep(root) is not None, (
        f"the default sweep root {root} holds no finished sweeps")


@pytest.mark.skipif(not SHARE.is_dir(), reason="the QNAP share is not mounted")
def test_the_rsync_in_the_docs_does_not_send_180_gb_to_the_wrong_path():
    """The header and usage both print a transfer an operator will paste.

    It is 180 GB and hours long, so a destination that is not where the drain
    writes is not a typo in a comment -- it is a second copy of the corpus in a
    directory nothing reads.
    """
    destination = _planned(_run(["--plan"]), "sweeps").rstrip("/")
    help_text = _run(["--help"]).stdout

    # The path that held nothing must not survive anywhere in the documentation.
    for name, text in (("the script", SCRIPT.read_text()), ("--help", help_text)):
        assert "/mnt/qnap01/mouse9911/leo/sync-scans" not in text, name

    # And the transfer that is still shown has to end up where the sweeps live.
    # An rsync plus every line it is continued onto, since the destination of a
    # wrapped command sits on the next line and is the whole point here.
    blocks = re.findall(r'rsync(?:[^\n]*\\\n)*[^\n]*', help_text)
    assert blocks, "the usage no longer shows the transfer at all"
    targets = [token for block in blocks
               for token in re.findall(r'/mnt/qnap01/\S+', block)]
    assert targets, f"no destination on the share in: {blocks}"
    for token in targets:
        assert token.rstrip("/") == destination, (
            f"documented rsync target {token} is not {destination}")


def test_a_bounded_pass_picks_up_where_the_last_one_stopped(tmp_path):
    """``--limit N`` and re-run has to reach the end of the corpus.

    The documented way to bound a pass is ``--limit N``, re-run.  If the deal
    counts sweeps that are already imported, every pass hands the workers the
    same first N sweeps, they all come back "already imported", and the corpus
    stops growing after pass one no matter how many times it is run.
    """
    root = tmp_path / "shared"
    sweeps = _sweeps(root, count=6)
    corpus = root / "surveys" / "sync-corpus"
    env = {"LEO_SYNC_ANALYSE_CORES": "2"}

    first = _run(["--import-only", "--limit", "2", "--sweep-root", str(sweeps)],
                 root=root, extra_env=env, timeout=600)
    assert first.returncode == 0, first.stderr
    after_first = sorted(path.name for path in corpus.iterdir())
    assert len(after_first) == 4, first.stdout

    second = _run(["--import-only", "--limit", "2", "--sweep-root", str(sweeps)],
                  root=root, extra_env=env, timeout=600)
    assert second.returncode == 0, second.stderr
    after_second = sorted(path.name for path in corpus.iterdir())
    assert set(after_first) < set(after_second), (
        "the second bounded pass imported nothing new:\n" + second.stdout)
    assert len(after_second) == 8, second.stdout


def test_repeated_bounded_passes_reach_the_end_of_the_sweeps(tmp_path):
    """Keep running it and the corpus finishes, then says so.

    The skip is what makes ``--limit`` terminate, and it is also what makes the
    last pass find nothing.  "nothing to deal" now has two causes -- a finished
    corpus and an empty share -- and the whole point of the guard above them is
    that those two must not print the same line.
    """
    root = tmp_path / "shared"
    sweeps = _sweeps(root, count=3)
    corpus = root / "surveys" / "sync-corpus"
    env = {"LEO_SYNC_ANALYSE_CORES": "2"}
    passes = []
    for _ in range(4):
        result = _run(["--import-only", "--limit", "1", "--sweep-root",
                       str(sweeps)], root=root, extra_env=env, timeout=600)
        assert result.returncode == 0, result.stderr
        passes.append(result.stdout)

    assert len(list(corpus.iterdir())) == 6, passes
    assert "already in" in passes[-1], passes[-1]
    assert "no finished sweeps" not in passes[-1], passes[-1]


def test_rebuild_still_reimports_what_the_skip_would_pass_over(tmp_path):
    """The skip must not swallow the one flag whose job is to defeat it."""
    root = tmp_path / "shared"
    sweeps = _sweeps(root, count=2)
    env = {"LEO_SYNC_ANALYSE_CORES": "2"}
    first = _run(["--import-only", "--sweep-root", str(sweeps)], root=root,
                 extra_env=env, timeout=600)
    assert first.returncode == 0, first.stderr

    forced = _run(["--import-only", "--rebuild", "--sweep-root", str(sweeps)],
                  root=root, extra_env=env, timeout=600)
    assert forced.returncode == 0, forced.stderr
    assert "dealing 2 sweeps" in forced.stdout, forced.stdout
    assert "imported 4" in forced.stdout, forced.stdout
