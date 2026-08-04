import numpy as np
import pytest

from leo_tracker.radio import cli
from leo_tracker.radio.measurement import MEASUREMENT_SCHEMA
from leo_tracker.radio.tracking.association import associate_tracks
from leo_tracker.radio.tracking.broadband import (
    consensus_pilot_tracks, track_envelope_and_edges,
    track_spectral_translation)
from leo_tracker.radio.tracking.dedoppler import search_dedoppler
from leo_tracker.radio.tracking.models import TrackCandidate
from leo_tracker.radio.tracking.observation import (
    TrackingObservation, load_tracking_observation)
from leo_tracker.radio.tracking.tle_match import match_joint_tracks_to_tles
from leo_tracker.radio.tracking.viterbi import search_viterbi_ridges


def _observation(spectra, *, cadence=.1, bin_hz=1_000):
    count, bins = spectra.shape[1:]
    time = np.arange(count)*cadence
    return TrackingObservation("synthetic", np.asarray(spectra, float), time,
        np.asarray(time*1e9, np.int64),
        (np.arange(bins)-bins//2)*bin_hz, 1_500_000_000, 1_000_000,
        900_000, 9_750_000_000, "manual", np.full((2, count), 50.0), {})


def test_direct_dedoppler_recovers_below_row_threshold_dual_receiver_chirp():
    rng = np.random.default_rng(7)
    count, bins, cadence, bin_hz = 101, 256, .1, 1_000
    spectra = rng.normal(-80, .04, (2, count, bins))
    path = 80+np.rint(np.arange(count)*cadence*2_000/bin_hz).astype(int)
    for receiver in (0, 1):
        spectra[receiver, np.arange(count), path+receiver*50] += .24
    candidates = search_dedoppler(_observation(spectra, cadence=cadence, bin_hz=bin_hz),
        integration_s=.5, window_s=10, step_s=5,
        minimum_drift_hz_s=1_000, maximum_drift_hz_s=3_000,
        drift_step_hz_s=250, prominence_db=.03)
    qualified = [item for item in candidates if item.qualified]
    assert {item.receiver for item in qualified} == {0, 1}
    assert all(abs(item.drift_hz_s-2_000) <= 500 for item in qualified[:2])
    assert all(item.diagnostics["stationary_improvement_db"] > 0 for item in qualified)
    assert all(item.false_alarm_probability <= .1 for item in qualified)


def test_direct_dedoppler_time_scramble_rejects_unstructured_noise():
    rng = np.random.default_rng(31)
    spectra = rng.normal(-80, .06, (2, 101, 256))
    candidates = search_dedoppler(_observation(spectra), integration_s=.5,
        window_s=10, step_s=5, minimum_drift_hz_s=2_000,
        maximum_drift_hz_s=2_000, drift_step_hz_s=250, prominence_db=.02)
    # A receiver-local p-value is not a final detection claim; across many
    # intercepts one noise candidate can survive. Independent receiver
    # association must still reject the observation.
    assert not any(item.qualified for item in associate_tracks(candidates))


def test_viterbi_ridge_recovers_continuous_track_over_stationary_competitor():
    rng = np.random.default_rng(44); count, bins = 101, 256
    spectra = rng.normal(-80, .025, (2, count, bins))
    path = 65+np.rint(np.arange(count)*.2).astype(int)
    for receiver in (0, 1):
        spectra[receiver, :, 180+receiver*5] += .18
        spectra[receiver, np.arange(count), path+receiver*70] += .32
    candidates = search_viterbi_ridges(_observation(spectra), windows=[(0, 10)],
        integration_s=.5, maximum_drift_hz_s=5_000, paths_per_window=2)
    positive = [item for item in candidates
                if item.qualified and item.diagnostics["polarity"] == "positive"]
    assert {item.receiver for item in positive} == {0, 1}
    assert all(item.drift_hz_s == pytest.approx(2_000, abs=600) for item in positive[:2])


def test_texture_tracker_recovers_translation_despite_power_change():
    rng = np.random.default_rng(4)
    count, bins = 30, 256
    texture = rng.normal(0, .2, bins)
    spectra = np.empty((2, count, bins))
    for receiver in (0, 1):
        for row in range(count):
            spectra[receiver, row] = -80+row*.03+np.roll(texture, row)
    candidates = track_spectral_translation(
        _observation(spectra, cadence=.25, bin_hz=1_000), [(0, 7.25)],
        maximum_step_bins=3, minimum_correlation=.8)
    assert len(candidates) == 2
    assert all(item.qualified for item in candidates)
    assert all(item.drift_hz_s == pytest.approx(4_000, rel=.05) for item in candidates)


def test_envelope_tracker_warns_that_power_redistribution_is_not_translation():
    count, bins = 30, 256
    spectra = np.full((2, count, bins), -80.0)
    spectra[:, :15, 60:100] += .5
    spectra[:, 15:, 140:180] += .5
    candidates = track_envelope_and_edges(
        _observation(spectra, cadence=.25, bin_hz=2_000), [(0, 7.25)],
        threshold_db=.1, minimum_width_hz=50_000)
    envelopes = [item for item in candidates if item.tracker == "broadband-envelope/v1"]
    assert len(envelopes) == 2
    assert all(not item.qualified for item in envelopes)
    assert all("power redistribution" in " ".join(item.warnings) for item in envelopes)


def test_broadband_edges_qualify_only_common_continuous_translation():
    count, bins = 60, 256
    spectra = np.full((2, count, bins), -80.0)
    for receiver in (0, 1):
        for row in range(10, 50):
            start = 50+(row-10)//2+receiver*3
            spectra[receiver, row, start:start+60] += .8
    candidates = track_envelope_and_edges(
        _observation(spectra, cadence=.25, bin_hz=2_000), [(2.5, 12.25)],
        threshold_db=.1, minimum_width_hz=50_000)
    edges = [item for item in candidates if "edge" in item.tracker]
    assert len(edges) == 4 and all(item.qualified for item in edges)
    assert all(item.drift_hz_s == pytest.approx(4_000, rel=.20) for item in edges)
    joint = []
    for tracker in {item.tracker for item in edges}:
        joint.extend(associate_tracks([item for item in edges if item.tracker == tracker]))
    assert len(joint) == 2 and all(item.qualified for item in joint)


def test_broadband_edges_reject_changing_width_confound():
    count, bins = 40, 256
    spectra = np.full((2, count, bins), -80.0)
    for row in range(count):
        spectra[:, row, 50+row//2:100+row] += .8
    candidates = track_envelope_and_edges(
        _observation(spectra, cadence=.25, bin_hz=2_000), [(0, 9.75)],
        threshold_db=.1, minimum_width_hz=50_000)
    edges = [item for item in candidates if "edge" in item.tracker]
    assert edges and not any(item.qualified for item in edges)
    assert all("common motion" in " ".join(item.warnings) or
               "bandwidth" in " ".join(item.warnings) for item in edges)


def test_consensus_requires_multiple_independent_pilots_and_preserves_support():
    candidates = []
    for receiver in (0, 1):
        for index, drift in enumerate((3_900, 4_050, 4_100, -2_000)):
            times = (0.0, 5.0); center = receiver*1_000_000+index*50_000
            candidates.append(TrackCandidate("dedoppler-linear/v1", receiver, 0, 5,
                times, (center, center+drift*5), drift,
                frequency_low_hz=center, frequency_high_hz=center+abs(drift*5),
                signal_score=1, qualified=True))
    populations = consensus_pilot_tracks(candidates, slope_tolerance_hz_s=300,
                                         minimum_support=3)
    assert len(populations) == 2
    assert all(item.supporting_features == 3 for item in populations)
    assert all(item.drift_hz_s == pytest.approx(4_050, abs=100) for item in populations)


def test_dual_receiver_association_allows_arbitrary_constant_lnb_offset():
    times = tuple(np.linspace(0, 5, 11)); path = np.linspace(10_000, 30_000, 11)
    candidates = [TrackCandidate("dedoppler-linear/v1", receiver, 0, 5, times,
        tuple(path+receiver*900_000), 4_000, qualified=True)
        for receiver in (0, 1)]
    joint = associate_tracks(candidates)
    assert len(joint) == 1 and joint[0].qualified
    assert joint[0].receiver_frequency_offset_hz == pytest.approx(900_000)
    assert joint[0].receiver_path_correlation > .99


def test_dual_receiver_association_rejects_anticorrelated_heldout_power():
    times = tuple(np.linspace(0, 5, 11)); path = np.linspace(10_000, 30_000, 11)
    heldout_times = list(np.linspace(0, 5, 6)); trace = np.arange(6, dtype=float)
    candidates = [TrackCandidate("dedoppler-linear/v1", receiver, 0, 5, times,
        tuple(path+receiver*900_000), 4_000, qualified=True,
        diagnostics={"heldout_time_s": heldout_times,
                     "heldout_trace_db": (trace if receiver == 0 else -trace).tolist()})
        for receiver in (0, 1)]
    joint = associate_tracks(candidates)
    assert len(joint) == 1 and not joint[0].qualified
    assert any("held-out amplitudes disagree" in warning for warning in joint[0].warnings)


def test_dual_receiver_association_uses_population_offset_consensus():
    times = tuple(np.linspace(0, 5, 11)); candidates = []
    for index, start in enumerate((0, 200_000, 400_000, 600_000)):
        path = np.linspace(start, start+20_000, 11)
        candidates.append(TrackCandidate("ridge", 0, 0, 5, times, tuple(path), 4_000,
                                         qualified=True))
        offset = 900_000 if index < 3 else 1_500_000
        candidates.append(TrackCandidate("ridge", 1, 0, 5, times, tuple(path+offset), 4_000,
                                         qualified=True))
    joint = associate_tracks(candidates, offset_consensus_tolerance_hz=50_000)
    assert sum(item.qualified for item in joint) == 3
    assert any(any("offset outside population consensus" in warning
                   for warning in item.warnings) for item in joint)


def _write_measurement(path, spectra, *, cadence=.1, bin_hz=1_000):
    count, bins = spectra.shape[1:]
    utc = 1_700_000_000_000_000_000 + np.asarray(
        np.arange(count)*cadence*1e9, np.int64)
    shape = spectra.shape[:2]
    np.savez_compressed(path, schema=np.array(MEASUREMENT_SCHEMA),
        psd_db_raw_per_hz=np.asarray(spectra, np.float32), utc_ns=utc,
        frequency_offsets_hz=(np.arange(bins)-bins//2)*bin_hz,
        sample_rate_hz=np.array(bins*bin_hz), bandwidth_hz=np.array(bins*bin_hz*.9),
        center_frequency_hz=np.array(1_600_000_000.0), fft_size=np.array(bins),
        samples_per_snapshot=np.array(4096), rms_raw=np.ones(shape), peak_raw=np.ones(shape),
        crest_factor_db=np.zeros(shape), clip_fraction=np.full(shape, np.nan),
        hardware_gain_db=np.full(shape, 50.0), gain_mode=np.array("manual"),
        configured_gain_db=np.array(50.0), identity_json=np.array("{}"),
        lnb_lo_hz=np.array(9_750_000_000.0))


def test_windowed_loader_preserves_float32_and_releases_unselected_payload(tmp_path):
    spectra = np.zeros((2, 1_000, 512), np.float32)
    path = tmp_path/"measurement.npz"; _write_measurement(path, spectra)
    full = load_tracking_observation(path)
    window = full.window(10, 11)
    assert full.spectra_db.dtype == window.spectra_db.dtype == np.float32
    assert window.spectra_db.nbytes < full.spectra_db.nbytes/50
    assert not np.shares_memory(window.spectra_db, full.spectra_db)


def test_tracker_ensemble_cli_e2e_writes_common_schema(tmp_path, capsys):
    rng = np.random.default_rng(22); count, bins = 101, 256
    spectra = rng.normal(-80, .03, (2, count, bins)).astype(np.float32)
    path_bins = 70+np.rint(np.arange(count)*.2).astype(int)
    for receiver in (0, 1):
        spectra[receiver, np.arange(count), path_bins+receiver*70] += .3
    measurement, output = tmp_path/"measurement.npz", tmp_path/"trackers.json"
    plot = tmp_path/"trackers.png"
    _write_measurement(measurement, spectra)
    assert cli.main(["doppler-trackers", str(measurement), str(output),
        "--window", "0:10", "--integration-s", ".5",
        "--plot", str(plot),
        "--dedoppler-window-s", "10", "--dedoppler-step-s", "5",
        "--minimum-drift-hz-s", "1000", "--maximum-drift-hz-s", "3000"]) == 0
    summary = __import__("json").loads(capsys.readouterr().out)
    report = __import__("json").loads(output.read_text())
    assert summary["candidates"] == len(report["candidates"])
    assert report["schema"] == "leo-tracker.tracker-ensemble/v1"
    trackers = {item["tracker"] for item in report["candidates"]}
    assert {"dedoppler-linear/v1", "viterbi-ridge/v1",
            "spectral-texture-translation/v1"} <= trackers
    assert report["configuration"]["analysis_windows_s"] == [[0.0, 10.0]]
    assert plot.is_file() and plot.stat().st_size > 1_000


def test_comb_adapter_restores_parent_elapsed_time_origin():
    rng = np.random.default_rng(19)
    spectra = rng.normal(-80, .01, (2, 121, 512))
    observation = _observation(spectra, cadence=.1, bin_hz=1_000).window(5, 12)
    from leo_tracker.radio.tracking.adapters import comb_tracks
    candidates = comb_tracks(observation, integration_s=.5, window_s=6, step_s=5)
    assert candidates
    assert min(item.start_time_s for item in candidates) >= 5


def test_tle_identification_is_post_detection_and_matches_drift_rate():
    times = (10.0, 20.0); first = TrackCandidate("ridge", 0, 10, 20, times,
        (0.0, 10_000.0), 1_000, qualified=True)
    second = TrackCandidate("ridge", 1, 10, 20, times,
        (500_000.0, 510_000.0), 1_000, qualified=True)
    from leo_tracker.radio.tracking.models import JointTrack
    joint = JointTrack("ridge", (0, 1), 1, 500_000, 0, 1, True)
    def point(seconds, doppler):
        from datetime import datetime, timezone
        return {"time": datetime.fromtimestamp(seconds, timezone.utc).isoformat(),
                "expected_doppler_hz": doppler, "elevation_deg": 20}
    catalog = {"carrier_hz": 10_000_000_000, "satellites": [{"name": "TEST",
        "norad_id": 123, "passes": [{"rise": point(1_005, -5_000),
        "culmination": point(1_015, 5_000), "set": point(1_025, 15_000)}]}]}
    matches = match_joint_tracks_to_tles([first, second], [joint], catalog,
        capture_start_unix_s=1_000, observed_carrier_hz=10_000_000_000)
    assert matches[0]["qualified"] and matches[0]["norad_id"] == 123
    assert matches[0]["compatible"] and matches[0]["specific_identification"]
    assert matches[0]["predicted_drift_hz_s"] == pytest.approx(1_000)
    assert match_joint_tracks_to_tles([first, second], [
        JointTrack("ridge", (0, 1), 1, 500_000, 0, 1, False)], catalog,
        capture_start_unix_s=1_000, observed_carrier_hz=10_000_000_000) == []


def test_tle_matching_reports_compatible_but_ambiguous_dense_passes():
    times = (10.0, 20.0)
    candidates = [TrackCandidate("ridge", receiver, 10, 20, times,
        (receiver*500_000.0, receiver*500_000.0+10_000), 1_000, qualified=True)
        for receiver in (0, 1)]
    from leo_tracker.radio.tracking.models import JointTrack
    joint = JointTrack("ridge", (0, 1), 1, 500_000, 0, 1, True)
    from datetime import datetime, timezone
    def point(seconds, doppler):
        return {"time": datetime.fromtimestamp(seconds, timezone.utc).isoformat(),
                "expected_doppler_hz": doppler}
    passes = [{"rise": point(1_005, -5_000+offset),
               "culmination": point(1_015, 5_000+offset),
               "set": point(1_025, 15_000+offset)} for offset in (0, 50)]
    catalog = {"carrier_hz": 1e10, "satellites": [
        {"name": f"S{index}", "norad_id": index, "passes": [pass_]}
        for index, pass_ in enumerate(passes)]}
    matches = match_joint_tracks_to_tles(candidates, [joint], catalog,
        capture_start_unix_s=1_000, observed_carrier_hz=1e10)
    assert sum(item["compatible"] for item in matches) == 2
    assert not any(item["qualified"] for item in matches)
    assert matches[0]["compatible_pass_count"] == 2


def test_retrospective_tle_match_cli_updates_existing_report(tmp_path, capsys):
    spectra = np.full((2, 101, 64), -80, np.float32)
    measurement = tmp_path/"measurement.npz"; _write_measurement(measurement, spectra)
    candidate_rows = [TrackCandidate("ridge", receiver, 0, 10, (0.0, 10.0),
        (receiver*500_000.0, receiver*500_000.0+10_000), 1_000, qualified=True).to_dict()
        for receiver in (0, 1)]
    from leo_tracker.radio.tracking.models import JointTrack
    report_path, passes_path, output = (tmp_path/"trackers.json", tmp_path/"passes.json",
                                        tmp_path/"rematched.json")
    report_path.write_text(__import__("json").dumps({
        "schema": "leo-tracker.tracker-ensemble/v1", "source": str(measurement),
        "configuration": {}, "metrics": {}, "candidates": candidate_rows,
        "joint_tracks": [JointTrack("ridge", (0, 1), 1, 500_000, 0, 1, True).to_dict()]}))
    start_unix = 1_700_000_000
    from datetime import datetime, timezone
    def point(seconds, doppler):
        return {"time": datetime.fromtimestamp(start_unix+seconds, timezone.utc).isoformat(),
                "expected_doppler_hz": doppler}
    passes_path.write_text(__import__("json").dumps({"carrier_hz": 11_350_000_000,
        "source": {"catalog_sha256": "abc"}, "satellites": [{"name": "TEST",
        "norad_id": 12, "passes": [{"rise": point(0, 0),
        "culmination": point(5, 5_000), "set": point(10, 10_000)}]}]}))
    assert cli.main(["doppler-tle-match", str(report_path), str(passes_path),
                     str(output)]) == 0
    summary = __import__("json").loads(capsys.readouterr().out)
    updated = __import__("json").loads(output.read_text())
    assert summary["specific_identifications"] == 1
    assert updated["identifications"][0]["norad_id"] == 12
    assert updated["configuration"]["tle_catalog"]["catalog_sha256"] == "abc"
