"""Compare catalog providers on identical Doppler tracks.

Providers are only comparable when they are scored against the same
observations. Aggregating each provider's qualified associations separately
would confound element quality with which tracks happened to be analysed, so
every statistic here is computed over tracks both providers actually saw.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median

from .artifacts import utc_iso


COMPARISON_SCHEMA = "leo-tracker.catalog-source-comparison/v1"


def _qualified_tracks(path: Path) -> dict[str, dict]:
    """Return each qualified track in one association artifact, keyed by track id."""
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    found: dict[str, dict] = {}
    for association in report.get("associations", []):
        if not association.get("qualified"):
            continue
        primary = (association.get("stability") or {}).get("primary") or {}
        found[str(association.get("track_id"))] = {
            "norad_id": primary.get("best_norad_id"),
            "name": (primary.get("best_name") or "").strip(),
            "holdout_residual_rms_hz": primary.get("holdout_residual_rms_hz"),
            "margin_to_second_hz": primary.get("margin_to_second_hz"),
            "epoch_adjustment_s": primary.get("epoch_adjustment_s"),
            "duration_s": association.get("duration_s"),
        }
    return found


def _summarize(values: list[float]) -> dict | None:
    usable = sorted(value for value in values if value is not None)
    if not usable:
        return None
    return {"count": len(usable), "median": median(usable),
            "minimum": usable[0], "maximum": usable[-1]}


def compare_source_associations(reports_root: Path, *, sources: tuple[str, str],
                                output: Path | None = None) -> dict:
    """Pair two providers' associations track by track and score the difference.

    Agreement on the NORAD identity is the strongest available evidence, because
    the two catalogs are retrieved independently: a shared identity is not
    something a single provider's error can manufacture.
    """
    if len(set(sources)) != 2:
        raise ValueError("comparison needs two distinct sources")
    reports_root = Path(reports_root)
    left, right = sources
    per_source = {name: {} for name in sources}
    for name in sources:
        directory = reports_root / "associations" / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            for track_id, record in _qualified_tracks(path).items():
                per_source[name][(path.stem, track_id)] = record

    paired = sorted(set(per_source[left]) & set(per_source[right]))
    agree = [key for key in paired
             if per_source[left][key]["norad_id"] == per_source[right][key]["norad_id"]]
    disagree = [key for key in paired if key not in set(agree)]
    metrics = {}
    for name in sources:
        # Restricted to the paired set so the two columns describe the same
        # tracks; a provider is not rewarded for qualifying easier ones.
        metrics[name] = {
            "qualified_total": len(per_source[name]),
            "holdout_residual_rms_hz": _summarize(
                [per_source[name][key]["holdout_residual_rms_hz"] for key in paired]),
            "margin_to_second_hz": _summarize(
                [per_source[name][key]["margin_to_second_hz"] for key in paired]),
            "absolute_epoch_adjustment_s": _summarize(
                [abs(per_source[name][key]["epoch_adjustment_s"])
                 for key in paired
                 if per_source[name][key]["epoch_adjustment_s"] is not None]),
        }
    result = {
        "schema": COMPARISON_SCHEMA,
        "created_utc": utc_iso(datetime.now(timezone.utc)),
        "reports_root": str(reports_root.resolve()),
        "sources": list(sources),
        "paired_track_count": len(paired),
        "identity_agreement_count": len(agree),
        "identity_disagreement_count": len(disagree),
        "only_qualified_by": {
            name: len(set(per_source[name]) - set(per_source[other]))
            for name, other in ((left, right), (right, left))},
        "metrics": metrics,
        "disagreements": [
            {"recording": key[0], "track_id": key[1],
             left: per_source[left][key], right: per_source[right][key]}
            for key in disagree[:50]],
    }
    if output is not None:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result
