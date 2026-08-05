import hashlib
import json
import os
import shutil
import subprocess
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import numpy as np
import pytest
from scipy.signal import resample_poly

from leo_tracker.radio.cli import main
from leo_tracker.radio.beacon.analysis import analyze_capture, summarize_doppler_track
from leo_tracker.radio.beacon.acquisition import (acquire_exact_receiver,
    acquisition_centers, extract_complex_subband)
from leo_tracker.radio.beacon.artifact import (BeaconCapture, capture_beacon_iq,
                                               queued_paired_blocks)
from leo_tracker.radio.beacon.channels import (starlink_channel_center_hz,
    starlink_edge_pilot_if_hz, starlink_edge_pilot_offset_hz, starlink_if_hz)
from leo_tracker.radio.beacon.structure import analyze_frame_period, frame_period_score
from leo_tracker.radio.beacon.templates import (acquire_pss_epoch, pss_subband_samples,
    pss_subsequence_phase_states, pss_time_samples)
from leo_tracker.radio.beacon.pilots import (EDGE_PILOT_HEX, edge_pilot_frame,
    edge_pilot_symbols, matched_pilot_control_scores, matched_pilot_score,
    track_edge_pilots)
from leo_tracker.radio.beacon.retention import apply_retention
from leo_tracker.radio.beacon.recovery import recover_unanalyzed
from leo_tracker.radio.beacon.followup import (followup_capture,
                                               summarize_temporal_confirmation)
from leo_tracker.radio.beacon.calibration import build_calibration
from leo_tracker.radio.dashboard import DashboardModel, make_handler
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
    control = matched_pilot_score(signal, rate,
        frequency_offsets_hz=(-25_000, 0, 25_000), symbol_roll=17)
    assert found["frequency_offset_hz"] == 25_000
    assert abs((found["sample_index"] - len(prefix)) % len(template)) <= 1
    assert found["score"] > .8
    assert found["score"] - control["score"] > .5
    assert noise["score"] < .35


def test_acquisition_bank_is_symmetric_and_rejects_out_of_band_requests():
    assert acquisition_centers(2_000_000, 500_000) == (
        -2_000_000, -1_500_000, -1_000_000, -500_000, 0,
        500_000, 1_000_000, 1_500_000, 2_000_000)
    with pytest.raises(ValueError, match="beyond"):
        extract_complex_subband(np.ones(100, np.complex64), 10_000_000,
                                4_000_000, 2_500_000)


def test_wide_acquisition_recovers_large_lnb_offset_and_exact_pilots():
    source_rate, subband_rate, offset = 10_000_000.0, 2_500_000.0, 1_500_000.0
    epoch, count, period = 97, 25_000, subband_rate / 750
    baseband = np.zeros(count, np.complex64)
    pilot, pss = edge_pilot_frame(subband_rate), pss_subband_samples(subband_rate)
    for frame in range(7):
        start = epoch + round(frame * period)
        if start + len(pilot) <= count:
            baseband[start:start + len(pilot)] += pilot
        if start + len(pss) <= count:
            baseband[start:start + len(pss)] += 5 * pss
    wide = resample_poly(baseband, 4, 1).astype(np.complex64)
    time_s = np.arange(wide.size) / source_rate
    wide *= np.exp(2j * np.pi * offset * time_s)
    rng = np.random.default_rng(9)
    wide += .15 * (rng.normal(size=wide.size) + 1j * rng.normal(size=wide.size))
    found = acquire_exact_receiver(wide, source_rate, edge="lower",
        acquisition_span_hz=2_000_000, acquisition_step_hz=500_000,
        subband_rate_hz=subband_rate)
    assert found["acquisition"]["selected_center_offset_hz"] == offset
    assert found["pilot"]["frequency_offset_hz"] == pytest.approx(offset, abs=1_000)
    assert found["pilot"]["score_margin"] > .5
    assert found["acquisition"]["match_score_margin"] > .05
    assert found["acquisition"]["pilot_evaluated_bank_count"] == 1
    assert abs(found["pss"]["epoch_sample"] - epoch) <= 1


def test_symbolwise_tracker_is_skipped_below_configurable_joint_prefilter():
    rng = np.random.default_rng(44)
    noise = (rng.normal(size=25_000) + 1j * rng.normal(size=25_000)).astype(np.complex64)
    found = acquire_exact_receiver(noise, 2_500_000, edge="lower",
                                   symbolwise_prefilter_margin=1.0)
    assert not found["pilot"]["evaluated"]
    assert found["pilot"]["symbol_matches"] == 0
    assert found["acquisition"]["pilot_evaluated_bank_count"] == 0


def test_batched_exact_and_control_match_is_equivalent_to_independent_searches():
    rng = np.random.default_rng(512)
    samples = (rng.normal(size=25_000) + 1j * rng.normal(size=25_000)).astype(np.complex64)
    offsets = (-100_000.0, 0.0, 75_000.0)
    exact, control = matched_pilot_control_scores(
        samples, 2_500_000, edge="lower", frequency_offsets_hz=offsets)

    independent_exact = matched_pilot_score(
        samples, 2_500_000, edge="lower", frequency_offsets_hz=offsets)
    independent_control = matched_pilot_score(
        samples, 2_500_000, edge="lower", frequency_offsets_hz=offsets, symbol_roll=17)

    for batched, independent in ((exact, independent_exact),
                                 (control, independent_control)):
        assert batched["score"] == pytest.approx(independent["score"], rel=1e-6)
        assert batched["frequency_offset_hz"] == independent["frequency_offset_hz"]
        assert batched["sample_index"] == independent["sample_index"]


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
    np.testing.assert_array_equal(capture.read_window(9_500, 1_000)[:, 0], rx0[9_500:10_500])
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
    assert report["summary"]["exact_sampled_time_s"] == pytest.approx(.1)
    assert report["summary"]["exact_temporal_coverage_fraction"] == pytest.approx(.5)
    assert report["summary"]["exact_qualified_count"] == 0
    assert '"stored_bytes": 160000' in capsys.readouterr().out


def test_exact_replay_can_be_restricted_to_a_targeted_time_interval(tmp_path):
    capture = tmp_path / "beacon"
    analysis = tmp_path / "targeted.json"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".2",
                 "--sample-rate-hz", "100000", "--bandwidth-hz", "90000",
                 "--block-size", "4096", "--chunk-s", ".07", "--fake"]) == 0
    assert main(["starlink-beacon-analyze", str(capture), str(analysis),
                 "--window-s", ".1", "--maximum-analysis-rate-hz", "100000",
                 "--exact-interval-s", ".02", "--exact-window-s", ".01",
                 "--exact-start-s", ".05", "--exact-stop-s", ".11"]) == 0
    report = json.loads(analysis.read_text())
    assert [item["start_s"] for item in report["exact_checks"]] == pytest.approx([.05, .07, .09])
    assert report["summary"]["exact_temporal_coverage_fraction"] == pytest.approx(.15)


def test_retention_preserves_candidates_and_bounds_negative_ring(tmp_path):
    root = tmp_path / "store"
    (root / "captures").mkdir(parents=True); (root / "reports").mkdir()
    for index, candidates in enumerate((0, 1, 0, 0)):
        capture = root / "captures" / f"capture-{index}"
        capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
            "created_utc_ns": index}) + "\n")
        (root / "reports" / f"capture-{index}.json").write_text(json.dumps({
            "summary": {"exact_candidate_count": candidates,
                        "single_receiver_candidate_count": int(index == 3)}}) + "\n")
    report = apply_retention(root, keep_negative=1)
    assert not (root / "captures" / "capture-0").exists()
    assert (root / "captures" / "capture-1").exists()
    assert (root / "captures" / "capture-2").exists()
    assert (root / "captures" / "capture-3").exists()
    assert report["removed"] == [str(root / "captures" / "capture-0")]


def test_recovery_analyzes_complete_unreported_capture_and_is_idempotent(tmp_path):
    root = tmp_path / "store"
    capture = root / "captures" / "orphan"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".04",
                 "--sample-rate-hz", "100000", "--bandwidth-hz", "90000",
                 "--block-size", "1000", "--chunk-s", ".02", "--fake"]) == 0
    first = recover_unanalyzed(root)
    assert first["errors"] == []
    assert first["recovered"][0]["capture"] == "orphan"
    assert (root / "reports" / "orphan.json").is_file()
    assert (root / "reports" / "plots" / "orphan.png").read_bytes().startswith(b"\x89PNG")
    second = recover_unanalyzed(root)
    assert second["recovered"] == []
    assert second["skipped_count"] == 1


def test_recovery_cli_accepts_pass_archive_for_retrospective_annotation(tmp_path):
    root = tmp_path / "store"; (root / "captures").mkdir(parents=True)
    passes = tmp_path / "passes.json"
    passes.write_text(json.dumps({"satellites": []}) + "\n")
    assert main(["starlink-beacon-recover", str(root), "--passes", str(passes)]) == 0


def test_temporal_followup_requires_consecutive_stable_epoch_and_cfo():
    def point(time_s, epoch, cfo, candidate_receiver=0):
        receivers = []
        for receiver in range(2):
            receivers.append({"acquisition": {"selected_epoch_sample": epoch + receiver,
                                               "subband_rate_hz": 2.5e6},
                              "pilot": {"frequency_offset_hz": cfo + 1000 * receiver}})
        candidates = [False, False]
        if candidate_receiver is not None:
            candidates[candidate_receiver] = True
        return {"start_s": time_s, "receiver_candidates": candidates,
                "epoch_difference_samples": 1,
                "candidate": False, "receivers": receivers}
    confirmed = summarize_temporal_confirmation(
        [point(1.0, 100, -50_000), point(1.1, 104, -49_000)], interval_s=.1)
    assert confirmed["confirmed"]
    assert confirmed["same_receiver_confirmed"]
    assert confirmed["receivers"][0]["confirmed_link_count"] == 1
    rejected = summarize_temporal_confirmation(
        [point(1.0, 100, -50_000), point(1.1, 500, 40_000)], interval_s=.1)
    assert not rejected["confirmed"]
    switched = summarize_temporal_confirmation(
        [point(1.0, 100, -50_000, 0), point(1.3, 102, -49_000, 1)], interval_s=.1)
    assert switched["confirmed"]
    assert switched["cross_receiver_confirmed"]
    assert switched["cross_receiver_links"][0]["candidate_receivers"] == [[0], [1]]


def test_followup_without_triggers_is_fast_idempotent_cli_artifact(tmp_path):
    source = tmp_path / "base.json"
    source.write_text(json.dumps({"exact_checks": []}) + "\n")
    output = tmp_path / "followup.json"
    report = followup_capture(tmp_path / "capture-not-needed", source, output)
    assert report["trigger_count"] == 0
    assert report["checks"] == []
    assert json.loads(output.read_text())["schema"] == "leo-tracker.starlink-beacon-followup/v1"


def test_empirical_calibration_separates_modes_and_excludes_confirmed_events(tmp_path):
    reports = tmp_path / "reports"; reports.mkdir(); (reports / "followups").mkdir()
    def write(name, span, margins):
        receivers = [{"acquisition": {"match_score_margin": margin},
                      "pilot": {"score_margin": margin * 2}} for margin in margins]
        (reports / f"{name}.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-beacon-analysis/v1",
            "analysis": {"acquisition_span_hz": span},
            "exact_checks": [{"epoch_difference_samples": 2, "receivers": receivers}]}) + "\n")
    write("narrow", 0, [.001, .002]); write("wide", 3.5e6, [.004, .006])
    write("confirmed", 0, [.1, .1])
    (reports / "followups" / "confirmed.json").write_text(json.dumps({
        "confirmation": {"confirmed": True}}) + "\n")
    output = tmp_path / "calibration.json"
    result = build_calibration(reports, output)
    assert result["excluded_confirmed_report_count"] == 1
    assert result["modes"]["narrow"]["check_count"] == 1
    assert result["modes"]["wide"]["receiver_check_count"] == 2
    assert result["modes"]["narrow"]["match_margin_quantiles"]["maximum"] == .002
    assert json.loads(output.read_text())["gates"]["dual_epoch_delta_samples"] == 20


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
    (beacon / "reports" / "calibration").mkdir()
    (beacon / "reports" / "followups").mkdir()
    (beacon / "reports" / "calibration" / "calibration.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-calibration/v1",
        "modes": {"narrow": {"check_count": 123}}}) + "\n")
    (beacon / "reports" / "one.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1",
        "capture_manifest": {"created_utc_ns": 1_700_000_000_000_000_000,
            "metadata": {"channel_number": 3, "region": "lower-edge"},
            "center_frequency_hz": 1.459e9, "rf_center_hz": 11.209e9,
            "sample_rate_hz": 2.5e6, "bandwidth_hz": 2.3e6,
            "gain_mode": "manual", "configured_gain_db": 50},
        "summary": {"exact_candidate_count": 1, "exact_qualified_count": 0,
                    "single_receiver_candidate_count": 2,
                    "exact_sampled_time_s": 1.2,
                    "exact_temporal_coverage_fraction": .01},
        "analysis": {"acquisition_span_hz": 3.5e6, "acquisition_step_hz": .5e6},
        "exact_checks": [{"candidate": True, "qualified": False,
            "epoch_difference_samples": 2, "cfo_difference_hz": 100,
            "receivers": [{"pss": {"peak_to_median": 3}, "pilot": {"score_margin": .1},
                           "acquisition": {"selected_center_offset_hz": 1.5e6,
                                           "match_score_margin": .08}},
                          {"pss": {"peak_to_median": 3.1}, "pilot": {"score_margin": .09},
                           "acquisition": {"selected_center_offset_hz": -1e6,
                                           "match_score_margin": .07}}]}]
    }) + "\n")
    (beacon / "reports" / "followups" / "one.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-followup/v1", "checks": [{}, {}],
        "confirmation": {"confirmed": True, "same_receiver_confirmed": False,
            "cross_receiver_confirmed": True, "receivers": [],
            "cross_receiver_links": [{"start_s": 30.0, "stop_s": 30.3,
                                       "drift_hz_s": -3827.95}]},
        "overlapping_passes": [{"name": "STARLINK-TEST", "norad_id": 123,
            "observation_utc": "2026-08-05T15:15:34Z",
            "culmination_elevation_deg": 87.6,
            "nearest_prediction": {"expected_doppler_hz": 139181}}]}) + "\n")
    report = DashboardModel(observation, beacon_root=beacon).beacon()
    assert report["candidate_count"] == 1
    assert report["calibration"]["modes"]["narrow"]["check_count"] == 123
    assert report["captures"][0]["single_receiver_candidate_count"] == 2
    assert report["captures"][0]["acquisition_span_hz"] == 3.5e6
    assert report["captures"][0]["exact_temporal_coverage_fraction"] == .01
    assert report["captures"][0]["exact_checks"][0]["pss_ratios"] == [3, 3.1]
    assert report["captures"][0]["exact_checks"][0]["matched_margins"] == [.08, .07]
    assert report["captures"][0]["exact_checks"][0]["selected_subband_offsets_hz"] == [1.5e6, -1e6]
    capture = report["captures"][0]
    assert capture["followup_confirmed"]
    assert capture["cross_receiver_confirmed"]
    assert capture["confirmed_link_count"] == 1
    assert capture["strongest_confirmed_link"]["drift_hz_s"] == -3827.95
    assert capture["overlapping_pass_count"] == 1
    assert capture["overlapping_passes"][0]["norad_id"] == 123
    assert capture["followup_url"] == "/beacon-followups/one.json"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
        DashboardModel(observation, beacon_root=beacon)))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        served = json.loads(urlopen(
            f"http://127.0.0.1:{server.server_port}{capture['followup_url']}",
            timeout=2).read())
        assert served["confirmation"]["cross_receiver_confirmed"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_dashboard_orders_beacon_reports_by_recency_not_filename(tmp_path):
    observation = tmp_path / "watch"; observation.mkdir()
    beacon = tmp_path / "beacon"; (beacon / "reports").mkdir(parents=True)
    (beacon / "captures").mkdir()
    payload = {"schema": "leo-tracker.starlink-beacon-analysis/v1",
               "capture_manifest": {}, "summary": {}, "exact_checks": []}
    older = beacon / "reports" / "z-older.json"
    newer = beacon / "reports" / "a-newer.json"
    older.write_text(json.dumps(payload)); newer.write_text(json.dumps(payload))
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
    rows = DashboardModel(observation, beacon_root=beacon).beacon()["captures"]
    assert [row["name"] for row in rows] == ["a-newer", "z-older"]


def test_dashboard_never_ages_confirmed_beacon_out_of_recent_window(tmp_path):
    observation = tmp_path / "watch"; observation.mkdir()
    beacon = tmp_path / "beacon"; reports = beacon / "reports"
    reports.mkdir(parents=True); (beacon / "captures").mkdir()
    (reports / "followups").mkdir()
    payload = {"schema": "leo-tracker.starlink-beacon-analysis/v1",
               "capture_manifest": {}, "summary": {}, "exact_checks": []}
    confirmed = reports / "confirmed-old.json"; confirmed.write_text(json.dumps(payload))
    (reports / "followups" / confirmed.name).write_text(json.dumps({
        "confirmation": {"confirmed": True, "receivers": [],
                         "cross_receiver_links": []}}))
    os.utime(confirmed, ns=(1_000_000_000, 1_000_000_000))
    for index in range(13):
        path = reports / f"recent-{index:02d}.json"; path.write_text(json.dumps(payload))
        os.utime(path, ns=(2_000_000_000 + index, 2_000_000_000 + index))

    rows = DashboardModel(observation, beacon_root=beacon).beacon(limit=12)["captures"]

    assert len(rows) == 13
    assert rows[0]["name"] == "confirmed-old"
    assert rows[0]["followup_confirmed"]


def test_production_beacon_watch_combines_narrow_lock_and_periodic_wide_acquisition():
    script = (Path(__file__).parents[1] / "scripts" / "starlink-beacon-watch.sh").read_text()
    assert 'capture_target "${target}" narrow' in script
    assert 'capture_target "${target}" wide' in script
    assert 'LEO_BEACON_TARGETS:-4:lower-edge' in script
    assert 'LEO_BEACON_WIDE_EVERY_CYCLES:-15' in script
    assert "Exactly one analyzer is allowed" in script
    assert "start_pending_analysis" in script
    assert 'LEO_BEACON_MAX_CYCLES:-0' in script
    assert "--sample-rate-hz 10000000" in script
    assert "--acquisition-span-hz 3500000" in script
    assert "--exact-interval-s 1 --exact-window-s .01" in script
    assert "--plot \"${plot}\"" in script
    assert "starlink-beacon-recover" in script
    assert "starlink-beacon-followup" in script
    assert "starlink-beacon-calibrate" in script
    assert "LEO_BEACON_MAX_PI_TEMP_MILLIC" in script


def test_beacon_watch_fake_e2e_drains_bounded_analysis_pipeline(tmp_path):
    repo = Path(__file__).parents[1]
    storage = tmp_path / "beacon-store"
    environment = os.environ | {
        "LEO_TRACKER_REPO": str(repo), "LEO_BEACON_STORAGE": str(storage),
        "LEO_BEACON_DWELL_S": ".04", "LEO_BEACON_WIDE_DWELL_S": ".04",
        "LEO_BEACON_WIDE_EVERY_CYCLES": "1",
        "LEO_BEACON_TARGETS": "4:lower-edge", "LEO_BEACON_MAX_CYCLES": "1",
        "LEO_BEACON_FAKE": "1", "LEO_BEACON_MAX_PI_TEMP_MILLIC": "999999",
        "UV_CACHE_DIR": str(repo / ".uv-cache"), "UV_BIN": shutil.which("uv") or "uv"}
    result = subprocess.run(["bash", str(repo / "scripts/starlink-beacon-watch.sh")],
        cwd=repo, env=environment, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    reports = list((storage / "reports").glob("*.json"))
    assert len(reports) == 2
    assert {"narrow", "wide"} == {
        "wide" if report.stem.split("-")[-2] == "wide" else "narrow"
        for report in reports}
    for report_path in reports:
        report = json.loads(report_path.read_text())
        assert report["capture_manifest"]["state"] == "complete"
        assert report["capture_manifest"]["metadata"] == {
            "channel_number": 4, "region": "lower-edge",
            "tuning_basis": "published Starlink channel and edge-pilot geometry"}
        assert (storage / "reports" / "followups" / report_path.name).is_file()
    assert (storage / "reports" / "calibration" / "calibration.json").is_file()


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
