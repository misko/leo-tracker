import json

import pytest

from leo_tracker.radio import cli
from leo_tracker.radio.hybrid import (
    build_hybrid_plan, requires_fallback, should_retain_iq, survey_centers,
)


def test_survey_tiles_cover_channels_three_and_four_without_gaps():
    centers = survey_centers()
    assert len(centers) == 28
    for group, nominal in ((centers[:14], 1_575_117_187.5),
                           (centers[14:], 1_825_117_187.5)):
        assert group[0]-9_000_000 == pytest.approx(nominal-120_000_000)
        assert group[-1]+9_000_000 == pytest.approx(nominal+120_000_000)
        assert max(b-a for a, b in zip(group, group[1:])) <= 18_000_000


def test_hybrid_plan_uses_four_megasample_dwell_and_25_fallback():
    plan = build_hybrid_plan(dwell_seconds=600)
    assert plan["survey"]["sample_rate_hz"] == 30_720_000
    assert plan["dwell"]["sample_rate_hz"] == 4_000_000
    assert plan["dwell"]["snapshots"] == 9156
    assert plan["fallback"]["sample_rate_hz"] == 2_500_000
    assert plan["fallback"]["snapshots"] == 5723


def test_hybrid_plan_cli_writes_machine_readable_center_plan(tmp_path, capsys):
    output = tmp_path/"plan.json"
    assert cli.main(["starlink-hybrid-plan", str(output), "--dwell-seconds", "60"]) == 0
    saved = json.loads(output.read_text())
    assert saved["schema"] == "leo-tracker.starlink-hybrid-plan/v1"
    assert saved["centers_hz"] == saved["survey"]["centers_hz"]
    assert json.loads(capsys.readouterr().out)["survey_tiles"] == 28


def test_low_four_megasample_duty_selects_fallback_but_fallback_is_stable():
    assert requires_fallback(4_000_000, 0.5061)
    assert not requires_fallback(4_000_000, 0.80)
    assert not requires_fallback(2_500_000, 0.50)


@pytest.mark.parametrize("rate,duty", [(0, .5), (4_000_000, -0.1),
                                        (4_000_000, 1.1)])
def test_fallback_rejects_invalid_measurements(rate, duty):
    with pytest.raises(ValueError):
        requires_fallback(rate, duty)


def test_hybrid_watcher_runs_bounded_tracker_ensemble_and_coherent_iq_gate():
    source = (__import__("pathlib").Path(__file__).parents[1]/
              "scripts/starlink_hybrid_watch.sh").read_text()
    assert 'tracker_max_windows="${TRACKER_MAX_WINDOWS:-4}"' in source
    assert 'tracker_args=(doppler-trackers' in source
    assert '--plot "$root/plots/$stem-trackers.png"' in source
    assert '--drift-step-hz-s 1000' in source
    assert 'tracker_args+=(--window "$tracker_window")' in source
    assert 'tracker_args+=(--passes "$pass_catalog")' in source
    assert '--iq-evidence-output "$iq_stage"' in source
    assert 'leo-radio doppler-iq-track' in source
    assert 'should_retain_iq' in source


def test_iq_gate_accepts_legacy_or_tracker_ensemble_qualification():
    empty = {"joint_events": []}
    legacy = {"joint_events": [{"qualification": {"qualified": True}}]}
    ensemble = {"joint_tracks": [{"qualified": True}]}
    assert should_retain_iq(legacy, None)
    assert should_retain_iq(empty, ensemble)
    assert not should_retain_iq(empty, {"joint_tracks": [{"qualified": False}]})


def test_pass_refresh_uses_archived_tles_uv_and_atomic_promotion():
    source = (__import__("pathlib").Path(__file__).parents[1]/
              "scripts/refresh_starlink_passes.sh").read_text()
    assert "uv_bin" in source and "run --active --no-sync" in source
    assert 'latest="$archive_dir/latest.json"' in source
    assert 'temporary="${output}.next"' in source
    assert 'mv "$temporary" "$output"' in source


def test_radio_archive_is_age_gated_low_priority_and_recoverable():
    source = (__import__("pathlib").Path(__file__).parents[1]/
              "scripts/archive_radio_artifacts.sh").read_text()
    assert 'ARCHIVE_MINIMUM_AGE_MINUTES:-720' in source
    assert "mountpoint -q /mnt/qnap01" in source
    assert "--remove-source-files" in source and "--ignore-existing" in source
    assert "nice -n 19 ionice -c 3 rsync" in source
    assert '-mmin "+$minimum_age_minutes"' in source
    assert "--dry-run" in source
