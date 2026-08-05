import hashlib
import json

import numpy as np
import pytest

from leo_tracker.radio.cli import main
from leo_tracker.radio.beacon.analysis import analyze_capture, summarize_doppler_track
from leo_tracker.radio.beacon.artifact import (BeaconCapture, capture_beacon_iq,
                                               queued_paired_blocks)
from leo_tracker.radio.beacon.channels import (starlink_channel_center_hz,
    starlink_edge_pilot_if_hz, starlink_edge_pilot_offset_hz, starlink_if_hz)
from leo_tracker.radio.beacon.structure import analyze_frame_period, frame_period_score
from leo_tracker.radio.beacon.templates import (acquire_pss_epoch, pss_subband_samples,
    pss_subsequence_phase_states, pss_time_samples)
from leo_tracker.radio.beacon.pilots import (EDGE_PILOT_HEX, edge_pilot_frame,
    edge_pilot_symbols, matched_pilot_score, track_edge_pilots)
from leo_tracker.radio.beacon.retention import apply_retention
from leo_tracker.radio.dashboard import DashboardModel
from leo_tracker.radio.beacon.plot import plot_beacon_report
from leo_tracker.radio.paired import PairedSampleBlock
from leo_tracker.radio.paired import FakePairedSource
from leo_tracker.radio.source import RadioConfig


def _blocks(rx0, rx1, block_size=4096, start_ns=1_700_000_000_000_000_000):
    for start in range(0, len(rx0), block_size):
        yield PairedSampleBlock(rx0[start:start+block_size], rx1[start:start+block_size],
            start, start_ns + start * 400, read_duration_ns=block_size * 400)


def test_published_channel_centers_map_to_universal_lnb_if():
    assert starlink_channel_center_hz(3) == pytest.approx(11_325_117_187.5)
    assert starlink_if_hz(3) == pytest.approx(1_575_117_187.5)
    assert starlink_if_hz(4) == pytest.approx(1_825_117_187.5)
    with pytest.raises(ValueError): starlink_channel_center_hz(0)


def test_published_edge_pilot_tunings_fit_a_narrow_capture():
    assert starlink_edge_pilot_offset_hz("lower") == pytest.approx(-115_429_687.5)
    assert starlink_edge_pilot_offset_hz("upper") == pytest.approx(115_195_312.5)
    assert starlink_edge_pilot_if_hz(3, "lower") == pytest.approx(1_459_687_500.0)
    assert starlink_edge_pilot_if_hz(3, "upper") == pytest.approx(1_690_312_500.0)


def test_published_pss_exact_sequence_and_inversions():
    pss = pss_time_samples()
    assert pss.shape == (1056,)
    np.testing.assert_array_equal(pss_subsequence_phase_states()[:8], [0, 1, 2, 1, 0, 1, 0, 1])
    np.testing.assert_allclose(pss[:32], -pss[1024:1056], atol=1e-6)
    np.testing.assert_allclose(pss[32:160], -pss[160:288], atol=1e-6)
    for repetition in range(2, 8):
        np.testing.assert_allclose(pss[160:288], pss[32+128*repetition:160+128*repetition], atol=1e-6)


def test_published_lower_edge_pilot_codes_and_waveform():
    assert all(len(value) == 150 for value in EDGE_PILOT_HEX.values())
    symbols = edge_pilot_symbols("lower")
    assert symbols.shape == (300, 8)
    np.testing.assert_allclose(np.abs(symbols), 1, atol=1e-6)
    # q_p528 begins in base 4 with 3,0 and ends with 1,0 (hex CC...74).
    expected_states = [3, 0, 1, 0]
    actual = np.rint((np.angle(symbols[[0, 1, -2, -1], 0]) / (np.pi/2)) - .5).astype(int) % 4
    np.testing.assert_array_equal(actual, expected_states)
    assert edge_pilot_frame(2_500_000).shape == (3333,)
    assert edge_pilot_symbols("upper").shape == (300, 8)
    assert edge_pilot_frame(2_500_000, "upper").shape == (3333,)


def test_exact_pss_noncoherent_frame_folding_finds_epoch():
    rate, frame_count, epoch = 2_500_000.0, 30, 997
    period = rate / 750
    pss = pss_subband_samples(rate)
    rng = np.random.default_rng(22)
    signal = .3 * (rng.normal(size=round((frame_count+1)*period)) +
                   1j*rng.normal(size=round((frame_count+1)*period)))
    for frame in range(frame_count):
        start = round(epoch + frame * period)
        signal[start:start+len(pss)] += pss * 3
    found = acquire_pss_epoch(signal, rate)
    assert abs(found["epoch_sample"] - epoch) <= 1
    assert found["peak_to_median"] > 2


def test_exact_pilot_match_beats_noise_and_recovers_cfo():
    rate = 250_000.0
    template = edge_pilot_frame(rate)
    rng = np.random.default_rng(19)
    prefix = np.zeros(83, np.complex64)
    signal = np.concatenate((prefix, template, template))
    time_s = np.arange(signal.size) / rate
    signal *= np.exp(2j * np.pi * 25_000 * time_s)
    signal += .25 * (rng.normal(size=signal.size) + 1j*rng.normal(size=signal.size))
    found = matched_pilot_score(signal, rate, frequency_offsets_hz=(-25_000, 0, 25_000))
    noise = matched_pilot_score((rng.normal(size=signal.size) + 1j*rng.normal(size=signal.size)), rate)
    assert found["frequency_offset_hz"] == 25_000
    assert abs((found["sample_index"] - len(prefix)) % len(template)) <= 1
    assert found["score"] > .8
    assert noise["score"] < .35


def test_symbolwise_pilot_tracker_refines_cfo_and_beats_scrambled_control():
    rate, epoch, frames, cfo = 250_000.0, 83, 8, 73_000.0
    template = edge_pilot_frame(rate)
    period = rate / 750
    count = round(epoch + (frames + 1) * period)
    signal = np.zeros(count, np.complex64)
    for frame in range(frames):
        start = epoch + round(frame * period)
        signal[start:start+len(template)] += template
    time_s = np.arange(count) / rate
    signal *= np.exp(2j * np.pi * cfo * time_s)
    rng = np.random.default_rng(31)
    signal += .25 * (rng.normal(size=count) + 1j*rng.normal(size=count))
    report = track_edge_pilots(signal, rate, epoch,
        coarse_frequency_offsets_hz=(50_000, 75_000, 100_000))
    assert report["frequency_offset_hz"] == pytest.approx(cfo, abs=1_000)
    assert report["score_margin"] > .2
    assert report["coherence"] > report["control_coherence"]


def test_chunked_beacon_capture_round_trip_and_checksums(tmp_path):
    size, rate = 25_000, 10_000.0
    base = np.arange(size, dtype=np.float32)
    rx0 = (base % 1000 + 1j * -(base % 800)).astype(np.complex64)
    rx1 = (-base % 700 + 1j * (base % 600)).astype(np.complex64)
    root = tmp_path / "capture"
    report = capture_beacon_iq(_blocks(rx0, rx1), root, sample_rate_hz=rate,
        center_frequency_hz=1.575e9, bandwidth_hz=9_000, duration_s=2.5,
        lnb_lo_hz=9.75e9, chunk_s=1)
    assert report["state"] == "complete"
    assert [x["sample_count"] for x in report["chunks"]] == [10_000, 10_000, 5_000]
    capture = BeaconCapture.open(root, verify=True)
    replay = np.concatenate([values for _, values in capture.chunks()])
    np.testing.assert_array_equal(replay[:, 0], rx0)
    np.testing.assert_array_equal(replay[:, 1], rx1)
    assert report["stored_bytes"] == size * 8
    assert report["stream_timing"]["read_count"] == 7
    assert report["stream_timing"]["maximum_read_duration_s"] > 0


def test_capture_leaves_a_recoverable_interrupted_manifest(tmp_path):
    rx = np.ones(4_096, np.complex64)
    root = tmp_path / "short"
    with pytest.raises(RuntimeError, match="radio ended"):
        capture_beacon_iq(_blocks(rx, rx), root, sample_rate_hz=10_000,
            center_frequency_hz=1.575e9, bandwidth_hz=9_000, duration_s=1,
            chunk_s=.2)
    report = json.loads((root / "manifest.json").read_text())
    assert report["state"] == "interrupted"
    assert report["captured_samples_per_receiver"] == len(rx)


def test_capture_rejects_a_non_contiguous_radio_stream(tmp_path):
    rx = np.ones(100, np.complex64)
    blocks = [PairedSampleBlock(rx, rx, 0, 100), PairedSampleBlock(rx, rx, 101, 200)]
    with pytest.raises(RuntimeError, match="non-contiguous"):
        capture_beacon_iq(blocks, tmp_path / "gap", sample_rate_hz=1_000,
            center_frequency_hz=1.575e9, bandwidth_hz=900, duration_s=.2, chunk_s=.1)


def test_bounded_reader_queue_preserves_paired_blocks_and_closes(tmp_path):
    values = np.arange(100, dtype=np.float32).astype(np.complex64)
    source = FakePairedSource(values, -values, RadioConfig(1e9, 1_000, 900), block_size=17)
    queued = queued_paired_blocks(source, queue_blocks=2)
    report = capture_beacon_iq(queued, tmp_path / "queued", sample_rate_hz=1_000,
        center_frequency_hz=1e9, bandwidth_hz=900, duration_s=.1, chunk_s=.03)
    queued.close(); source.close()
    assert report["captured_samples_per_receiver"] == 100
    assert source.closed


def test_frame_period_detector_finds_fractional_750_hz_cadence_on_both_receivers():
    rate, duration = 100_000.0, 1.0
    rng = np.random.default_rng(4)
    period = rate / 750
    template = (rng.normal(size=round(period)) + 1j*rng.normal(size=round(period))).astype(np.complex64)
    signal = np.tile(template, int(np.ceil(rate/len(template))))[:round(rate)]
    signal += .2 * (rng.normal(size=signal.size)+1j*rng.normal(size=signal.size))
    report = analyze_frame_period(np.stack((signal, signal*.8)), rate)
    assert report["qualified"]
    assert report["minimum_receiver_correlation"] > .9
    assert all(abs(item["best_lag_samples"]-period) < 1 for item in report["receivers"])


def test_frame_period_detector_rejects_nonrepeating_noise():
    rng = np.random.default_rng(7); rate = 100_000.0
    noise = (rng.normal(size=100_000)+1j*rng.normal(size=100_000)).astype(np.complex64)
    report = analyze_frame_period(np.stack((noise, noise[::-1])), rate)
    assert not report["qualified"]


def test_beacon_capture_and_analysis_cli_end_to_end(tmp_path, capsys):
    capture = tmp_path / "beacon"
    analysis = tmp_path / "analysis.json"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".2",
                 "--sample-rate-hz", "100000", "--bandwidth-hz", "90000",
                 "--block-size", "4096", "--chunk-s", ".07", "--fake"]) == 0
    assert main(["starlink-beacon-analyze", str(capture), str(analysis),
                 "--window-s", ".1", "--maximum-analysis-rate-hz", "100000"]) == 0
    report = json.loads(analysis.read_text())
    assert report["summary"]["window_count"] == 2
    assert report["summary"]["qualified_window_count"] == 2
    assert report["summary"]["exact_check_count"] == 1
    assert report["summary"]["exact_qualified_count"] == 0
    assert '"stored_bytes": 160000' in capsys.readouterr().out


def test_retention_preserves_candidates_and_bounds_negative_ring(tmp_path):
    root = tmp_path / "store"
    (root / "captures").mkdir(parents=True); (root / "reports").mkdir()
    for index, candidates in enumerate((0, 1, 0)):
        capture = root / "captures" / f"capture-{index}"
        capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
            "created_utc_ns": index}) + "\n")
        (root / "reports" / f"capture-{index}.json").write_text(json.dumps({
            "summary": {"exact_candidate_count": candidates}}) + "\n")
    report = apply_retention(root, keep_negative=1)
    assert not (root / "captures" / "capture-0").exists()
    assert (root / "captures" / "capture-1").exists()
    assert (root / "captures" / "capture-2").exists()
    assert report["removed"] == [str(root / "captures" / "capture-0")]


def test_beacon_agc_does_not_silently_apply_manual_gain(tmp_path):
    capture = tmp_path / "agc"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".01",
                 "--sample-rate-hz", "100000", "--bandwidth-hz", "90000",
                 "--gain-mode", "slow_attack", "--gain-db", "50",
                 "--host-temperature-c", "54", "--radio-temperature-c", "46.5",
                 "--fake"]) == 0
    manifest = json.loads((capture / "manifest.json").read_text())
    assert manifest["gain_mode"] == "slow_attack"
    assert manifest["configured_gain_db"] is None
    assert manifest["identity"]["host_temperature_c"] == 54
    assert manifest["identity"]["radio_temperature_c"] == 46.5


def test_dashboard_exposes_exact_beacon_evidence(tmp_path):
    observation = tmp_path / "watch"; observation.mkdir()
    beacon = tmp_path / "beacon"; (beacon / "reports").mkdir(parents=True)
    (beacon / "captures").mkdir()
    (beacon / "reports" / "one.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1",
        "capture_manifest": {"created_utc_ns": 1_700_000_000_000_000_000,
            "metadata": {"channel_number": 3, "region": "lower-edge"},
            "center_frequency_hz": 1.459e9, "rf_center_hz": 11.209e9,
            "sample_rate_hz": 2.5e6, "bandwidth_hz": 2.3e6,
            "gain_mode": "manual", "configured_gain_db": 50},
        "summary": {"exact_candidate_count": 1, "exact_qualified_count": 0},
        "exact_checks": [{"candidate": True, "qualified": False,
            "epoch_difference_samples": 2, "cfo_difference_hz": 100,
            "receivers": [{"pss": {"peak_to_median": 3}, "pilot": {"score_margin": .1}},
                          {"pss": {"peak_to_median": 3.1}, "pilot": {"score_margin": .09}}]}]
    }) + "\n")
    report = DashboardModel(observation, beacon_root=beacon).beacon()
    assert report["candidate_count"] == 1
    assert report["captures"][0]["exact_checks"][0]["pss_ratios"] == [3, 3.1]


def test_doppler_summary_uses_lnb_slopes_not_absolute_cfo_agreement():
    checks = []
    for index, time_s in enumerate((0, 20, 40, 60)):
        checks.append({"candidate": True, "start_s": time_s, "receivers": [
            {"pilot": {"frequency_offset_hz": 20_000 + 1000 * index}},
            {"pilot": {"frequency_offset_hz": -80_000 + 1010 * index}}]})
    report = summarize_doppler_track(checks)
    assert report["qualified"]
    assert report["receiver_slopes_hz_s"] == pytest.approx([50, 50.5])
    assert report["receiver_frequency_correlation"] == pytest.approx(1)


def test_beacon_evidence_plot_is_published(tmp_path):
    report = {"capture_manifest": {"rf_center_hz": 11.2e9,
        "metadata": {"channel_number": 3, "region": "lower-edge"}},
        "summary": {"exact_candidate_count": 0, "exact_qualified_count": 0},
        "exact_checks": [{"start_s": 0, "receivers": [
            {"pss": {"peak_to_median": 1.5}, "pilot": {"score_margin": .001,
                "frequency_offset_hz": 20_000}},
            {"pss": {"peak_to_median": 1.4}, "pilot": {"score_margin": .002,
                "frequency_offset_hz": -30_000}}]}]}
    output = tmp_path / "evidence.png"
    plot_beacon_report(report, output)
    assert output.read_bytes().startswith(b"\x89PNG")
