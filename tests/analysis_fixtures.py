"""Receipt fixture shared by the analysis-index tests.

Writes a complete analysis run through the same contract paths production
uses, so a test exercises the real authentication rather than a stub. Lived
in test_analysis_store.py until the DuckDB store it tested was retired.
"""
import hashlib
import json
from pathlib import Path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _completed_run(root: Path, *,
                   name="ch4-lower-edge-narrow-20260811T120000Z",
                   pipeline="kalman-test", probes=3, confirmed=False):
    reports = root / "reports"
    for relative in ("followups", "tracks", "decoded", "associations",
                     "frame-tracks", "channel-links", "fragment-associations",
                     "fragment-diagnostics", "fingerprints", "plots"):
        (reports / relative).mkdir(parents=True, exist_ok=True)
    capture = {
        "state": "complete", "receiver_count": 2,
        "created_utc_ns": 1_786_000_000_000_000_000,
        "center_frequency_hz": 1_709_687_500.0,
        "rf_center_hz": 11_459_687_500.0,
        "sample_rate_hz": 2_500_000.0, "bandwidth_hz": 2_300_000.0,
        "gain_mode": "manual", "configured_gain_db": 50.0,
        "requested_duration_s": 60.0,
        "identity": {"radio_id": "pluto-19f2", "serial": "abc",
                     "receiver_labels": ["lnb-c", "lnb-d"]},
        "metadata": {"channel_number": 4, "region": "lower-edge",
                     "observation_mode": "narrow"},
        "sample_statistics": {"receivers": [
            {"rms_magnitude": 300.0, "near_full_scale_fraction": 0.0},
            {"rms_magnitude": 310.0, "near_full_scale_fraction": 0.01}]},
        "chunks": [],
    }
    checks = []
    for index in range(probes):
        checks.append({"start_s": float(index), "duration_s": .01,
            "candidate": index == 0, "qualified": False,
            "followup_trigger": index == 0, "cfo_difference_hz": 1200.0,
            "epoch_difference_samples": 2,
            "receiver_candidates": [index == 0, False],
            "receiver_qualified": [False, False],
            "receivers": [{"acquisition": {
                "exact_match": {"frequency_offset_hz": 1000.0 + side,
                                "epoch_sample": 40 + side, "score": .5},
                "match_score_margin": .1 + side / 100}}
                for side in (0, 1)]})
    analysis = {"schema": "leo-tracker.starlink-beacon-analysis/v1",
        "created_utc": "2026-08-11T12:02:00+00:00",
        "capture_manifest": capture,
        "analysis": {"exact_acquisition_method": "pilot_symbolwise_v3",
                     "exact_interval_s": 1.0},
        "windows": [{"start_s": 0.0, "duration_s": 1.0, "qualified": False}],
        "exact_checks": checks,
        "summary": {"window_count": 1, "qualified_window_count": 0,
                    "exact_check_count": probes, "exact_candidate_count": 1,
                    "exact_qualified_count": 0,
                    "single_receiver_candidate_count": 1,
                    "single_receiver_qualified_count": 0,
                    "followup_trigger_count": 1, "exact_sampled_time_s": probes * .01,
                    "exact_temporal_coverage_fraction": probes * .01 / 60}}
    followup = {"schema": "leo-tracker.starlink-beacon-followup/v1",
        "checks": [{"start_s": 0.0, "duration_s": .01, "candidate": True,
                    "qualified": False}],
        "confirmation": {"confirmed": confirmed,
            "cross_receiver_links": ([{"start_s": 0.0, "stop_s": .2}]
                                     if confirmed else [])}}
    analysis_path = reports / f"{name}.json"
    followup_path = reports / "followups" / f"{name}.json"
    analysis_path.write_text(json.dumps(analysis))
    followup_path.write_text(json.dumps(followup))
    (reports / "plots" / f"{name}.png").write_bytes(b"plot")
    completion = {"schema": "leo-tracker.analysis-receipt/v1", "job": name,
        "mode": "narrow", "status": "success", "confirmed": confirmed,
        "full_coverage": False, "pipeline_id": pipeline,
        "completed_utc": "2026-08-11T12:03:00+00:00", "context": None,
        "outputs": {
            "analysis": {"path": str(analysis_path),
                         "bytes": analysis_path.stat().st_size,
                         "sha256": _sha(analysis_path)},
            "followup": {"path": str(followup_path),
                         "bytes": followup_path.stat().st_size,
                         "sha256": _sha(followup_path)}}}
    receipt = reports / "runs" / pipeline / name / "completion.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps(completion))
    return name, pipeline
