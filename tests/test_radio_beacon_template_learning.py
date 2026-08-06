import hashlib
import json

import numpy as np
import pytest

from leo_tracker.radio.beacon.artifact import capture_beacon_iq
from leo_tracker.radio.beacon.pilots import edge_pilot_frame
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S
from leo_tracker.radio.beacon.template_learning import (
    LEARNED_BEACON_SCHEMA, estimate_repeating_template, load_learned_beacon,
    normalized_matches, validate_template)
from leo_tracker.radio.cli import main
from leo_tracker.radio.paired import PairedSampleBlock


RATE = 2_500_000


def test_robust_template_learning_recovers_held_out_repeating_waveform():
    rng = np.random.default_rng(9)
    count = 600
    truth = rng.normal(size=count) + 1j * rng.normal(size=count)
    truth /= np.linalg.norm(truth)
    anchor = truth.copy(); anchor[80:] = 0
    gains = ((.7 + .6 * rng.random(90)) *
             np.exp(2j * np.pi * rng.random(90)))
    frames = gains[:, None] * truth[None, :] * 20
    frames += rng.normal(size=frames.shape) + 1j * rng.normal(size=frames.shape)
    # A minority of frames contains unrelated high-power user data.
    frames[:12] += 4 * (rng.normal(size=(12, count)) +
                        1j * rng.normal(size=(12, count)))

    learned, details = estimate_repeating_template(frames[:60], anchor)
    validation = validate_template(frames[60:], learned, anchor)

    assert details["retained_frame_count"] >= 4
    assert abs(np.vdot(learned, truth)) > .9
    assert validation["learned_beats_control_fraction"] > .9
    assert validation["learned_to_pilot_amplitude_ratio_db"] > 4


def test_noise_learned_on_training_split_does_not_validate_on_independent_noise():
    rng = np.random.default_rng(11)
    anchor = rng.normal(size=400) + 1j * rng.normal(size=400)
    train = rng.normal(size=(50, 400)) + 1j * rng.normal(size=(50, 400))
    held = rng.normal(size=(80, 400)) + 1j * rng.normal(size=(80, 400))

    learned, _ = estimate_repeating_template(train, anchor)
    validation = validate_template(held, learned, anchor)

    assert validation["learned_minus_control_median"] < .02
    assert validation["learned_beats_control_fraction"] < .7


def _full_beacon_signal(duration_s, cfo_hz, receiver, *, epoch=137):
    rng = np.random.default_rng(50 + receiver)
    count = round(duration_s * RATE)
    pilot = edge_pilot_frame(RATE, "lower").astype(np.complex128)
    # Persistent structure absent from the published eight-pilot template.
    extra = rng.normal(size=pilot.size) + 1j * rng.normal(size=pilot.size)
    extra -= np.vdot(pilot, extra) / np.vdot(pilot, pilot) * pilot
    extra *= np.linalg.norm(pilot) / np.linalg.norm(extra) * 2.5
    template = pilot + extra
    values = np.zeros(count, np.complex128)
    frame = 0
    while True:
        start = epoch + round(frame * RATE * STARLINK_FRAME_DURATION_S)
        if start + template.size > count:
            break
        indexes = start + np.arange(template.size)
        values[start:start+template.size] += template * np.exp(
            2j * np.pi * cfo_hz * indexes / RATE)
        frame += 1
    values += .25 * (rng.normal(size=count) + 1j * rng.normal(size=count))
    return np.asarray(values, np.complex64)


def test_learned_beacon_cli_artifact_uses_held_out_check_and_verifies_hash(tmp_path,
                                                                          capsys):
    duration = .23
    cfo = (12_300.0, 12_380.0)
    rx = [_full_beacon_signal(duration, value, receiver)
          for receiver, value in enumerate(cfo)]
    capture = tmp_path / "capture"
    capture_beacon_iq([PairedSampleBlock(rx[0], rx[1], 0,
        1_700_000_000_000_000_000)], capture, sample_rate_hz=RATE,
        center_frequency_hz=1_709_687_500, bandwidth_hz=RATE,
        duration_s=duration, lnb_lo_hz=9_750_000_000, chunk_s=duration,
        metadata={"channel_number": 4, "region": "lower-edge"})
    period = RATE * STARLINK_FRAME_DURATION_S
    checks = []
    for start in (0, round(.11 * RATE)):
        frame = int(np.ceil((start - 137) / period))
        epoch = 137 + round(frame * period) - start
        checks.append({"start_s": start / RATE, "start_sample": start,
            "candidate": True, "receiver_candidates": [True, True],
            "epoch_difference_samples": 0,
            "receivers": [{"acquisition": {"selected_epoch_sample": epoch,
                "subband_rate_hz": RATE, "selected_center_offset_hz": 0},
                "pilot": {"frequency_offset_hz": value}}
                for value in cfo]})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"capture": str(capture.resolve()),
                                    "checks": checks}))
    output = tmp_path / "beacon.json"; samples = tmp_path / "beacon.npz"

    assert main(["starlink-beacon-template-learn", str(capture), str(followup),
        str(output), "--samples", str(samples), "--maximum-frames", "200"]) == 0
    cli = json.loads(capsys.readouterr().out)
    report, arrays = load_learned_beacon(output)

    assert report["schema"] == LEARNED_BEACON_SCHEMA
    assert report["samples_sha256"] == hashlib.sha256(samples.read_bytes()).hexdigest()
    assert cli["qualified"] and report["summary"]["qualified"]
    assert report["summary"]["minimum_held_out_frame_count"] > 60
    assert report["summary"]["minimum_learned_to_pilot_amplitude_ratio_db"] > 3
    assert arrays["template_rx0"].shape == (round(RATE * STARLINK_FRAME_DURATION_S),)
    assert normalized_matches(arrays["template_rx0"][None, :],
                              arrays["template_rx0"])[0] == pytest.approx(1)

    frame_report = tmp_path / "frames.json"; frame_samples = tmp_path / "frames.npz"
    assert main(["starlink-beacon-frame-track", str(capture), str(followup),
        str(frame_report), "--samples", str(frame_samples),
        "--beacon-template", str(output)]) == 0
    capsys.readouterr()
    tracked = json.loads(frame_report.read_text())
    assert tracked["configuration"]["template_source"] == "learned_bandpass_beacon"
    assert tracked["source_learned_beacon"] == str(output.resolve())
    assert tracked["summary"]["dual_valid_frame_count"] > 100

    data = bytearray(samples.read_bytes()); data[-20] ^= 1; samples.write_bytes(data)
    with pytest.raises(ValueError, match="checksum"):
        load_learned_beacon(output)
