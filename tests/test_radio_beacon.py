import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

import numpy as np
import pytest
from scipy.signal import resample_poly

from leo_tracker.radio.cli import main
from leo_tracker.radio.beacon.analysis import (analyze_capture, analyze_exact_window,
                                               detection_gates,
                                               summarize_doppler_track)
from leo_tracker.radio.beacon.acquisition import (TIMING_PILOT_STEP_HZ,
    TIMING_PSS_STEP_HZ, TIMING_SEARCH_SPAN_HZ, acquire_exact_receiver,
    acquisition_centers, extract_complex_subband, usable_acquisition_span_hz)
from leo_tracker.radio.beacon.artifact import (BeaconCapture, capture_beacon_iq,
                                               queued_paired_blocks)
from leo_tracker.radio.beacon.channels import (starlink_channel_center_hz,
    starlink_edge_pilot_if_hz, starlink_edge_pilot_offset_hz, starlink_if_hz)
from leo_tracker.radio.beacon.structure import analyze_frame_period, frame_period_score
from leo_tracker.radio.beacon.templates import (acquire_pss_epoch, pss_subband_samples,
    pss_subsequence_phase_states, pss_time_samples)
from leo_tracker.radio.beacon.pilots import (EDGE_PILOT_HEX, edge_pilot_frame,
    edge_pilot_symbols, acquire_pilot_epoch, conditioned_pilot_frequency_search,
    conditioned_pilot_score, matched_pilot_control_scores, matched_pilot_score,
    track_edge_pilots)
from leo_tracker.radio.beacon.retention import apply_retention
from leo_tracker.radio.beacon.recovery import recover_unanalyzed
from leo_tracker.radio.beacon.followup import (followup_capture,
                                               summarize_temporal_confirmation)
from leo_tracker.radio.beacon.calibration import build_calibration
from leo_tracker.radio.beacon.null_replay import replay_null_calibration
from leo_tracker.radio.dashboard import DashboardModel, make_handler
from leo_tracker.radio.beacon.plot import plot_beacon_followup, plot_beacon_report
from leo_tracker.radio.paired import PairedCI16Block, PairedSampleBlock
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


def test_learned_full_frame_is_an_independent_dual_receiver_acquisition_gate(
        monkeypatch):
    rate = 2_500_000.0; epoch = 137; count = round(.01 * rate)
    rng = np.random.default_rng(411)
    template = (rng.normal(size=3333) + 1j * rng.normal(size=3333)).astype(np.complex64)
    template /= np.linalg.norm(template)
    cfos = (12_345.0, 12_405.0)
    paired = np.column_stack([(rng.normal(size=count) + 1j * rng.normal(size=count))
                              for _ in range(2)]).astype(np.complex64)
    period = rate / 750
    for receiver, cfo in enumerate(cfos):
        frame = 0
        while True:
            start = epoch + round(frame * period)
            if start + template.size > count:
                break
            indexes = np.arange(template.size) + start
            paired[start:start + template.size, receiver] += (
                500 * template * np.exp(2j * np.pi * cfo * indexes / rate))
            frame += 1
    calls = 0

    def weak_pilot(*_args, **_kwargs):
        nonlocal calls
        receiver = calls; calls += 1
        return {"pss": {"epoch_sample": epoch},
            "pilot": {"frequency_offset_hz": cfos[receiver],
                      "score_margin": 0.0, "coherence": 0.0},
            "acquisition": {"subband_rate_hz": rate,
                "selected_epoch_sample": epoch, "match_score_margin": 0.0}}

    monkeypatch.setattr("leo_tracker.radio.beacon.analysis.acquire_exact_receiver",
                        weak_pilot)
    result = analyze_exact_window(paired, rate, edge="lower",
        acquisition_method="pilot_symbolwise_v3",
        learned_templates=(template, template),
        learned_template_source="qualified-template.json")

    assert result["candidate"]
    assert result["candidate_basis"] == ["learned_full_frame"]
    assert result["full_frame_evidence"]["candidate"]
    assert result["full_frame_evidence"]["template_source"] == "qualified-template.json"
    assert min(item["score_margin"] for item in
               result["full_frame_evidence"]["receivers"]) > .5
    assert [item["frequency_offset_hz"] for item in
            result["full_frame_evidence"]["receivers"]] == pytest.approx(cfos, abs=3)


def test_learned_full_frame_dual_gate_rejects_independent_noise(monkeypatch):
    rate = 2_500_000.0; epoch = 137; count = round(.01 * rate)
    rng = np.random.default_rng(412)
    template = (rng.normal(size=3333) + 1j * rng.normal(size=3333)).astype(np.complex64)
    template /= np.linalg.norm(template)
    paired = (rng.normal(size=(count, 2)) +
              1j * rng.normal(size=(count, 2))).astype(np.complex64)

    def weak_pilot(*_args, **_kwargs):
        return {"pss": {"epoch_sample": epoch},
            "pilot": {"frequency_offset_hz": 12_345.0,
                      "score_margin": 0.0, "coherence": 0.0},
            "acquisition": {"subband_rate_hz": rate,
                "selected_epoch_sample": epoch, "match_score_margin": 0.0}}

    monkeypatch.setattr("leo_tracker.radio.beacon.analysis.acquire_exact_receiver",
                        weak_pilot)
    result = analyze_exact_window(paired, rate, edge="lower",
        acquisition_method="pilot_symbolwise_v3",
        learned_templates=(template, template))

    assert not result["candidate"]
    assert not result["full_frame_evidence"]["candidate"]
    assert result["candidate_basis"] == []


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


def test_the_timing_stage_searches_the_whole_derived_doppler_span():
    """It is the tight link: every later stage inherits its candidate CFO from here.

    The requirement is 279,059 Hz of all-sky Doppler p99.9 from 63,035,467
    satellite-instants, plus 19,346 Hz of LNB bias uncertainty, plus 21,595 Hz
    of margin from p99.9 out to closed form -- 320 kHz.  It searched +/-300 kHz,
    which clears Doppler plus bias by 1.6 kHz and leaves nothing for either
    margin term, so a satellite in the tail was unreachable by every stage
    downstream and not only by this one.
    """
    assert TIMING_SEARCH_SPAN_HZ >= 279_059 + 19_346 + 21_595
    for step in (TIMING_PSS_STEP_HZ, TIMING_PILOT_STEP_HZ):
        grid = acquisition_centers(TIMING_SEARCH_SPAN_HZ, step)
        assert min(grid) <= -320_000.0 and max(grid) >= 320_000.0
        assert 0.0 in grid


def test_widening_the_timing_stage_did_not_coarsen_it():
    """Extra width bought with extra spacing would be a trade, not a fix.

    Spacing is the other half of whether a hypothesis is near enough to find
    anything: the same injection sweep that sized the survey bank puts the
    largest usable spacing at 150 kHz for -12 dB.  Both grids came out finer
    than they were -- 150 kHz to 106.7 kHz and 100 kHz to 80 kHz -- because
    ``acquisition_centers`` rounds the step down to fit the span rather than
    stretching it to cover.
    """
    pss = acquisition_centers(TIMING_SEARCH_SPAN_HZ, TIMING_PSS_STEP_HZ)
    pilot = acquisition_centers(TIMING_SEARCH_SPAN_HZ, TIMING_PILOT_STEP_HZ)

    assert max(np.diff(pss)) <= TIMING_PSS_STEP_HZ <= 150_000.0
    assert max(np.diff(pilot)) <= TIMING_PILOT_STEP_HZ <= 100_000.0


def test_usable_span_is_bounded_by_the_sampled_bandwidth():
    assert usable_acquisition_span_hz(10_000_000, 2_500_000) == 3_750_000
    assert usable_acquisition_span_hz(10_000_000, 5_000_000) == 2_500_000
    # An analysis rate above the recording rate is already clamped to it, which
    # leaves no room to tune away from DC.
    assert usable_acquisition_span_hz(2_500_000, 2_500_000) == 0
    assert usable_acquisition_span_hz(2_500_000, 10_000_000) == 0


def test_oversized_acquisition_span_is_clamped_and_recorded_not_fatal():
    """A wide capture must stay analyzable when asked for more search than it sampled."""
    source_rate, subband_rate = 10_000_000.0, 2_500_000.0
    rng = np.random.default_rng(3)
    noise = (rng.normal(size=80_000) + 1j * rng.normal(size=80_000)).astype(np.complex64)
    found = acquire_exact_receiver(noise, source_rate, edge="lower",
        acquisition_span_hz=12_000_000, acquisition_step_hz=2_000_000,
        subband_rate_hz=subband_rate)
    acquisition = found["acquisition"]
    assert acquisition["span_hz"] == 3_750_000
    assert acquisition["requested_span_hz"] == 12_000_000
    assert acquisition["span_clamped_to_sampled_bandwidth"] is True
    assert abs(acquisition["selected_center_offset_hz"]) <= 3_750_000
    within = acquire_exact_receiver(noise, source_rate, edge="lower",
        acquisition_span_hz=3_500_000, acquisition_step_hz=2_000_000,
        subband_rate_hz=subband_rate)
    assert within["acquisition"]["span_hz"] == 3_500_000
    assert within["acquisition"]["span_clamped_to_sampled_bandwidth"] is False


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


@pytest.mark.parametrize("cfo_hz", [2_500, 12_500, 77_777, 187_500, 337_500])
def test_pss_symbolwise_v2_acquires_published_pilot_across_off_grid_cfo(cfo_hz):
    rate, size, epoch = 2_500_000.0, 25_000, 500
    frame = edge_pilot_frame(rate, "lower")
    pss = pss_subband_samples(rate, "lower")
    frame[:pss.size] += np.sqrt(pss.size) * pss
    signal = np.zeros(size, np.complex64)
    frame_index = 0
    while True:
        start = epoch + round(frame_index * rate / 750)
        if start + frame.size > size:
            break
        signal[start:start + frame.size] += frame
        frame_index += 1
    time_s = np.arange(size) / rate
    signal *= np.exp(2j * np.pi * cfo_hz * time_s)
    rng = np.random.default_rng(600 + int(cfo_hz))
    signal += .3 * (rng.normal(size=size) + 1j * rng.normal(size=size))

    found = acquire_exact_receiver(signal, rate, edge="lower", method="pss_symbolwise_v2")

    assert abs(found["pss"]["epoch_sample"] - epoch) <= 1
    assert found["pilot"]["frequency_offset_hz"] == pytest.approx(cfo_hz, abs=150)
    assert found["acquisition"]["match_score_margin"] > .5
    assert found["pilot"]["score_margin"] > .5


def test_coherent_grid_v1_documents_off_grid_cfo_blind_spot():
    rate, size, epoch, cfo_hz = 2_500_000.0, 25_000, 500, 12_500.0
    frame = edge_pilot_frame(rate, "lower")
    pss = pss_subband_samples(rate, "lower")
    frame[:pss.size] += np.sqrt(pss.size) * pss
    signal = np.zeros(size, np.complex64)
    for frame_index in range(7):
        start = epoch + round(frame_index * rate / 750)
        signal[start:start + frame.size] += frame
    signal *= np.exp(2j * np.pi * cfo_hz * np.arange(size) / rate)

    legacy = acquire_exact_receiver(signal, rate, edge="lower", method="coherent_grid_v1")
    replacement = acquire_exact_receiver(signal, rate, edge="lower", method="pss_symbolwise_v2")

    assert legacy["acquisition"]["match_score_margin"] < .008
    assert replacement["acquisition"]["match_score_margin"] > .5


def test_pilot_symbolwise_v3_recovers_weak_timing_when_narrow_pss_is_insufficient():
    """Timing is the property; the exact-minus-control margin is not.

    177,777 Hz is deliberately off-grid, and the margin at an off-grid CFO is a
    lock indicator rather than a quantity: it is large when a hypothesis lands
    within a few kHz of the truth and small otherwise, with nothing in between.
    Measured across three noise seeds at this CFO it reads 0.010, 0.128 and
    0.018, so the 0.015 it used to be pinned at was pinning a coin flip that
    the old +/-300 kHz grid happened to win with this seed.

    Bounded here against ``symbolwise_prefilter_margin`` instead, which is the
    number the pipeline itself uses to decide whether the symbolwise track is
    worth running.  What the test is named for -- the epoch, and the CFO to
    within 5 kHz -- is asserted exactly as before and is unaffected.
    """
    rate, size, epoch, cfo_hz = 2_500_000.0, 25_000, 500, 177_777.0
    frame = edge_pilot_frame(rate, "lower")
    pss = pss_subband_samples(rate, "lower")
    frame[:pss.size] += np.sqrt(pss.size) * pss
    model = np.zeros(size, np.complex64)
    for frame_index in range(8):
        start = epoch + round(frame_index * rate / 750)
        if start + frame.size <= size:
            model[start:start + frame.size] += frame
    model /= np.sqrt(np.mean(np.abs(model) ** 2))
    rng = np.random.default_rng(992)
    noise = (rng.normal(size=size) + 1j * rng.normal(size=size)).astype(np.complex64)
    time_s = np.arange(size) / rate
    signal = noise + .3 * np.sqrt(np.mean(np.abs(noise) ** 2)) * model * np.exp(
        2j * np.pi * cfo_hz * time_s)

    timing = acquire_pilot_epoch(signal, rate, edge="lower")
    found = acquire_exact_receiver(signal, rate, edge="lower",
                                   method="pilot_symbolwise_v3")

    assert abs(timing["epoch_sample"] - epoch) <= 1
    assert timing["frequency_offset_hz"] == 200_000
    assert found["acquisition"]["selected_epoch_sample"] == epoch
    assert found["pilot_epoch"]["epoch_sample"] == epoch
    assert found["pilot"]["frequency_offset_hz"] == pytest.approx(cfo_hz, abs=5_000)
    assert (found["acquisition"]["match_score_margin"]
            > found["acquisition"]["symbolwise_prefilter_margin"])
    assert found["pilot"]["score_margin"] > .03


@pytest.mark.parametrize("cfo_hz", [77_777.0, 240_000.0])
def test_the_widened_timing_grid_locks_offsets_the_narrow_one_missed(cfo_hz):
    """What the extra width and the finer step actually buy, on the same signal.

    The old grid ran +/-300 kHz at 100 kHz for the pilot-epoch search.  At
    77,777 Hz its nearest hypothesis was 22 kHz away and the exact-minus-
    control margin came out 0.005 -- no lock -- with the CFO landing at
    81,146.  At 240,000 Hz its nearest was 40 kHz away and it picked the
    *wrong epoch*, 289 instead of 500.  The grid is now +/-320 kHz at 80 kHz,
    which puts a hypothesis 2 kHz from the first and exactly on the second.

    Across a sweep of seven CFOs by three seeds the median margin moves from
    0.021 to 0.277 and epoch-plus-CFO recovery from 15/21 to 16/21.  It is not
    uniformly better -- 290 kHz was 10 kHz from an old hypothesis and is
    30 kHz from a new one -- because that is what any grid does.  It is better
    where the requirement says it has to be.
    """
    rate, size, epoch = 2_500_000.0, 25_000, 500
    frame = edge_pilot_frame(rate, "lower")
    pss = pss_subband_samples(rate, "lower")
    frame[:pss.size] += np.sqrt(pss.size) * pss
    model = np.zeros(size, np.complex64)
    for frame_index in range(8):
        start = epoch + round(frame_index * rate / 750)
        if start + frame.size <= size:
            model[start:start + frame.size] += frame
    model /= np.sqrt(np.mean(np.abs(model) ** 2))
    rng = np.random.default_rng(992)
    noise = (rng.normal(size=size) + 1j * rng.normal(size=size)).astype(np.complex64)
    signal = noise + .3 * np.sqrt(np.mean(np.abs(noise) ** 2)) * model * np.exp(
        2j * np.pi * cfo_hz * np.arange(size) / rate)

    found = acquire_exact_receiver(signal, rate, edge="lower",
                                   method="pilot_symbolwise_v3")

    assert found["acquisition"]["selected_epoch_sample"] == epoch
    assert found["pilot"]["frequency_offset_hz"] == pytest.approx(cfo_hz, abs=5_000)
    assert found["acquisition"]["match_score_margin"] > .2


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


def test_batched_conditioned_cfo_search_matches_independent_scores():
    rate, epoch = 2_500_000.0, 313
    rng = np.random.default_rng(814)
    samples = (rng.normal(size=25_000) + 1j * rng.normal(size=25_000)).astype(np.complex64)
    offsets = np.array((-12_500.0, 0.0, 7_300.0, 25_000.0))
    expected = [conditioned_pilot_score(samples, rate, epoch, offset, edge="lower")
                for offset in offsets]

    actual = conditioned_pilot_frequency_search(
        samples, rate, epoch, offsets, edge="lower")
    best = max(expected, key=lambda item: item["score"])

    assert actual["frequency_offset_hz"] == best["frequency_offset_hz"]
    assert actual["score"] == pytest.approx(best["score"], rel=1e-6, abs=1e-8)
    assert actual["maximum_score"] == pytest.approx(best["maximum_score"], rel=1e-6)
    assert actual["frame_support"] == best["frame_support"]


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
    assert len(report["stream_timing"]["clock_samples"]) == 7
    assert [item["first_sample_index"] for item in
            report["stream_timing"]["clock_samples"]] == sorted(
                item["first_sample_index"] for item in
                report["stream_timing"]["clock_samples"])


def test_native_ci16_capture_is_byte_equivalent_to_complex_path(tmp_path):
    count, rate = 4096, 4096.0
    i0 = (np.arange(count, dtype=np.int16) % 2000) - 1000
    q0 = -i0
    i1 = i0 // 2
    q1 = -i1
    native = PairedCI16Block((i0, q0, i1, q1), 0, 1_000,
                             read_duration_ns=500)
    complex_block = PairedSampleBlock(
        i0.astype(np.float32) + 1j*q0.astype(np.float32),
        i1.astype(np.float32) + 1j*q1.astype(np.float32), 0, 1_000,
        read_duration_ns=500)
    settings = dict(sample_rate_hz=rate, center_frequency_hz=1.5e9,
                    bandwidth_hz=rate, duration_s=1, chunk_s=1)
    native_report = capture_beacon_iq([native], tmp_path / "native", **settings)
    complex_report = capture_beacon_iq(
        [complex_block], tmp_path / "complex", **settings)
    assert ((tmp_path / "native" / "chunk-000000.ci16").read_bytes() ==
            (tmp_path / "complex" / "chunk-000000.ci16").read_bytes())
    assert native_report["sample_statistics"] == complex_report["sample_statistics"]
    replay = BeaconCapture.open(tmp_path / "native", verify=True).read_window(0, count)
    np.testing.assert_array_equal(replay[:, 0].real, i0)
    np.testing.assert_array_equal(replay[:, 0].imag, q0)


def test_beacon_capture_records_hardware_gain_and_adc_utilization(tmp_path):
    rate, size = 10_000.0, 10_000
    rx0 = np.full(size, 3 + 4j, np.complex64)
    rx1 = np.full(size, 6 + 8j, np.complex64)
    blocks = [PairedSampleBlock(rx0[:5000], rx1[:5000], 0, 100,
        read_duration_ns=10, gain_db=(31.5, 42.5)),
        PairedSampleBlock(rx0[5000:], rx1[5000:], 5000, 200,
        read_duration_ns=10, gain_db=(32.5, 43.5))]
    report = capture_beacon_iq(blocks, tmp_path / "telemetry",
        sample_rate_hz=rate, center_frequency_hz=1.5e9,
        bandwidth_hz=9_000, duration_s=1, chunk_s=.5,
        gain_mode="slow_attack")
    assert [entry["rx_gain_db"] for entry in report["gain_telemetry"]["entries"]] == [
        [31.5, 42.5], [32.5, 43.5]]
    stats = report["sample_statistics"]["receivers"]
    assert stats[0]["rms_magnitude"] == pytest.approx(5)
    assert stats[1]["rms_magnitude"] == pytest.approx(10)
    assert [row["peak_abs_component"] for row in stats] == [4, 8]
    assert [row["near_full_scale_fraction"] for row in stats] == [0, 0]


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


def test_interrupted_capture_opens_at_the_extent_it_durably_wrote(tmp_path):
    """A kill mid-write can leave the declared total ahead of the chunks on disk.

    All 30 quarantined field recordings were unreadable for this reason alone,
    though every chunk's checksum was valid. The prefix is still usable, and the
    source manifest must not be rewritten to say so.
    """
    rx = np.ones(4_096, np.complex64)
    root = tmp_path / "short"
    with pytest.raises(RuntimeError, match="radio ended"):
        capture_beacon_iq(_blocks(rx, rx), root, sample_rate_hz=10_000,
            center_frequency_hz=1.575e9, bandwidth_hz=9_000, duration_s=1,
            chunk_s=.2)
    manifest_path = root / "manifest.json"
    stored = json.loads(manifest_path.read_text())
    written = sum(item["sample_count"] for item in stored["chunks"])
    # Simulate the field case: samples were read from the radio but the tail
    # chunk never reached disk, so the manifest claims more than it holds.
    stored["captured_samples_per_receiver"] = written + 12_500_000
    manifest_path.write_text(json.dumps(stored))

    capture = BeaconCapture.open(root, verify=True)

    assert capture.manifest["captured_samples_per_receiver"] == written
    assert capture.manifest["declared_samples_per_receiver"] == written + 12_500_000
    # The source on disk is evidence and stays exactly as the capture left it.
    assert json.loads(manifest_path.read_text())[
        "captured_samples_per_receiver"] == written + 12_500_000


def test_complete_capture_still_requires_an_exact_sample_total(tmp_path):
    rx = np.ones(4_096, np.complex64)
    root = tmp_path / "complete"
    capture_beacon_iq(_blocks(rx, rx), root, sample_rate_hz=10_000,
        center_frequency_hz=1.575e9, bandwidth_hz=9_000,
        duration_s=len(rx) / 10_000, chunk_s=.2)
    manifest_path = root / "manifest.json"
    stored = json.loads(manifest_path.read_text())
    assert stored["state"] == "complete"
    stored["captured_samples_per_receiver"] += 1_000
    manifest_path.write_text(json.dumps(stored))

    with pytest.raises(ValueError, match="sample total is inconsistent"):
        BeaconCapture.open(root, verify=True)


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


def test_beacon_capture_files_the_pre_dwell_survey_in_the_manifest(
        tmp_path, capsys, monkeypatch):
    """What the scanner called active must travel with what the dwell recorded.

    The manifest is embedded whole in the report, so filing it here is what
    makes the two comparable later without a join on wall time.
    """
    from leo_tracker.radio.beacon import presurvey

    monkeypatch.setattr(presurvey, "run_survey", lambda **kwargs: ({
        "schema": presurvey.SURVEY_SCHEMA, "state": "complete",
        "active_count": 1, "total_ms": 372.0,
        "dwell": {"channel": kwargs["dwell_channel"],
                  "region": kwargs["dwell_region"]},
        "active": [{"channel": 4, "region": "lower-edge", "receiver": 0}]}, None))
    capture = tmp_path / "surveyed"

    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".02",
                 "--sample-rate-hz", "10000", "--bandwidth-hz", "9000",
                 "--block-size", "100", "--chunk-s", ".02", "--fake",
                 "--channel-number", "4", "--region", "lower-edge",
                 "--survey-before-dwell"]) == 0

    manifest = json.loads((capture / "manifest.json").read_text())
    survey = manifest["metadata"]["pre_dwell_survey"]
    assert survey["active"] == [{"channel": 4, "region": "lower-edge",
                                 "receiver": 0}]
    assert survey["dwell"] == {"channel": 4, "region": "lower-edge"}
    assert json.loads(capsys.readouterr().out)["survey_active"] == 1


def test_the_survey_iq_is_written_before_the_dwell_and_digested(tmp_path):
    """A capture interrupted later still keeps the probes that preceded it."""
    import numpy as np
    from leo_tracker.radio.beacon.artifact import (SURVEY_IQ_FILENAME,
                                                   _write_survey_iq)

    samples = np.arange(8 * 5 * 2 * 2, dtype="<i2").reshape(8, 5, 2, 2)
    tmp_path.mkdir(parents=True, exist_ok=True)

    record = _write_survey_iq(tmp_path, samples)

    written = (tmp_path / SURVEY_IQ_FILENAME).read_bytes()
    assert np.frombuffer(written, dtype="<i2").reshape(8, 5, 2, 2).tolist() \
        == samples.tolist()
    assert record["tunings"] == 8 and record["samples_per_tuning"] == 5
    assert record["bytes"] == len(written)
    assert not list(tmp_path.glob("*.partial"))


def test_survey_iq_of_the_wrong_shape_is_refused(tmp_path):
    """Silently writing a mis-shaped buffer would make it undecodable later."""
    import numpy as np
    from leo_tracker.radio.beacon.artifact import _write_survey_iq

    with pytest.raises(ValueError, match="tuning, sample, receiver"):
        _write_survey_iq(tmp_path, np.zeros((8, 5, 3), dtype="<i2"))


def test_the_survey_iq_is_not_mistaken_for_a_dwell_chunk(tmp_path, monkeypatch):
    """Readers enumerate the chunk list, so the probe must not join it."""
    import numpy as np
    from leo_tracker.radio.beacon import presurvey

    monkeypatch.setattr(presurvey, "run_survey", lambda **kw: (
        {"schema": presurvey.SURVEY_SCHEMA, "state": "complete",
         "active_count": 0, "total_ms": 1.0, "active": [], "dwell": None},
        np.zeros((8, 5, 2, 2), dtype="<i2")))
    capture = tmp_path / "with-iq"

    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".02",
                 "--sample-rate-hz", "10000", "--bandwidth-hz", "9000",
                 "--block-size", "100", "--chunk-s", ".02", "--fake",
                 "--survey-before-dwell", "--keep-survey-iq"]) == 0

    manifest = json.loads((capture / "manifest.json").read_text())
    assert manifest["survey_iq"]["path"] == "survey.ci16"
    assert (capture / "survey.ci16").is_file()
    assert all(not item["path"].startswith("survey")
               for item in manifest["chunks"])


def test_a_capture_without_the_flag_carries_no_survey(tmp_path):
    """The flag is the whole switch, so an absent survey stays absent."""
    capture = tmp_path / "unsurveyed"

    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".02",
                 "--sample-rate-hz", "10000", "--bandwidth-hz", "9000",
                 "--block-size", "100", "--chunk-s", ".02", "--fake"]) == 0

    manifest = json.loads((capture / "manifest.json").read_text())
    assert "pre_dwell_survey" not in manifest["metadata"]


def test_a_failed_survey_still_yields_a_capture(tmp_path, capsys):
    """A delayed capture is gone for good; a missing survey is an annotation."""
    capture = tmp_path / "survey-failed"

    # No radio is reachable under --fake, so the survey fails for real here
    # rather than being made to fail.
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".02",
                 "--sample-rate-hz", "10000", "--bandwidth-hz", "9000",
                 "--block-size", "100", "--chunk-s", ".02", "--fake",
                 "--survey-before-dwell"]) == 0

    manifest = json.loads((capture / "manifest.json").read_text())
    assert manifest["state"] == "complete"
    assert manifest["metadata"]["pre_dwell_survey"]["state"] == "failed"


def test_beacon_capture_records_operator_radio_and_receiver_labels(tmp_path, capsys):
    capture = tmp_path / "radio-provenance"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".02",
                 "--sample-rate-hz", "10000", "--bandwidth-hz", "9000",
                 "--block-size", "100", "--chunk-s", ".02", "--fake",
                 "--radio-id", "pluto-a", "--receiver-labels",
                 "north-lnb", "south-lnb"]) == 0

    output = json.loads(capsys.readouterr().out)
    manifest = json.loads((capture / "manifest.json").read_text())
    assert output["radio_id"] == "pluto-a"
    assert manifest["identity"]["radio_id"] == "pluto-a"
    assert manifest["identity"]["receiver_labels"] == ["north-lnb", "south-lnb"]


def test_exact_replay_can_be_restricted_to_a_targeted_time_interval(tmp_path):
    capture = tmp_path / "beacon"
    analysis = tmp_path / "targeted.json"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".2",
                 "--sample-rate-hz", "100000", "--bandwidth-hz", "90000",
                 "--block-size", "4096", "--chunk-s", ".07", "--fake"]) == 0
    assert main(["starlink-beacon-analyze", str(capture), str(analysis),
                 "--window-s", ".1", "--maximum-analysis-rate-hz", "100000",
                 "--exact-interval-s", ".02", "--exact-window-s", ".01",
                 "--exact-start-s", ".05", "--exact-stop-s", ".11",
                 "--exact-acquisition-method", "pss_symbolwise_v2"]) == 0
    report = json.loads(analysis.read_text())
    assert [item["start_s"] for item in report["exact_checks"]] == pytest.approx([.05, .07, .09])
    assert report["summary"]["exact_temporal_coverage_fraction"] == pytest.approx(.15)
    assert report["analysis"]["exact_acquisition_method"] == "pss_symbolwise_v2"


def test_retention_preserves_confirmed_and_pending_and_bounds_rejections(tmp_path):
    root = tmp_path / "store"
    (root / "captures").mkdir(parents=True)
    (root / "reports" / "followups").mkdir(parents=True)
    for index, candidates in enumerate((0, 1, 0, 0)):
        capture = root / "captures" / f"capture-{index}"
        capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
            "created_utc_ns": index}) + "\n")
        (root / "reports" / f"capture-{index}.json").write_text(json.dumps({
            "summary": {"exact_candidate_count": candidates,
                        "single_receiver_candidate_count": int(index == 3)}}) + "\n")
        if index < 3:
            (root / "reports" / "followups" / f"capture-{index}.json").write_text(
                json.dumps({"confirmation": {"confirmed": index == 1}}) + "\n")
    report = apply_retention(root, keep_negative=1)
    assert not (root / "captures" / "capture-0").exists()
    assert (root / "captures" / "capture-1").exists()
    assert (root / "captures" / "capture-2").exists()
    assert (root / "captures" / "capture-3").exists()
    assert report["removed"] == [str(root / "captures" / "capture-0")]


def test_retention_never_removes_capture_claimed_by_parallel_analysis_worker(tmp_path):
    root = tmp_path / "store"
    for directory in ("captures", "reports/followups",
                      "staging/analysis-queue"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for index in range(2):
        name = f"capture-{index}"
        capture = root / "captures" / name
        capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
            "created_utc_ns": index}) + "\n")
        (root / "reports" / f"{name}.json").write_text("{}\n")
        (root / "reports" / "followups" / f"{name}.json").write_text(
            json.dumps({"confirmation": {"confirmed": False}}) + "\n")
    (root / "staging" / "analysis-queue" / "0001.running.3.99").write_text(
        f"capture-0\tcaptures/capture-0\tnarrow\n")

    report = apply_retention(root, keep_negative=0)

    assert (root / "captures" / "capture-0").is_dir()
    assert not (root / "captures" / "capture-1").exists()
    assert str(root / "captures" / "capture-0") in report["protected"]


def test_retention_bounds_only_fully_derived_confirmed_iq_and_preserves_template_source(
        tmp_path):
    root = tmp_path / "store"
    for directory in ("captures", "reports/followups", "reports/decoded",
                      "reports/tracks", "reports/learned-beacons"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for index in range(5):
        name = f"confirmed-{index}"
        capture = root / "captures" / name; capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
            "created_utc_ns": index}) + "\n")
        (root / "reports" / f"{name}.json").write_text("{}\n")
        (root / "reports" / "followups" / f"{name}.json").write_text(
            json.dumps({"confirmation": {"confirmed": True}}) + "\n")
        if index != 1:  # incomplete derivatives remain protected regardless of age
            (root / "reports" / "decoded" / f"{name}.json").write_text("{}\n")
            (root / "reports" / "tracks" / f"{name}.json").write_text("{}\n")
    (root / "reports" / "learned-beacons" / "active.json").write_text(json.dumps({
        "capture": str((root / "captures" / "confirmed-0").resolve())}) + "\n")

    dry = apply_retention(root, keep_confirmed=2, dry_run=True)
    assert dry["removed_confirmed"] == [str(root / "captures" / "confirmed-2")]
    assert all((root / "captures" / f"confirmed-{index}").exists()
               for index in range(5))

    report = apply_retention(root, keep_confirmed=2)
    assert report["removed_confirmed"] == [str(root / "captures" / "confirmed-2")]
    assert (root / "captures" / "confirmed-0").exists()
    assert (root / "captures" / "confirmed-1").exists()
    assert not (root / "captures" / "confirmed-2").exists()
    assert (root / "captures" / "confirmed-3").exists()
    assert (root / "captures" / "confirmed-4").exists()


def test_retention_durably_pins_channel_link_sources_after_association_changes(tmp_path):
    root = tmp_path / "store"
    for directory in ("captures", "reports/followups", "reports/decoded",
                      "reports/tracks", "reports/channel-links", "reports/associations"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for index in range(3):
        name = f"narrow-{index}"
        capture = root / "captures" / name; capture.mkdir()
        (capture / "manifest.json").write_text(json.dumps({
            "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
            "created_utc_ns": index,
            "metadata": {"observation_mode": "narrow"}}) + "\n")
        (root / "reports" / f"{name}.json").write_text("{}\n")
        (root / "reports" / "followups" / f"{name}.json").write_text(
            json.dumps({"confirmation": {"confirmed": True}}) + "\n")
        (root / "reports" / "decoded" / f"{name}.json").write_text("{}\n")
    track = root / "reports" / "tracks" / "narrow-0.json"
    track.write_text(json.dumps({
        "capture": str((root / "captures" / "narrow-0").resolve())}) + "\n")
    linked = root / "reports" / "channel-links" / "rolling.json"
    linked.write_text(json.dumps({"source_track_artifacts": [str(track)]}) + "\n")
    association = root / "reports" / "associations" / "rolling.json"
    association.write_text(json.dumps({
        "source_observations": str(linked),
        "associations": [{"qualified": True, "best_norad_id": 68000}]}) + "\n")

    report = apply_retention(root, keep_confirmed=0)
    pinned = str((root / "captures" / "narrow-0").resolve())
    assert report["newly_pinned"] == [pinned]
    assert (root / "captures" / "narrow-0").is_dir()
    assert not (root / "captures" / "narrow-1").exists()
    assert not (root / "captures" / "narrow-2").exists()
    ledger = json.loads(Path(report["qualified_pin_ledger"]).read_text())
    assert ledger["schema"] == "leo-tracker.qualified-capture-pins/v1"
    assert ledger["captures"][0]["path"] == pinned

    # A rolling association can later be overwritten by a new sky interval.
    # Its historical source must remain pinned by the durable ledger.
    association.write_text(json.dumps({
        "source_observations": str(linked),
        "associations": [{"qualified": False}]}) + "\n")
    second = apply_retention(root, keep_confirmed=0)
    assert second["newly_pinned"] == []
    assert second["qualified_capture_pins"] == [pinned]
    assert (root / "captures" / "narrow-0").is_dir()


def test_retention_bounds_complete_hop_sessions_but_preserves_pending_and_qualified(
        tmp_path):
    root = tmp_path / "store"
    for directory in ("captures", "hop-sessions", "reports/followups",
                      "reports/tracks", "reports/associations"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    children = []
    for index in range(4):
        session = root / "hop-sessions" / f"hop-{index}"
        child = session / "00-ch1-lower-edge"; child.mkdir(parents=True)
        children.append(child)
        (child / "manifest.json").write_text(json.dumps({
            "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
            "created_utc_ns": index,
            "metadata": {"observation_mode": "channel-hop"}}) + "\n")
        report_name = f"{session.name}-{child.name}"
        (root / "reports" / f"{report_name}.json").write_text("{}\n")
        if index != 3:  # an incomplete analysis session is never aged out
            (root / "reports" / "followups" / f"{report_name}.json").write_text(
                json.dumps({"confirmation": {"confirmed": False}}) + "\n")
    track = root / "reports" / "tracks" / "hop-0.json"
    track.write_text(json.dumps({"capture": str(children[0].resolve())}) + "\n")
    (root / "reports" / "associations" / "hop-qualified.json").write_text(json.dumps({
        "source_observations": str(track),
        "associations": [{"qualified": True}]}) + "\n")

    report = apply_retention(root, keep_hop_sessions=1)
    assert (root / "hop-sessions" / "hop-0").is_dir()  # qualified
    assert not (root / "hop-sessions" / "hop-1").exists()
    assert (root / "hop-sessions" / "hop-2").is_dir()  # newest completed
    assert (root / "hop-sessions" / "hop-3").is_dir()  # pending
    assert report["removed_hop_sessions"] == [str(root / "hop-sessions" / "hop-1")]
    assert str(root / "hop-sessions" / "hop-3") in report["protected_hop_sessions"]


def test_retention_uses_small_separate_rings_for_wide_and_oversampled_iq(tmp_path):
    root = tmp_path / "store"
    for directory in ("captures", "reports/followups", "reports/decoded"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for mode in ("wide", "oversample"):
        for index in range(3):
            name = f"capture-{mode}-{index}"
            capture = root / "captures" / name; capture.mkdir()
            (capture / "manifest.json").write_text(json.dumps({
                "schema": "leo-tracker.beacon-iq/v1", "state": "complete",
                "created_utc_ns": index,
                "metadata": {"observation_mode": mode}}) + "\n")
            (root / "reports" / f"{name}.json").write_text("{}\n")
            (root / "reports" / "followups" / f"{name}.json").write_text(
                json.dumps({"confirmation": {"confirmed": False}}) + "\n")

    report = apply_retention(root, keep_wide=1, keep_oversample=2)
    assert report["removed_wide"] == [
        str(root / "captures" / "capture-wide-0"),
        str(root / "captures" / "capture-wide-1")]
    assert report["removed_oversample"] == [
        str(root / "captures" / "capture-oversample-0")]
    assert (root / "captures" / "capture-wide-2").is_dir()
    assert (root / "captures" / "capture-oversample-1").is_dir()
    assert (root / "captures" / "capture-oversample-2").is_dir()


def test_recovery_analyzes_complete_unreported_capture_and_is_idempotent(tmp_path):
    root = tmp_path / "store"
    capture = root / "captures" / "orphan"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".04",
                 "--sample-rate-hz", "100000", "--bandwidth-hz", "90000",
                 "--block-size", "1000", "--chunk-s", ".02", "--fake"]) == 0
    first = recover_unanalyzed(root, exact_acquisition_method="pss_symbolwise_v2",
                               narrow_exact_interval_s=.02)
    assert first["errors"] == []
    assert first["recovered"][0]["capture"] == "orphan"
    assert (root / "reports" / "orphan.json").is_file()
    recovered_report = json.loads((root / "reports" / "orphan.json").read_text())
    assert recovered_report["analysis"]["exact_interval_s"] == .02
    assert recovered_report["analysis"]["exact_acquisition_method"] == "pss_symbolwise_v2"
    assert (root / "reports" / "plots" / "orphan.png").read_bytes().startswith(b"\x89PNG")
    second = recover_unanalyzed(root)
    assert second["recovered"] == []
    assert second["skipped_count"] == 1


def test_recovery_backfills_decode_for_existing_confirmed_narrow_capture(
        tmp_path, monkeypatch):
    root = tmp_path / "store"
    capture = root / "captures" / "confirmed"
    reports = root / "reports"
    followups = reports / "followups"
    capture.mkdir(parents=True); followups.mkdir(parents=True)
    (capture / "manifest.json").write_text(json.dumps({
        "sample_rate_hz": 2_500_000}) + "\n")
    (reports / "confirmed.json").write_text("{}\n")
    (followups / "confirmed.json").write_text(json.dumps({
        "confirmation": {"confirmed": True}}) + "\n")

    def fake_decode(_capture, _followup, output, *, symbols_output):
        report = {"combined": {"minimum_pilot_accuracy": .7,
                               "minimum_sss_accuracy": .4}}
        output.write_text(json.dumps(report) + "\n")
        symbols_output.write_bytes(b"symbols")
        return report, {}

    monkeypatch.setattr("leo_tracker.radio.beacon.recovery.decode_followup", fake_decode)
    monkeypatch.setattr("leo_tracker.radio.beacon.recovery.plot_decode_report",
                        lambda _report, _arrays, output: output.write_bytes(b"png"))

    result = recover_unanalyzed(root)

    assert result["errors"] == []
    assert result["recovered_decodes"] == [{"capture": "confirmed",
        "minimum_pilot_accuracy": .7, "minimum_sss_accuracy": .4}]
    assert (reports / "decoded" / "confirmed.json").is_file()
    assert (reports / "decoded" / "confirmed.npz").read_bytes() == b"symbols"
    assert (reports / "decoded" / "confirmed.png").read_bytes() == b"png"


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

    def dual_point(time_s, epoch, frequencies):
        return {"start_s": time_s, "candidate": True,
                "receiver_candidates": [True, True],
                "epoch_difference_samples": 0,
                "receivers": [{
                    "acquisition": {"selected_epoch_sample": epoch,
                                    "subband_rate_hz": 2.5e6},
                    "pilot": {"frequency_offset_hz": frequencies[receiver]}}
                    for receiver in range(2)]}
    aliased = summarize_temporal_confirmation([
        dual_point(2.0, 100, (-100_000, -97_000)),
        dual_point(2.1, 2_000, (-100_400, -97_450))], interval_s=.1)
    assert aliased["confirmed"]
    assert aliased["dual_receiver_confirmed"]
    assert not aliased["same_receiver_confirmed"]
    assert aliased["dual_receiver_links"][0]["slope_difference_hz_s"] == pytest.approx(500)
    divergent = summarize_temporal_confirmation([
        dual_point(2.0, 100, (-100_000, -97_000)),
        dual_point(2.1, 2_000, (-100_400, -95_000))], interval_s=.1)
    assert not divergent["confirmed"]

    learned = [dual_point(3.0, 100, (-200_000, 100_000)),
               dual_point(3.1, 100, (150_000, -200_000))]
    for point_value, frequencies in zip(
            learned, ((-100_000, -97_000), (-100_400, -97_450)), strict=True):
        point_value["full_frame_evidence"] = {"candidate": True,
            "receivers": [{"frequency_offset_hz": value} for value in frequencies]}
    learned_confirmation = summarize_temporal_confirmation(learned, interval_s=.1)
    assert learned_confirmation["dual_receiver_confirmed"]
    assert learned_confirmation["dual_receiver_links"][0][
        "slope_difference_hz_s"] == pytest.approx(500)


def test_followup_without_triggers_is_fast_idempotent_cli_artifact(tmp_path):
    source = tmp_path / "base.json"
    source.write_text(json.dumps({"exact_checks": []}) + "\n")
    output = tmp_path / "followup.json"
    report = followup_capture(tmp_path / "capture-not-needed", source, output)
    assert report["trigger_count"] == 0
    assert report["checks"] == []
    assert json.loads(output.read_text())["schema"] == "leo-tracker.starlink-beacon-followup/v1"
    assert main(["starlink-beacon-followup-rescore", str(output)]) == 0
    assert json.loads(output.read_text())["confirmation"]["confirmed"] is False


def test_empirical_calibration_separates_methods_modes_and_confirmed_events(tmp_path):
    reports = tmp_path / "reports"; reports.mkdir(); (reports / "followups").mkdir()
    def write(name, span, margins, method="coherent_grid_v1"):
        receivers = [{"acquisition": {"match_score_margin": margin},
                      "pilot": {"score_margin": margin * 2}} for margin in margins]
        (reports / f"{name}.json").write_text(json.dumps({
            "schema": "leo-tracker.starlink-beacon-analysis/v1",
            "analysis": {"acquisition_span_hz": span,
                         "exact_acquisition_method": method},
            "exact_checks": [{"epoch_difference_samples": 2, "receivers": receivers}]}) + "\n")
    write("narrow", 0, [.001, .002]); write("wide", 3.5e6, [.004, .006])
    write("v2-narrow", 0, [.04, .06], "pss_symbolwise_v2")
    supplementary = reports / "calibration" / "pss_symbolwise_v2-null"
    supplementary.mkdir(parents=True)
    (supplementary / "v2-extra.json").write_text((reports / "v2-narrow.json").read_text())
    write("confirmed", 0, [.1, .1])
    (reports / "followups" / "confirmed.json").write_text(json.dumps({
        "confirmation": {"confirmed": True}}) + "\n")
    output = tmp_path / "calibration.json"
    result = build_calibration(reports, output)
    assert result["excluded_confirmed_report_count"] == 1
    assert result["modes"]["narrow"]["check_count"] == 1
    assert result["modes"]["wide"]["receiver_check_count"] == 2
    assert result["modes"]["narrow"]["match_margin_quantiles"]["maximum"] == .002
    assert result["acquisition_methods"]["coherent_grid_v1"]["narrow"][
        "receiver_check_count"] == 2
    assert result["acquisition_methods"]["pss_symbolwise_v2"]["narrow"][
        "receiver_check_count"] == 4
    assert result["acquisition_methods"]["pss_symbolwise_v2"]["narrow"][
        "match_margin_quantiles"]["maximum"] == .06
    assert result["acquisition_methods"]["pss_symbolwise_v2"]["wide"][
        "check_count"] == 0
    assert result["gates_by_acquisition_method"]["pss_symbolwise_v2"] == detection_gates(
        "pss_symbolwise_v2")
    assert result["gates_by_acquisition_method"]["pss_symbolwise_v2"][
        "dual_match_margin"] > result["gates"]["dual_match_margin"]
    assert json.loads(output.read_text())["schema"].endswith("/v2")
    assert json.loads(output.read_text())["gates"]["dual_epoch_delta_samples"] == 20


def test_detector_specific_null_replay_is_resumable_and_exposed_by_cli(tmp_path):
    root = tmp_path / "store"; reports = root / "reports"
    captures = root / "captures"; reports.mkdir(parents=True); captures.mkdir()
    (reports / "followups").mkdir()
    rate, size = 100_000.0, 4_000
    rng = np.random.default_rng(881)
    rx0 = (rng.normal(size=size) + 1j * rng.normal(size=size)).astype(np.complex64)
    rx1 = (rng.normal(size=size) + 1j * rng.normal(size=size)).astype(np.complex64)
    capture = captures / "negative"
    capture_beacon_iq(_blocks(rx0, rx1), capture, sample_rate_hz=rate,
        center_frequency_hz=1.7e9, bandwidth_hz=90_000, duration_s=.04,
        metadata={"channel_number": 4, "region": "lower-edge"}, chunk_s=.02)
    (reports / "negative.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-analysis/v1",
        "capture": str(capture), "summary": {}}) + "\n")
    output = tmp_path / "null"

    first = replay_null_calibration(root, output,
        acquisition_method="coherent_grid_v1", capture_limit=1,
        checks_per_capture=2, window_s=.01,
        maximum_host_temperature_c=999, resume_host_temperature_c=998)

    assert len(first["completed_reports"]) == 1
    replay = json.loads((output / "negative.json").read_text())
    assert replay["null_replay"]["source_selection"] == "strict_no-trigger_negative"
    assert replay["summary"]["exact_check_count"] == 2
    assert first["method_calibration"]["narrow"]["check_count"] == 2
    assert main(["starlink-beacon-null-replay", str(root), str(output),
                 "--exact-acquisition-method", "coherent_grid_v1",
                 "--capture-limit", "1", "--checks-per-capture", "2",
                 "--exact-window-s", ".01", "--maximum-host-temperature-c", "999",
                 "--resume-host-temperature-c", "998"]) == 0
    summary = json.loads((output / "replay-summary.json").read_text())
    assert len(summary["reused_reports"]) == 1
    with pytest.raises(ValueError, match="resume temperature"):
        replay_null_calibration(root, output, maximum_host_temperature_c=70,
                                resume_host_temperature_c=70)


def test_beacon_agc_does_not_silently_apply_manual_gain(tmp_path):
    capture = tmp_path / "agc"
    assert main(["starlink-beacon-capture", str(capture), "--duration-s", ".01",
                 "--sample-rate-hz", "100000", "--bandwidth-hz", "90000",
                 "--gain-mode", "slow_attack", "--gain-db", "50",
                 "--gain-experiment-id", "test-ab", "--gain-random-draw-u32", "17",
                 "--gain-assignment-probability", ".5",
                 "--host-temperature-c", "54", "--radio-temperature-c", "46.5",
                 "--fake"]) == 0
    manifest = json.loads((capture / "manifest.json").read_text())
    assert manifest["gain_mode"] == "slow_attack"
    assert manifest["configured_gain_db"] is None
    assert manifest["metadata"]["assigned_gain_mode"] == "slow_attack"
    assert manifest["metadata"]["gain_random_draw_u32"] == 17
    assert manifest["metadata"]["agc_assignment_probability"] == .5
    assert manifest["identity"]["host_temperature_c"] == 54
    assert manifest["identity"]["radio_temperature_c"] == 46.5


def test_dashboard_exposes_exact_beacon_evidence(tmp_path):
    observation = tmp_path / "watch"; observation.mkdir()
    beacon = tmp_path / "beacon"; (beacon / "reports").mkdir(parents=True)
    (beacon / "captures").mkdir()
    (beacon / "reports" / "calibration").mkdir()
    (beacon / "reports" / "followups").mkdir()
    (beacon / "reports" / "gain-experiment").mkdir()
    (beacon / "reports" / "gain-experiment" / "summary.json").write_text(json.dumps({
        "schema": "leo-tracker.beacon-gain-comparison/v1",
        "randomized_capture_count": 12, "groups": {},
        "experiment_ids": ["gain-ab"], "decision_guidance": {"ready": False}}))
    (beacon / "reports" / "calibration" / "calibration.json").write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-calibration/v2",
        "modes": {"narrow": {"check_count": 123}},
        "acquisition_methods": {"pss_symbolwise_v2": {
            "narrow": {"check_count": 7}, "wide": {"check_count": 0}}}}) + "\n")
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
        "analysis": {"acquisition_span_hz": 3.5e6, "acquisition_step_hz": .5e6,
                     "exact_acquisition_method": "pss_symbolwise_v2"},
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
    model = DashboardModel(observation, beacon_root=beacon)
    report = model.beacon()
    assert report["candidate_count"] == 1
    assert report["gain_experiment"]["randomized_capture_count"] == 12
    assert report["calibration"]["modes"]["narrow"]["check_count"] == 123
    assert report["active_acquisition_method"] == "pss_symbolwise_v2"
    assert report["calibration"]["acquisition_methods"]["pss_symbolwise_v2"][
        "narrow"]["check_count"] == 7
    assert report["captures"][0]["single_receiver_candidate_count"] == 2
    assert report["captures"][0]["acquisition_span_hz"] == 3.5e6
    assert report["captures"][0]["exact_temporal_coverage_fraction"] == .01
    assert report["captures"][0]["exact_checks"][0]["pss_ratios"] == [3, 3.1]
    assert report["captures"][0]["exact_checks"][0]["matched_margins"] == [.08, .07]
    assert report["captures"][0]["exact_checks"][0]["selected_subband_offsets_hz"] == [1.5e6, -1e6]
    capture = report["captures"][0]
    assert capture["exact_acquisition_method"] == "pss_symbolwise_v2"
    assert capture["followup_confirmed"]
    assert capture["cross_receiver_confirmed"]
    assert capture["confirmed_link_count"] == 1
    assert capture["strongest_confirmed_link"]["drift_hz_s"] == -3827.95
    assert capture["overlapping_pass_count"] == 1
    assert capture["overlapping_passes"][0]["norad_id"] == 123
    assert capture["followup_url"] == "/beacon-followups/one.json"
    assert model.beacon() is report
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
    assert 'capture_target "${target}" oversample' in script
    assert 'capture_target "${target}" wide' in script
    assert 'LEO_BEACON_TARGETS:-4:lower-edge' in script
    assert 'LEO_BEACON_WIDE_EVERY_CYCLES:-15' in script
    assert 'LEO_BEACON_OVERSAMPLE_EVERY_CYCLES:-10' in script
    assert 'LEO_BEACON_OVERSAMPLE_ON_STARTUP:-1' in script
    assert 'LEO_BEACON_HOP_EVERY_CYCLES:-2' in script
    assert 'LEO_BEACON_HOP_BURST_SESSIONS:-3' in script
    assert 'LEO_BEACON_HOP_CHANNELS:-1 2 3 4' in script
    assert 'LEO_BEACON_HOP_DWELL_S:-2' in script
    assert 'LEO_BEACON_HOP_EXACT_INTERVAL_S:-0.7' in script
    assert 'LEO_BEACON_HOP_EXACT_WINDOW_S:-0.02' in script
    assert 'LEO_BEACON_HOP_ACQUISITION_METHOD:-pilot_symbolwise_v3' in script
    assert "starlink-beacon-hop-capture" in script
    assert 'pending_mode="hop"' in script
    assert "analysis_worker" in script
    assert "analysis_worker ordinary &" in script
    assert "analysis_worker hop &" in script
    # The ordinary worker must not regress to unbounded FIFO latency when
    # expensive full-frame analysis falls behind acquisition. Four newest jobs
    # are selected for every oldest recovery job, preserving both freshness
    # and eventual durable backlog progress.
    assert "ordinary_selection_count=1" in script
    assert "$((ordinary_selection_count % 5)) -ne 0" in script
    assert "index=$((${#jobs[@]} - 1))" in script
    assert "ordinary_selection_count=$((ordinary_selection_count + 1))" in script
    assert '.running.${BASHPID}' in script
    assert 'running_jobs=("${analysis_queue}"/*.running.*)' in script
    assert "terminate_process_tree" in script
    assert "enqueue_pending_analysis" in script
    assert 'analysis_queue="${storage_root}/staging/analysis-queue"' in script
    assert 'analysis_execution_lock="${storage_root}/staging/analysis-execution.lock"' in script
    assert 'exec 8>"${analysis_execution_lock}"' in script
    assert "flock 8" in script
    assert 'LEO_BEACON_MAX_CYCLES:-0' in script
    assert "--sample-rate-hz 10000000" in script
    assert "--sample-rate-hz 5000000" in script
    assert '--sample-rate-hz 2500000' in script
    assert '--bandwidth-hz 2500000' in script
    assert "--bandwidth-hz 3000000" in script
    assert "--acquisition-span-hz 3500000" in script
    assert 'LEO_BEACON_EXACT_ACQUISITION_METHOD:-pilot_symbolwise_v3' in script
    assert 'LEO_BEACON_NARROW_EXACT_INTERVAL_S:-6' in script
    assert 'LEO_BEACON_WIDE_EXACT_INTERVAL_S:-10' in script
    assert 'LEO_BEACON_TRACK_MAXIMUM_GAP_S:-15' in script
    assert 'LEO_BEACON_TRACK_MAXIMUM_REACQUISITION_SPAN_HZ:-15000' in script
    assert 'LEO_BEACON_FRAME_MAXIMUM_EXTENSION_S:-60' in script
    assert 'LEO_BEACON_ROLLING_ASSOCIATION_INTERVAL_S:-600' in script
    assert 'LEO_BEACON_PRESERVE_RAW:-0' in script
    assert 'LEO_BEACON_MINIMUM_FREE_GB:-150' in script
    assert 'if [[ "${preserve_raw}" != "1" ]]' in script
    assert '"storage_backoff":true' in script
    assert '--exact-acquisition-method "${exact_acquisition_method}"' in script
    assert '--exact-acquisition-method "${analysis_method}"' in script
    assert 'template_analysis_args=(--beacon-template "${learned_beacon}")' in script
    assert '"${analysis_args[@]}" "${template_analysis_args[@]}"' in script
    assert "--plot \"${plot}\"" in script
    assert "starlink-beacon-recover" in script
    assert "recover_startup" in script
    assert '"startup_recovery_deferred":true' in script
    assert "starlink-beacon-followup" in script
    assert "starlink-beacon-decode" in script
    assert "starlink-beacon-fingerprint" in script
    assert "starlink-beacon-track" in script
    assert "starlink-beacon-frame-track" in script
    assert '--maximum-extension-s "${frame_maximum_extension_s}"' in script
    assert "starlink-beacon-channel-link" in script
    assert "refresh_capture_links" in script
    capture_link_function = script.split("refresh_capture_links() {", 1)[1].split(
        "refresh_rolling_narrow_links() {", 1)[0]
    assert '"capture_link_unavailable":true' in capture_link_function
    assert "return 0" in capture_link_function
    assert capture_link_function.index("return 0") < capture_link_function.index(
        'associate --observations "${linked}"')
    assert "refresh_rolling_narrow_links" in script
    assert 'channel-links/rolling-${channel}-${region}.json' in script
    assert '--maximum-gap-s 45' in script
    assert "--maximum-same-tuning-quadratic-rms-hz 2000" in script
    assert "--minimum-dual-epochs 30 --minimum-coverage-fraction .1" in script
    assert "reports/frame-tracks" in script
    assert '--maximum-gap-s "${track_maximum_gap_s}"' in script
    assert '--measurement-source conditioned_frames' in script
    assert '--measurement-source dense_followup' in script
    assert '("${mode}" == "narrow" || "${mode}" == "hop")' in script
    assert '"dual_valid_frame_count": [1-9]' in script
    assert '"conditioned_frame_track_empty":true' in script
    assert "leo-orbit" in script and "associate --observations" in script
    assert "reports/tracks" in script and "reports/associations" in script
    assert '[[ "${mode}" != "wide" && -f "${confirmation_marker}" ]]' in script
    assert '"dual_receiver_confirmed": true' in script
    assert "starlink-beacon-calibrate" in script
    assert "LEO_BEACON_MAX_PI_TEMP_MILLIC" in script
    assert 'LEO_BEACON_AGC_PERCENT:-50' in script
    assert "/dev/urandom" in script
    assert "--gain-experiment-id" in script
    assert "starlink-beacon-gain-summary" in script
    assert "starlink-beacon-dashboard-index" in script
    assert '[[ "${mode}" != "hop" ]]' in script
    assert '[[ "${mode}" != "hop" || -f "${confirmation_marker}" ]]' in script


def test_beacon_watch_service_drains_foreground_capture_before_killing_children():
    unit = (Path(__file__).parents[1] /
            "deploy/systemd/leo-tracker-beacon-watch.service").read_text()
    assert "KillSignal=SIGINT" in unit
    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=300" in unit


def test_beacon_watch_fake_e2e_drains_bounded_analysis_pipeline(tmp_path):
    repo = Path(__file__).parents[1]
    storage = tmp_path / "beacon-store"
    environment = os.environ | {
        "LEO_TRACKER_REPO": str(repo), "LEO_BEACON_STORAGE": str(storage),
        "LEO_BEACON_DWELL_S": ".04", "LEO_BEACON_WIDE_DWELL_S": ".04",
        "LEO_BEACON_OVERSAMPLE_ON_STARTUP": "0",
        "LEO_BEACON_HOP_EVERY_CYCLES": "0",
        "LEO_BEACON_WIDE_EVERY_CYCLES": "1",
        "LEO_BEACON_TARGETS": "4:lower-edge", "LEO_BEACON_MAX_CYCLES": "1",
        "LEO_BEACON_FAKE": "1", "LEO_BEACON_MAX_PI_TEMP_MILLIC": "999999",
        "UV_CACHE_DIR": str(repo / ".uv-cache"), "UV_BIN": shutil.which("uv") or "uv"}
    result = subprocess.run(["bash", str(repo / "scripts/starlink-beacon-watch.sh")],
        cwd=repo, env=environment, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    reports = [path for path in (storage / "reports").glob("*.json")
               if path.name != "dashboard-index.json"]
    assert len(reports) == 2
    assert {"narrow", "wide"} == {
        "wide" if report.stem.split("-")[-2] == "wide" else "narrow"
        for report in reports}
    for report_path in reports:
        report = json.loads(report_path.read_text())
        assert report["capture_manifest"]["state"] == "complete"
        metadata = report["capture_manifest"]["metadata"]
        assert metadata["channel_number"] == 4
        assert metadata["region"] == "lower-edge"
        assert metadata["observation_mode"] == (
            "wide" if "-wide-" in report_path.stem else "narrow")
        assert metadata["tuning_basis"] == "published Starlink channel and edge-pilot geometry"
        assert metadata["gain_experiment_id"] == "randomized-manual-vs-slow-attack-v1"
        assert isinstance(metadata["gain_random_draw_u32"], int)
        assert metadata["agc_assignment_probability"] == .5
        assert metadata["assigned_gain_mode"] in {"manual", "slow_attack"}
        assert (storage / "reports" / "followups" / report_path.name).is_file()
    by_mode = {json.loads(path.read_text())["capture_manifest"]["metadata"]
               ["observation_mode"]: json.loads(path.read_text()) for path in reports}
    # The wide comparison began while the prior narrow report was still being
    # computed: the capture scheduler is independent from the serialized worker.
    assert by_mode["wide"]["capture_manifest"]["created_utc_ns"] < int(
        datetime.fromisoformat(by_mode["narrow"]["created_utc"]).timestamp() * 1e9)
    assert not list((storage / "staging" / "analysis-queue").glob("*.job"))
    assert (storage / "reports" / "calibration" / "calibration.json").is_file()
    fingerprint_index = storage / "reports" / "fingerprints" / "index.json"
    assert json.loads(fingerprint_index.read_text())["fingerprint_count"] == 0
    gain_summary = json.loads((storage / "reports" / "gain-experiment" /
                               "summary.json").read_text())
    assert gain_summary["randomized_capture_count"] == 2
    dashboard_index = json.loads((storage / "reports" / "dashboard-index.json").read_text())
    assert len(dashboard_index["recordings"]) == 2


def test_beacon_watch_fake_e2e_claims_ordinary_and_hop_jobs_exactly_once(tmp_path):
    repo = Path(__file__).parents[1]
    storage = tmp_path / "dual-worker-store"
    environment = os.environ | {
        "LEO_TRACKER_REPO": str(repo), "LEO_BEACON_STORAGE": str(storage),
        "LEO_BEACON_DWELL_S": ".04", "LEO_BEACON_OVERSAMPLE_ON_STARTUP": "0",
        "LEO_BEACON_OVERSAMPLE_EVERY_CYCLES": "0",
        "LEO_BEACON_WIDE_EVERY_CYCLES": "0",
        "LEO_BEACON_HOP_EVERY_CYCLES": "1",
        "LEO_BEACON_HOP_BURST_SESSIONS": "1",
        "LEO_BEACON_HOP_CHANNELS": "1 2", "LEO_BEACON_HOP_DWELL_S": ".04",
        "LEO_BEACON_HOP_SETTLE_BUFFERS": "0",
        "LEO_BEACON_TARGETS": "4:lower-edge", "LEO_BEACON_MAX_CYCLES": "1",
        "LEO_BEACON_FAKE": "1", "LEO_BEACON_MAX_PI_TEMP_MILLIC": "999999",
        "UV_CACHE_DIR": str(repo / ".uv-cache"), "UV_BIN": shutil.which("uv") or "uv"}

    result = subprocess.run(["bash", str(repo / "scripts/starlink-beacon-watch.sh")],
        cwd=repo, env=environment, text=True, capture_output=True, timeout=180)

    assert result.returncode == 0, result.stderr
    reports = [path for path in (storage / "reports").glob("*.json")
               if path.name != "dashboard-index.json"]
    assert len(reports) == 3
    modes = [json.loads(path.read_text())["capture_manifest"]["metadata"]
             ["observation_mode"] for path in reports]
    assert modes.count("narrow") == 1
    assert modes.count("channel-hop") == 2
    queue = storage / "staging" / "analysis-queue"
    assert not list(queue.glob("*.job"))
    assert not list(queue.glob("*.running.*"))
    assert not list(queue.glob("*.failed"))


def test_beacon_watch_default_cadence_runs_three_sessions_after_second_cycle(tmp_path):
    repo = Path(__file__).parents[1]
    storage = tmp_path / "three-session-store"
    environment = os.environ | {
        "LEO_TRACKER_REPO": str(repo), "LEO_BEACON_STORAGE": str(storage),
        "LEO_BEACON_DWELL_S": ".04", "LEO_BEACON_OVERSAMPLE_ON_STARTUP": "0",
        "LEO_BEACON_OVERSAMPLE_EVERY_CYCLES": "0",
        "LEO_BEACON_WIDE_EVERY_CYCLES": "0",
        "LEO_BEACON_HOP_CHANNELS": "1 2", "LEO_BEACON_HOP_DWELL_S": ".04",
        "LEO_BEACON_HOP_SETTLE_BUFFERS": "0",
        "LEO_BEACON_TARGETS": "4:lower-edge", "LEO_BEACON_MAX_CYCLES": "2",
        "LEO_BEACON_FAKE": "1", "LEO_BEACON_MAX_PI_TEMP_MILLIC": "999999",
        "UV_CACHE_DIR": str(repo / ".uv-cache"), "UV_BIN": shutil.which("uv") or "uv"}

    result = subprocess.run(["bash", str(repo / "scripts/starlink-beacon-watch.sh")],
        cwd=repo, env=environment, text=True, capture_output=True, timeout=180)

    assert result.returncode == 0, result.stderr
    sessions = sorted((storage / "hop-sessions").glob("hop-lower-edge-*-b??"))
    assert len(sessions) == 3
    assert all(len(list(session.glob("[0-9][0-9]-ch*-lower-edge"))) == 2
               for session in sessions)
    reports = [path for path in (storage / "reports").glob("*.json")
               if path.name != "dashboard-index.json"]
    assert len(reports) == 8  # two fixed captures plus three two-channel sessions
    queue = storage / "staging" / "analysis-queue"
    assert not list(queue.glob("*.job"))
    assert not list(queue.glob("*.running.*"))


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
    source = tmp_path / "source.json"; source.write_text(json.dumps(report))
    followup_plot = tmp_path / "followup.png"
    plot_beacon_followup({"source_analysis": str(source),
                          "checks": report["exact_checks"]}, followup_plot)
    assert followup_plot.read_bytes().startswith(b"\x89PNG")


def test_capture_name_survives_a_same_second_collision(tmp_path):
    """A recording name is an identity, and it resolves to one second.

    Two captures of the same channel, region, and mode inside one second used
    to collide on the exclusive mkdir and abort the capture. Pre-creating the
    names for this second and the next two forces the collision regardless of
    where the test lands relative to the second boundary.
    """
    repo = Path(__file__).parents[1]
    storage = tmp_path / "collision-store"
    captures = storage / "captures"
    captures.mkdir(parents=True)
    taken = []
    for ahead in range(3):
        moment = datetime.now(timezone.utc) + timedelta(seconds=ahead)
        stamp = moment.strftime("%Y%m%dT%H%M%SZ")
        occupied = captures / f"ch4-lower-edge-narrow-{stamp}"
        occupied.mkdir()
        taken.append(occupied.name)

    environment = os.environ | {
        "LEO_TRACKER_REPO": str(repo), "LEO_BEACON_STORAGE": str(storage),
        "LEO_BEACON_DWELL_S": ".04", "LEO_BEACON_OVERSAMPLE_ON_STARTUP": "0",
        "LEO_BEACON_OVERSAMPLE_EVERY_CYCLES": "0",
        "LEO_BEACON_WIDE_EVERY_CYCLES": "0", "LEO_BEACON_HOP_EVERY_CYCLES": "0",
        "LEO_BEACON_TARGETS": "4:lower-edge", "LEO_BEACON_MAX_CYCLES": "1",
        "LEO_BEACON_FAKE": "1", "LEO_BEACON_MAX_PI_TEMP_MILLIC": "999999",
        "UV_CACHE_DIR": str(repo / ".uv-cache"), "UV_BIN": shutil.which("uv") or "uv"}

    result = subprocess.run(["bash", str(repo / "scripts/starlink-beacon-watch.sh")],
                            env=environment, text=True, capture_output=True,
                            timeout=180)

    assert result.returncode == 0, result.stdout + result.stderr
    written = {path.name for path in captures.iterdir()
               if path.is_dir() and (path / "manifest.json").is_file()}
    # It must have stepped past every occupied name rather than failing.
    assert written, result.stdout + result.stderr
    assert not written & set(taken)
