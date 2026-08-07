import json
from pathlib import Path

import numpy as np
import pytest

from leo_tracker.radio.beacon.artifact import capture_beacon_iq
from leo_tracker.radio.beacon.continuous import (
    TRACK_SCHEMA, _candidate_seeds, _reacquisition_span, _sample_utc_ns,
    measure_full_frame_window, track_capture)
import leo_tracker.radio.beacon.continuous as continuous_module
from leo_tracker.radio.beacon.pilots import edge_pilot_frame
from leo_tracker.radio.paired import PairedSampleBlock


RATE = 2_500_000.0


def _beacon_signal(duration_s: float, *, epoch: int, cfo_hz: float,
                   drift_hz_s: float, noise_std: float, seed: int) -> np.ndarray:
    count = round(duration_s * RATE)
    frame = edge_pilot_frame(RATE, "lower")
    signal = np.zeros(count, np.complex64)
    period = RATE / 750
    index = 0
    while True:
        start = epoch + round(index * period)
        if start + frame.size > count:
            break
        signal[start:start + frame.size] += frame * 500
        index += 1
    time_s = np.arange(count) / RATE
    phase = 2 * np.pi * (cfo_hz * time_s + .5 * drift_hz_s * time_s**2)
    signal *= np.exp(1j * phase)
    rng = np.random.default_rng(seed)
    signal += noise_std * (rng.normal(size=count) + 1j * rng.normal(size=count))
    return np.asarray(signal, np.complex64)


def test_full_frame_measurement_uses_every_frame_and_rejects_rolled_control():
    epoch, cfo = 313, 123_456.0
    signal = _beacon_signal(.1, epoch=epoch, cfo_hz=cfo,
                            drift_hz_s=0, noise_std=25, seed=3)
    result = measure_full_frame_window(
        signal, RATE, epoch_sample=epoch, predicted_cfo_hz=123_000,
        search_span_hz=2_000, search_step_hz=100)

    assert result["frame_count"] >= 70
    assert result["frequency_offset_hz"] == pytest.approx(cfo, abs=75)
    assert result["score_margin"] > .2
    assert not result["peak_at_search_boundary"]
    assert 0 < result["formal_sigma_hz"] <= 500


def test_full_frame_measurement_does_not_promote_noise():
    rng = np.random.default_rng(8)
    noise = (rng.normal(size=round(.1 * RATE)) +
             1j * rng.normal(size=round(.1 * RATE))).astype(np.complex64)
    result = measure_full_frame_window(
        noise, RATE, epoch_sample=313, predicted_cfo_hz=0)
    assert result["score_margin"] < .015


def test_production_frequency_grid_retains_sub_bin_cfo_accuracy():
    epoch, cfo = 313, 123_456.0
    signal = _beacon_signal(.1, epoch=epoch, cfo_hz=cfo,
                            drift_hz_s=-3_500, noise_std=25, seed=31)
    result = measure_full_frame_window(
        signal, RATE, epoch_sample=epoch, predicted_cfo_hz=123_000,
        search_span_hz=2_000, search_step_hz=200)

    assert result["frequency_offset_hz"] == pytest.approx(cfo - 175, abs=100)
    assert result["formal_sigma_hz"] <= 200
    assert result["score_margin"] > .15


def test_intermittent_acquisitions_group_by_predicted_cfo_not_timing_alias():
    checks = []
    for index, (time_s, cfo) in enumerate(((0.5, -87_000), (2.6, -95_400),
                                           (3.0, -150_000), (5.5, -107_800),
                                           (9.2, -124_400), (17.0, -155_600))):
        checks.append({"start_s": time_s, "candidate": True,
            "receiver_candidates": [True, True], "receivers": [{
                "acquisition": {"selected_epoch_sample": (313 + 700 * index) % 3333,
                                "match_score_margin": .2},
                "pilot": {"frequency_offset_hz": cfo - 40 * receiver,
                          "score_margin": .2}}
                for receiver in range(2)]})

    seeds = _candidate_seeds({"checks": checks}, RATE, maximum_gap_s=15)

    primary = min(seeds, key=lambda item: abs(item.cfo_hz[0] + 87_000))
    assert len(seeds) == 2
    assert primary.drift_hz_s == pytest.approx((-4_100, -4_100), abs=300)


def test_reacquisition_span_grows_with_outage_but_remains_bounded():
    assert _reacquisition_span(2_000, 15_000, 0) == 2_000
    assert _reacquisition_span(2_000, 15_000, 3.5) == 5_500
    assert _reacquisition_span(2_000, 15_000, 30) == 15_000


def test_sample_utc_uses_measured_chunk_time_instead_of_nominal_sample_clock():
    manifest = {"sample_rate_hz": 100.0, "created_utc_ns": 900,
        "stream_timing": {"first_read_start_utc_ns": 1_000,
            "read_count": 20, "maximum_read_duration_s": .12,
            "maximum_positive_host_gap_s": .02},
        "chunks": [
            {"first_sample_index": 0, "sample_count": 100,
             "first_utc_ns": 1_100, "last_utc_ns": 2_300},
            {"first_sample_index": 100, "sample_count": 100,
             "first_utc_ns": 2_300, "last_utc_ns": 3_800}]}

    midpoint, method, uncertainty = _sample_utc_ns(manifest, 50)
    later, later_method, _ = _sample_utc_ns(manifest, 150)

    assert midpoint == 1_700
    assert later == 3_050
    assert method == later_method == "chunk_host_midpoint_interpolation"
    assert uncertainty == pytest.approx(.08)


def test_sample_utc_prefers_per_refill_midpoints_when_available():
    manifest = {"sample_rate_hz": 100.0, "created_utc_ns": 900,
        "stream_timing": {"first_read_start_utc_ns": 1_000, "read_count": 3,
            "clock_samples": [
                {"first_sample_index": 0, "sample_count": 20,
                 "utc_ns": 1_000_000_000, "read_duration_ns": 200_000_000},
                {"first_sample_index": 20, "sample_count": 20,
                 "utc_ns": 1_250_000_000, "read_duration_ns": 220_000_000},
                {"first_sample_index": 40, "sample_count": 20,
                 "utc_ns": 1_600_000_000, "read_duration_ns": 240_000_000}]},
        "chunks": [{"first_sample_index": 0, "sample_count": 60,
            "first_utc_ns": 1_000_000_000, "last_utc_ns": 1_600_000_000}]}

    utc_ns, method, uncertainty = _sample_utc_ns(manifest, 40)

    assert utc_ns == 1_425_000_000
    assert method == "iio_read_midpoint_interpolation"
    assert uncertainty == pytest.approx(.11)


def test_sample_utc_falls_back_to_nominal_clock_without_host_read_brackets():
    manifest = {"sample_rate_hz": 100.0, "created_utc_ns": 900,
        "stream_timing": {}, "chunks": []}

    utc_ns, method, uncertainty = _sample_utc_ns(manifest, 50)

    assert utc_ns == 500_000_900
    assert method == "capture_manifest_created"
    assert uncertainty is None


def test_seed_collapse_preserves_bursts_across_long_frame_epoch_outage():
    checks = []
    for index, (time_s, cfo) in enumerate(((15.0, -90_000), (18.0, -100_500),
                                           (24.5, -123_250), (38.7, -172_950),
                                           (39.0, -174_000))):
        checks.append({"start_s": time_s, "candidate": True,
            "receiver_candidates": [True, True], "receivers": [{
                "acquisition": {"selected_epoch_sample": (313 + index * 517) % 3333,
                                "match_score_margin": .2 + .01 * index},
                "pilot": {"frequency_offset_hz": cfo - 3_800 * receiver,
                          "score_margin": .2}}
                for receiver in range(2)]})

    seeds = _candidate_seeds({"checks": checks}, RATE, maximum_gap_s=15)

    assert len(seeds) == 2
    assert seeds[0].time_s <= 24.5
    assert seeds[1].time_s >= 38.7
    assert all(seed.drift_hz_s[0] == pytest.approx(-3_500, abs=300)
               for seed in seeds)


def test_dual_seed_trajectory_ignores_single_receiver_outlier():
    checks = []
    for time_s, cfo, flags in ((1.0, -90_000, [True, True]),
                               (1.1, -135_000, [True, False]),
                               (1.2, -90_800, [True, True])):
        checks.append({"start_s": time_s, "candidate": True,
            "receiver_candidates": flags, "receivers": [{
                "acquisition": {"selected_epoch_sample": 313,
                                "match_score_margin": .2},
                "pilot": {"frequency_offset_hz": cfo - 3_800 * receiver,
                          "score_margin": .2}}
                for receiver in range(2)]})

    seeds = _candidate_seeds({"checks": checks}, RATE)

    assert len(seeds) == 1
    assert seeds[0].drift_hz_s == pytest.approx((-4_000, -4_000), abs=1)


def test_continuous_tracker_writes_10_hz_dual_receiver_observations(tmp_path):
    duration, epoch, drift = .8, 313, -3_000.0
    rx0 = _beacon_signal(duration, epoch=epoch, cfo_hz=80_000,
                         drift_hz_s=drift, noise_std=30, seed=10)
    rx1 = _beacon_signal(duration, epoch=epoch, cfo_hz=81_200,
                         drift_hz_s=drift, noise_std=40, seed=11)
    start_utc_ns = 1_800_000_000_000_000_000
    block = PairedSampleBlock(rx0, rx1, 0, start_utc_ns,
                              read_duration_ns=round(duration * 1e9))
    capture = tmp_path / "capture"
    capture_beacon_iq([block], capture, sample_rate_hz=RATE,
        center_frequency_hz=1_709_687_500, bandwidth_hz=2_300_000,
        duration_s=duration, lnb_lo_hz=9_750_000_000, chunk_s=1,
        metadata={"region": "lower-edge", "channel_number": 4,
                  "nominal_rf_hz": 11_459_687_500})

    seed_time = .1
    seed_cfo = [80_000 + drift * seed_time, 81_200 + drift * seed_time]
    check = {"start_s": seed_time, "candidate": True,
        "receiver_candidates": [True, True], "receivers": [{
            "acquisition": {"selected_epoch_sample": epoch,
                            "subband_rate_hz": RATE, "match_score_margin": .5},
            "pilot": {"frequency_offset_hz": seed_cfo[receiver],
                      "score_margin": .5}}
            for receiver in range(2)]}
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()),
                                    "checks": [check]}))
    output = tmp_path / "track.json"
    report = track_capture(capture, followup, output, maximum_gap_s=.2)

    assert report["schema"] == TRACK_SCHEMA
    assert report["configuration"]["search_step_hz"] == 200
    assert report["configuration"]["measurement_source"] == "periodic_epoch"
    assert report["summary"]["track_count"] == 1
    track = report["tracks"][0]
    valid = [item for item in track["observations"] if item["lock_valid"]]
    assert len(valid) >= 6
    assert track["summary"]["dual_valid_observation_count"] >= 6
    assert report["summary"]["longest_dual_valid_duration_s"] >= .5
    assert np.diff([item["time_s"] for item in valid]) == pytest.approx(.1)
    assert all(item["utc"].endswith("Z") for item in valid)
    assert track["relative_receiver_calibration"]["available"]
    assert track["relative_receiver_calibration"]["rx1_minus_rx0_offset_hz"] == \
        pytest.approx(1_200, abs=150)
    assert abs(track["relative_receiver_calibration"][
        "rx1_minus_rx0_drift_hz_s"]) < 500
    assert all(item["consensus"]["valid"] for item in valid)
    assert json.loads(output.read_text())["schema"] == TRACK_SCHEMA


def test_empty_followup_writes_valid_zero_track_full_coverage_result(tmp_path):
    duration = .02
    zeros = np.zeros(round(duration * RATE), np.complex64)
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(
        zeros, zeros, 0, 1_800_000_000_000_000_000,
        read_duration_ns=round(duration * 1e9))], capture,
        sample_rate_hz=RATE, center_frequency_hz=1_709_687_500,
        bandwidth_hz=2_500_000, duration_s=duration,
        lnb_lo_hz=9_750_000_000, chunk_s=1,
        metadata={"region": "lower-edge", "channel_number": 4,
                  "nominal_rf_hz": 11_459_687_500})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()),
                                     "checks": []}))

    report = track_capture(
        capture, followup, tmp_path / "track.json",
        measurement_source="periodic_epoch")

    assert report["schema"] == TRACK_SCHEMA
    assert report["configuration"]["measurement_source"] == "periodic_epoch"
    assert report["summary"]["seed_count"] == 0
    assert report["summary"]["track_count"] == 0


def test_ungrouped_conditioned_frames_write_nonfatal_zero_track_result(
        tmp_path, monkeypatch):
    duration = .02
    zeros = np.zeros(round(duration * RATE), np.complex64)
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(
        zeros, zeros, 0, 1_800_000_000_000_000_000,
        read_duration_ns=round(duration * 1e9))], capture,
        sample_rate_hz=RATE, center_frequency_hz=1_709_687_500,
        bandwidth_hz=2_500_000, duration_s=duration,
        lnb_lo_hz=9_750_000_000, chunk_s=1,
        metadata={"region": "lower-edge", "channel_number": 4,
                  "nominal_rf_hz": 11_459_687_500})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()),
                                     "checks": [{"candidate": True}]}))
    frame_track = tmp_path / "frame-track.json"
    frame_track.write_text("{}\n")
    monkeypatch.setattr(
        continuous_module, "_tracks_from_conditioned_frames",
        lambda *args, **kwargs: [])

    output = tmp_path / "track.json"
    report = track_capture(
        capture, followup, output, measurement_source="conditioned_frames",
        frame_track_path=frame_track)

    assert report["configuration"]["requested_measurement_source"] == \
        "conditioned_frames"
    assert report["configuration"]["measurement_source"] == "conditioned_frames"
    assert report["summary"]["seed_count"] == 0
    assert report["summary"]["track_count"] == 0
    assert report["summary"]["no_track_reason"] == \
        "no_grouped_dual_rx_observations"
    assert json.loads(output.read_text())["summary"]["track_count"] == 0


def test_dense_followup_epochs_become_calibrated_10_hz_track(tmp_path):
    duration = .8
    zeros = np.zeros(round(duration * RATE), np.complex64)
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(zeros, zeros, 0,
        1_800_000_000_000_000_000, read_duration_ns=round(duration * 1e9))],
        capture, sample_rate_hz=RATE, center_frequency_hz=1_709_687_500,
        bandwidth_hz=2_500_000, duration_s=duration, lnb_lo_hz=9_750_000_000,
        chunk_s=1, metadata={"region": "lower-edge", "channel_number": 4,
                             "nominal_rf_hz": 11_459_687_500})
    checks = []
    for index in range(6):
        time_s = .1 + index * .1
        checks.append({"start_s": time_s, "start_sample": round(time_s * RATE),
            "duration_s": .01, "candidate": True,
            "receiver_candidates": [True, True], "epoch_difference_samples": 0,
            "receivers": [{"pilot": {"frequency_offset_hz":
                80_000 + 1_200 * receiver - 3_000 * time_s,
                "score_margin": .1}, "acquisition": {
                    "selected_epoch_sample": (313 + 97 * index) % 3333,
                    "match_score_margin": .2,
                    "subband_rate_hz": RATE,
                    "exact_match": {"score": .3,
                        "searched_frequency_offsets_hz": [79_800, 80_000, 80_200]},
                    "control_match": {"score": .01}}}
                for receiver in range(2)]})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()),
                                    "checks": checks}))

    report = track_capture(capture, followup, tmp_path / "track.json",
                           maximum_gap_s=.5)

    assert report["configuration"]["measurement_source"] == "dense_followup"
    assert report["summary"]["track_count"] == 1
    track = report["tracks"][0]
    assert track["summary"]["dual_valid_observation_count"] == 6
    assert track["summary"]["dual_valid_duration_s"] == pytest.approx(.5)
    assert track["relative_receiver_calibration"]["rx1_minus_rx0_offset_hz"] == \
        pytest.approx(1_200)
    assert all(item["consensus"]["valid"] for item in track["observations"])
