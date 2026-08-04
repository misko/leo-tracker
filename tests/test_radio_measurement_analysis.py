import json
from datetime import datetime, timedelta, timezone

import numpy as np

from leo_tracker.radio import cli
from leo_tracker.radio.dashboard import DashboardModel
from leo_tracker.radio.measurement import MEASUREMENT_SCHEMA
from leo_tracker.radio.measurement import capture_measurement_waterfall
from leo_tracker.radio.measurement_analysis import (
    annotated_doppler_tracks, paired_doppler_paths)


def test_plot_paths_include_only_paired_non_broadband_events():
    event = lambda frequency, broadband=False: {"time_s": [1, 2, 3],
        "centroid_hz": [frequency, frequency-1000, frequency-2000],
        "broadband": broadband}
    analysis = {"events": [[event(10_000), event(20_000), event(30_000, True)],
                            [event(12_000), event(22_000), event(32_000, True)]],
        "joint_events": [
            {"association": {"rx0_index": 0, "rx1_index": 1,
                "rx0_drift_hz_s": -1000, "rx1_drift_hz_s": -900},
             "doppler_observation": {"qualified": True}},
            {"association": {"rx0_index": 2, "rx1_index": 2,
                "rx0_drift_hz_s": 1000, "rx1_drift_hz_s": 1100},
             "doppler_observation": {"qualified": False}}]}
    rx0, rx1 = paired_doppler_paths(analysis, 0), paired_doppler_paths(analysis, 1)
    assert len(rx0) == len(rx1) == 1
    assert rx0[0]["centroid_hz"] == [10_000, 9_000, 8_000]
    assert rx1[0]["centroid_hz"] == [22_000, 21_000, 20_000]
    assert rx0[0]["validated"] and rx0[0]["drift_hz_s"] == -1000


def test_annotation_tracks_are_coherent_limited_and_time_distributed():
    events = [[], []]
    joint = []
    # Two candidates in the first interval and one later: the first pass must
    # favor temporal coverage rather than filling the plot from one burst.
    for index, (start, duration, correlation) in enumerate(
            ((3, 4, .8), (10, 8, .9), (65, 3, .7), (95, .4, .99))):
        for receiver in (0, 1):
            events[receiver].append({"time_s": [start, start+duration],
                "centroid_hz": [100_000+receiver*10_000,
                                100_000+receiver*10_000-duration*2_000],
                "broadband": False})
        joint.append({"association": {"rx0_index": index, "rx1_index": index,
            "rx0_drift_hz_s": -2_000, "rx1_drift_hz_s": -2_000,
            "centered_path_correlation": correlation, "association_score": .8},
            "doppler_observation": {"qualified": index == 0}})

    selected = annotated_doppler_tracks(
        {"events": events, "joint_events": joint}, maximum=2)

    assert [item["track_index"] for item in selected] == [1, 2]
    assert all(item["duration_s"] >= .5 for item in selected)


def _tle_catalog(start):
    times = [start, start + timedelta(seconds=10), start + timedelta(seconds=20)]
    doppler = (-120_000, 0, 120_000)
    points = [{"time": time.isoformat().replace("+00:00", "Z"),
               "expected_doppler_hz": shift, "range_rate_km_s": 0}
              for time, shift in zip(times, doppler)]
    return {"generated_at": "synthetic", "carrier_hz": 11_580_117_187.5,
            "observer": {"latitude_deg": 37.84903, "longitude_deg": -122.48564,
                         "altitude_m": 55},
            "satellites": [{"name": "STARLINK-SYNTHETIC", "norad_id": 42,
                "passes": [{"rise": points[0],
                    "culmination": {**points[1], "elevation_deg": 70},
                    "set": points[2]}]}]}


def _write_synthetic_measurement(path, *, moving):
    rng = np.random.default_rng(17)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    count, bins, bin_hz = 201, 256, 7_500
    utc = np.array([(start + timedelta(seconds=index / 10)).timestamp() * 1e9
                    for index in range(count)], dtype=np.int64)
    frequencies = (np.arange(bins) - bins // 2) * bin_hz
    spectra = rng.normal(-80, .04, (2, count, bins)).astype(np.float32)
    shifts = (np.rint(np.linspace(-120_000, 120_000, count) / bin_hz).astype(int)
              if moving else np.zeros(count, dtype=int))
    for receiver, base in enumerate((80, 170)):
        spectra[receiver, np.arange(count), base + shifts] += 1.2
    shape = (2, count)
    np.savez_compressed(path,
        schema=np.array(MEASUREMENT_SCHEMA), psd_db_raw_per_hz=spectra,
        utc_ns=utc, frequency_offsets_hz=frequencies,
        sample_rate_hz=np.array(1_920_000.0), bandwidth_hz=np.array(1_800_000.0),
        center_frequency_hz=np.array(1_830_117_187.5), fft_size=np.array(256),
        samples_per_snapshot=np.array(19_200), rms_raw=np.ones(shape),
        peak_raw=np.ones(shape), crest_factor_db=np.zeros(shape),
        clip_fraction=np.zeros(shape), hardware_gain_db=np.full(shape, 50.0),
        gain_mode=np.array("manual"), configured_gain_db=np.array(50.0),
        identity_json=np.array(json.dumps({"kind": "synthetic-e2e", "gps_fix": {
            "latitude_deg": 37.84903, "longitude_deg": -122.48564,
            "host_minus_gps_s": .07, "fix_quality": 2, "satellites": 10,
            "hdop": 1.02}})),
        lnb_lo_hz=np.array(9_750_000_000.0))
    return start


def test_measurement_capture_analysis_cli_e2e_has_finite_events_and_six_views(
        tmp_path, capsys, monkeypatch):
    measurement = tmp_path / "measurement.npz"
    analysis = tmp_path / "analysis.json"
    plot = tmp_path / "analysis.png"
    monkeypatch.setattr(cli, "read_nmea_snapshot", lambda path, timeout_s: {
        "latitude_deg": 37.84903, "longitude_deg": -122.48564,
        "host_minus_gps_s": .07, "fix_quality": 2, "satellites": 10,
        "hdop": 1.02})
    assert cli.main(["starlink-measurement-capture", str(measurement), "--fake",
        "--sample-rate-hz", "1000000", "--bandwidth-hz", "900000",
        "--snapshots", "30", "--block-size", "4096", "--fft-size", "1024",
        "--output-bins", "256", "--gain-db", "40",
        "--gps-device", "/dev/fake-gps"]) == 0
    capsys.readouterr()

    assert cli.main(["starlink-measurement-analyze", str(measurement), str(analysis),
                     "--plot", str(plot), "--threshold-db", ".2",
                     "--carrier-hz", "11325117187.5"]) == 0
    output = json.loads(capsys.readouterr().out)
    report = json.loads(analysis.read_text())

    assert output["events"][0] > 0 and output["events"][1] > 0
    assert output["joint_events"] > 0
    assert plot.is_file() and plot.stat().st_size > 10_000
    assert report["measurement"]["duty_fraction"] < .1
    assert report["measurement"]["artifact_frequency_bins"] == 256
    assert report["measurement"]["event_frequency_bins"] == 256
    assert report["measurement"]["median_read_duration_s"] is None
    assert report["capture_identity"]["kind"] == "fake-measurement"
    assert report["capture_identity"]["gps_fix"]["fix_quality"] == 2
    # Doppler physics uses original Ku RF, independently of LNB IF and Pluto tuning.
    assert report["carrier_hz"] == 11_325_117_187.5
    for receiver in report["events"]:
        assert all(event["start_time_s"] > 0 and event["stop_time_s"] < 3 for event in receiver)


def test_tle_doppler_artifact_cli_to_dashboard_e2e_with_stationary_control(tmp_path, capsys):
    root = tmp_path / "watch"
    for directory in ("chunks", "analysis", "plots"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    catalog_path = root / "passes.json"
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    catalog_path.write_text(json.dumps(_tle_catalog(start)))

    results = []
    for chunk, moving in enumerate((True, False)):
        stem = f"chunk-{chunk:05d}-ch3-20260802T000000Z"
        measurement = root / "chunks" / f"{stem}.npz"
        analysis = root / "analysis" / f"{stem}.json"
        plot = root / "plots" / f"{stem}.png"
        _write_synthetic_measurement(measurement, moving=moving)
        assert cli.main(["starlink-measurement-analyze", str(measurement),
                         str(analysis), "--passes", str(catalog_path),
                         "--plot", str(plot), "--tle-dwell-window-s", "25",
                         "--tle-dwell-step-s", "5", "--tle-comb-spacing-hz", "43900"]) == 0
        cli_output = json.loads(capsys.readouterr().out)
        report = json.loads(analysis.read_text())
        results.append((cli_output, report))
        assert plot.is_file() and plot.stat().st_size > 10_000

    assert results[0][0]["tle_guided_candidates"] == 1
    assert results[0][0]["tle_guided_models"]
    assert results[0][1]["tle_guided_search"]["configuration"]["dwell_window_s"] == 25
    assert results[0][1]["tle_guided_search"]["configuration"]["dwell_step_s"] == 5
    assert results[0][1]["tle_guided_search"]["configuration"]["comb_spacing_hz"] == 43_900
    assert results[0][1]["observer_validation"]["position_within_100m"]
    assert results[0][1]["observer_validation"]["timing_within_1s"]
    assert results[0][1]["tle_guided_search"]["candidates"][0]["norad_id"] == 42
    assert results[1][0]["tle_guided_candidates"] == 0
    assert "stationary-path control" in " ".join(
        results[1][1]["tle_guided_search"]["candidates"][0]["rejection_reasons"])

    snapshot = DashboardModel(root).snapshot(start + timedelta(seconds=21))
    assert snapshot["status"]["completed_chunks"] == 2
    assert snapshot["status"]["detection_count"] == 1
    assert snapshot["status"]["tle_guided_candidate_count"] == 1
    detection = snapshot["detected"]["detections"][0]
    assert detection["classification"] == "event-qualified + TLE-integrated Doppler candidate"
    assert detection["radial_acceleration_m_s2"] is not None
    assert detection["tle_guided_candidate"]["norad_id"] == 42
    assert detection["tle_guided_candidate"]["stationary_improvement_db"] > .5


def _capture_iq_comb(path, *, moving):
    rng = np.random.default_rng(91)
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    sample_rate, samples, snapshots = 30_720_000.0, 16_384, 41
    sample_indexes = np.arange(samples)

    def blocks():
        for index in range(snapshots):
            elapsed = index * .5
            doppler = (-120_000 + 240_000*index/(snapshots-1)) if moving else 0
            receivers = []
            for base_hz in (-3_000_000, 3_000_000):
                iq = (rng.normal(0, .1, samples) + 1j*rng.normal(0, .1, samples))
                for tooth in range(-4, 5):
                    frequency = base_hz + tooth*43_900 + doppler
                    iq += .012*np.exp(2j*np.pi*frequency*sample_indexes/sample_rate)
                receivers.append(np.asarray(iq, np.complex64))
            yield int((start+timedelta(seconds=elapsed)).timestamp()*1e9), receivers

    capture_measurement_waterfall(blocks(), path, sample_rate_hz=sample_rate,
        center_frequency_hz=1_830_117_187.5, bandwidth_hz=20_000_000,
        snapshots=snapshots, fft_size=4096, output_bins=4096,
        samples_per_snapshot=samples, lnb_lo_hz=9_750_000_000,
        gain_mode="manual", configured_gain_db=50,
        gain_reader=lambda: (50, 50), identity={"kind": "synthetic-iq-comb"})
    return start


def test_actual_rate_iq_fft_cli_recovers_moving_comb_and_rejects_stationary(tmp_path, capsys):
    catalog = tmp_path / "passes.json"
    start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    catalog.write_text(json.dumps(_tle_catalog(start)))
    counts = []
    reports = []
    for name, moving in (("moving", True), ("stationary", False)):
        measurement, analysis = tmp_path/f"{name}.npz", tmp_path/f"{name}.json"
        _capture_iq_comb(measurement, moving=moving)
        assert cli.main(["starlink-measurement-analyze", str(measurement),
                         str(analysis), "--passes", str(catalog),
                         "--threshold-db", "3"]) == 0
        output = json.loads(capsys.readouterr().out)
        counts.append(output["tle_guided_candidates"])
        reports.append(json.loads(analysis.read_text()))

    assert counts == [1, 0]
    moving = reports[0]["tle_guided_search"]["candidates"][0]
    assert reports[0]["measurement"]["artifact_frequency_bins"] == 4096
    assert reports[0]["measurement"]["event_frequency_bins"] == 1024
    assert moving["signal_model"] == "nine-tone-43.9khz-comb"
    assert moving["stationary_improvement_db"] > .03
    stationary = reports[1]["tle_guided_search"]["candidates"][0]
    rejection = " ".join(stationary["rejection_reasons"])
    assert ("stationary-path control" in rejection
            or "wrong-spacing controls" in rejection
            or "not correlated" in rejection)
