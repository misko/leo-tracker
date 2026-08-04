import json

import numpy as np

from leo_tracker.radio import cli
from leo_tracker.radio.iq_evidence import (IQEvidenceSelector, IQ_SCHEMA,
                                           analyze_starlink_waveform_iq,
                                           gate_iq_evidence,
                                           _complex_lag_correlations,
                                           _normalized_lag_correlation,
                                           _texture_lag_correlations)


def test_selector_keeps_bounded_separated_dual_receiver_novelty(tmp_path):
    selector = IQEvidenceSelector(maximum_blocks=2, warmup_blocks=3,
                                  threshold_db=.3, minimum_separation_blocks=2)
    rng = np.random.default_rng(3)
    for index in range(12):
        spectra = rng.normal(0, .01, (2, 128))
        if index in (5, 6, 10):
            spectra[:, 60:64] += .9 if index != 6 else .5
        samples = np.full((2, 1024), index, np.complex64)
        selector.observe(index, 1_700_000_000_000_000_000+index, samples,
                         spectra, (50, 50))
    path = tmp_path/"triggered.npz"
    report = selector.write(path, sample_rate_hz=1_000_000,
                            center_frequency_hz=1.8e9, lnb_lo_hz=9.75e9)
    assert report is not None and report["blocks"] == 2
    with np.load(path, allow_pickle=False) as stored:
        assert str(stored["schema"]) == IQ_SCHEMA
        assert stored["snapshot_index"].tolist() == [5, 10]
        assert stored["iq"].shape == (2, 2, 1024)
        assert np.all(stored["trigger_empirical_tail_probability"] > 0)
        assert np.all(stored["trigger_empirical_tail_probability"] <= 1)


def test_selector_temporal_strata_preserve_coverage_not_only_global_peaks():
    selector = IQEvidenceSelector(maximum_blocks=4, warmup_blocks=2,
        threshold_db=.2, stratum_blocks=4)
    samples = np.zeros((2, 32), np.complex64)
    for index in range(14):
        spectra = np.zeros((2, 64))
        if index >= 2:
            spectra[:, 30:34] = .4+index/100
        selector.observe(index, index, samples, spectra, (50, 50))
    indices = sorted(item.index for item in selector._selected)
    assert indices == [3, 7, 11, 13]


def test_fft_texture_lag_correlations_match_direct_calculation():
    rng = np.random.default_rng(31); values = rng.uniform(size=257)
    shifts = np.array([3, 17, 80])
    observed = _texture_lag_correlations(values, shifts)
    expected = [values[:-shift]@values[shift:] /
                (np.linalg.norm(values[:-shift])*np.linalg.norm(values[shift:]))
                for shift in shifts]
    assert observed.tolist() == __import__("pytest").approx(expected, abs=1e-12)


def test_fft_complex_lag_correlations_match_direct_calculation():
    rng = np.random.default_rng(32)
    values = rng.normal(size=257)+1j*rng.normal(size=257)
    lags = np.array([3, 17, 80])
    observed = _complex_lag_correlations(values, lags)
    expected = [_normalized_lag_correlation(values, int(lag)) for lag in lags]
    assert observed.tolist() == __import__("pytest").approx(expected, abs=1e-12)


def _waveform_artifact(path):
    rate, count, period = 1_000_000.0, 65_536, 1333
    rng = np.random.default_rng(8)
    symbol = rng.normal(size=period)+1j*rng.normal(size=period)
    repeated = np.resize(symbol, count)
    time = np.arange(count)/rate
    comb = sum(np.exp(2j*np.pi*(80_000+k*43_950)*time) for k in range(-4, 5))
    signal = (repeated+.3*comb).astype(np.complex64)
    iq = np.asarray([[signal, signal*np.exp(.3j)]])
    np.savez(path, schema=np.array(IQ_SCHEMA), iq=iq,
             snapshot_index=np.array([20]), utc_ns=np.array([1_700_000_000_000_000_000]),
             trigger_score_db=np.array([1.2]), hardware_gain_db=np.array([[50, 50]]),
             sample_rate_hz=np.array(rate), center_frequency_hz=np.array(1.8e9),
             identity_json=np.array("{}"))


def test_waveform_analysis_recovers_period_and_tone_spacing(tmp_path):
    path = tmp_path/"iq.npz"; _waveform_artifact(path)
    report = analyze_starlink_waveform_iq(path)
    for receiver in report["results"][0]["receivers"]:
        assert abs(receiver["best_period_s"]-0.001333) < 2e-6
        assert receiver["period_excess_ratio"] > 2
        assert receiver["period_empirical_false_alarm_probability"] < .1
        assert abs(receiver["best_tone_spacing_hz"]-43_950) < 500
        assert receiver["tone_spacing_excess_ratio"] > 1
        assert receiver["tone_spacing_empirical_false_alarm_probability"] < .1
    assert report["results"][0]["dual_receiver_feature_consistent"]


def test_iq_gate_requires_timestamp_overlap_with_qualified_feature(tmp_path, capsys):
    source = tmp_path/"source.npz"
    times = np.array([1_700_000_000_000_000_000,
                      1_700_000_010_000_000_000,
                      1_700_000_020_000_000_000])
    np.savez(source, schema=np.array(IQ_SCHEMA), iq=np.zeros((3, 2, 32), np.complex64),
             snapshot_index=np.arange(3), utc_ns=times,
             trigger_score_db=np.ones(3), hardware_gain_db=np.ones((3, 2)),
             sample_rate_hz=np.array(1e6), center_frequency_hz=np.array(1.8e9),
             identity_json=np.array("{}"))
    def stamp(ns):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ns/1e9, timezone.utc).isoformat().replace("+00:00", "Z")
    report_path = tmp_path/"wide.json"
    report_path.write_text(json.dumps({"candidates": [
        {"leo_like_qualified": True, "start_utc": stamp(times[1]-100_000_000),
         "stop_utc": stamp(times[1]+100_000_000)},
        {"leo_like_qualified": False, "start_utc": stamp(times[2]-100_000_000),
         "stop_utc": stamp(times[2]+100_000_000)}]}))
    output = tmp_path/"gated.npz"
    result = gate_iq_evidence(source, report_path, output, margin_s=0)
    assert result["retained_blocks"] == 1
    with np.load(output, allow_pickle=False) as stored:
        assert stored["utc_ns"].tolist() == [times[1]]

    cli_output = tmp_path/"gated-cli.npz"
    assert cli.main(["starlink-iq-evidence-gate", str(source), str(report_path),
                     str(cli_output), "--margin-s", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["retained_blocks"] == 1
    with np.load(cli_output, allow_pickle=False) as stored:
        assert stored["snapshot_index"].tolist() == [1]


def test_iq_gate_requires_dual_receiver_frequency_path_overlap(tmp_path):
    source = tmp_path/"source.npz"
    times = np.array([1_700_000_010_000_000_000, 1_700_000_020_000_000_000])
    np.savez(source, schema=np.array(IQ_SCHEMA), iq=np.zeros((2, 2, 32), np.complex64),
             snapshot_index=np.arange(2), utc_ns=times, trigger_score_db=np.ones(2),
             hardware_gain_db=np.ones((2, 2)), trigger_bin=np.array([[50, 50], [70, 70]]),
             spectrum_bins=np.array(100), sample_rate_hz=np.array(1e6),
             center_frequency_hz=np.array(1.8e9), lnb_lo_hz=np.array(0),
             identity_json=np.array("{}"))
    from datetime import datetime, timezone
    stamp = lambda ns: datetime.fromtimestamp(ns/1e9, timezone.utc).isoformat().replace(
        "+00:00", "Z")
    candidate = {"leo_like_qualified": True, "start_utc": stamp(times[0]-100_000_000),
        "stop_utc": stamp(times[1]+100_000_000), "time_s": [0, 11],
        "receivers": [{"path_rf_hz": [1.8e9, 1.8e9], "median_width_hz": 15_000},
                      {"path_rf_hz": [1.8e9, 1.8e9], "median_width_hz": 15_000}]}
    wide = tmp_path/"wide.json"; wide.write_text(json.dumps({
        "rf_registration_shift_hz": [0, 0], "candidates": [candidate]}))
    output = tmp_path/"gated.npz"
    result = gate_iq_evidence(source, wide, output, margin_s=0)
    assert result["retained_blocks"] == 1
    assert result["rejected_frequency_blocks"] == 1
    with np.load(output, allow_pickle=False) as stored:
        assert stored["snapshot_index"].tolist() == [0]


def test_waveform_analysis_cli_e2e(tmp_path, capsys):
    path, output = tmp_path/"iq.npz", tmp_path/"analysis.json"
    _waveform_artifact(path)
    assert cli.main(["starlink-waveform-iq-analyze", str(path), str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["blocks"] == 1
    assert json.loads(output.read_text())["schema"].endswith("/v1")
