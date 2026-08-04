import json

import numpy as np

from leo_tracker.radio import cli
from leo_tracker.radio.doppler_observations import associate_boundary_tracks


def _track(track_id, start, stop, centers, slope=1000.0):
    receivers = []
    times = np.linspace(start, stop, 9)
    for receiver, center in enumerate(centers):
        values = center+slope*(times-(start+stop)/2)
        receivers.append({"receiver": receiver, "time_s": times.tolist(),
                          "centroid_hz": values.tolist()})
    return {"track_id": track_id, "start_elapsed_s": start,
            "stop_elapsed_s": stop, "receivers": receivers,
            "confidence": {"receiver_path_correlation": .92}}


def test_boundary_association_distinguishes_sky_from_baseband_step():
    before = _track("before", 5, 9, (100_000, 120_000))
    sky = _track("sky", 11, 15, (-394_000, -374_000))
    baseband = _track("baseband", 11, 15, (100_000, 120_000))

    sky_result = associate_boundary_tracks(
        [before], [sky], center_delta_hz=500_000, boundary_time_s=10)
    baseband_result = associate_boundary_tracks(
        [before], [baseband], center_delta_hz=500_000, boundary_time_s=10)

    assert sky_result[0]["classification"] == "sky-fixed"
    assert sky_result[0]["sky_fixed_qualified"]
    assert max(abs(x["sky_step_error_hz"]) for x in sky_result[0]["receivers"]) < 1
    assert baseband_result[0]["classification"] == "baseband-fixed"
    assert baseband_result[0]["baseband_fixed_qualified"]


def test_boundary_association_rejects_tracks_far_from_transition():
    before = _track("before", 0, 2, (100_000, 120_000))
    after = _track("after", 18, 20, (-400_000, -380_000))
    assert not associate_boundary_tracks(
        [before], [after], center_delta_hz=500_000, boundary_time_s=10)


def test_doppler_observation_cli_writes_capture_and_monitoring_schema(tmp_path, capsys):
    measurement = tmp_path/"capture.npz"; output = tmp_path/"capture.json"
    assert cli.main(["starlink-measurement-capture", str(measurement), "--fake",
        "--snapshots", "40", "--block-size", "2048", "--fft-size", "1024",
        "--output-bins", "256", "--sample-rate-hz", "1000000",
        "--bandwidth-hz", "800000", "--interleaved-dither-hz", "200000",
        "--dither-segment-s", ".02048", "--dither-discard-buffers", "1"]) == 0
    capsys.readouterr()
    assert cli.main(["doppler-observations", str(measurement), str(output),
                     "--event-frequency-bins", "256"]) == 0
    summary = json.loads(capsys.readouterr().out); report = json.loads(output.read_text())
    assert report["schema"] == "leo-tracker.doppler-observations/v1"
    assert report["capture"]["start_utc"] and report["capture"]["stop_utc"]
    assert report["capture"]["receiver_count"] == 2
    assert report["monitoring"]["sample_rate_hz"] == 1_000_000
    assert len(report["monitoring"]["segments"]) == 4
    segment = report["monitoring"]["segments"][0]
    assert segment["monitored_rf_low_hz"] < segment["monitored_rf_high_hz"]
    assert "frequency_shift_tracks" in report["detections"]
    assert summary["output"] == str(output)


def test_assumption_policy_processes_rejected_tracks_without_erasing_validation(tmp_path, capsys):
    measurement = tmp_path/"capture.npz"; output = tmp_path/"capture.json"
    assert cli.main(["starlink-measurement-capture", str(measurement), "--fake",
        "--snapshots", "40", "--block-size", "2048", "--fft-size", "1024",
        "--output-bins", "256", "--sample-rate-hz", "1000000",
        "--bandwidth-hz", "800000"]) == 0
    capsys.readouterr()
    assert cli.main(["doppler-observations", str(measurement), str(output),
        "--event-frequency-bins", "256", "--assume-all-shifts-doppler"]) == 0
    capsys.readouterr(); report = json.loads(output.read_text())
    assert report["processing_policy"]["assume_all_shifts_doppler"]
    assert report["summary"]["processed_as_doppler_count"] == report["summary"]["track_count"]
    for track in report["detections"]["frequency_shift_tracks"]:
        assert track["processed_as_doppler"]
        assert track["validation_passed"] == track["accepted"]
