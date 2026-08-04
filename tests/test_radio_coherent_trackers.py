import numpy as np
import pytest

from leo_tracker.radio import cli
from leo_tracker.radio.iq_evidence import IQ_SCHEMA
from leo_tracker.radio.tracking.coherent import (
    cross_ambiguity, fll_frequency, polynomial_phase_track, repetition_search)


def test_fll_and_polynomial_phase_recover_linear_frequency_drift():
    sample_rate = 100_000.0
    time = np.arange(100_000)/sample_rate
    frequency, drift = 8_000.0, 400.0
    phase = 2*np.pi*(frequency*time+.5*drift*time**2)
    iq = np.exp(1j*phase).astype(np.complex64)

    fll = fll_frequency(iq, sample_rate, samples_per_estimate=5_000)
    phase_fit = polynomial_phase_track(iq, sample_rate)

    assert fll["drift_hz_s"] == pytest.approx(drift, rel=.02)
    assert phase_fit["drift_hz_s"] == pytest.approx(drift, rel=1e-3)
    assert phase_fit["phase_residual_rms_rad"] < .02
    assert len(phase_fit["time_s"]) <= 512
    assert phase_fit["fit_point_count"] >= len(phase_fit["time_s"])


def test_repetition_search_finds_blind_complex_period():
    rng = np.random.default_rng(8)
    symbol = (rng.normal(size=127)+1j*rng.normal(size=127)).astype(np.complex64)
    iq = np.tile(symbol, 8)
    report = repetition_search(iq, minimum_lag=100, maximum_lag=150)
    assert report["best_lag_samples"] == 127
    assert report["best_correlation"] > .99


def test_cross_ambiguity_recovers_known_delay_and_doppler():
    rng = np.random.default_rng(5); sample_rate = 20_000.0
    template = np.exp(1j*rng.uniform(-np.pi, np.pi, 512)).astype(np.complex64)
    delay, doppler = 7, 625.0
    time = np.arange(template.size)/sample_rate
    received = np.r_[np.zeros(delay, np.complex64),
        template[:-delay]*np.exp(2j*np.pi*doppler*time[:-delay])]
    report = cross_ambiguity(received, template, sample_rate,
        delays=range(0, 12), doppler_hz=np.arange(0, 1001, 125))
    assert report["best_delay_samples"] == delay
    assert report["best_doppler_hz"] == doppler
    assert report["best_score"] > .98


def test_coherent_iq_tracker_cli_e2e(tmp_path, capsys):
    sample_rate = 20_000.0; time = np.arange(8_192)/sample_rate
    signals = []
    for receiver in (0, 1):
        phase = 2*np.pi*((2_000+receiver*300)*time+250*time**2)
        signals.append(np.exp(1j*phase))
    artifact, output = tmp_path/"iq.npz", tmp_path/"coherent.json"
    np.savez(artifact, schema=np.array(IQ_SCHEMA),
        iq=np.asarray([signals], np.complex64), utc_ns=np.array([1_700_000_000_000_000_000]),
        sample_rate_hz=np.array(sample_rate), center_frequency_hz=np.array(1.6e9))
    assert cli.main(["doppler-iq-track", str(artifact), str(output),
        "--estimates-per-block", "8", "--repetition-maximum-lag", "128"]) == 0
    assert __import__("json").loads(capsys.readouterr().out)["receiver_count"] == 2
    report = __import__("json").loads(output.read_text())
    assert report["schema"] == "leo-tracker.coherent-doppler-ensemble/v1"
    for receiver in report["blocks"][0]["receivers"]:
        assert receiver["fll"]["drift_hz_s"] == pytest.approx(500, rel=.05)


def test_cross_ambiguity_cli_e2e(tmp_path, capsys):
    rng = np.random.default_rng(12); sample_rate = 20_000.0
    template = np.exp(1j*rng.uniform(-np.pi, np.pi, 512)).astype(np.complex64)
    delay, doppler = 6, 500.0
    time = np.arange(template.size)/sample_rate
    received = np.r_[np.zeros(delay, np.complex64),
        template[:-delay]*np.exp(2j*np.pi*doppler*time[:-delay])]
    artifact, reference, output = (tmp_path/"iq.npz", tmp_path/"template.npy",
                                   tmp_path/"ambiguity.json")
    np.savez(artifact, iq=received[None, None], sample_rate_hz=np.array(sample_rate))
    np.save(reference, template)
    assert cli.main(["doppler-ambiguity", str(artifact), str(reference), str(output),
        "--maximum-delay-samples", "10", "--minimum-doppler-hz", "0",
        "--maximum-doppler-hz", "1000", "--doppler-step-hz", "125"]) == 0
    summary = __import__("json").loads(capsys.readouterr().out)
    report = __import__("json").loads(output.read_text())
    assert summary["best_delay_samples"] == report["best_delay_samples"] == delay
    assert report["best_doppler_hz"] == doppler
    assert report["schema"] == "leo-tracker.cross-ambiguity/v1"


def test_coherent_iq_inter_block_tracker_allows_lnb_offset(tmp_path):
    sample_rate, size, drift = 100_000.0, 16_384, 800.0
    block_times = np.asarray((0, 2, 4, 6), float)
    iq = np.empty((block_times.size, 2, size), np.complex64)
    local_time = np.arange(size)/sample_rate
    for block, elapsed in enumerate(block_times):
        for receiver in (0, 1):
            frequency = 5_000+receiver*1_200+drift*elapsed
            iq[block, receiver] = np.exp(2j*np.pi*frequency*local_time)
    artifact, output = tmp_path/"blocks.npz", tmp_path/"coherent.json"
    np.savez(artifact, iq=iq, sample_rate_hz=np.array(sample_rate),
        utc_ns=np.asarray(1_700_000_000_000_000_000+block_times*1e9, np.int64))
    assert cli.main(["doppler-iq-track", str(artifact), str(output),
        "--estimates-per-block", "8", "--repetition-maximum-lag", "64"]) == 0
    report = __import__("json").loads(output.read_text())
    assert all(item["qualified"] for item in report["receiver_tracks"])
    assert all(item["drift_hz_s"] == pytest.approx(drift, rel=.02)
               for item in report["receiver_tracks"])
    assert report["joint_track"]["qualified"]
    assert report["joint_track"]["receiver_frequency_offset_hz"] == pytest.approx(1_200,
                                                                                   abs=5)
