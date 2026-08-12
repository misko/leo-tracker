import json
import os

import pytest

from leo_tracker.radio.beacon.probe_index import (PROBE_COLUMNS,
                                                  PROBE_INDEX_SCHEMA,
                                                  build, build_lock,
                                                  build_partition,
                                                  partition_is_current,
                                                  partition_path,
                                                  partition_status, query,
                                                  temporary_path,
                                                  report_date, source_reports)

duckdb = pytest.importorskip("duckdb", reason="probe index needs the analysis extra")


def _report(root, name, *, radio="pluto-19f2", labels=("lnb-c", "lnb-d"),
            channel=4, region="lower-edge", probes=3, hits=((True, False),)):
    """A beacon report carrying the fields the projection reads."""
    checks = []
    for index in range(probes):
        rc = list(hits[index % len(hits)])
        checks.append({
            "start_s": float(index), "candidate": all(rc), "qualified": False,
            "cfo_difference_hz": 1234.0,
            "receiver_candidates": rc, "receiver_qualified": [False, False],
            "receivers": [{"acquisition": {
                "exact_match": {"frequency_offset_hz": 1000.0 * (side + 1),
                                "epoch_sample": 42 + side, "score": 0.5},
                "match_score_margin": 0.05}} for side in (0, 1)]})
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{name}.json").write_text(json.dumps({
        "capture_manifest": {
            "identity": {"radio_id": radio, "serial": "abc",
                         "receiver_labels": list(labels)},
            "metadata": {"channel_number": channel, "region": region,
                         "observation_mode": "narrow"},
            "created_utc_ns": 1_786_000_000_000_000_000,
            "gain_mode": "manual",
            "sample_statistics": {"receivers": [
                {"rms_magnitude": 300.0, "near_full_scale_fraction": 0.0},
                {"rms_magnitude": 310.0, "near_full_scale_fraction": 0.0}]}},
        "exact_checks": checks,
        "lnb_calibration": {"applied": True, "reason": "measured"}}))


@pytest.mark.parametrize("name,expected", [
    ("ch4-lower-edge-narrow-pluto-19f2-20260811T164205Z", "2026-08-11"),
    ("ch1-upper-edge-narrow-20260731T000000Z", "2026-07-31"),
])
def test_partition_is_chosen_by_the_recording_stamp(name, expected):
    """A file time changes whenever an artifact is re-copied; the stamp does not."""
    assert report_date(name) == expected


def test_a_name_without_a_stamp_has_no_partition():
    assert report_date("not-a-recording") is None


def test_projection_emits_one_row_per_probe_per_receiver(tmp_path):
    """The pair exists so the two can disagree; folding them would lose that."""
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z", probes=5)

    result = build_partition(tmp_path, "2026-08-11")

    assert result["rows"] == 5 * 2
    rows = query(tmp_path, "SELECT lnb, rx FROM probes ORDER BY rx")
    assert {(lnb, rx) for lnb, rx in rows} == {("lnb-c", 0), ("lnb-d", 1)}


def test_per_receiver_detections_are_kept_apart(tmp_path):
    """One port detecting and the other not is the signal, not noise."""
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z",
            probes=10, hits=((False, True),))

    build_partition(tmp_path, "2026-08-11")

    rows = dict(query(tmp_path,
                      "SELECT lnb, sum(candidate)::INT FROM probes GROUP BY lnb"))
    assert rows == {"lnb-c": 0, "lnb-d": 10}


def test_aggregates_match_the_reports_they_came_from(tmp_path):
    """A derived table that disagrees with its source is worse than none.

    Both plausible, only one right, and nothing downstream can tell which.
    """
    for index in range(4):
        _report(tmp_path, f"ch4-lower-edge-narrow-20260811T12000{index}Z",
                probes=6, hits=((True, True), (False, False)))

    build_partition(tmp_path, "2026-08-11")

    expected = {"lnb-c": 0, "lnb-d": 0}
    for path in sorted((tmp_path / "reports").glob("*narrow*.json")):
        report = json.loads(path.read_text())
        labels = report["capture_manifest"]["identity"]["receiver_labels"]
        for check in report["exact_checks"]:
            for side, flag in enumerate(check["receiver_candidates"]):
                expected[labels[side]] += 1 if flag else 0

    actual = dict(query(tmp_path,
                        "SELECT lnb, sum(candidate)::INT FROM probes GROUP BY lnb"))
    assert actual == expected


def test_a_report_whose_shape_drifted_fails_the_ingest(tmp_path):
    """Naming the schema is a contract, not only a memory bound.

    Inference would accept a changed document and yield nulls, which read
    downstream as an absence of detections rather than as a broken pipeline.
    """
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")
    broken = tmp_path / "reports" / "ch4-lower-edge-narrow-20260811T130000Z.json"
    broken.write_text(json.dumps({
        "capture_manifest": {"identity": {"radio_id": "pluto-19f2"}},
        # receiver_candidates has become an object where a list is declared
        "exact_checks": [{"start_s": 0.0, "receiver_candidates": {"a": 1}}]}))

    with pytest.raises(Exception):
        build_partition(tmp_path, "2026-08-11")


def test_rebuilding_does_not_double_count(tmp_path):
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z", probes=4)

    first = build_partition(tmp_path, "2026-08-11")
    second = build_partition(tmp_path, "2026-08-11")

    assert first["rows"] == second["rows"] == 8
    assert query(tmp_path, "SELECT count(*) FROM probes")[0][0] == 8


def test_a_day_that_gained_reports_is_no_longer_current(tmp_path):
    """Staleness must be detected, not assumed away by the file existing."""
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")
    build_partition(tmp_path, "2026-08-11")
    assert partition_is_current(tmp_path, "2026-08-11",
                                source_reports(tmp_path, "2026-08-11"))

    _report(tmp_path, "ch4-lower-edge-narrow-20260811T130000Z")

    assert not partition_is_current(tmp_path, "2026-08-11",
                                    source_reports(tmp_path, "2026-08-11"))


def test_build_skips_days_that_are_already_current(tmp_path):
    _report(tmp_path, "ch4-lower-edge-narrow-20260810T120000Z")
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")
    build(tmp_path)

    _report(tmp_path, "ch4-lower-edge-narrow-20260811T130000Z")
    again = build(tmp_path)

    assert [item["date"] for item in again["built"]] == ["2026-08-11"]
    assert again["skipped"] == ["2026-08-10"]


def test_status_reports_indexed_against_source_counts(tmp_path):
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")
    build(tmp_path)
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T130000Z")

    status = {item["date"]: item for item in partition_status(tmp_path)}

    assert status["2026-08-11"]["source_count"] == 2
    assert status["2026-08-11"]["indexed_count"] == 1
    assert status["2026-08-11"]["current"] is False


def test_partitions_are_separate_files_per_day(tmp_path):
    """Immutable once a day closes, so drift is bounded to today."""
    _report(tmp_path, "ch4-lower-edge-narrow-20260810T120000Z")
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")

    build(tmp_path)

    assert partition_path(tmp_path, "2026-08-10").is_file()
    assert partition_path(tmp_path, "2026-08-11").is_file()
    assert query(tmp_path, "SELECT count(DISTINCT date) FROM probes")[0][0] == 2


def test_the_schema_names_every_column_the_queries_use():
    """A field dropped from the contract returns nulls, not an error."""
    for field in ("receiver_candidates", "receiver_qualified", "start_s",
                  "candidate", "cfo_difference_hz"):
        assert field in PROBE_COLUMNS["exact_checks"]
    for field in ("radio_id", "receiver_labels", "channel_number", "region",
                  "rms_magnitude", "created_utc_ns"):
        assert field in PROBE_COLUMNS["capture_manifest"]


def test_an_empty_day_is_reported_rather_than_written(tmp_path):
    (tmp_path / "reports").mkdir(parents=True)
    result = build_partition(tmp_path, "2026-08-11")

    assert result["built"] is False and result["rows"] == 0
    assert not partition_path(tmp_path, "2026-08-11").exists()


def test_the_capture_path_does_not_import_duckdb():
    """A capture host must not fail to start because analysis tooling is absent."""
    source = open("src/leo_tracker/radio/beacon/probe_index.py").read()
    header = source.split("def _connect")[0]
    assert "import duckdb" not in header, "duckdb must be imported lazily"
    for module in ("analysis.py", "artifact.py", "acquisition.py"):
        text = open(f"src/leo_tracker/radio/beacon/{module}").read()
        assert "duckdb" not in text


def test_a_second_builder_skips_rather_than_failing(tmp_path):
    """A refresh firing while one is still running is normal, not an error.

    The timer fires on a fixed period and a build takes as long as the day is
    long, so overlap is expected. Reporting failure for a condition that
    resolves itself teaches everyone to ignore the failure.
    """
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")

    with build_lock(tmp_path) as held:
        assert held is True
        result = build(tmp_path)

    assert result["skipped_locked"] is True
    assert result["built"] == []


def test_the_lock_is_released_for_the_next_run(tmp_path):
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")
    with build_lock(tmp_path) as held:
        assert held is True

    assert build(tmp_path)["built"] != []


def test_concurrent_builders_stage_to_separate_files(tmp_path, monkeypatch):
    """Correctness must not rest on the lock.

    The index lives on an NFS mount that another host also mounts, and
    cross-host advisory locking is not something we can rely on. Two builders
    must therefore be merely wasteful, not destructive, which holds only if
    each stages to its own file before the atomic rename.
    """
    target = partition_path(tmp_path, "2026-08-11")

    monkeypatch.setattr(os, "getpid", lambda: 111)
    first = temporary_path(target)
    monkeypatch.setattr(os, "getpid", lambda: 222)
    second = temporary_path(target)

    assert first != second
    assert first.parent == second.parent == target.parent


def test_a_failed_rebuild_leaves_the_good_partition_intact(tmp_path):
    """A day that breaks must keep answering with what it last knew.

    Truncating a partition on a bad ingest would turn one unreadable report
    into a whole day reading as zero detections.
    """
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z", probes=4)
    build_partition(tmp_path, "2026-08-11")
    before = partition_path(tmp_path, "2026-08-11").read_bytes()

    broken = tmp_path / "reports" / "ch4-lower-edge-narrow-20260811T130000Z.json"
    broken.write_text('{"capture_manifest": {"identity": {"radio_id": 7}},'
                      ' "exact_checks": [{"start_s": "not-a-number"}]}')
    with pytest.raises(Exception):
        build_partition(tmp_path, "2026-08-11")

    assert partition_path(tmp_path, "2026-08-11").read_bytes() == before
    assert query(tmp_path, "SELECT count(*) FROM probes")[0][0] == 8


def test_a_failed_rebuild_leaves_no_staged_file_behind(tmp_path):
    """Otherwise every failure leaks a file the size of the partition."""
    _report(tmp_path, "ch4-lower-edge-narrow-20260811T120000Z")
    broken = tmp_path / "reports" / "ch4-lower-edge-narrow-20260811T130000Z.json"
    broken.write_text('{"capture_manifest": {"identity": {"radio_id": 7}},'
                      ' "exact_checks": [{"start_s": "not-a-number"}]}')

    with pytest.raises(Exception):
        build_partition(tmp_path, "2026-08-11")

    staged = list(partition_path(tmp_path, "2026-08-11").parent.glob("*.next*"))
    assert staged == []
