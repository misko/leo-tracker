"""The conversion script the analysis host runs to keep the probe index fresh.

The script exists because the projection has to run somewhere other than the
capture host, and the host that runs it must be told plainly when it lacks the
optional dependency rather than failing somewhere inside DuckDB.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "starlink-probe-index-sync.sh"


def _run(args, root=None, extra_env=None, repo=None):
    env = dict(os.environ)
    env["LEO_TRACKER_REPO"] = str(repo or SCRIPT.resolve().parents[1])
    env.update(extra_env or {})
    return subprocess.run([str(SCRIPT), *args, *([str(root)] if root else [])],
                          capture_output=True, text=True, env=env, timeout=300)


def _reports(root, count=1):
    (root / "reports").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / "reports" /
         f"ch4-lower-edge-narrow-2026081{index}T120000Z.json").write_text("{}")


def test_the_script_is_executable():
    """A unit calling it directly fails opaquely otherwise."""
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_it_parses_as_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_unmounted_storage_is_named_rather_than_guessed_at(tmp_path):
    """The likeliest failure on the analysis host is a mount that is not there.

    Reported as itself, it takes a moment to fix; reported as an empty index it
    looks like a night with no detections.
    """
    result = _run(["--check"], root=tmp_path / "absent")

    assert result.returncode == 2
    assert "mounted" in result.stderr


def test_a_missing_dependency_names_the_command_that_installs_it(tmp_path):
    """duckdb is deliberately optional so a capture host cannot fail to start.

    The cost is that the analysis host must ask for it, so the message has to
    say how rather than only that.
    """
    _reports(tmp_path)
    fake_repo = tmp_path / "repo"
    (fake_repo / ".venv" / "bin").mkdir(parents=True)
    python = fake_repo / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 1\n")          # import duckdb fails
    python.chmod(0o755)

    result = _run(["--check"], root=tmp_path, repo=fake_repo)

    assert result.returncode == 3
    assert "duckdb" in result.stderr
    assert "analysis" in result.stderr and "install" in result.stderr


def test_check_reports_what_it_found_without_building(tmp_path):
    pytest.importorskip("duckdb")
    _reports(tmp_path, count=3)

    result = _run(["--check"], root=tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout)["reports"] == 3
    assert not (tmp_path / "reports" / "probe-index").exists()


def test_an_unknown_flag_is_refused_rather_than_treated_as_a_path(tmp_path):
    """Otherwise a typo silently indexes the wrong root."""
    result = _run(["--rebiuld"], root=tmp_path)

    assert result.returncode == 2
    assert "usage" in result.stderr


def test_it_converts_reports_into_partitions(tmp_path):
    pytest.importorskip("duckdb")
    from test_radio_probe_index import _report

    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z", probes=4)

    result = _run([], root=tmp_path)

    assert result.returncode == 0, result.stderr
    built = json.loads(result.stdout)
    assert [item["date"] for item in built["built"]] == ["2026-08-11"]
    assert (tmp_path / "reports" / "probe-index" / "date=2026-08-11" /
            "probes.parquet").is_file()


def test_a_second_run_rebuilds_nothing(tmp_path):
    """It is meant for a timer, so the steady-state cost has to be near zero."""
    pytest.importorskip("duckdb")
    from test_radio_probe_index import _report

    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")
    _run([], root=tmp_path)

    again = json.loads(_run([], root=tmp_path).stdout)

    assert again["built"] == []
    assert again["skipped"] == ["2026-08-11"]


def test_status_answers_without_building(tmp_path):
    pytest.importorskip("duckdb")
    from test_radio_probe_index import _report

    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")

    status = json.loads(_run(["--status"], root=tmp_path).stdout)

    assert status[0]["current"] is False
    assert not (tmp_path / "reports" / "probe-index" / "date=2026-08-11" /
                "probes.parquet").exists()
