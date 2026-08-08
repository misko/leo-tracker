import hashlib
import json

import numpy as np
import pytest

from leo_tracker.radio.beacon.artifact import capture_beacon_iq
from leo_tracker.radio.beacon.frame_tracking import (
    FRAME_TRACK_SCHEMA, conditioned_frame_observations, track_conditioned_frames)
from leo_tracker.radio.beacon.pilots import edge_pilot_frame
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S
from leo_tracker.radio.cli import main
from leo_tracker.radio.paired import PairedSampleBlock


RATE = 2_500_000


def _signal(duration_s, cfo_hz, *, epoch=137, seed=3, active_every=1):
    count = round(duration_s * RATE)
    template = edge_pilot_frame(RATE, "lower")
    values = np.zeros(count, np.complex64)
    frame = 0
    while True:
        start = epoch + round(frame * RATE * STARLINK_FRAME_DURATION_S)
        if start + template.size > count:
            break
        if frame % active_every == 0:
            indexes = np.arange(template.size) + start
            values[start:start + template.size] += 500 * template * np.exp(
                2j * np.pi * cfo_hz * indexes / RATE)
        frame += 1
    rng = np.random.default_rng(seed)
    values += (rng.normal(size=count) + 1j * rng.normal(size=count)) * 20
    return values


def _chirped_signal(duration_s, cfo_hz, cfo_rate_hz_s, *, epoch=137, seed=3):
    values = _signal(duration_s, 0, epoch=epoch, seed=seed)
    indexes = np.arange(values.size)
    times = indexes / RATE
    return values * np.exp(2j * np.pi * (
        cfo_hz * times + .5 * cfo_rate_hz_s * times**2))


def test_conditioned_frame_phase_tracks_cfo_at_750_hz():
    cfo = 12_345.0
    result = conditioned_frame_observations(_signal(.08, cfo), RATE,
        epoch_sample=137, coarse_cfo_hz=12_300, minimum_margin=.005)

    valid = result["valid"]
    assert len(result["time_s"]) >= 55
    assert np.mean(valid) > .95
    assert np.median(result["exact_score"][valid]) > .9
    assert np.median(result["score_margin"][valid]) > .5
    assert np.median(result["frequency_offset_hz"][valid]) == pytest.approx(cfo, abs=2)


def test_conditioned_frame_phase_tracks_a_predicted_doppler_rate():
    cfo = 12_345.0; rate_hz_s = -4_200.0
    result = conditioned_frame_observations(
        _chirped_signal(.2, cfo, rate_hz_s), RATE, epoch_sample=137,
        coarse_cfo_hz=cfo, coarse_cfo_rate_hz_s=rate_hz_s,
        coarse_reference_sample=0, minimum_margin=.005)

    valid = result["valid"]
    expected = cfo + rate_hz_s * result["time_s"]
    assert np.mean(valid) > .95
    assert result["frequency_offset_hz"][valid] == pytest.approx(
        expected[valid], abs=3)


def test_conditioned_frame_cfo_ignores_inactive_neighbor_frames():
    cfo = 12_345.0
    result = conditioned_frame_observations(
        _signal(.3, cfo, active_every=50), RATE,
        epoch_sample=137, coarse_cfo_hz=12_300, minimum_margin=.05)

    valid = result["valid"]
    assert 3 <= np.count_nonzero(valid) <= 6
    assert np.median(result["score_margin"][valid]) > .5
    assert result["frequency_offset_hz"][valid] == pytest.approx(cfo, abs=3)


def test_conditioned_frame_artifact_dual_rx_e2e(tmp_path, capsys):
    duration = .22
    cfo = (12_345.0, 12_405.0)
    rx = [_signal(duration, value, seed=10 + index)
          for index, value in enumerate(cfo)]
    count = len(rx[0])
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(rx[0], rx[1], 0,
        1_700_000_000_000_000_000)], capture, sample_rate_hz=RATE,
        center_frequency_hz=1_709_687_500, bandwidth_hz=2_500_000,
        duration_s=duration, lnb_lo_hz=9_750_000_000, chunk_s=duration,
        metadata={"channel_number": 4, "region": "lower-edge"})
    period = RATE * STARLINK_FRAME_DURATION_S
    checks = []
    for check_index, start in enumerate((0, round(.1 * RATE))):
        frame = int(np.ceil((start - 137) / period))
        local_epoch = 137 + round(frame * period) - start
        checks.append({"start_s": start / RATE, "start_sample": start,
            "duration_s": .01, "candidate": True,
            "receiver_candidates": [True, True], "epoch_difference_samples": 0,
            "receivers": [{"acquisition": {"selected_epoch_sample": local_epoch,
                "match_score_margin": .1},
                "pilot": {"frequency_offset_hz": value, "score_margin": .1}}
                for value in cfo]})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()),
                                    "checks": checks}))
    output = tmp_path / "frames.json"; samples = tmp_path / "frames.npz"

    assert main(["starlink-beacon-frame-track", str(capture), str(followup),
        str(output), "--samples", str(samples)]) == 0

    cli_result = json.loads(capsys.readouterr().out)
    report = json.loads(output.read_text())
    assert report["schema"] == FRAME_TRACK_SCHEMA
    assert report["configuration"]["maximum_extension_s"] == 60
    assert cli_result["dual_valid_frame_count"] > 140
    assert report["samples"]["sha256"] == hashlib.sha256(samples.read_bytes()).hexdigest()
    with np.load(samples, allow_pickle=False) as arrays:
        valid = np.all(arrays["valid"], axis=1)
        assert arrays["frequency_offset_hz"].shape[1] == 2
        assert np.median(arrays["frequency_offset_hz"][valid], axis=0) == pytest.approx(
            cfo, abs=3)

    calibrated = tmp_path / "track.json"
    assert main(["starlink-beacon-track", str(capture), str(followup),
        str(calibrated), "--measurement-source", "conditioned_frames",
        "--frame-track", str(output), "--maximum-gap-s", ".2"]) == 0
    capsys.readouterr()
    track = json.loads(calibrated.read_text())
    assert track["configuration"]["measurement_source"] == "conditioned_frames"
    assert track["source_frame_track"] == str(output.resolve())
    assert track["summary"]["dual_valid_observation_count"] >= 2
    assert track["tracks"][0]["summary"]["conditioned_frame_count"] > 140
    assert all(item["consensus"]["valid"]
               for item in track["tracks"][0]["observations"])


def test_single_exact_seed_extends_full_frame_lock_through_unsearched_iq(tmp_path):
    duration = 1.0
    cfo = (12_345.0, 12_405.0)
    rx = [_signal(duration, value, seed=20 + receiver)
          for receiver, value in enumerate(cfo)]
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(rx[0], rx[1], 0,
        1_700_000_000_000_000_000)], capture, sample_rate_hz=RATE,
        center_frequency_hz=1_709_687_500, bandwidth_hz=2_500_000,
        duration_s=duration, lnb_lo_hz=9_750_000_000, chunk_s=duration,
        metadata={"channel_number": 4, "region": "lower-edge"})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()), "checks": [{
        "start_s": 0, "start_sample": 0, "duration_s": .01, "candidate": True,
        "receiver_candidates": [True, True], "epoch_difference_samples": 0,
        "receivers": [{"acquisition": {"selected_epoch_sample": 137,
            "match_score_margin": .1}, "pilot": {"frequency_offset_hz": value,
            "score_margin": .1}} for value in cfo]}]}))
    output = tmp_path / "frames.json"; samples = tmp_path / "frames.npz"

    report = track_conditioned_frames(capture, followup, output, samples,
        maximum_extension_s=.8)

    assert report["summary"]["seed_count"] == 1
    assert report["summary"]["extension_accepted_window_count"] >= 7
    assert report["summary"]["measured_span_s"] > .75
    with np.load(samples, allow_pickle=False) as arrays:
        valid = np.all(arrays["valid"], axis=1)
        assert np.median(arrays["frequency_offset_hz"][valid], axis=0) == pytest.approx(
            cfo, abs=3)


def test_propagated_lock_stops_on_noise_without_inventing_frames(tmp_path):
    duration = 1.0; cfo = (12_345.0, 12_405.0)
    rng = np.random.default_rng(99)
    rx = []
    for receiver, value in enumerate(cfo):
        signal = _signal(.1, value, seed=30 + receiver)
        noise = (rng.normal(size=round(.9 * RATE)) +
                 1j * rng.normal(size=round(.9 * RATE))) * 20
        rx.append(np.concatenate((signal, noise)).astype(np.complex64))
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(rx[0], rx[1], 0,
        1_700_000_000_000_000_000)], capture, sample_rate_hz=RATE,
        center_frequency_hz=1_709_687_500, bandwidth_hz=2_500_000,
        duration_s=duration, lnb_lo_hz=9_750_000_000, chunk_s=duration,
        metadata={"channel_number": 4, "region": "lower-edge"})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()), "checks": [{
        "start_s": 0, "start_sample": 0, "duration_s": .01, "candidate": True,
        "receiver_candidates": [True, True], "epoch_difference_samples": 0,
        "receivers": [{"acquisition": {"selected_epoch_sample": 137,
            "match_score_margin": .1}, "pilot": {"frequency_offset_hz": value,
            "score_margin": .1}} for value in cfo]}]}))

    report = track_conditioned_frames(capture, followup, tmp_path / "frames.json",
        tmp_path / "frames.npz", maximum_extension_s=.8,
        maximum_missed_windows=3)

    assert report["summary"]["extension_accepted_window_count"] == 0
    assert report["extensions"][0]["terminal_missed_window_count"] == 3
    assert report["summary"]["measured_span_s"] < .11


def test_propagated_lock_reacquires_after_bounded_half_second_silence(tmp_path):
    duration = 1.0; cfo = (12_345.0, 12_405.0)
    rng = np.random.default_rng(109)
    rx = []
    for receiver, value in enumerate(cfo):
        signal = _signal(duration, value, seed=40 + receiver)
        first = round(.2 * RATE); stop = round(.7 * RATE)
        signal[first:stop] = (rng.normal(size=stop-first) +
                              1j * rng.normal(size=stop-first)) * 20
        rx.append(np.asarray(signal, np.complex64))
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(rx[0], rx[1], 0,
        1_700_000_000_000_000_000)], capture, sample_rate_hz=RATE,
        center_frequency_hz=1_709_687_500, bandwidth_hz=2_500_000,
        duration_s=duration, lnb_lo_hz=9_750_000_000, chunk_s=duration,
        metadata={"channel_number": 4, "region": "lower-edge"})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()), "checks": [{
        "start_s": 0, "start_sample": 0, "duration_s": .01, "candidate": True,
        "receiver_candidates": [True, True], "epoch_difference_samples": 0,
        "receivers": [{"acquisition": {"selected_epoch_sample": 137,
            "match_score_margin": .1}, "pilot": {"frequency_offset_hz": value,
            "score_margin": .1}} for value in cfo]}]}))

    report = track_conditioned_frames(capture, followup, tmp_path / "frames.json",
        tmp_path / "frames.npz", maximum_extension_s=.9,
        maximum_missed_windows=10)

    assert report["summary"]["extension_accepted_window_count"] >= 3
    assert report["summary"]["measured_span_s"] > .8
    assert report["extensions"][0]["terminal_missed_window_count"] == 0


def test_sparse_two_percent_prf_extends_and_emits_10_hz_measurements(tmp_path):
    duration = 1.0
    cfo = (12_345.0, 12_405.0)
    rx = [_signal(duration, value, seed=50 + receiver, active_every=50)
          for receiver, value in enumerate(cfo)]
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(rx[0], rx[1], 0,
        1_700_000_000_000_000_000)], capture, sample_rate_hz=RATE,
        center_frequency_hz=1_709_687_500, bandwidth_hz=2_500_000,
        duration_s=duration, lnb_lo_hz=9_750_000_000, chunk_s=duration,
        metadata={"channel_number": 4, "region": "lower-edge"})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()), "checks": [{
        "start_s": 0, "start_sample": 0, "duration_s": .01, "candidate": True,
        "receiver_candidates": [True, True], "epoch_difference_samples": 0,
        "receivers": [{"acquisition": {"selected_epoch_sample": 137,
            "match_score_margin": .1}, "pilot": {"frequency_offset_hz": value,
            "score_margin": .1}} for value in cfo]}]}))
    frame_output = tmp_path / "frames.json"
    frame_samples = tmp_path / "frames.npz"

    frame_report = track_conditioned_frames(
        capture, followup, frame_output, frame_samples, maximum_extension_s=.8)

    assert frame_report["summary"]["extension_accepted_window_count"] >= 6
    assert frame_report["summary"]["measured_span_s"] > .7
    assert any(item.get("accepted_window_count", 0) for item in
               frame_report["extensions"])
    calibrated = tmp_path / "track.json"
    assert main(["starlink-beacon-track", str(capture), str(followup),
        str(calibrated), "--measurement-source", "conditioned_frames",
        "--frame-track", str(frame_output), "--maximum-gap-s", ".3"]) == 0
    track = json.loads(calibrated.read_text())
    assert track["summary"]["dual_valid_observation_count"] >= 6
    assert track["summary"]["longest_dual_valid_duration_s"] > .6
