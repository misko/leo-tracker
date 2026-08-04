import json
from datetime import datetime, timezone

import numpy as np

from leo_tracker.radio import cli
from leo_tracker.radio.confounds import analyze_confound_population


def _fixture(root, count=8):
    (root/"chunks").mkdir(parents=True); (root/"wide").mkdir()
    base = 1_700_000_000_000_000_000
    for index in range(count):
        channel = 3 if index % 4 < 2 else 4
        stem = f"chunk-{index:05d}-ch{channel}-fixture"
        path = root/"chunks"/f"{stem}.npz"
        identity = {"tuning_dither_hz": 0 if index % 2 == 0 else 1_250_000,
                    "host_temperature_c": 50+index, "radio_temperature_c": 55+index,
                    "discarded_settling_buffers": 8}
        np.savez(path, identity_json=np.array(json.dumps(identity)),
            utc_ns=np.array([base+index*100_000_000_000,
                             base+index*100_000_000_000+10_000_000_000]),
            rms_raw=np.full((2, 2), 100+index), clip_fraction=np.zeros((2, 2)),
            read_duration_ns=np.full(2, 2_000_000+index),
            snapshot_observer_score_db=np.array([1, 1.1+index/100]))
        qualified = index in (1, 2, 3)
        start_ns = base+index*100_000_000_000+2_000_000_000
        start = datetime.fromtimestamp(start_ns/1e9, timezone.utc).isoformat().replace(
            "+00:00", "Z")
        candidate = {"leo_like_qualified": qualified, "start_utc": start,
            "start_time_s": 2, "receiver_path_correlation": .7+index/100}
        (root/"wide"/f"{stem}.json").write_text(json.dumps({
            "source": str(path), "candidates": [candidate]}))


def test_confound_analysis_reports_order_settling_telemetry_and_clustering(tmp_path):
    root = tmp_path/"watch"; _fixture(root)
    report = analyze_confound_population(root, settling_window_s=5)
    assert report["capture_count"] == 8
    assert report["qualified_event_count"] == 3
    assert report["qualified_inside_settling_window"] == 3
    assert len(report["controls"]["tuning"]["groups"]) == 2
    assert report["controls"]["host_temperature_vs_candidate_correlation"]["n"] == 8
    assert report["controls"]["radio_temperature_vs_candidate_correlation"]["n"] == 8
    cluster = report["controls"]["event_clustering"]
    assert cluster["minimum_three_event_span_s"] == 200
    assert cluster["conditional_exact_p"] is not None


def test_confound_analysis_cli_e2e(tmp_path, capsys):
    root = tmp_path/"watch"; _fixture(root)
    output = tmp_path/"confounds.json"
    assert cli.main(["starlink-confound-analyze", str(root), str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["capture_count"] == 8
    assert json.loads(output.read_text())["schema"].endswith("/v1")
