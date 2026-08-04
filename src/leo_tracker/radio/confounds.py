"""Population controls for scan order, settling, telemetry, and event clustering."""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import combinations
import json
import math
from pathlib import Path
import re

import numpy as np
from scipy.stats import fisher_exact, spearmanr


SCHEMA = "leo-tracker.radio-confound-analysis/v1"


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc).timestamp()


def _association(rows: list[dict], field: str, outcome: str) -> dict | None:
    pairs = [(row.get(field), row.get(outcome)) for row in rows]
    pairs = [(float(x), float(y)) for x, y in pairs
             if x is not None and y is not None and np.isfinite(x) and np.isfinite(y)]
    if len(pairs) < 5 or len({x for x, _ in pairs}) < 2 or len({y for _, y in pairs}) < 2:
        return None
    result = spearmanr([x for x, _ in pairs], [y for _, y in pairs])
    return {"n": len(pairs), "spearman_r": float(result.statistic),
            "two_sided_p": float(result.pvalue)}


def _binary_control(rows: list[dict], field: str) -> dict:
    values = sorted({row[field] for row in rows})
    groups = []
    for value in values:
        selected = [row for row in rows if row[field] == value]
        detected = sum(row["qualified_count"] > 0 for row in selected)
        groups.append({"value": value, "captures": len(selected),
                       "qualified_captures": detected,
                       "qualified_fraction": detected/len(selected)})
    result = {"groups": groups, "fisher_exact_two_sided_p": None,
              "odds_ratio": None}
    if len(groups) == 2:
        table = [[group["qualified_captures"],
                  group["captures"]-group["qualified_captures"]] for group in groups]
        test = fisher_exact(table)
        result.update({"odds_ratio": None if not np.isfinite(test.statistic) else
                                      float(test.statistic),
                       "fisher_exact_two_sided_p": float(test.pvalue)})
    return result


def _cluster_control(capture_times: list[float], event_times: list[float]) -> dict:
    if len(event_times) < 3:
        return {"event_count": len(event_times), "minimum_three_event_span_s": None,
                "conditional_exact_p": None, "combinations_tested": 0}
    observed = min(event_times[i+2]-event_times[i] for i in range(len(event_times)-2))
    total = math.comb(len(capture_times), len(event_times))
    if total > 2_000_000:
        return {"event_count": len(event_times), "minimum_three_event_span_s": observed,
                "conditional_exact_p": None, "combinations_tested": total,
                "note": "exact conditional enumeration exceeds limit"}
    extreme = 0
    for chosen in combinations(capture_times, len(event_times)):
        span = min(chosen[i+2]-chosen[i] for i in range(len(chosen)-2))
        extreme += span <= observed
    return {"event_count": len(event_times), "minimum_three_event_span_s": observed,
            "conditional_exact_p": extreme/total, "combinations_tested": total,
            "null": "event labels exchangeable across observed capture start times"}


def analyze_confound_population(root: Path, *, settling_window_s: float = 10) -> dict:
    root = Path(root); rows = []; event_times = []
    for wide_path in sorted((root/"wide").glob("chunk-*.json")):
        try:
            wide = json.loads(wide_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        source = Path(wide.get("source", ""))
        match = re.search(r"chunk-(\d+)-ch([34])-", source.name)
        if match is None or not source.is_file():
            continue
        try:
            with np.load(source, allow_pickle=False) as stored:
                identity = json.loads(str(stored["identity_json"]))
                utc = np.asarray(stored["utc_ns"], np.int64)
                rms = np.asarray(stored["rms_raw"], float)
                clipping = np.asarray(stored["clip_fraction"], float)
                read_duration = np.asarray(stored.get("read_duration_ns", []), float)
                trigger = np.asarray(stored.get("snapshot_observer_score_db", []), float)
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        candidates = wide.get("candidates", []); qualified = [
            item for item in candidates if item.get("leo_like_qualified")]
        event_times.extend(_timestamp(item["start_utc"]) for item in qualified)
        slot = int(match.group(1)) % 8
        row = {"chunk": int(match.group(1)), "channel": int(match.group(2)),
            "slot": slot, "pair_position": "first" if slot % 2 == 0 else "second",
            "tuning": "dither" if float(identity.get("tuning_dither_hz", 0)) else "nominal",
            "start_utc": datetime.fromtimestamp(utc[0]/1e9, timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            "host_temperature_c": identity.get("host_temperature_c"),
            "radio_temperature_c": identity.get("radio_temperature_c"),
            "discarded_settling_buffers": identity.get("discarded_settling_buffers", 0),
            "median_rms_rx0": float(np.median(rms[0])),
            "median_rms_rx1": float(np.median(rms[1])),
            "maximum_clip_fraction": (None if not np.any(np.isfinite(clipping)) else
                                       float(np.nanmax(clipping))),
            "median_read_duration_ms": (None if not read_duration.size else
                                        float(np.nanmedian(read_duration)/1e6)),
            "maximum_trigger_score_db": (None if not np.any(np.isfinite(trigger)) else
                                         float(np.nanmax(trigger))),
            "candidate_count": len(candidates), "qualified_count": len(qualified),
            "maximum_candidate_path_correlation": (None if not candidates else max(
                float(item["receiver_path_correlation"]) for item in candidates)),
            "qualified_inside_settling_window": sum(
                float(item["start_time_s"]) <= settling_window_s for item in qualified)}
        rows.append(row)
    rows.sort(key=lambda row: row["start_utc"]); event_times.sort()
    capture_times = [_timestamp(row["start_utc"]) for row in rows]
    return {"schema": SCHEMA, "root": str(root), "capture_count": len(rows),
        "qualified_capture_count": sum(row["qualified_count"] > 0 for row in rows),
        "qualified_event_count": sum(row["qualified_count"] for row in rows),
        "settling_window_s": settling_window_s,
        "qualified_inside_settling_window": sum(
            row["qualified_inside_settling_window"] for row in rows),
        "controls": {"tuning": _binary_control(rows, "tuning") if rows else {},
                     "pair_position": _binary_control(rows, "pair_position") if rows else {},
                     "channel": _binary_control(rows, "channel") if rows else {},
                     "event_clustering": _cluster_control(capture_times, event_times),
                     "host_temperature_vs_candidate_correlation": _association(
                         rows, "host_temperature_c", "maximum_candidate_path_correlation"),
                     "radio_temperature_vs_candidate_correlation": _association(
                         rows, "radio_temperature_c", "maximum_candidate_path_correlation"),
                     "rms_rx0_vs_candidate_correlation": _association(
                         rows, "median_rms_rx0", "maximum_candidate_path_correlation"),
                     "rms_rx1_vs_candidate_correlation": _association(
                         rows, "median_rms_rx1", "maximum_candidate_path_correlation"),
                     "read_duration_vs_candidate_correlation": _association(
                         rows, "median_read_duration_ms", "maximum_candidate_path_correlation")},
        "interpretation": ("controls diagnose confounds; small samples or a non-significant p-value "
                           "do not establish a sky origin"), "captures": rows}


def write_confound_population(root: Path, output: Path, *, settling_window_s: float = 10) -> dict:
    result = analyze_confound_population(root, settling_window_s=settling_window_s)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
    return result
