import json

import numpy as np
import pytest

from leo_tracker.radio import cli
from leo_tracker.radio.paired import FakePairedSource
from leo_tracker.radio.source import RadioConfig
from leo_tracker.radio.starlink import (STARLINK_GUTTER_WIDTH_HZ,
    aggregate_gutter_search, analyze_starlink_block, channel_plan,
    fake_blocks, frame_periodicity, search_gutter_offsets,
    synthetic_starlink_block, threaded_source_blocks)


RATE = 2_500_000
BLOCK = 65_536


def test_exact_channel_plan_and_lnb_mapping():
    channels = channel_plan()
    assert len(channels) == 8
    assert channels[0].if_center_hz == pytest.approx(1_075_117_187.5)
    assert channels[2].rf_center_hz == pytest.approx(11_325_117_187.5)
    assert channels[2].if_center_hz == pytest.approx(1_575_117_187.5)
    assert channels[3].if_center_hz == pytest.approx(1_825_117_187.5)
    assert channels[4].lnb_band == "high"
    assert channels[4].if_center_hz == pytest.approx(1_225_117_187.5)
    assert [item.reported_active for item in channels[:3]] == [False, False, True]
    assert STARLINK_GUTTER_WIDTH_HZ == 937_500


def test_known_structure_promotes_and_noise_control_does_not():
    signal = synthetic_starlink_block(RATE, BLOCK, seed=3, gutter_offset_hz=120_000)
    detected = analyze_starlink_block(signal, RATE, fft_size=8192)
    assert detected.promoted and detected.evidence_count >= 2
    assert detected.gutter_depth_db > 8
    assert detected.gutter_offset_hz == pytest.approx(120_000, abs=5_000)
    assert detected.frame_periodicity > .05

    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(BLOCK) + 1j*rng.standard_normal(BLOCK)).astype(np.complex64)
    rejected = analyze_starlink_block(noise, RATE, fft_size=8192)
    assert not rejected.promoted and rejected.evidence_count == 0
    assert frame_periodicity(noise, RATE) < .02


def test_threaded_dual_receiver_reader_preserves_blocks_and_timestamps():
    rx0 = np.arange(10, dtype=np.float32).astype(np.complex64)
    rx1 = (100 + np.arange(10, dtype=np.float32)).astype(np.complex64)
    config = RadioConfig(1_575_117_187.5, RATE, 2_300_000, 40, 0)
    source = FakePairedSource(rx0, rx1, config, block_size=4,
                              start_utc_ns=1_700_000_000_000_000_000)

    blocks = list(threaded_source_blocks(source, paired=True, queue_blocks=1))

    assert [timestamp for timestamp, _ in blocks] == [
        source.start_utc_ns + round(index * 1e9 / RATE) for index in (0, 4, 8)
    ]
    np.testing.assert_array_equal(np.concatenate([values[0] for _, values in blocks]), rx0)
    np.testing.assert_array_equal(np.concatenate([values[1] for _, values in blocks]), rx1)


def test_wide_offset_search_finds_displaced_gutter_and_aggregates():
    rate, count, expected = 30_720_000, 262_144, 4_200_000
    signal = synthetic_starlink_block(rate, count, seed=4, gutter_offset_hz=expected)
    candidates = search_gutter_offsets(signal, rate, search_hz=14_000_000,
                                       fft_size=16_384)
    assert candidates[0].offset_hz == pytest.approx(expected, abs=30_000)
    assert candidates[0].depth_db > 5

    report = aggregate_gutter_search(fake_blocks(sample_rate_hz=rate,
        block_size=count, duration_s=2*count/rate, receiver_count=2,
        signal=True, seed=7), sample_rate_hz=rate, snapshots=2,
        search_hz=14_000_000, fft_size=16_384)
    assert report["receiver_count"] == 2
    assert report["ranked_candidates"][0]["hits"] == 2


def test_starlink_cli_e2e_observe_event_and_reanalyze(tmp_path, capsys):
    output = tmp_path / "observation"
    common = [str(output), "--channel-number", "3", "--duration-s", ".04",
              "--if-offset-hz", "200000",
              "--sample-rate-hz", str(RATE), "--bandwidth-hz", "2300000",
              "--block-size", str(BLOCK), "--fft-size", "8192",
              "--channels", "0,1", "--fake", "--seed", "9"]
    assert cli.main(["starlink-observe", *common]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["promoted_blocks"] >= 1 and result["event_iq"] == "event_iq.npz"
    report = json.loads((output / "observation.json").read_text())
    assert report["receiver_count"] == 2
    assert report["channel"]["if_center_hz"] == pytest.approx(1_575_317_187.5)
    assert report["observed_duration_s"] >= .04
    assert report["continuity"] == "synthetic_exact"
    assert report["max_interblock_timing_excess_s"] == pytest.approx(0, abs=1e-9)

    analysis = output / "reanalysis.json"
    assert cli.main(["starlink-analyze", str(output/"event_iq.npz"), str(analysis),
                     "--fft-size", "8192"]) == 0
    assert json.loads(analysis.read_text())["promoted"] is True


def test_starlink_cli_noise_control_and_high_band_guard(tmp_path, capsys):
    output = tmp_path / "noise"
    assert cli.main(["starlink-observe", str(output), "--channel-number", "3",
        "--duration-s", ".03", "--sample-rate-hz", str(RATE),
        "--bandwidth-hz", "2300000", "--block-size", str(BLOCK),
        "--fft-size", "8192", "--fake", "--fake-noise-only"]) == 0
    assert json.loads((output/"observation.json").read_text())["promoted_blocks"] == 0
    assert not (output/"event_iq.npz").exists()
    assert cli.main(["starlink-observe", str(tmp_path/"high"), "--channel-number", "5",
                     "--duration-s", ".01", "--fake"]) == 1
    assert "22 kHz" in capsys.readouterr().err


def test_starlink_offset_search_cli_e2e(tmp_path, capsys):
    output = tmp_path / "offset.json"
    assert cli.main(["starlink-offset-search", str(output), "--channel-number", "3",
        "--sample-rate-hz", "30720000", "--block-size", "262144",
        "--snapshots", "2", "--channels", "0,1", "--fake"]) == 0
    report = json.loads(output.read_text())
    assert report["schema"] == "leo-tracker.starlink-offset-search/v1"
    assert report["receiver_count"] == 2 and report["snapshots"] == 2
    assert json.loads(capsys.readouterr().out)["top_candidates"]
