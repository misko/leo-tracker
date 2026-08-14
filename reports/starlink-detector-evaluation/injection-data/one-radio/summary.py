"""Collect every headline number from E1-E5 into one file."""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as A

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"


def load(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def main() -> None:
    smoke = load(FIG / "smoke-test.json")
    roc = load(FIG / "detection-vs-snr.json")
    rank = load(FIG / "detector-ranking.json")
    fa = load(HERE / "e3_falsealarm.json")
    nullarm = load(HERE / "e3b_summary.json")
    cliff = load(FIG / "offset-cliff.json")
    coin = load(FIG / "coincidence-recovery.json")
    spread = load(HERE / "e5_spread.json")

    out = {
        "title": "Injected-signal characterisation of the eight Starlink pilot detectors",
        "note": A.LOOPBACK_NOTE,
        "radio": "PlutoSDR Rev.C ip:192.168.1.183, serial 104000bac495...",
        "configuration": {"sample_rate_hz": 5e6, "lo_hz": 1_190_312_500.0,
                          "rf_bandwidth_hz": 5e6, "rx_gain_db": 40.0,
                          "rx_gain_mode": "manual", "probe_ms": 20.0,
                          "frames_per_probe": 15,
                          "waveform": "leo_tracker.radio.beacon.pilots."
                                      "edge_pilot_frame(5e6, 'lower')",
                          "scoring_path": "survey_scoring.search_observation -> "
                                          "distinct_points -> confirm_points; "
                                          "cross_radio.observation_fires for the "
                                          "per-cell decision"},

        "E1_smoke": {
            "reference": smoke["reference_from_bench_notes"],
            "measured_rms_counts": smoke["ladder"][-1]["rx1_rms"],
            "measured_peak_to_median": smoke["ladder"][-1]["rx1_peak_to_median"],
            "transmitter_off_peak_to_median": smoke["tx_off"]["rx1_peak_to_median"],
            "highest_rx_peak_counts": max(r["rx1_peak"] for r in smoke["ladder"]),
            "passed": smoke["verdict"]["passed"]},

        "E2_roc": {
            "snr50_db": {m: roc["fits"][m]["snr50_db"] for m in A.METHODS},
            "snr50_ci": {m: [roc["fits"][m]["snr50_lo"], roc["fits"][m]["snr50_hi"]]
                         for m in A.METHODS},
            "measured_ranking_best_first": roc["measured_ranking_best_first"],
            "spread_db": rank["snr50_spread_db"],
            "pairs_resolved": roc["pairs_resolved"],
            "pairs_total": roc["pairs_total"],
            "spearman_vs_model_d": roc["spearman_measured_vs_model_d"],
            "spearman_vs_fire_count": roc["spearman_measured_vs_fire_count"],
            "cells_per_rung": 160, "rungs": len(roc["rungs"])},

        "E3_false_alarm": {
            "per_point_rate_range": fa["summary"]["per_point_range"],
            "per_cell_rate_range": fa["summary"]["per_cell_range"],
            "sky_per_cell_range": fa["summary"]["sky_per_cell_range"],
            "nominal_per_point": 0.01,
            "points_per_cell": fa["population"]["points_per_cell_mean"],
            "minus_89_75_is_empty": fa["off_versus_dark_verdict"]["reads"],
            "conditioned_cross_edge_threshold_ratio":
                fa["cross_edge_null_validity"]["range"],
            "per_cell_at_conditioned_cross_edge_threshold": {
                m: fa["measured"][m]["per_cell_cross_edge_threshold"]["rate"]
                for m in A.METHODS},
            "null_arm_threshold_ratio": (
                None if nullarm is None
                else [min(nullarm["threshold_ratio_null_arm_over_empty"].values()),
                      max(nullarm["threshold_ratio_null_arm_over_empty"].values())]),
            "per_cell_at_null_arm_threshold": (
                None if nullarm is None
                else {m: nullarm["per_cell_at_null_arm_threshold"][m]["rate"]
                      for m in A.METHODS})},

        "E4_offset": {
            "sky_claim_hz": cliff["sky_cliff_hz"],
            "passes": [{"snr_db": p["snr_db"], "tx2_gain_db": p["tx2_gain_db"],
                        "collapse_hz_at_pd_50": p["collapse_hz_at_pd_50"],
                        "pd_at_350_400khz": {
                            m: [e["methods"][m]["rate"] for e in p["table"]
                                if 340_000 <= e["offset_hz"] <= 410_000]
                            for m in A.METHODS}}
                       for p in cliff["passes"]],
            "coarse_search_spans_hz": cliff["coarse_search_spans_hz"]},

        "E5_coincidence": {
            "f_true": coin["f_true"],
            "tx2_gain_db": coin["tx2_gain_db"],
            "cells": coin["probes"],
            "f_recovered": {m: coin["per_method"][m]["solved_with_known_p"]["f"]
                            for m in A.METHODS},
            "f_ci": {m: coin["per_method"][m]["f_ci"] for m in A.METHODS},
            "every_interval_covers_truth": all(
                coin["per_method"][m]["f_ci"][0] <= coin["f_true"]
                <= coin["per_method"][m]["f_ci"][1] for m in A.METHODS),
            "d_a_model_minus_measured": {
                m: coin["per_method"][m]["d_a_error"] for m in A.METHODS},
            "d_b_model_minus_measured": {
                m: coin["per_method"][m]["d_b_error"] for m in A.METHODS},
            "independence_excess_on_empty_cells": {
                m: coin["per_method"][m]["independence_check"]["empty"]["excess"]
                for m in A.METHODS},
            "independence_excess_on_occupied_cells": {
                m: coin["per_method"][m]["independence_check"]["occupied"]["excess"]
                for m in A.METHODS},
            "f_spread_measured": spread["observed_spread"],
            "f_spread_ci": spread["spread_ci"],
            "report_sky_f_spread": spread["report_sky_spread"],
            "probability_loopback_spread_exceeds_sky": spread["spread_exceeds_sky_share"]},

        "figures": sorted(p.name for p in FIG.glob("*.png")),
    }
    (HERE / "summary.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items()
                      if k.startswith("E")}, indent=1)[:1800])


if __name__ == "__main__":
    main()
