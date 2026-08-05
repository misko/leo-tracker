"""Empirical exact/control null calibration from accumulated sky reports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

from .analysis import (ANALYSIS_SCHEMA, DUAL_EPOCH_DELTA_SAMPLES,
                       DUAL_MATCH_MARGIN, DUAL_SYMBOL_MARGIN,
                       SINGLE_MATCH_MARGIN, SINGLE_SYMBOL_MARGIN)

CALIBRATION_SCHEMA = "leo-tracker.starlink-beacon-calibration/v1"


def _quantiles(values: list[float]) -> dict:
    if not values:
        return {key: None for key in ("p50", "p90", "p95", "p99", "p99_9", "maximum")}
    result = np.quantile(values, [.5, .9, .95, .99, .999, 1])
    return dict(zip(("p50", "p90", "p95", "p99", "p99_9", "maximum"),
                    map(float, result)))


def build_calibration(reports_root: Path, output: Path) -> dict:
    reports_root = Path(reports_root)
    modes = {name: {"match": [], "symbol": [], "checks": [], "reports": 0}
             for name in ("narrow", "wide")}
    excluded_confirmed = 0
    for path in reports_root.glob("*.json"):
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("schema") != ANALYSIS_SCHEMA:
            continue
        followup_path = reports_root / "followups" / path.name
        try:
            followup = json.loads(followup_path.read_text())
        except (OSError, json.JSONDecodeError):
            followup = {}
        if followup.get("confirmation", {}).get("confirmed"):
            excluded_confirmed += 1
            continue
        mode = "wide" if report.get("analysis", {}).get("acquisition_span_hz", 0) else "narrow"
        selected = modes[mode]; selected["reports"] += 1
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
    result_modes = {}
    for name, values in modes.items():
        receiver_count = len(values["match"])
        dual_exceedances = sum(min(match) >= DUAL_MATCH_MARGIN and
                               min(symbol) >= DUAL_SYMBOL_MARGIN and
                               epoch <= DUAL_EPOCH_DELTA_SAMPLES
                               for match, symbol, epoch in values["checks"])
        single_exceedances = sum(match >= SINGLE_MATCH_MARGIN and symbol >= SINGLE_SYMBOL_MARGIN
                                 for match, symbol in zip(values["match"], values["symbol"]))
        result_modes[name] = {"report_count": values["reports"],
            "check_count": len(values["checks"]), "receiver_check_count": receiver_count,
            "match_margin_quantiles": _quantiles(values["match"]),
            "symbol_margin_quantiles": _quantiles(values["symbol"]),
            "single_receiver_gate_exceedance_count": single_exceedances,
            "dual_gate_exceedance_count": dual_exceedances,
            "smoothed_dual_exceedance_fraction": (dual_exceedances + 1) /
                (len(values["checks"]) + 1) if values["checks"] else None}
    report = {"schema": CALIBRATION_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "reports_root": str(reports_root.resolve()),
        "excluded_confirmed_report_count": excluded_confirmed,
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
