import json
import os
import time
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb",
                             reason="the analysis index needs the analysis extra")

from leo_tracker.radio.analysis_store import partition as partition_module
from leo_tracker.radio.analysis_store.partition import (PARTITION_SCHEMA,
                                                        PROJECTED_TABLES,
                                                        build, build_lock,
                                                        build_partition,
                                                        manifest_path,
                                                        partition_dir,
                                                        partition_is_current,
                                                        partition_signatures,
                                                        partition_sources,
                                                        partition_status,
                                                        partition_units,
                                                        read_connection,
                                                        sweep_stale_scratch,
                                                        temporary_path)
from leo_tracker.radio.cli import main as radio_main

# The receipt fixture is shared with the store tests it will outlive; it writes
# a complete run through the same contract paths production uses.
from test_analysis_store import _completed_run

KALMAN = "kalman-full-v1"
LEGACY = "legacy-v1"
AUGUST_11 = "ch4-lower-edge-narrow-20260811T120000Z"
AUGUST_10 = "ch4-lower-edge-narrow-20260810T090000Z"


def _run(root, name=AUGUST_11, pipeline=KALMAN, **kwargs):
    return _completed_run(root, name=name, pipeline=pipeline, **kwargs)


def _rows(root, table):
    connection = read_connection(root)
    try:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


# --------------------------------------------------------------- partitioning

def test_a_run_lands_in_the_partition_its_recording_stamp_names(tmp_path):
    """The stamp, not the completion time, decides the partition.

    A run analysed days after its capture still belongs to the day it observed,
    which is also the rule the probe index uses, so a recording lands in the
    same date in both projections.
    """
    _run(tmp_path)

    assert partition_units(tmp_path) == [(KALMAN, "2026-08-11")]


def test_two_pipelines_on_the_same_date_are_separate_partitions(tmp_path):
    """Pipeline is a partition level so a frozen one is never rewritten."""
    _run(tmp_path, name=AUGUST_11, pipeline=KALMAN)
    _run(tmp_path, name=AUGUST_11, pipeline=LEGACY)

    assert partition_units(tmp_path) == [(KALMAN, "2026-08-11"),
                                         (LEGACY, "2026-08-11")]
    build(tmp_path, tmp_path)
    for pipeline in (KALMAN, LEGACY):
        assert (partition_dir(tmp_path, pipeline, "2026-08-11")
                / "analysis_runs.parquet").is_file()


# ----------------------------------------------------------------- projection

def test_every_table_the_schema_declares_is_written(tmp_path):
    """A missing table reads downstream as an absence of facts, not a bug."""
    _run(tmp_path)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    written = {path.name for path in
               partition_dir(tmp_path, KALMAN, "2026-08-11").glob("*.parquet")}
    assert written == {f"{table}.parquet" for table in PROJECTED_TABLES}


def test_row_counts_match_the_receipts_they_came_from(tmp_path):
    _run(tmp_path, name=AUGUST_11, probes=3)
    _run(tmp_path, name="ch4-lower-edge-narrow-20260811T130000Z", probes=3)

    result = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert result["run_count"] == 2
    assert result["rows"]["analysis_runs"] == 2
    assert result["rows"]["probe_checks"] == 6
    assert _rows(tmp_path, "analysis_runs") == 2


def test_payloads_are_referenced_rather_than_copied(tmp_path):
    """Reports are the system of record; a second copy can only go stale.

    ``source_documents`` keeps the path, size and digest, so a payload is always
    reachable without the projection carrying 38% more bytes.
    """
    _run(tmp_path)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert "structured_documents" not in PROJECTED_TABLES
    assert not (partition_dir(tmp_path, KALMAN, "2026-08-11")
                / "structured_documents.parquet").exists()

    connection = read_connection(tmp_path)
    try:
        path, digest = connection.execute(
            "SELECT path, sha256 FROM source_documents WHERE kind = 'analysis'"
        ).fetchone()
    finally:
        connection.close()
    assert Path(path).is_file() and len(digest) == 64


def test_every_child_row_has_a_parent_run(tmp_path):
    """Parquet cannot express the foreign keys the schema declares.

    Building through the real schema keeps them enforced per partition, so the
    integrity survives even though the published artefact cannot state it.
    """
    _run(tmp_path)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    connection = read_connection(tmp_path)
    try:
        for table in ("probe_checks", "receiver_probes", "source_documents"):
            orphans = connection.execute(
                f"SELECT count(*) FROM {table} t "
                "LEFT JOIN analysis_runs r USING (run_id) WHERE r.run_id IS NULL"
            ).fetchone()[0]
            assert orphans == 0, table
    finally:
        connection.close()


# ------------------------------------------------------------------- currency

def test_a_partition_that_gained_a_run_is_no_longer_current(tmp_path):
    _run(tmp_path, name=AUGUST_11)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")
    assert partition_is_current(tmp_path, KALMAN, "2026-08-11",
                                partition_signatures(tmp_path, KALMAN, "2026-08-11"))

    _run(tmp_path, name="ch4-lower-edge-narrow-20260811T140000Z")

    assert not partition_is_current(
        tmp_path, KALMAN, "2026-08-11",
        partition_signatures(tmp_path, KALMAN, "2026-08-11"))


def test_a_run_reanalysed_in_place_invalidates_its_partition(tmp_path):
    """Currency is a digest set, not a count.

    Re-analysis rewrites a receipt without changing how many there are. Counting
    would answer from a stale projection, and this corpus re-runs recordings a
    second pipeline already covered, so it is the expected case rather than a
    corner one.
    """
    _run(tmp_path, name=AUGUST_11)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    receipt = tmp_path / "reports" / "runs" / KALMAN / AUGUST_11 / "completion.json"
    payload = json.loads(receipt.read_text())
    payload["completed_utc"] = "2026-08-11T18:00:00+00:00"
    receipt.write_text(json.dumps(payload))

    signatures = partition_signatures(tmp_path, KALMAN, "2026-08-11")
    assert len(signatures) == 1
    assert not partition_is_current(tmp_path, KALMAN, "2026-08-11", signatures,
                                    shared_root=tmp_path)


def test_a_touched_receipt_does_not_provoke_a_rebuild(tmp_path):
    """Stat finds drift; the digest decides whether it mattered.

    Hashing the corpus costs about 280 seconds against 0.1 for stat, so a scan
    must not read. But stat alone would rebuild a whole partition because a
    receipt was re-copied, so a moved signature is confirmed against the
    recorded digest before anything is rewritten.
    """
    _run(tmp_path, name=AUGUST_11)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    receipt = tmp_path / "reports" / "runs" / KALMAN / AUGUST_11 / "completion.json"
    os.utime(receipt, ns=(1, 1))  # same bytes, new modification time

    signatures = partition_signatures(tmp_path, KALMAN, "2026-08-11")
    assert not partition_is_current(tmp_path, KALMAN, "2026-08-11", signatures)
    assert partition_is_current(tmp_path, KALMAN, "2026-08-11", signatures,
                                shared_root=tmp_path)


def test_the_scan_reads_no_receipts_when_nothing_moved(tmp_path, monkeypatch):
    """The common case must cost no reads, or the timer cannot run on time."""
    _run(tmp_path)
    build(tmp_path, tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the scan digested a receipt it did not need to")

    monkeypatch.setattr(partition_module, "_sha256", refuse)
    result = build(tmp_path, tmp_path)

    assert result["built"] == [] and len(result["current"]) == 1


def test_a_frozen_pipeline_is_not_rebuilt_when_another_gains_runs(tmp_path):
    """The reason pipeline is a partition level at all.

    Without it, every new Kalman run would rewrite twelve thousand frozen legacy
    rows, forever, for nothing.
    """
    _run(tmp_path, name=AUGUST_11, pipeline=LEGACY)
    _run(tmp_path, name=AUGUST_11, pipeline=KALMAN)
    build(tmp_path, tmp_path)
    legacy_parquet = (partition_dir(tmp_path, LEGACY, "2026-08-11")
                      / "analysis_runs.parquet")
    untouched = legacy_parquet.stat().st_mtime_ns

    _run(tmp_path, name="ch4-lower-edge-narrow-20260811T150000Z", pipeline=KALMAN)
    result = build(tmp_path, tmp_path)

    assert legacy_parquet.stat().st_mtime_ns == untouched
    assert [entry["pipeline"] for entry in result["built"]] == [KALMAN]
    assert [entry["pipeline"] for entry in result["current"]] == [LEGACY]


def test_build_skips_partitions_that_are_already_current(tmp_path):
    _run(tmp_path)
    assert build(tmp_path, tmp_path)["built"] != []

    again = build(tmp_path, tmp_path)

    assert again["built"] == []
    assert [entry["date"] for entry in again["current"]] == ["2026-08-11"]


def test_rebuilding_produces_the_same_rows(tmp_path):
    """A projection that changes without its sources changing is not a
    projection."""
    _run(tmp_path)
    first = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    second = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert first["rows"] == second["rows"]
    assert first["sources"] == second["sources"]


# --------------------------------------------------------- atomicity, overlap

def test_the_manifest_is_written_after_the_data(tmp_path, monkeypatch):
    """The manifest landing is the commit.

    A build that dies between the data and the manifest must read as
    not-current, so the next run repairs it rather than answering from a
    partition nobody vouched for.
    """
    _run(tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("interrupted before the commit marker")

    monkeypatch.setattr(partition_module, "_atomic_json", explode)
    with pytest.raises(RuntimeError):
        build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert (partition_dir(tmp_path, KALMAN, "2026-08-11")
            / "analysis_runs.parquet").is_file()
    assert not manifest_path(tmp_path, KALMAN, "2026-08-11").exists()
    assert not partition_is_current(
        tmp_path, KALMAN, "2026-08-11",
        partition_signatures(tmp_path, KALMAN, "2026-08-11"))


def test_a_failed_build_leaves_the_previous_partition_intact(tmp_path,
                                                             monkeypatch):
    """A day keeps answering with what it last knew.

    Truncating a good partition because a rebuild failed turns a recoverable
    fault into missing data.
    """
    _run(tmp_path)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")
    good = (partition_dir(tmp_path, KALMAN, "2026-08-11")
            / "analysis_runs.parquet").read_bytes()

    monkeypatch.setattr(partition_module, "PROJECTED_TABLES",
                        PROJECTED_TABLES + ("no_such_table",))
    with pytest.raises(Exception):
        build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert (partition_dir(tmp_path, KALMAN, "2026-08-11")
            / "analysis_runs.parquet").read_bytes() == good
    assert manifest_path(tmp_path, KALMAN, "2026-08-11").is_file()


def test_a_failed_build_leaves_no_staged_file_behind(tmp_path, monkeypatch):
    _run(tmp_path)

    monkeypatch.setattr(partition_module, "PROJECTED_TABLES",
                        PROJECTED_TABLES + ("no_such_table",))
    with pytest.raises(Exception):
        build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    leftovers = list(partition_dir(tmp_path, KALMAN, "2026-08-11").glob("*.next"))
    assert leftovers == []


def test_concurrent_builders_stage_to_separate_files(tmp_path, monkeypatch):
    """Correctness must not rest on the lock.

    The partitions live on an NFS mount another host also mounts, and cross-host
    advisory locking is not dependable. Two builders must therefore be merely
    wasteful, not destructive.
    """
    target = partition_dir(tmp_path, KALMAN, "2026-08-11") / "probe_checks.parquet"

    monkeypatch.setattr(os, "getpid", lambda: 111)
    first = temporary_path(target)
    monkeypatch.setattr(os, "getpid", lambda: 222)
    second = temporary_path(target)

    assert first != second
    assert first.parent == second.parent == target.parent


def test_a_staged_file_is_invisible_to_a_reader(tmp_path):
    """A reader globs ``*.parquet``; a half-written table must not match it."""
    target = partition_dir(tmp_path, KALMAN, "2026-08-11") / "probe_checks.parquet"

    assert not temporary_path(target).name.endswith(".parquet")


def test_a_second_builder_skips_rather_than_failing(tmp_path):
    _run(tmp_path)

    with build_lock(tmp_path) as held:
        assert held is True
        result = build(tmp_path, tmp_path)

    assert result["skipped_locked"] is True
    assert result["built"] == []


def test_the_lock_is_released_for_the_next_run(tmp_path):
    _run(tmp_path)
    with build_lock(tmp_path) as held:
        assert held is True

    assert build(tmp_path, tmp_path)["built"] != []


# -------------------------------------------------------------- query surface

def test_views_span_every_partition(tmp_path):
    """Hive keys come back typed: ``date`` is a DATE, ``pipeline`` a VARCHAR."""
    _run(tmp_path, name=AUGUST_11)
    _run(tmp_path, name=AUGUST_10)
    build(tmp_path, tmp_path)

    connection = read_connection(tmp_path)
    try:
        dates = connection.execute(
            "SELECT DISTINCT CAST(date AS VARCHAR) FROM analysis_runs ORDER BY 1"
        ).fetchall()
        pipelines = connection.execute(
            "SELECT DISTINCT pipeline FROM analysis_runs").fetchall()
    finally:
        connection.close()
    assert dates == [("2026-08-10",), ("2026-08-11",)]
    assert pipelines == [(KALMAN,)]


def test_a_date_filter_returns_only_that_partition(tmp_path):
    _run(tmp_path, name=AUGUST_11)
    _run(tmp_path, name=AUGUST_10)
    build(tmp_path, tmp_path)

    connection = read_connection(tmp_path)
    try:
        count = connection.execute(
            "SELECT count(*) FROM analysis_runs WHERE date = '2026-08-11'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_current_runs_keeps_the_latest_run_per_recording(tmp_path):
    """Two pipelines analyse the same recording; a listing wants one row."""
    _run(tmp_path, name=AUGUST_11, pipeline=LEGACY)
    _run(tmp_path, name=AUGUST_11, pipeline=KALMAN)
    build(tmp_path, tmp_path)

    connection = read_connection(tmp_path)
    try:
        assert connection.execute("SELECT count(*) FROM analysis_runs").fetchone()[0] == 2
        rows = connection.execute(
            "SELECT recording_id, count(*) FROM current_runs GROUP BY 1").fetchall()
    finally:
        connection.close()
    assert rows == [(AUGUST_11, 1)]


def test_queries_take_no_lock_while_a_build_runs(tmp_path):
    """The property that makes this safe on a soft NFS mount.

    Readers open no database file, so a build in flight neither blocks them nor
    is blocked by them.
    """
    _run(tmp_path)
    build(tmp_path, tmp_path)

    with build_lock(tmp_path) as held:
        assert held is True
        connection = read_connection(tmp_path)
        try:
            assert connection.execute(
                "SELECT count(*) FROM analysis_runs").fetchone()[0] == 1
        finally:
            connection.close()


# ------------------------------------------------------------------- contract

def test_an_empty_partition_is_reported_rather_than_written(tmp_path):
    result = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert result["built"] is False
    assert result["run_count"] == 0
    assert not partition_dir(tmp_path, KALMAN, "2026-08-11").exists()


def test_a_superseded_report_excludes_its_run_rather_than_failing(tmp_path):
    """One unauthenticatable run must not cost the whole projection.

    A report is a single mutable path shared by every pipeline and every
    re-analysis, while receipts are per-pipeline and immutable. Re-analysing a
    recording leaves older receipts attesting to a file that no longer exists,
    and roughly a tenth of the real corpus is in that state permanently.
    Aborting the build would mean it can never complete at all.
    """
    _run(tmp_path, name=AUGUST_11)
    _run(tmp_path, name="ch4-lower-edge-narrow-20260811T170000Z")
    analysis = tmp_path / "reports" / f"{AUGUST_11}.json"
    analysis.write_text(analysis.read_text() + " ")  # same JSON, new digest

    result = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert result["built"] is True
    assert result["rows"]["analysis_runs"] == 1, "the healthy run still projects"
    assert result["excluded_count"] == 1
    assert result["excluded"][0]["recording_id"] == AUGUST_11
    assert "changed" in result["excluded"][0]["error"]


def test_an_excluded_run_does_not_provoke_an_endless_rebuild(tmp_path):
    """The exclusion is permanent, so the partition must settle as current.

    Treating a superseded report as drift would rebuild its partition on every
    firing of the timer, forever, and never fix anything.
    """
    _run(tmp_path)
    analysis = tmp_path / "reports" / f"{AUGUST_11}.json"
    analysis.write_text(analysis.read_text() + " ")
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert partition_is_current(tmp_path, KALMAN, "2026-08-11",
                                partition_signatures(tmp_path, KALMAN, "2026-08-11"))
    assert build(tmp_path, tmp_path)["built"] == []


def test_status_counts_what_was_excluded(tmp_path):
    """A partition that projected a tenth of its receipts is still current;
    without a number that reads as completeness."""
    _run(tmp_path)
    analysis = tmp_path / "reports" / f"{AUGUST_11}.json"
    analysis.write_text(analysis.read_text() + " ")
    build(tmp_path, tmp_path)

    status = partition_status(tmp_path, tmp_path)

    assert status["partitions"][0]["excluded"] == 1
    assert status["excluded_total"] == 1


def test_a_structurally_broken_report_is_named_not_swallowed(tmp_path):
    """Exclusion is not silence: the run and its reason are both recorded."""
    _run(tmp_path)
    analysis = tmp_path / "reports" / f"{AUGUST_11}.json"
    analysis.write_text(json.dumps({"schema": "leo-tracker.starlink-beacon-analysis/v1"}))

    result = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert result["excluded_count"] == 1
    excluded = result["excluded"][0]
    assert excluded["recording_id"] == AUGUST_11
    assert excluded["error_type"] and excluded["error"]


def test_the_cli_builds_reports_and_queries(tmp_path, capsys):
    """One command surface, mirroring starlink-probe-index."""
    _run(tmp_path)
    index = tmp_path / "index"

    assert radio_main(["starlink-analysis-index", "build", str(tmp_path),
                       "--index-root", str(index)]) == 0
    assert json.loads(capsys.readouterr().out)["built"][0]["run_count"] == 1

    assert radio_main(["starlink-analysis-index", "status", str(tmp_path),
                       "--index-root", str(index)]) == 0
    assert json.loads(capsys.readouterr().out)["stale_count"] == 0

    assert radio_main(["starlink-analysis-index", "query", str(tmp_path),
                       "--index-root", str(index),
                       "--sql", "SELECT count(*) FROM analysis_runs"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == [1]


def test_the_cli_refuses_a_query_without_sql(tmp_path):
    _run(tmp_path)

    assert radio_main(["starlink-analysis-index", "query", str(tmp_path)]) == 2


def test_the_index_can_be_written_beside_the_reports_it_reads(tmp_path):
    """The projection need not live in the root it projects.

    Kalman writes partitions to QNAP while reading receipts from it, but a
    verification run wants them somewhere else entirely.
    """
    _run(tmp_path)
    elsewhere = tmp_path / "elsewhere"

    build(elsewhere, tmp_path)

    assert (partition_dir(elsewhere, KALMAN, "2026-08-11")
            / "analysis_runs.parquet").is_file()
    assert not (tmp_path / "reports" / "analysis-index").exists()


# -------------------------------------------------------------- build scratch

def test_scratch_from_an_interrupted_build_is_swept(tmp_path):
    """A build that is killed rather than raised leaves its database behind.

    At roughly 10 MB per run of the partition it was assembling, and under
    systemd in a cache directory nothing else reclaims, that accumulates until
    it matters. Two killed builds left 6 GB behind in practice.
    """
    work = tmp_path / "scratch"
    work.mkdir()
    orphan = work / "analysis-index-deadbeef"
    orphan.mkdir()
    (orphan / "partition.duckdb").write_bytes(b"x" * 1024)
    old = time.time() - 12 * 3600
    os.utime(orphan, (old, old))

    swept = sweep_stale_scratch(work)

    assert swept == [str(orphan)]
    assert not orphan.exists()


def test_a_concurrent_builders_scratch_is_never_swept(tmp_path):
    """The advisory lock does not stop a second builder, so the sweep must not
    depend on being the only one running.

    Age decides rather than liveness: a pid can be reused, and deleting a live
    builder's database mid-partition would be far worse than leaking one.
    """
    work = tmp_path / "scratch"
    work.mkdir()
    live = work / "analysis-index-inflight"
    live.mkdir()
    (live / "partition.duckdb").write_bytes(b"x" * 1024)

    assert sweep_stale_scratch(work) == []
    assert live.is_dir()


def test_a_build_sweeps_before_it_starts(tmp_path):
    _run(tmp_path)
    work = tmp_path / "scratch"
    work.mkdir()
    orphan = work / "analysis-index-stale"
    orphan.mkdir()
    old = time.time() - 12 * 3600
    os.utime(orphan, (old, old))

    result = build(tmp_path, tmp_path, work_dir=work)

    assert result["swept_scratch"] == [str(orphan)]
    assert result["built"] != []


def test_a_finished_build_leaves_no_scratch(tmp_path):
    _run(tmp_path)
    work = tmp_path / "scratch"
    work.mkdir()

    build(tmp_path, tmp_path, work_dir=work)

    assert list(work.glob("analysis-index-*")) == []


# ----------------------------------------------------------------- deployment

UNIT = Path("deploy/leo-tracker-analysis-index.service")
TIMER = Path("deploy/leo-tracker-analysis-index.timer")


def test_the_builder_service_is_activated_only_by_its_timer():
    """An [Install] section here would run one build at boot and never again.

    That looks like a working installation right up until someone asks why the
    projection is a day behind.
    """
    sections = [line.strip() for line in UNIT.read_text().splitlines()
                if line.strip().startswith("[")]
    assert "[Install]" not in sections, sections
    assert "WantedBy=timers.target" in TIMER.read_text()


def test_the_build_scratch_is_local_and_not_a_tmpfs():
    """The throwaway build database is about 9 MB per run against 0.35 MB of
    parquet out, so the largest partition needs tens of gigabytes. On a tmpfs
    that is RAM."""
    text = UNIT.read_text()
    assert "CacheDirectory=leo-tracker-analysis-index" in text
    assert "--work-dir /var/cache/leo-tracker-analysis-index" in text
    assert "--work-dir /tmp" not in text
    assert "--work-dir /mnt" not in text


def test_the_builder_refuses_to_run_without_the_receipts():
    """A build that 'succeeded' against an absent mount would report zero
    partitions rather than a missing filesystem."""
    assert ("ConditionPathIsDirectory=/mnt/qnap01/mouse9911/leo/reports/runs"
            in UNIT.read_text())


def test_the_builder_yields_to_the_dsp_workers():
    """Sixteen analysis workers on this host matter more than a projection that
    is explicitly not authoritative."""
    text = UNIT.read_text()
    assert "Nice=10" in text and "IOSchedulingClass=idle" in text


def test_a_host_that_was_down_catches_up_once():
    text = TIMER.read_text()
    assert "Persistent=true" in text
    assert "OnUnitActiveSec=30min" in text


def test_the_capture_path_does_not_import_duckdb():
    """A capture host must not fail to start because analysis tooling is absent."""
    source = open("src/leo_tracker/radio/analysis_store/partition.py").read()
    header = source.split("def read_connection")[0]
    assert "import duckdb" not in header, "duckdb must be imported lazily"


def test_the_manifest_records_columns_that_arrived_null(tmp_path):
    """Drift must be visible in the manifest, not discovered in a query.

    This corpus gained ``identity.receiver_labels`` partway through its life, so
    partitions either side of that change project the same column with and
    without values. Nothing in the Parquet says so; the manifest must.
    """
    _run(tmp_path)
    result = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    nulls = result["nulls"]
    connection = read_connection(tmp_path)
    try:
        for table, columns in nulls.items():
            for column, recorded in columns.items():
                actual = connection.execute(
                    f'SELECT count(*) - count("{column}") FROM {table}').fetchone()[0]
                assert actual == recorded, f"{table}.{column}"
    finally:
        connection.close()
    assert all(count > 0 for columns in nulls.values() for count in columns.values())


def test_a_column_that_is_never_null_is_not_recorded(tmp_path):
    """Presence is the signal, so a fully populated column must stay absent."""
    _run(tmp_path)
    result = build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    assert "run_id" not in result["nulls"].get("analysis_runs", {})
    assert "recording_id" not in result["nulls"].get("recordings", {})


def test_status_reports_receipts_against_what_was_built(tmp_path):
    """A partition that quietly stopped rebuilding shows as a number."""
    _run(tmp_path, name=AUGUST_11)
    build(tmp_path, tmp_path)
    _run(tmp_path, name="ch4-lower-edge-narrow-20260811T160000Z")

    status = partition_status(tmp_path, tmp_path)

    entry = status["partitions"][0]
    assert entry["receipts"] == 2 and entry["built"] == 1
    assert entry["current"] is False
    assert status["stale_count"] == 1


def test_status_names_an_unbuilt_partition_rather_than_omitting_it(tmp_path):
    _run(tmp_path)

    status = partition_status(tmp_path, tmp_path)

    assert status["partitions"] == [{"pipeline": KALMAN, "date": "2026-08-11",
                                     "receipts": 1, "current": False,
                                     "built": None}]


def test_the_manifest_records_what_it_was_built_from(tmp_path):
    _run(tmp_path)
    build_partition(tmp_path, tmp_path, KALMAN, "2026-08-11")

    recorded = json.loads(manifest_path(tmp_path, KALMAN, "2026-08-11").read_text())
    assert recorded["schema"] == PARTITION_SCHEMA
    assert recorded["pipeline"] == KALMAN and recorded["date"] == "2026-08-11"
    assert set(recorded["sources"]) == {AUGUST_11}
    assert len(recorded["sources"][AUGUST_11]) == 64
