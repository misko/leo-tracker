from datetime import datetime, timezone
import json
import pytest

from leo_tracker.orbit.cli import main
from leo_tracker.orbit.fragment_diagnostics import (
    FRAGMENT_DIAGNOSTIC_SCHEMA, diagnose_fragments)


TRACK_SCHEMA = "leo-tracker.starlink-continuous-track/v1"
ASSOCIATION_SCHEMA = "leo-tracker.starlink-tle-association/v2"


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _track(track_id, start, stop, *, common_step=0, differential_step=0):
    observations = []
    for index in range(round((stop-start)*10)+1):
        moment = start+index/10
        base = 100_000+100*(moment-start)+common_step
        observations.append({"utc": _utc(moment), "consensus": {
            "valid": True, "receiver_referenced_cfo_hz": base,
            "frequency_sigma_hz": 20}, "receivers": [
                {"receiver": 0, "valid": True, "frequency_offset_hz": base,
                 "formal_sigma_hz": 20},
                {"receiver": 1, "valid": True,
                 "frequency_offset_hz": base+500+differential_step,
                 "formal_sigma_hz": 20}]})
    return {"track_id": track_id, "observations": observations,
        "summary": {"dual_valid_observation_count": len(observations)}}


def _candidate(rank, norad, rms, *, name=None):
    return {"rank": rank, "name": name or f"STARLINK-{norad}", "norad_id": norad,
        "holdout_residual_rms_hz": rms, "train_residual_rms_hz": rms/2,
        "epoch_adjustment_s": .2, "epoch_at_search_boundary": False}


def _artifacts(tmp_path, *, second_identity=100, differential_step=0):
    tracks = tmp_path/"tracks.json"; links = tmp_path/"links.json"
    joint = tmp_path/"joint.json"; fragments = tmp_path/"fragments.json"
    first = _track("track-000", 1_700_000_000, 1_700_000_004)
    second = _track("track-001", 1_700_000_010, 1_700_000_014,
                    common_step=600, differential_step=differential_step)
    tracks.write_text(json.dumps({"schema": TRACK_SCHEMA, "tracks": [first, second]}))
    linked_observations = first["observations"]+second["observations"]
    links.write_text(json.dumps({"schema": TRACK_SCHEMA, "tracks": [{
        "track_id": "channel-hypothesis-000", "observations": linked_observations,
        "source_segments": [
            {"source_track_id": "track-000", "start_s": 1_700_000_000,
             "stop_s": 1_700_000_004},
            {"source_track_id": "track-001", "start_s": 1_700_000_010,
             "stop_s": 1_700_000_014}],
        "summary": {"dual_valid_observation_count": len(linked_observations),
                    "source_segment_count": 2}}]}))
    joint_candidates = [_candidate(1, 100, 700), _candidate(2, 200, 1500)]
    joint.write_text(json.dumps({"schema": ASSOCIATION_SCHEMA, "associations": [{
        "track_id": "channel-hypothesis-000", "qualified": False,
        "best_norad_id": 100, "best_holdout_residual_rms_hz": 700,
        "candidates": joint_candidates}]}))
    fragment_rows = []
    for track_id, identity in (("track-000", 100), ("track-001", second_identity)):
        candidates = [_candidate(1, identity, 40),
                      _candidate(2, 300 if identity != 300 else 301, 240),
                      _candidate(3, 400, 500)]
        if identity != 100:
            candidates.append(_candidate(4, 100, 450))
        fragment_rows.append({"track_id": track_id, "qualified": False,
            "duration_s": 4, "dual_epoch_count": 41,
            "best_norad_id": identity, "candidates": candidates})
    fragments.write_text(json.dumps({"schema": ASSOCIATION_SCHEMA,
                                     "associations": fragment_rows}))
    return tracks, links, joint, fragments


def test_diagnostic_supports_same_identity_with_common_receiver_behavior(tmp_path):
    paths = _artifacts(tmp_path)
    report = diagnose_fragments(*paths, tmp_path/"diagnostic.json")

    assert report["schema"] == FRAGMENT_DIAGNOSTIC_SCHEMA
    hypothesis = report["hypotheses"][0]
    assert hypothesis["shadow_classification"] == \
        "same_identity_discontinuity_candidate"
    assert hypothesis["fragment_top_identity_agreement"] is True
    assert hypothesis["receiver_common_mode_consistent"] is True
    assert hypothesis["gaps"][0]["differential_step_hz"] == pytest.approx(0, abs=1e-6)
    assert hypothesis["model_comparison"][
        "same_identity_piecewise_holdout_rms_hz"] == 40
    assert report["summary"]["production_qualification_affected"] is False


def test_diagnostic_identifies_distinct_fragment_winners_but_remains_shadow(tmp_path):
    paths = _artifacts(tmp_path, second_identity=200)
    report = diagnose_fragments(*paths, tmp_path/"diagnostic.json")

    hypothesis = report["hypotheses"][0]
    assert hypothesis["shadow_classification"] == "satellite_switch_candidate"
    assert hypothesis["fragment_top_norad_ids"] == [100, 200]
    assert hypothesis["production_qualification_affected"] is False


def test_receiver_differential_step_forces_indeterminate_result(tmp_path):
    paths = _artifacts(tmp_path, differential_step=2_000)
    report = diagnose_fragments(*paths, tmp_path/"diagnostic.json")

    hypothesis = report["hypotheses"][0]
    assert hypothesis["receiver_common_mode_consistent"] is False
    assert hypothesis["shadow_classification"] == "indeterminate"


def test_fragment_diagnostic_cli_writes_auditable_json(tmp_path, capsys):
    paths = _artifacts(tmp_path); output = tmp_path/"diagnostic.json"
    assert main(["diagnose-fragments", "--tracks", str(paths[0]),
        "--links", str(paths[1]), "--joint-association", str(paths[2]),
        "--fragment-association", str(paths[3]), "--output", str(output)]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["fragment_diagnostic"] == str(output)
    assert json.loads(output.read_text())["summary"][
        "same_identity_discontinuity_candidate_count"] == 1
