"""Measure the local-oscillator mismatch between the two LNBs on one radio.

Two LNBs feeding one Pluto have independent references, and the pair share a
single tuner, so a mismatch puts one port outside the acquisition search and
nothing it sees can be confirmed. The mismatch is measurable without any test
source: differencing the two receivers of one capture cancels Doppler, the
shared tuner's error and the satellite's own error, because both ports observe
the same transmitter from the same place through the same tuner. What remains
is the LNB difference alone.

The same subtraction removes anything common to both, so absolute error is not
recoverable here and is not needed: only the difference decides whether both
ports fit inside one search.

Per-port medians are not a substitute. A port whose offset exceeds the search
range only detects when Doppler happens to carry it inside, so its own estimate
is biased toward the search boundary for exactly the port worth measuring.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import statistics

CALIBRATION_SCHEMA = "leo-tracker.lnb-calibration/v1"
#: Twice the diurnal wander observed in the field, so ordinary thermal
#: behaviour is silent while a swapped cable or failing PLL is caught at once.
ALERT_THRESHOLD_HZ = 20_000.0
#: Below this a median is noise; a real measurement has hundreds of pairs.
MINIMUM_SAMPLES = 40


def _paired_differences(report: dict) -> list[float]:
    """Signed rx0 - rx1 acquisition offsets for every dual candidate."""
    differences = []
    for check in report.get("exact_checks") or []:
        if not check.get("candidate"):
            continue
        receivers = check.get("receivers") or []
        if len(receivers) < 2:
            continue
        offsets = []
        for receiver in receivers[:2]:
            match = (receiver.get("acquisition") or {}).get("exact_match") or {}
            offsets.append(match.get("frequency_offset_hz"))
        if None in offsets:
            continue
        differences.append(float(offsets[0] - offsets[1]))
    return differences


def measure_mismatch(reports: Path, *, limit: int = 900,
                     minimum_samples: int = MINIMUM_SAMPLES) -> dict:
    """Measure per-radio LNB mismatch from recent narrow reports."""
    root = Path(reports)
    paths = sorted(root.glob("*narrow*.json"),
                   key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
    samples: dict[str, list[float]] = {}
    labels: dict[str, list[str]] = {}
    for path in paths:
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        manifest = report.get("capture_manifest") or {}
        identity = manifest.get("identity") or {}
        radio = identity.get("radio_id")
        ports = identity.get("receiver_labels") or []
        if not radio or len(ports) < 2:
            continue
        found = _paired_differences(report)
        if found:
            samples.setdefault(radio, []).extend(found)
            labels[radio] = list(ports[:2])
    radios = {}
    for radio, values in sorted(samples.items()):
        values.sort()
        if len(values) < minimum_samples:
            radios[radio] = {"sample_count": len(values), "measured": False,
                             "receiver_labels": labels.get(radio)}
            continue
        percentile = lambda fraction: values[int(fraction * (len(values) - 1))]
        radios[radio] = {
            "measured": True, "sample_count": len(values),
            "receiver_labels": labels.get(radio),
            "mismatch_hz": round(statistics.median(values), 1),
            "p10_hz": round(percentile(0.1), 1),
            "p90_hz": round(percentile(0.9), 1),
            "spread_hz": round(percentile(0.9) - percentile(0.1), 1),
        }
    return {"schema": CALIBRATION_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"),
            "reports_examined": len(paths), "radios": radios}


def compare_calibration(previous: dict, current: dict, *,
                        threshold_hz: float = ALERT_THRESHOLD_HZ) -> list[dict]:
    """Report radios whose mismatch moved enough to mean something changed.

    A timer alone cannot catch a cable moved between ports, which shifts the
    mismatch instantly; comparing against the stored value can. A sign change
    means the ports were exchanged, which calls for relabelling rather than
    recalibration, because the position names would otherwise stop describing
    the hardware.
    """
    alerts = []
    old = (previous or {}).get("radios") or {}
    for radio, entry in ((current or {}).get("radios") or {}).items():
        if not entry.get("measured"):
            continue
        before = old.get(radio) or {}
        if not before.get("measured"):
            continue
        shift = float(entry["mismatch_hz"]) - float(before["mismatch_hz"])
        swapped = (entry["mismatch_hz"] * before["mismatch_hz"] < 0 and
                   min(abs(entry["mismatch_hz"]),
                       abs(before["mismatch_hz"])) > threshold_hz)
        if abs(shift) < threshold_hz and not swapped:
            continue
        alerts.append({
            "radio_id": radio, "previous_hz": before["mismatch_hz"],
            "current_hz": entry["mismatch_hz"], "shift_hz": round(shift, 1),
            "receiver_labels": entry.get("receiver_labels"),
            "ports_exchanged": bool(swapped),
            "reason": ("mismatch changed sign, ports appear exchanged" if swapped
                       else f"mismatch moved {abs(shift):.0f} Hz, "
                            f"beyond the {threshold_hz:.0f} Hz threshold")})
    return alerts


def receiver_centers(calibration: dict, radio_id: str) -> tuple[float, float]:
    """Search centres for one radio's two receivers, in Hz.

    The mismatch is split across the pair rather than assigned wholly to one
    port, because only the difference is measurable: which port carries the
    error is not knowable from this data, and splitting keeps either
    interpretation inside the search.
    """
    entry = ((calibration or {}).get("radios") or {}).get(radio_id) or {}
    if not entry.get("measured"):
        return (0.0, 0.0)
    half = float(entry["mismatch_hz"]) / 2.0
    return (half, -half)


def write_calibration(root: Path, value: dict) -> Path:
    """Store the calibration beside the reports it was measured from."""
    path = Path(root) / "reports" / "lnb-calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.next")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return path


def load_calibration(root: Path) -> dict:
    """Read the stored calibration, or an empty one when none exists yet."""
    path = Path(root) / "reports" / "lnb-calibration.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
