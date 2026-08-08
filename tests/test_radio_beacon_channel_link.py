from datetime import datetime, timezone
import json

from leo_tracker.radio.beacon.channel_link import link_channel_tracks
from leo_tracker.radio.cli import main


def _report(path, channel, start_s, stop_s, acceleration, *, base=100_000,
            capture=None):
    rf = 10_709_687_500 + (channel - 1) * 250_000_000
    slope = -acceleration * rf / 299_792_458.0
    observations = []
    index = 0
    time_s = start_s
    while time_s <= stop_s + 1e-9:
        cfo = base + slope * (time_s - start_s)
        observations.append({
            "utc": datetime.fromtimestamp(time_s, timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            "consensus": {"valid": True, "receiver_referenced_cfo_hz": cfo,
                          "frequency_sigma_hz": 20},
            "receivers": [{"receiver": receiver, "valid": True,
                "frequency_offset_hz": cfo + 500 * receiver,
                "formal_sigma_hz": 20} for receiver in range(2)]})
        index += 1; time_s = start_s + index * .1
    path.write_text(json.dumps({
        "schema": "leo-tracker.starlink-continuous-track/v1",
        "capture": capture or f"capture-{channel}",
        "capture_manifest": {"metadata": {"channel_number": channel}},
        "signal": {"nominal_rf_hz": rf},
        "tracks": [{"track_id": "track-000", "observations": observations}]}))


def test_channel_link_normalizes_doppler_rate_by_rf_and_keeps_ambiguous_arc_separate(
        tmp_path, capsys):
    first = tmp_path / "ch1.json"; second = tmp_path / "ch3.json"
    unrelated = tmp_path / "ch2.json"
    _report(first, 1, 1_700_000_000, 1_700_000_005, 105)
    _report(second, 3, 1_700_000_009, 1_700_000_025, 108, base=-80_000)
    _report(unrelated, 2, 1_700_000_009, 1_700_000_015, -70, base=40_000)
    output = tmp_path / "linked.json"

    assert main(["starlink-beacon-channel-link", str(output), str(first),
                 str(second), str(unrelated)]) == 0
    cli = json.loads(capsys.readouterr().out)
    report = json.loads(output.read_text())

    assert cli["multi_segment_hypothesis_count"] == 1
    assert report["summary"]["hypothesis_count"] == 2
    linked = max(report["tracks"],
                 key=lambda item: item["summary"]["source_segment_count"])
    assert linked["summary"]["channel_numbers"] == [1, 3]
    assert linked["summary"]["dual_valid_duration_s"] == 25
    assert {item["nominal_rf_hz"] for item in linked["observations"]} == {
        10_709_687_500, 11_209_687_500}
    assert {item["nuisance_group"] for item in linked["observations"]} == {1, 3}


def test_channel_link_rejects_ambiguous_predecessors_instead_of_forcing_a_merge(tmp_path):
    first = tmp_path / "a.json"; second = tmp_path / "b.json"; third = tmp_path / "c.json"
    _report(first, 1, 1_700_000_000, 1_700_000_004, 100)
    _report(second, 2, 1_700_000_000, 1_700_000_004, 104)
    _report(third, 3, 1_700_000_007, 1_700_000_012, 102)

    report = link_channel_tracks([first, second, third], tmp_path / "out.json")

    assert report["summary"]["hypothesis_count"] == 3
    decision = next(item for item in report["link_decisions"]
                    if item["source"] == str(third.resolve()))
    assert decision["action"] == "new_hypothesis"
    assert decision["reason"] == "ambiguous_continuation"


def test_channel_link_preserves_outage_but_links_smooth_same_tuning_fragments(tmp_path):
    first = tmp_path / "first.json"; second = tmp_path / "second.json"
    _report(first, 4, 1_700_000_000, 1_700_000_004, 100)
    _report(second, 4, 1_700_000_024, 1_700_000_028, 112)
    original = json.loads(first.read_text())
    continuation = json.loads(second.read_text())["tracks"][0]
    continuation["track_id"] = "track-001"
    # Continue the first fragment's CFO under a small, physically smooth
    # acceleration change. No samples are invented during the 20-second gap.
    slope = -112 * original["signal"]["nominal_rf_hz"] / 299_792_458.0
    prior = original["tracks"][0]["observations"][-1]["consensus"][
        "receiver_referenced_cfo_hz"]
    for item in continuation["observations"]:
        elapsed = (datetime.fromisoformat(item["utc"].replace("Z", "+00:00")).timestamp()
                   - 1_700_000_004)
        cfo = prior + slope * elapsed
        item["consensus"]["receiver_referenced_cfo_hz"] = cfo
        for receiver in item["receivers"]:
            receiver["frequency_offset_hz"] = cfo + 500 * receiver["receiver"]
    original["tracks"].append(continuation)
    first.write_text(json.dumps(original))

    report = link_channel_tracks([first], tmp_path / "linked.json")

    assert report["summary"]["multi_segment_hypothesis_count"] == 1
    linked = report["tracks"][0]
    assert linked["summary"]["dual_valid_duration_s"] == 28
    assert linked["summary"]["measured_segment_duration_s"] == 8
    assert linked["summary"]["outage_duration_s"] == 20
    assert linked["summary"]["source_segment_count"] == 2
    assert len(linked["observations"]) == 82
    assert not any(1_700_000_004 < datetime.fromisoformat(
        item["utc"].replace("Z", "+00:00")).timestamp() < 1_700_000_024
        for item in linked["observations"])
    decision = report["link_decisions"][1]
    assert decision["same_tuning"] is True
    assert decision["same_tuning_quadratic_residual_rms_hz"] < 2_000
    assert decision["same_tuning_range_jerk_m_s3"] < 5


def test_channel_link_rejects_same_tuning_offset_jump_despite_matching_rate(tmp_path):
    path = tmp_path / "fragments.json"
    other = tmp_path / "other.json"
    _report(path, 4, 1_700_000_000, 1_700_000_004, 100)
    _report(other, 4, 1_700_000_024, 1_700_000_028, 100, base=400_000)
    report = json.loads(path.read_text())
    continuation = json.loads(other.read_text())["tracks"][0]
    continuation["track_id"] = "track-001"
    report["tracks"].append(continuation)
    path.write_text(json.dumps(report))

    linked = link_channel_tracks([path], tmp_path / "linked.json")

    assert linked["summary"]["hypothesis_count"] == 2
    assert linked["summary"]["multi_segment_hypothesis_count"] == 0


def test_channel_link_requires_quadratic_continuity_across_capture_boundary(tmp_path):
    first = tmp_path / "first.json"; second = tmp_path / "second.json"
    _report(first, 4, 1_700_000_000, 1_700_000_004, 100,
            capture="capture-a")
    _report(second, 4, 1_700_000_014, 1_700_000_020, 106,
            capture="capture-b", base=350_000)

    rejected = link_channel_tracks([first, second], tmp_path / "rejected.json")

    assert rejected["summary"]["hypothesis_count"] == 2
    decision = rejected["link_decisions"][1]
    assert decision["action"] == "new_hypothesis"
    assert decision["reason"] == "no_compatible_predecessor"

    # Make the second recorder artifact continue the first artifact's measured
    # CFO. A phase reset is harmless; a frequency discontinuity is not.
    first_report = json.loads(first.read_text())
    second_report = json.loads(second.read_text())
    prior = first_report["tracks"][0]["observations"][-1]["consensus"][
        "receiver_referenced_cfo_hz"]
    rf = first_report["signal"]["nominal_rf_hz"]
    slope = -106 * rf / 299_792_458.0
    for item in second_report["tracks"][0]["observations"]:
        elapsed = (datetime.fromisoformat(item["utc"].replace("Z", "+00:00"))
                   .timestamp() - 1_700_000_004)
        cfo = prior + slope * elapsed
        item["consensus"]["receiver_referenced_cfo_hz"] = cfo
        for receiver in item["receivers"]:
            receiver["frequency_offset_hz"] = cfo + 500 * receiver["receiver"]
    second.write_text(json.dumps(second_report))

    accepted = link_channel_tracks([first, second], tmp_path / "accepted.json")

    assert accepted["summary"]["multi_segment_hypothesis_count"] == 1
    assert accepted["tracks"][0]["summary"]["dual_valid_duration_s"] == 20
    assert accepted["link_decisions"][1]["same_tuning"] is True


def test_fragment_linker_makes_two_subthreshold_arcs_associable_without_filling_gap(
        tmp_path):
    """Two <20 s detections may jointly expose enough Doppler curvature for TLE work."""
    path = tmp_path / "fragments.json"; continuation_path = tmp_path / "later.json"
    _report(path, 4, 1_700_000_000, 1_700_000_012, 100)
    _report(continuation_path, 4, 1_700_000_028, 1_700_000_040, 108)
    report = json.loads(path.read_text())
    continuation = json.loads(continuation_path.read_text())["tracks"][0]
    continuation["track_id"] = "track-001"
    prior = report["tracks"][0]["observations"][-1]["consensus"][
        "receiver_referenced_cfo_hz"]
    rf = report["signal"]["nominal_rf_hz"]
    slope = -108 * rf / 299_792_458.0
    for item in continuation["observations"]:
        elapsed = (datetime.fromisoformat(item["utc"].replace("Z", "+00:00"))
                   .timestamp() - 1_700_000_012)
        cfo = prior + slope * elapsed
        item["consensus"]["receiver_referenced_cfo_hz"] = cfo
        for receiver in item["receivers"]:
            receiver["frequency_offset_hz"] = cfo + 500 * receiver["receiver"]
    report["tracks"].append(continuation)
    path.write_text(json.dumps(report))

    linked = link_channel_tracks([path], tmp_path / "linked.json")

    source_durations = [(datetime.fromisoformat(
        track["observations"][-1]["utc"].replace("Z", "+00:00")) -
        datetime.fromisoformat(track["observations"][0]["utc"].replace(
            "Z", "+00:00"))).total_seconds() for track in report["tracks"]]
    assert source_durations == [12, 12]
    assert linked["summary"]["multi_segment_hypothesis_count"] == 1
    hypothesis = linked["tracks"][0]
    assert hypothesis["summary"]["dual_valid_duration_s"] == 40
    assert hypothesis["summary"]["measured_segment_duration_s"] == 24
    assert hypothesis["summary"]["outage_duration_s"] == 16
    assert hypothesis["summary"]["source_segment_count"] == 2
    assert not any(1_700_000_012 < datetime.fromisoformat(
        item["utc"].replace("Z", "+00:00")).timestamp() < 1_700_000_028
        for item in hypothesis["observations"])


def test_channel_link_excludes_tiny_fragment_from_long_wall_clock_hypothesis(tmp_path):
    path = tmp_path / "fragments.json"
    later = tmp_path / "later.json"
    _report(path, 4, 1_700_000_000, 1_700_000_000.3, 100)
    _report(later, 4, 1_700_000_009, 1_700_000_021, 105)
    report = json.loads(path.read_text())
    continuation = json.loads(later.read_text())["tracks"][0]
    continuation["track_id"] = "track-001"
    report["tracks"].append(continuation)
    path.write_text(json.dumps(report))

    linked = link_channel_tracks([path], tmp_path / "linked.json")

    assert linked["summary"]["source_track_count"] == 2
    assert linked["summary"]["ignored_short_source_track_count"] == 1
    assert linked["summary"]["hypothesis_count"] == 1
    assert linked["tracks"][0]["summary"]["dual_valid_duration_s"] == 12
