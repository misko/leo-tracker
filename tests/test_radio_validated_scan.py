import json
import numpy as np

from leo_tracker.radio import cli
from leo_tracker.radio.validated_scan import validated_scan


RATE = 1_000_000
N = 32768
NOMINAL = 100_000_000


def _tone(offset, amplitude=.2, seed=1):
    t = np.arange(N)/RATE; rng = np.random.default_rng(seed)
    return (amplitude*np.exp(2j*np.pi*offset*t)+.02/np.sqrt(2)*(rng.standard_normal(N)+1j*rng.standard_normal(N))).astype(np.complex64)


def _run(acquire, min_prominence=18):
    return validated_scan(acquire, nominal_centers_hz=[NOMINAL], validation_offset_hz=200_000,
        sample_rate_hz=RATE, fft_size=4096, min_prominence_db=min_prominence,
        frequency_tolerance_hz=500, max_features=8)[0]


def test_fixed_absolute_rf_tone_is_validated():
    rf = NOMINAL+300_000
    point = _run(lambda center: _tone(rf-center, seed=round(center)))
    assert point.validation_flag
    assert len(point.validated_features) == 1
    assert abs(point.validated_features[0].absolute_frequency_hz-rf) < 200
    assert point.validated_features[0].validation_score > 15


def test_tuning_locked_baseband_spur_is_rejected():
    point = _run(lambda center: _tone(100_000, seed=round(center)))
    assert point.primary_features and point.shifted_features
    assert not point.validation_flag
    assert point.validated_features == ()
    assert abs(point.primary_features[0].absolute_frequency_hz-point.shifted_features[0].absolute_frequency_hz) > 100_000


def test_noise_is_rejected():
    def noise(center):
        rng = np.random.default_rng(round(center)); return (rng.standard_normal(N)+1j*rng.standard_normal(N)).astype(np.complex64)
    point = _run(noise, min_prominence=20)
    assert not point.validation_flag
    assert not point.validated_features


def test_validated_scan_cli_outputs_json_and_plot(tmp_path, monkeypatch, capsys):
    rf = NOMINAL+300_000
    points = validated_scan(lambda center: _tone(rf-center, seed=round(center)),
        nominal_centers_hz=[NOMINAL], validation_offset_hz=200_000, sample_rate_hz=RATE,
        fft_size=4096, min_prominence_db=18, frequency_tolerance_hz=500)
    monkeypatch.setattr(cli, "validated_scan_pyadi",
                        lambda **kwargs: points * len(kwargs["nominal_centers_hz"]))
    output = tmp_path/"scan"
    code = cli.main(["validated-scan", str(output), "--start-hz", str(NOMINAL),
        "--stop-hz", str(NOMINAL), "--step-hz", "100000", "--sample-rate-hz", str(RATE),
        "--fft-size", "4096"])
    assert code == 0
    payload = json.loads((output/"validated_scan.json").read_text())
    assert payload["validated_feature_count"] == 2
    assert payload["confirmed_feature_count"] == 1
    assert payload["schema"].endswith("/v3")
    assert payload["scan_start_utc_ns"] <= payload["scan_end_utc_ns"]
    assert payload["promotion_grade"] is True
    assert payload["promotion_reasons"] == []
    assert payload["points"][0]["validation_flag"] is True
    assert (output/"validated_scan.png").read_bytes().startswith(b"\x89PNG")
    report = json.loads(capsys.readouterr().out)
    assert report["validated_features"] == 2
    assert report["confirmed_features"] == 1
    assert report["promotion_grade"] is True and report["warning"] is None
    assert report["strongest_validated_features"][0]["acquisition_midpoint_utc_ns"]


def test_deterministic_acquisition_timing_provenance():
    rf = NOMINAL+300_000
    ticks = iter((100, 200, 300, 400))
    point = validated_scan(lambda center: _tone(rf-center, seed=round(center)),
        nominal_centers_hz=[NOMINAL], validation_offset_hz=200_000, sample_rate_hz=RATE,
        fft_size=4096, min_prominence_db=18, frequency_tolerance_hz=500,
        clock_ns=lambda: next(ticks))[0]
    assert point.primary_acquisition_utc_ns == 150
    assert point.shifted_acquisition_utc_ns == 350
    assert point.acquisition_midpoint_utc_ns == 250
    assert point.acquisition_delta_ns == 200


def test_validated_scan_default_and_low_settle_diagnostic_warning(tmp_path, monkeypatch, capsys):
    parser = cli.build_parser()
    defaults = parser.parse_args(["validated-scan", str(tmp_path/"unused"), "--start-hz", "1",
                                  "--stop-hz", "1", "--step-hz", "1"])
    assert defaults.settle_seconds == 3.0
    rf = NOMINAL+300_000
    points = validated_scan(lambda center: _tone(rf-center, seed=round(center)),
        nominal_centers_hz=[NOMINAL], validation_offset_hz=200_000, sample_rate_hz=RATE,
        fft_size=4096, min_prominence_db=18, frequency_tolerance_hz=500)
    monkeypatch.setattr(cli, "validated_scan_pyadi", lambda **kwargs: points)
    output = tmp_path/"diagnostic"
    code = cli.main(["validated-scan", str(output), "--start-hz", str(NOMINAL),
        "--stop-hz", str(NOMINAL), "--step-hz", "1", "--settle-seconds", ".05"])
    assert code == 0
    payload = json.loads((output/"validated_scan.json").read_text())
    assert payload["promotion_grade"] is False
    assert "below promotion minimum" in payload["promotion_reasons"][0]
    report = json.loads(capsys.readouterr().out)
    assert report["promotion_grade"] is False
    assert "diagnostic only" in report["warning"]


def test_validated_scan_cli_repeats_centers_in_acquisition_order(tmp_path, monkeypatch):
    rf = NOMINAL+300_000
    point = validated_scan(lambda center: _tone(rf-center, seed=round(center)),
        nominal_centers_hz=[NOMINAL], validation_offset_hz=200_000, sample_rate_hz=RATE,
        fft_size=4096, min_prominence_db=18, frequency_tolerance_hz=500)[0]
    observed = {}
    def fake_scan(**kwargs):
        observed["centers"] = kwargs["nominal_centers_hz"]
        return [point] * len(kwargs["nominal_centers_hz"])
    monkeypatch.setattr(cli, "validated_scan_pyadi", fake_scan)
    output = tmp_path/"repeat"
    assert cli.main(["validated-scan", str(output), "--start-hz", str(NOMINAL),
        "--stop-hz", str(NOMINAL+200_000), "--step-hz", "200000", "--repeats", "3"]) == 0
    assert observed["centers"] == [NOMINAL, NOMINAL, NOMINAL+200_000, NOMINAL+200_000] * 3
    payload = json.loads((output/"validated_scan.json").read_text())
    assert payload["metadata"]["repeats"] == 3
    assert len(payload["points"]) == 12
