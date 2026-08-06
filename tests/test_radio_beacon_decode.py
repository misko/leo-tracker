import json
from pathlib import Path

import numpy as np
import pytest

from leo_tracker.radio.cli import main
from leo_tracker.radio.beacon.artifact import capture_beacon_iq
from leo_tracker.radio.beacon.channels import (STARLINK_EDGE_PILOT_SUBCARRIERS,
    starlink_edge_pilot_offset_hz, subcarrier_offset_hz)
from leo_tracker.radio.beacon.decode import (SSS_HEX, demodulate_edge_window,
    sss_edge_symbols, sss_phase_states)
from leo_tracker.radio.beacon.pilots import (CYCLIC_PREFIX_DURATION_S,
    OFDM_SYMBOL_DURATION_S, edge_pilot_symbols)
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S
from leo_tracker.radio.paired import PairedSampleBlock


def _tone_design(rate, frame_start, symbol, frequencies):
    start = frame_start + round(symbol * rate * OFDM_SYMBOL_DURATION_S)
    stop = frame_start + round((symbol + 1) * rate * OFDM_SYMBOL_DURATION_S)
    local = ((np.arange(start, stop) - frame_start) / rate -
             symbol * OFDM_SYMBOL_DURATION_S)
    design = np.exp(2j * np.pi *
                    (local[:, None] - CYCLIC_PREFIX_DURATION_S) *
                    frequencies[None, :]) / np.sqrt(len(frequencies))
    return start, stop, design


def _decoded_fixture(*, seed=91, noise_std=.04, rate=2_500_000.0):
    duration, epoch, cfo = .01, round(347 * rate / 2_500_000), 83_700.0
    count = round(rate * duration)
    indexes = STARLINK_EDGE_PILOT_SUBCARRIERS["lower"]
    frequencies = np.asarray([subcarrier_offset_hz(index) -
        starlink_edge_pilot_offset_hz("lower") for index in indexes])
    pilots, sss = edge_pilot_symbols("lower"), sss_edge_symbols("lower")
    channel = np.asarray([.7 + .08j, .8 - .15j, .92 + .2j, 1.0 - .04j,
                          .88 + .25j, .75 - .2j, .68 + .12j, .61 - .08j])
    signal = np.zeros(count, np.complex64)
    frame = 0
    while True:
        frame_start = epoch + round(frame * rate * STARLINK_FRAME_DURATION_S)
        if frame_start + round(302 * rate * OFDM_SYMBOL_DURATION_S) > count:
            break
        phase = np.exp(1j * (.37 * frame + .05 * frame ** 2))
        start, stop, design = _tone_design(rate, frame_start, 1, frequencies)
        signal[start:stop] += design @ (sss * channel * phase)
        for symbol in range(2, 302):
            start, stop, design = _tone_design(rate, frame_start, symbol, frequencies)
            signal[start:stop] += design @ (pilots[symbol - 2] * channel * phase)
        frame += 1
    rng = np.random.default_rng(seed)
    signal += noise_std * (rng.normal(size=count) + 1j * rng.normal(size=count))
    signal *= np.exp(2j * np.pi * cfo * np.arange(count) / rate)
    return np.asarray(signal, np.complex64), rate, epoch, cfo, frame


def test_published_sss_sequence_and_edge_slices():
    assert len(SSS_HEX) == 510
    np.testing.assert_array_equal(sss_phase_states(tuple(range(2, 10))),
                                  [3, 0, 0, 0, 0, 2, 1, 1])
    assert sss_phase_states().shape == (1020,)
    for edge in ("lower", "upper"):
        symbols = sss_edge_symbols(edge)
        assert symbols.shape == (8,)
        np.testing.assert_allclose(np.abs(symbols), 1)
    with pytest.raises(ValueError):
        sss_phase_states((1,))


def test_narrow_decoder_recovers_held_out_pilots_and_sss():
    signal, rate, epoch, cfo, frames = _decoded_fixture()
    report, arrays = demodulate_edge_window(
        signal, rate, epoch_sample=epoch, carrier_offset_hz=cfo, edge="lower")
    assert report["pilot"]["frame_count"] == frames == 7
    assert report["pilot"]["hard_symbol_accuracy"] > .99
    assert report["sss"]["hard_symbol_accuracy"] > .95
    assert arrays["pilot_equalized"].shape == (300, 8)
    assert arrays["sss_equalized"].shape == (7, 8)
    assert arrays["pilot_correct"].dtype == np.bool_
    assert arrays["pilot_probabilities"].shape == (300, 8, 4)
    np.testing.assert_allclose(arrays["pilot_probabilities"].sum(axis=-1), 1,
                               atol=1e-6)
    assert report["pilot"]["effective_frame_count"] <= frames
    assert report["pilot"]["soft_mean_expected_probability"] > .9


def test_narrow_decoder_refines_residual_carrier_slope():
    signal, rate, epoch, cfo, _ = _decoded_fixture(noise_std=.02)
    report, _ = demodulate_edge_window(
        signal, rate, epoch_sample=epoch, carrier_offset_hz=cfo - 600,
        edge="lower")
    assert report["residual_cfo_refinement_hz"] == pytest.approx(600, abs=35)
    assert report["pilot"]["hard_symbol_accuracy"] > .98


def test_narrow_decoder_does_not_hard_decode_noise_as_known_code():
    rng = np.random.default_rng(109)
    noise = rng.normal(size=25_000) + 1j * rng.normal(size=25_000)
    report, _ = demodulate_edge_window(
        noise, 2_500_000, epoch_sample=347, carrier_offset_hz=83_700,
        edge="lower")
    assert report["pilot"]["hard_symbol_accuracy"] < .40
    assert report["sss"]["hard_symbol_accuracy"] < .45


def test_narrow_decoder_rejects_rate_below_full_pilot_span():
    with pytest.raises(ValueError, match="1875000"):
        demodulate_edge_window(
            np.zeros(20_000, np.complex64), 1_874_999,
            epoch_sample=0, carrier_offset_hz=0, edge="lower")


def test_decode_followup_writes_json_symbols_and_plot(tmp_path):
    signal, rate, epoch, cfo, _ = _decoded_fixture(noise_std=.02)
    capture_path = tmp_path / "capture"
    block = PairedSampleBlock(signal, signal * (.8 + .1j), 0,
                              1_700_000_000_000_000_000)
    capture_beacon_iq([block], capture_path, sample_rate_hz=rate,
        center_frequency_hz=1_709_687_500, bandwidth_hz=2_300_000,
        duration_s=.01, lnb_lo_hz=9_750_000_000, chunk_s=.01,
        metadata={"channel_number": 4, "region": "lower-edge"})
    receivers = []
    for receiver in range(2):
        receivers.append({"receiver": receiver,
            "acquisition": {"selected_epoch_sample": epoch,
                            "match_score_margin": .2},
            "pilot": {"edge": "lower", "frequency_offset_hz": cfo,
                      "score_margin": .08},
            "pss": {"peak_to_median": 2.4}})
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"checks": [{"start_s": 0,
        "duration_s": .01, "candidate": True, "qualified": True,
        "epoch_difference_samples": 0, "receivers": receivers}]}) + "\n")
    output, symbols, plot = (tmp_path / "decode.json", tmp_path / "symbols.npz",
                             tmp_path / "decode.png")
    assert main(["starlink-beacon-decode", str(capture_path), str(followup),
                 str(output), "--symbols", str(symbols), "--plot", str(plot)]) == 0
    saved = json.loads(output.read_text())
    assert saved["schema"] == "leo-tracker.starlink-edge-decode/v1"
    assert saved["combined"]["minimum_pilot_accuracy"] > .98
    assert saved["decoder_revision"] == 2
    assert saved["combined"]["soft_dual_rx"]["pilot"][
        "hard_symbol_accuracy"] > .98
    temporal = saved["combined"]["soft_dual_rx"]["pilot"]["temporal_qpsk"]
    assert temporal["frame_count"] >= 2
    assert len(temporal["mean_state_probabilities"]) == temporal["frame_count"]
    assert saved["waveform"]["decoded_sss_subcarriers_per_frame"] == 8
    assert saved["symbol_archive_bytes"] == symbols.stat().st_size
    assert len(saved["symbol_archive_sha256"]) == 64
    assert symbols.read_bytes().startswith(b"PK")
    with np.load(symbols, allow_pickle=False) as archived:
        assert archived["combined_pilot_frame_probabilities"].shape == (
            temporal["frame_count"], 300, 8, 4)
        assert archived["combined_pilot_frame_correct"].shape == (
            temporal["frame_count"], 300, 8)
    assert plot.read_bytes().startswith(b"\x89PNG")


def test_oversampled_decode_reports_same_window_downsample_control(tmp_path):
    signal, rate, epoch, cfo, _ = _decoded_fixture(noise_std=.02, rate=5_000_000)
    capture_path = tmp_path / "oversample"
    block = PairedSampleBlock(signal, signal * (.8 + .1j), 0,
                              1_700_000_000_000_000_000)
    capture_beacon_iq([block], capture_path, sample_rate_hz=rate,
        center_frequency_hz=1_709_687_500, bandwidth_hz=3_000_000,
        duration_s=.01, lnb_lo_hz=9_750_000_000, chunk_s=.01,
        metadata={"channel_number": 4, "region": "lower-edge",
                  "observation_mode": "oversample"})
    receivers = [{"receiver": receiver,
        "acquisition": {"selected_epoch_sample": epoch,
                        "subband_rate_hz": rate, "match_score_margin": .2},
        "pilot": {"edge": "lower", "frequency_offset_hz": cfo,
                  "score_margin": .08}, "pss": {"peak_to_median": 2.4}}
        for receiver in range(2)]
    followup = tmp_path / "followup.json"
    followup.write_text(json.dumps({"checks": [{"start_s": 0,
        "duration_s": .01, "candidate": True, "qualified": True,
        "epoch_difference_samples": 0, "receivers": receivers}]}) + "\n")
    output = tmp_path / "decode.json"

    assert main(["starlink-beacon-decode", str(capture_path), str(followup),
                 str(output)]) == 0
    saved = json.loads(output.read_text())
    assert saved["capture_parameters"]["sample_rate_hz"] == 5_000_000
    for receiver in saved["receivers"]:
        assert receiver["pilot"]["hard_symbol_accuracy"] > .98
        assert receiver["downsampled_comparison"]["sample_rate_hz"] == 2_500_000
        assert receiver["downsampled_comparison"]["pilot_hard_symbol_accuracy"] > .95
