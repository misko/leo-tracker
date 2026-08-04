import json

import numpy as np

from leo_tracker.radio import cli
from leo_tracker.radio.measurement import MEASUREMENT_SCHEMA
from leo_tracker.radio.rf_baseline import analyze_rf_novelty, build_rf_baseline


def _artifact(path, center, *, moving=False, wide=False, quantized=False):
    rng = np.random.default_rng(22); count, bins, bin_hz = 200, 256, 10_000.0
    offsets = (np.arange(bins)-bins//2)*bin_hz
    # Persistent features are functions of absolute IF/RF, not baseband bins.
    absolute = center+offsets
    profile = (.7*np.exp(-((absolute-1_800_100_000)/25_000)**2)+
               .4*np.exp(-((absolute-1_800_450_000)/35_000)**2))
    spectra = np.asarray([[profile+rng.normal(0, .01, bins) for _ in range(count)]
                          for _ in range(2)], np.float32)
    if moving:
        for receiver in range(2):
            for row in range(count):
                frequency = 1_799_900_000+row*4_000
                index = int(np.argmin(abs(absolute-frequency)))
                spectra[receiver, row, index] += 1.0
    if wide:
        for receiver in range(2):
            for row in range(20, 161):
                center_hz = 1_799_700_000+row*1_000
                selected = abs(absolute-center_hz) <= 250_000
                spectra[receiver, row, selected] -= 1.0
    shape = (2, count)
    stored = np.rint(spectra/.01).astype(np.int16) if quantized else spectra
    fields = dict(schema=np.array(MEASUREMENT_SCHEMA),
        psd_db_raw_per_hz=stored, utc_ns=np.arange(count,dtype=np.int64)*100_000_000,
        frequency_offsets_hz=offsets, sample_rate_hz=bins*bin_hz,
        bandwidth_hz=2_000_000.0, center_frequency_hz=center, fft_size=bins,
        samples_per_snapshot=4096, rms_raw=np.ones(shape), peak_raw=np.ones(shape),
        crest_factor_db=np.zeros(shape), clip_fraction=np.zeros(shape),
        hardware_gain_db=np.full(shape,50.0), gain_mode=np.array("manual"),
        configured_gain_db=np.array(50.0), identity_json=np.array("{}"),
        lnb_lo_hz=np.array(0.0))
    if quantized:
        fields["psd_db_quantization_db"] = np.array(.01)
    np.savez_compressed(path, **fields)


def test_rf_registered_baseline_removes_persistent_features_and_keeps_mover(tmp_path):
    first, second, moving = tmp_path/"a.npz", tmp_path/"b.npz", tmp_path/"moving.npz"
    baseline = tmp_path/"baseline.npz"
    _artifact(first, 1_800_000_000.0); _artifact(second, 1_800_200_000.0)
    _artifact(moving, 1_800_100_000.0, moving=True)
    built = build_rf_baseline([first, second], baseline)
    report = analyze_rf_novelty(moving, baseline, threshold_db=.3)
    assert built["dual_coverage_fraction"] > .8
    assert all(receiver["positive_novel_fraction"] > 0 for receiver in report["receivers"])
    assert all(any(abs(frequency-1_799_950_000) < 200_000
                   for frequency in receiver["strongest_novel_rf_hz"])
               for receiver in report["receivers"])


def test_rf_baseline_and_novelty_cli_e2e(tmp_path, capsys):
    first, second, moving = tmp_path/"a.npz", tmp_path/"b.npz", tmp_path/"moving.npz"
    baseline, report, plot = tmp_path/"baseline.npz", tmp_path/"report.json", tmp_path/"plot.png"
    _artifact(first,1_800_000_000.0); _artifact(second,1_800_200_000.0)
    _artifact(moving,1_800_100_000.0,moving=True)
    assert cli.main(["starlink-rf-baseline",str(baseline),str(first),str(second)]) == 0
    capsys.readouterr()
    assert cli.main(["starlink-rf-novelty",str(moving),str(baseline),str(report),
                     "--plot",str(plot),"--threshold-db",".3"]) == 0
    terminal=json.loads(capsys.readouterr().out); data=json.loads(report.read_text())
    assert terminal["plot"] == str(plot)
    assert data["schema"] == "leo-tracker.rf-novelty/v1"
    assert plot.stat().st_size > 10_000


def test_wide_feature_cli_tracks_dual_receiver_channel_block(tmp_path, capsys):
    first, second, wide = tmp_path/"a.npz", tmp_path/"b.npz", tmp_path/"wide.npz"
    baseline = tmp_path/"baseline.npz"
    output, plot = tmp_path/"wide.json", tmp_path/"wide.png"
    _artifact(first, 1_800_000_000.0); _artifact(second, 1_800_200_000.0)
    _artifact(wide, 1_800_100_000.0, wide=True)
    assert cli.main(["starlink-rf-baseline", str(baseline), str(first), str(second)]) == 0
    capsys.readouterr()
    assert cli.main(["starlink-wide-feature-analyze", str(wide), str(baseline),
                     str(output), "--plot", str(plot), "--threshold-db", ".3"]) == 0
    terminal = json.loads(capsys.readouterr().out)
    report = json.loads(output.read_text())
    assert terminal["candidate_count"] >= 1
    candidate = report["candidates"][0]
    assert candidate["polarity"] == "negative"
    assert candidate["duration_s"] >= 8
    assert 400_000 <= candidate["bounding_width_hz"] <= 700_000
    assert candidate["receiver_path_correlation"] > .9
    assert plot.stat().st_size > 10_000


def test_quantized_artifacts_flow_through_baseline_and_wide_analysis(tmp_path):
    first, second, wide = tmp_path/"a.npz", tmp_path/"b.npz", tmp_path/"wide.npz"
    baseline = tmp_path/"baseline.npz"
    _artifact(first, 1_800_000_000.0, quantized=True)
    _artifact(second, 1_800_200_000.0, quantized=True)
    _artifact(wide, 1_800_100_000.0, wide=True, quantized=True)

    build_rf_baseline([first, second], baseline)
    report = analyze_rf_novelty(wide, baseline, threshold_db=.3)

    assert all(receiver["negative_novel_fraction"] > 0
               for receiver in report["receivers"])
