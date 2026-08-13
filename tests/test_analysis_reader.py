import json

import pytest

duckdb = pytest.importorskip("duckdb",
                             reason="the analysis index needs the analysis extra")

from leo_tracker.radio.analysis_store.partition import build
from leo_tracker.radio.analysis_store.reader import ParquetAnalysisRepository
from leo_tracker.radio.dashboard import DashboardModel

from analysis_fixtures import _completed_run

KALMAN = "kalman-full-v1"
LEGACY = "legacy-v1"
AUGUST_11 = "ch4-lower-edge-narrow-20260811T120000Z"
AUGUST_10 = "ch4-lower-edge-narrow-20260810T090000Z"


def _built(root, *names, pipeline=KALMAN, **kwargs):
    for name in names:
        _completed_run(root, name=name, pipeline=pipeline, **kwargs)
    build(root, root)


def test_a_root_without_a_projection_is_not_available(tmp_path):
    """A capture-only host, or one before the first build, must degrade rather
    than raise."""
    assert ParquetAnalysisRepository(tmp_path).available() is False


def test_the_listing_comes_from_the_projection(tmp_path):
    _built(tmp_path, AUGUST_10, AUGUST_11)
    repository = ParquetAnalysisRepository(tmp_path)
    try:
        assert repository.available() is True
        rows = repository.recent_recordings(limit=10)
        assert {row["recording_id"] for row in rows} == {AUGUST_10, AUGUST_11}
    finally:
        repository.close()


def test_the_listing_is_newest_first(tmp_path):
    _built(tmp_path, AUGUST_10, AUGUST_11)
    repository = ParquetAnalysisRepository(tmp_path)
    try:
        rows = repository.recent_recordings(limit=10)
    finally:
        repository.close()
    assert [row["recording_id"] for row in rows] == [AUGUST_11, AUGUST_10]


def test_one_row_per_recording_even_with_two_pipelines(tmp_path):
    """Two pipelines commonly hold receipts for the same recording; a listing
    wants one row, from whichever analysed it last."""
    _completed_run(tmp_path, name=AUGUST_11, pipeline=LEGACY)
    _completed_run(tmp_path, name=AUGUST_11, pipeline=KALMAN)
    build(tmp_path, tmp_path)

    repository = ParquetAnalysisRepository(tmp_path)
    try:
        rows = repository.recent_recordings(limit=10)
        assert repository.summary()["analyzed_capture_count"] == 1
    finally:
        repository.close()
    assert [row["recording_id"] for row in rows] == [AUGUST_11]


def test_detail_is_returned_for_a_known_recording(tmp_path):
    _built(tmp_path, AUGUST_11)
    repository = ParquetAnalysisRepository(tmp_path)
    try:
        detail = repository.recording_detail(AUGUST_11)
        assert repository.recording_detail("never-analysed") is None
    finally:
        repository.close()
    assert detail is not None
    assert "_statistics" in detail and "_plots" in detail


def test_summary_counts_the_projection(tmp_path):
    _built(tmp_path, AUGUST_10, AUGUST_11, confirmed=True)
    repository = ParquetAnalysisRepository(tmp_path)
    try:
        summary = repository.summary()
    finally:
        repository.close()
    assert summary["analyzed_capture_count"] == 2
    assert summary["temporally_confirmed_capture_count"] == 2


def test_a_partition_built_later_is_picked_up_without_reconnecting(tmp_path):
    """The views resolve their glob when a query runs, so a long-lived
    dashboard sees a partition the timer built after it started."""
    _built(tmp_path, AUGUST_11)
    repository = ParquetAnalysisRepository(tmp_path)
    try:
        assert len(repository.recent_recordings(limit=10)) == 1

        _completed_run(tmp_path, name=AUGUST_10, pipeline=KALMAN)
        build(tmp_path, tmp_path)

        assert len(repository.recent_recordings(limit=10)) == 2
    finally:
        repository.close()


def test_a_listing_limit_must_be_positive(tmp_path):
    _built(tmp_path, AUGUST_11)
    repository = ParquetAnalysisRepository(tmp_path)
    try:
        with pytest.raises(ValueError):
            repository.recent_recordings(limit=0)
    finally:
        repository.close()


# ------------------------------------------------------------------ dashboard

def test_the_dashboard_serves_the_listing_from_the_projection(tmp_path):
    _built(tmp_path, AUGUST_11)
    model = DashboardModel(tmp_path / "obs", beacon_root=tmp_path)

    index = model._beacon_recording_index()

    assert index["schema"] == "leo-tracker.beacon-dashboard-index/v3"
    assert [row["recording_id"] for row in index["recordings"]] == [AUGUST_11]
    assert index["summary"]["analyzed_capture_count"] == 1


def test_the_dashboard_falls_back_when_there_is_no_projection(tmp_path):
    """Before the first build the dashboard must answer exactly as it did."""
    (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
    model = DashboardModel(tmp_path / "obs", beacon_root=tmp_path)

    assert model._analysis is None
    assert model._beacon_recording_index() == {}


def test_a_projection_that_cannot_answer_does_not_take_the_dashboard_down(
        tmp_path, monkeypatch):
    """DuckDB may be absent or QNAP briefly unreachable; the JSON path stands."""
    _built(tmp_path, AUGUST_11)
    model = DashboardModel(tmp_path / "obs", beacon_root=tmp_path)
    assert model._analysis is not None

    def explode(*_args, **_kwargs):
        raise RuntimeError("QNAP went away")

    monkeypatch.setattr(model._analysis, "recent_recordings", explode)

    assert model._beacon_recording_index() == {}


def test_the_dashboard_serves_detail_from_the_projection(tmp_path):
    _built(tmp_path, AUGUST_11)
    model = DashboardModel(tmp_path / "obs", beacon_root=tmp_path)

    detail = model.recording_detail("beacon", AUGUST_11)

    assert detail is not None
    assert detail["recording_id"] == AUGUST_11
    assert detail["active"] is False
    assert "statistics" in detail
