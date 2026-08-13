"""The fan-out the analysis host runs to score the survey corpus on many cores.

One ``starlink-survey-score run`` is three threads, because the kernel calls
``omp_set_num_threads(fast_scan.DEFAULT_THREADS)`` and that overrides
``OMP_NUM_THREADS``.  A twenty-four core host left to itself therefore scores at
an eighth of its capacity, and the corpus falls behind the captures feeding it.

The script deals the corpus into shards of symlinks and points one worker at
each.  The properties worth pinning are the ones that would let it damage
something rather than merely run slowly: it must not treat a probe directory as
scratch, it must not leave workers behind, and it must not size itself as though
a worker were one core.
"""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "starlink-survey-score-parallel.sh"


def _run(args, root=None, extra_env=None, timeout=120):
    env = dict(os.environ)
    env["LEO_TRACKER_REPO"] = str(SCRIPT.resolve().parents[1])
    env.update(extra_env or {})
    return subprocess.run([str(SCRIPT), *args, *([str(root)] if root else [])],
                          capture_output=True, text=True, env=env, timeout=timeout)


def _corpus(root, count, scored=0, schema="survey-detector-comparison/v2"):
    """A corpus of ``count`` entries, the first ``scored`` already carrying one."""
    corpus = root / "surveys" / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        entry = corpus / f"ch1-lower-edge-narrow-pluto-19f2-2026081{index % 10}T12000{index % 10}Z"
        entry.mkdir(exist_ok=True)
        (entry / "survey.ci16").write_bytes(b"\0" * 16)
        (entry / "manifest.json").write_text("{}")
        if index < scored:
            (entry / "scores.json").write_text('{"schema": "leo-tracker.%s"}' % schema)
    return corpus


def test_the_script_is_executable():
    """A unit or an operator calling it directly fails opaquely otherwise."""
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_it_parses_as_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_a_missing_corpus_is_named_rather_than_reported_as_no_work(tmp_path):
    """An unmounted share and a fully scored corpus must not look alike.

    Both would otherwise print nothing and exit zero, and the operator would
    conclude the backlog had cleared.
    """
    result = _run([], root=tmp_path / "absent")

    assert result.returncode == 2
    assert "no corpus" in result.stderr


def test_a_worker_is_three_threads_so_the_default_is_not_one_per_core(tmp_path):
    """The defect this script exists to avoid, applied to itself.

    fast_scan runs three OpenMP threads per process, so one worker per core
    oversubscribes threefold -- which is what held a three-worker run on a
    four-core host to 78% efficiency.  The default must divide.
    """
    root = tmp_path / "shared"
    _corpus(root, 4)

    result = _run(["--limit", "1"], root=root,
                  extra_env={"UV_BIN": "/bin/false"})

    cores = os.cpu_count() or 4
    assert f"into {max(1, cores // 3)} shards" in result.stdout, result.stdout


def test_shards_are_symlinks_so_cleanup_can_never_delete_a_probe(tmp_path):
    """The script removes its shard directory on every exit path.

    If a shard held probes rather than links to them, that cleanup would delete
    the corpus.  Nothing in the script's output is worth that risk, so the
    property is pinned rather than left to review.
    """
    body = SCRIPT.read_text()

    assert "ln -s" in body
    assert "rm -rf \"${shard_root}\"" in body
    # and the shard root is scratch, never the corpus
    assert "mktemp -d" in body
    assert "rm -rf \"${corpus_root}" not in body


def test_the_trap_kills_workers_rather_than_orphaning_them(tmp_path):
    """Ctrl-C must not leave scoring processes on a host that cannot name them.

    An orphaned worker competes with the analysis host's own per-job stage with
    no PID list to find it by, which is how a fan-out becomes a haunting.
    """
    body = SCRIPT.read_text()

    assert "trap cleanup EXIT INT TERM" in body
    assert 'kill -TERM "${pids[@]}"' in body


def test_the_kernel_is_warmed_before_any_worker_starts(tmp_path):
    """N workers against a cold cache means N compilers writing one path.

    The object is keyed on the build, so a host that has not run this code has
    none; the compile is not atomic, and the live capture path loads the same
    object.  Warming must therefore precede the fan-out, not accompany it.
    """
    body = SCRIPT.read_text()
    warm = body.index("_load_kernel()")
    # the fan-out itself, not the prose about it: the workers are the only place
    # the command is backgrounded.
    fanout = body.index("leo-radio starlink-survey-score run")

    assert warm < fanout, "kernel warm must precede the first worker"


def test_an_already_scored_corpus_deals_nothing(tmp_path):
    """Re-scoring costs 86 s an entry, so a finished corpus must be a no-op."""
    root = tmp_path / "shared"
    _corpus(root, 3, scored=3)

    result = _run([], root=root, extra_env={"UV_BIN": "/bin/false"})

    assert "nothing to score" in result.stdout, result.stdout
    assert result.returncode == 0


def test_a_nonsense_worker_count_is_refused_rather_than_clamped(tmp_path):
    """Silently correcting an operator's typo hides the typo."""
    root = tmp_path / "shared"
    _corpus(root, 2)

    for bad in ("0", "-4", "many"):
        result = _run(["--workers", bad], root=root)
        assert result.returncode == 2, bad
        assert "positive integer" in result.stderr, bad
