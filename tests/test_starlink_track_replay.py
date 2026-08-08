import json
from pathlib import Path

import pytest

from leo_tracker.radio.beacon import replay
from leo_tracker.radio.beacon.artifact import SCHEMA as CAPTURE_SCHEMA


def _write(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def _fixture(root: Path, name: str = "capture-001", *, gap: float = 5,
             reacquisition_span: float = 5_000, dual: int = 195,
             tracks: int = 3, longest: float = 9.6,
             manifest_present: bool = True) -> dict[str, Path]:
    capture = root / "captures" / name
    manifest_value = {"schema": CAPTURE_SCHEMA, "sample_rate_hz": 2_500_000}
    manifest = capture / "manifest.json"
    if manifest_present:
        _write(manifest, manifest_value)
    followup = _write(root / "reports" / "followups" / f"{name}.json", {"checks": []})
    frame_samples = root / "reports" / "frame-tracks" / f"{name}.npz"
    frame_samples.parent.mkdir(parents=True, exist_ok=True)
    frame_samples.write_bytes(b"conditioned frame fixture")
    frame = _write(root / "reports" / "frame-tracks" / f"{name}.json",
                   {"tracks": [], "samples": {"path": str(frame_samples)}})
    catalog = _write(root / "context" / "catalog.json", {"content": "fixture"})
    track = _write(root / "reports" / "tracks" / f"{name}.json", {
        "capture": str(capture), "source_followup": str(followup),
        "source_frame_track": str(frame),
        "capture_manifest": manifest_value,
        "configuration": {"maximum_gap_s": gap,
                          "maximum_reacquisition_span_hz": reacquisition_span},
        "summary": {"dual_valid_observation_count": dual,
                    "track_count": tracks,
                    "longest_dual_valid_duration_s": longest},
    })
    association = _write(root / "reports" / "associations" / f"{name}.json", {
        "catalog": {"resolved_object_path": str(catalog)},
        "associations": [], "summary": {"qualified_association_count": 0},
    })
    return {"capture": capture, "manifest": manifest, "followup": followup,
            "frame": frame, "frame_samples": frame_samples, "catalog": catalog, "track": track,
            "association": association}


def test_plan_selects_only_fragmented_old_tracks_with_enough_evidence(tmp_path):
    _fixture(tmp_path, "selected")
    _fixture(tmp_path, "current", gap=15, reacquisition_span=15_000)
    _fixture(tmp_path, "sparse", dual=44)
    _fixture(tmp_path, "single", tracks=1)
    _fixture(tmp_path, "already-long", longest=20)

    plan = replay.create_replay_plan(tmp_path)

    assert [job["name"] for job in plan["jobs"]] == ["selected"]
    assert plan["job_count"] == 1
    assert plan["excluded"] == {
        "already_current": 1,
        "already_long_enough": 1,
        "insufficient_dual_observations": 1,
        "not_fragmented": 1,
    }
    assert all(value["sha256"] for value in plan["jobs"][0]["inputs"].values())


def test_plan_is_immutable_for_one_replay_identity(tmp_path):
    _fixture(tmp_path)
    first = replay.create_replay_plan(tmp_path)
    assert replay.create_replay_plan(tmp_path) == first

    with pytest.raises(ValueError, match="identity collision"):
        replay.create_replay_plan(tmp_path, maximum_gap_s=30)


def test_plan_can_replay_conditioned_frames_from_embedded_manifest(tmp_path):
    _fixture(tmp_path, manifest_present=False)

    plan = replay.create_replay_plan(tmp_path)

    assert plan["job_count"] == 1
    assert plan["jobs"][0]["embedded_capture_manifest"]["schema"] == CAPTURE_SCHEMA
    assert "capture_manifest" not in plan["jobs"][0]["inputs"]


def test_replay_runs_end_to_end_without_overwriting_sources(tmp_path, monkeypatch):
    paths = _fixture(tmp_path)
    old_track = paths["track"].read_bytes()
    old_association = paths["association"].read_bytes()
    replay.create_replay_plan(tmp_path)

    def fake_track(capture, followup, output, **kwargs):
        assert kwargs["measurement_source"] == "conditioned_frames"
        assert kwargs["maximum_gap_s"] == 15
        assert kwargs["maximum_reacquisition_span_hz"] == 15_000
        report = {"schema": "leo-tracker.starlink-continuous-track/v1",
                  "summary": {"track_count": 2,
                              "longest_dual_valid_duration_s": 29.5}}
        _write(output, report)
        return report

    def fake_associate(track, catalog, output, *, observer):
        report = {"schema": "leo-tracker.starlink-tle-association/v2",
                  "source_observations": str(track),
                  "associations": [{"track_id": "track-000", "qualified": True,
                                    "best_norad_id": 67082,
                                    "best_holdout_residual_rms_hz": 83.8,
                                    "margin_to_second_hz": 297.8,
                                    "candidates": [{"norad_id": 67082,
                                                    "name": "STARLINK-36035"}]}],
                  "summary": {"qualified_association_count": 1}}
        _write(output, report)
        return report

    monkeypatch.setattr(replay, "track_capture", fake_track)
    monkeypatch.setattr(replay, "associate_tracks", fake_associate)
    result = replay.run_replay(tmp_path)

    assert result["results"] == {"succeeded": 1}
    assert result["status"]["qualified_norad_ids"] == [67082]
    job = (tmp_path / "reports" / "replays" / replay.DEFAULT_REPLAY_ID /
           "jobs" / "capture-001")
    receipt = json.loads((job / "completion.json").read_text())
    association = json.loads((job / "association.json").read_text())
    assert receipt["schema"] == replay.RECEIPT_SCHEMA
    assert receipt["new"]["qualified_identities"][0]["name"] == "STARLINK-36035"
    assert association["source_observations"] == str((job / "track.json").resolve())
    assert paths["track"].read_bytes() == old_track
    assert paths["association"].read_bytes() == old_association
    assert replay.run_replay(tmp_path)["attempted_count"] == 0


def test_changed_input_fails_closed_and_publishes_no_partial_job(tmp_path,
                                                                 monkeypatch):
    paths = _fixture(tmp_path)
    replay.create_replay_plan(tmp_path)
    paths["frame"].write_text("changed after planning")
    monkeypatch.setattr(replay, "track_capture",
                        lambda *args, **kwargs: pytest.fail("must not execute"))

    result = replay.run_replay(tmp_path)

    assert result["results"] == {"failed": 1}
    replay_root = tmp_path / "reports" / "replays" / replay.DEFAULT_REPLAY_ID
    assert not (replay_root / "jobs" / "capture-001").exists()
    failure = json.loads((replay_root / "failures" / "capture-001.json").read_text())
    assert "input changed" in failure["error"]


def test_status_reports_empty_unstarted_plan(tmp_path):
    _fixture(tmp_path)
    replay.create_replay_plan(tmp_path)

    status = replay.replay_status(tmp_path)

    assert status["job_count"] == 1
    assert status["remaining_count"] == 1
    assert status["running_count"] == 0


def test_run_rejects_name_outside_immutable_plan(tmp_path):
    _fixture(tmp_path)
    replay.create_replay_plan(tmp_path)

    with pytest.raises(ValueError, match="not in replay plan"):
        replay.run_replay(tmp_path, names=["not-planned"])
