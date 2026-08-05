"""Evidence-aware retention for high-rate beacon captures."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from .artifact import SCHEMA


def apply_retention(root: Path, *, keep_negative: int = 12, dry_run: bool = False) -> dict:
    if keep_negative < 0:
        raise ValueError("keep-negative cannot be negative")
    root = Path(root).resolve()
    captures_root = (root / "captures").resolve()
    reports_root = (root / "reports").resolve()
    if captures_root.parent != root or reports_root.parent != root:
        raise ValueError("invalid beacon storage root")
    quarantine_root = (root / "quarantine").resolve()
    negatives, protected, incomplete, interrupted = [], [], [], []
    for capture in sorted(captures_root.iterdir() if captures_root.exists() else []):
        if not capture.is_dir():
            continue
        manifest_path = capture / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            incomplete.append(str(capture)); continue
        if manifest.get("schema") == SCHEMA and manifest.get("state") == "interrupted":
            interrupted.append(capture); continue
        if manifest.get("schema") != SCHEMA or manifest.get("state") != "complete":
            incomplete.append(str(capture)); continue
        report_path = reports_root / f"{capture.name}.json"
        try:
            report = json.loads(report_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            protected.append(str(capture)); continue
        summary = report.get("summary", {})
        if summary.get("exact_candidate_count", 0) or summary.get("exact_qualified_count", 0):
            protected.append(str(capture))
        else:
            negatives.append((int(manifest.get("created_utc_ns", 0)), capture))
    negatives.sort()
    removable = negatives[:-keep_negative] if keep_negative else negatives
    removed = []
    for _, capture in removable:
        if capture.parent.resolve() != captures_root:
            raise ValueError(f"refusing to remove capture outside storage root: {capture}")
        removed.append(str(capture))
        if not dry_run:
            shutil.rmtree(capture)
    quarantined = []
    for capture in interrupted:
        target = quarantine_root / capture.name
        quarantined.append(str(target))
        if not dry_run:
            quarantine_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(target)
            shutil.move(str(capture), str(target))
    return {"schema": "leo-tracker.beacon-retention/v1", "root": str(root),
            "keep_negative": keep_negative, "dry_run": dry_run,
            "negative_capture_count": len(negatives), "protected": protected,
            "incomplete": incomplete, "removed": removed, "quarantined": quarantined}
