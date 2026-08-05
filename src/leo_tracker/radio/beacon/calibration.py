"""Empirical exact/control null calibration from accumulated sky reports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

from .analysis import (ANALYSIS_SCHEMA, DUAL_EPOCH_DELTA_SAMPLES,
                       DUAL_MATCH_MARGIN, DUAL_SYMBOL_MARGIN,
                       SINGLE_MATCH_MARGIN, SINGLE_SYMBOL_MARGIN,
                       detection_gates)

CALIBRATION_SCHEMA = "leo-tracker.starlink-beacon-calibration/v2"
LEGACY_ACQUISITION_METHOD = "coherent_grid_v1"


def _quantiles(values: list[float]) -> dict:
    if not values:
        return {key: None for key in ("p50", "p90", "p95", "p99", "p99_9", "maximum")}
    result = np.quantile(values, [.5, .9, .95, .99, .999, 1])
    return dict(zip(("p50", "p90", "p95", "p99", "p99_9", "maximum"),
                    map(float, result)))


def _empty_modes() -> dict:
    return {name: {"match": [], "symbol": [], "checks": [], "reports": 0}
            for name in ("narrow", "wide")}


def _summarize_modes(modes: dict, gates: dict) -> dict:
    result = {}
    for name, values in modes.items():
        receiver_count = len(values["match"])
        dual_exceedances = sum(min(match) >= gates["dual_match_margin"] and
                               min(symbol) >= gates["dual_symbol_margin"] and
                               epoch <= gates["dual_epoch_delta_samples"]
                               for match, symbol, epoch in values["checks"])
        single_exceedances = sum(match >= gates["single_match_margin"] and
                                 symbol >= gates["single_symbol_margin"]
                                 for match, symbol in zip(values["match"], values["symbol"]))
        result[name] = {"report_count": values["reports"],
            "check_count": len(values["checks"]), "receiver_check_count": receiver_count,
            "match_margin_quantiles": _quantiles(values["match"]),
            "symbol_margin_quantiles": _quantiles(values["symbol"]),
            "single_receiver_gate_exceedance_count": single_exceedances,
            "dual_gate_exceedance_count": dual_exceedances,
            "smoothed_dual_exceedance_fraction": (dual_exceedances + 1) /
                (len(values["checks"]) + 1) if values["checks"] else None}
    return result


def build_calibration(reports_root: Path, output: Path) -> dict:
    reports_root = Path(reports_root)
    methods: dict[str, dict] = {}
    excluded_confirmed = 0
    paths = list(reports_root.glob("*.json"))
    paths.extend((reports_root / "calibration").glob("*-null/*.json"))
    for path in paths:
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("schema") != ANALYSIS_SCHEMA:
            continue
        followup_path = path.parent / "followups" / path.name
        try:
            followup = json.loads(followup_path.read_text())
        except (OSError, json.JSONDecodeError):
            followup = {}
        if followup.get("confirmation", {}).get("confirmed"):
            excluded_confirmed += 1
            continue
        analysis = report.get("analysis", {})
        method = str(analysis.get("exact_acquisition_method") or
                     LEGACY_ACQUISITION_METHOD)
        mode = "wide" if analysis.get("acquisition_span_hz", 0) else "narrow"
        selected = methods.setdefault(method, _empty_modes())[mode]
        selected["reports"] += 1
        for check in report.get("exact_checks", []):
            match = [receiver.get("acquisition", {}).get("match_score_margin")
                     for receiver in check.get("receivers", [])]
            symbol = [receiver.get("pilot", {}).get("score_margin")
                      for receiver in check.get("receivers", [])]
            if len(match) != 2 or any(value is None for value in match):
                continue
            selected["match"].extend(map(float, match))
            selected["symbol"].extend(float(value) for value in symbol if value is not None)
            selected["checks"].append((match, symbol,
                                       float(check.get("epoch_difference_samples", np.inf))))
    result_methods = {method: _summarize_modes(modes, detection_gates(method))
                      for method, modes in sorted(methods.items())}
    # Keep the v1 projection during the dashboard/schema migration.  Crucially,
    # v2 observations never enter this compatibility view or the v1 null.
    result_modes = result_methods.get(
        LEGACY_ACQUISITION_METHOD,
        _summarize_modes(_empty_modes(), detection_gates(LEGACY_ACQUISITION_METHOD)))
    report = {"schema": CALIBRATION_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "reports_root": str(reports_root.resolve()),
        "excluded_confirmed_report_count": excluded_confirmed,
        "acquisition_methods": result_methods,
        "gates_by_acquisition_method": {method: detection_gates(method)
                                        for method in result_methods},
        "gates": {"dual_match_margin": DUAL_MATCH_MARGIN,
                  "dual_symbol_margin": DUAL_SYMBOL_MARGIN,
                  "dual_epoch_delta_samples": DUAL_EPOCH_DELTA_SAMPLES,
                  "single_match_margin": SINGLE_MATCH_MARGIN,
                  "single_symbol_margin": SINGLE_SYMBOL_MARGIN},
        "modes": result_modes}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    return report
