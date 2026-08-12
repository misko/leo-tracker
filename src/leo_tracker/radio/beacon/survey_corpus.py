"""Preserve survey probes before retention removes them.

The probes are written inside capture directories, and both reclamation paths
remove those directories whole. Measured on the live system, a probe survives
about three hours. Every measurement the detector work depends on — the CFO
distribution, frame occupancy, the fractional-epoch response, any ablation at
all — needs probes that outlive that, so this copies them somewhere retention
does not reach.

It runs on the analysis host beside the other per-capture stages, and it is
deliberately dull: copy, digest, record why. The interesting question is not how
to copy a file but which files to keep, because a corpus selected by the current
detector can only ever confirm the current detector.

Three overlapping strata, and every reason a probe was kept is written down:

  strong     the current detector scored it highly. Biased toward what we can
             already see, and kept anyway because confident positives are rare.
             Labelled so the bias can be corrected for rather than forgotten.
  predicted  a catalogued satellite was in view. Independent of our detector.
             Not yet implemented: it needs the TLE annotation stage.
  random     a fixed fraction regardless of anything. The only stratum that is
             unbiased by construction, and the reason the corpus can measure a
             false-alarm rate at all.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random

#: What a preserved probe's sidecar calls itself.
CORPUS_SCHEMA = "leo-tracker.survey-corpus/v1"

#: Above this peak-to-median, or this many agreeing anchors, a probe is kept as
#: a `strong` positive. Deliberately loose: the cost of keeping a probe is
#: 12.8 MB and the cost of discarding a real detection is that it never existed.
STRONG_PEAK_TO_MEDIAN = 1.6
STRONG_ANCHOR_AGREEMENT = 3

#: Default ceiling on the whole corpus. At 12.8 MB a probe this is about 5,000
#: probes, against roughly a terabyte free on a share that is already 85% full.
DEFAULT_BUDGET_BYTES = 64 * 1024 ** 3


def entry_path(corpus_root: Path, capture_name: str) -> Path:
    return Path(corpus_root) / capture_name


def _survey(manifest: dict) -> dict | None:
    record = (manifest.get("metadata") or {}).get("pre_dwell_survey")
    if not isinstance(record, dict) or record.get("state") != "complete":
        return None
    return record


def reasons_for(manifest: dict, *, random_keep: bool) -> list[str]:
    """Every stratum this probe belongs to, in a stable order.

    All of them, not the first match: a probe kept because the detector liked it
    *and* because the dice said so carries different evidence from one kept for
    either reason alone, and only the full list lets a later analysis reweight.
    """
    record = _survey(manifest)
    if record is None:
        return []
    reasons = []
    scored = [item for tuning in record.get("tunings") or []
              for item in tuning.get("receivers") or []]
    if any(item.get("peak_to_median", 0) >= STRONG_PEAK_TO_MEDIAN
           or (item.get("anchor_agreement") or 0) >= STRONG_ANCHOR_AGREEMENT
           for item in scored):
        reasons.append("strong")
    if random_keep:
        reasons.append("random")
    return reasons


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _held(entry: Path) -> bool:
    """Whether a previous run finished.

    The sidecar is written last and renamed atomically, so its presence is what
    distinguishes a complete copy from one interrupted partway. An IQ file alone
    proves nothing about how much of it arrived.
    """
    return (entry / "corpus.json").is_file() and (entry / "survey.ci16").is_file()


def _copy(source: Path, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".partial")
    with source.open("rb") as reader, partial.open("wb") as writer:
        for block in iter(lambda: reader.read(1 << 20), b""):
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(partial, target)


def held_bytes(corpus_root: Path) -> int:
    root = Path(corpus_root)
    if not root.is_dir():
        return 0
    total = 0
    for entry in root.iterdir():
        probe = entry / "survey.ci16"
        if _held(entry) and probe.is_file():
            total += probe.stat().st_size
    return total


def sample(root: Path, corpus_root: Path, *, random_fraction: float = 0.05,
           budget_bytes: int = DEFAULT_BUDGET_BYTES,
           seed: int | None = None) -> dict:
    """Copy every capture's probe that belongs in the corpus and is not yet held.

    Never raises on one bad capture: this runs beside analysis on every job, and
    a single unreadable directory must not stop the sweep. Damaged captures are
    counted and skipped.
    """
    root, corpus_root = Path(root), Path(corpus_root)
    captures = root / "captures"
    draw = random.Random(seed)
    outcome = {"kept": 0, "already_held": 0, "no_survey": 0, "unreadable": 0,
               "skipped": 0, "bytes": 0, "budget_reached": False,
               "by_reason": {}}
    if not captures.is_dir():
        return outcome
    used = held_bytes(corpus_root)
    for directory in sorted(captures.iterdir()):
        if not directory.is_dir():
            continue
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
            probe = directory / "survey.ci16"
            if not probe.is_file():
                outcome["no_survey"] += 1
                continue
            if _survey(manifest) is None:
                outcome["no_survey"] += 1
                continue
        except (OSError, ValueError):
            outcome["unreadable"] += 1
            continue
        entry = entry_path(corpus_root, directory.name)
        if _held(entry):
            outcome["already_held"] += 1
            continue
        reasons = reasons_for(manifest, random_keep=draw.random() < random_fraction)
        if not reasons:
            outcome["skipped"] += 1
            continue
        size = probe.stat().st_size
        if used + size > budget_bytes:
            outcome["budget_reached"] = True
            break
        try:
            entry.mkdir(parents=True, exist_ok=True)
            _copy(probe, entry / "survey.ci16")
            (entry / "manifest.json").write_text(json.dumps(manifest))
            sidecar = {"schema": CORPUS_SCHEMA, "capture": directory.name,
                       "reasons": reasons, "bytes": size,
                       "sha256": _sha256(entry / "survey.ci16")}
            temporary = entry / "corpus.json.partial"
            temporary.write_text(json.dumps(sidecar, indent=1, sort_keys=True))
            os.replace(temporary, entry / "corpus.json")
        except OSError:
            outcome["unreadable"] += 1
            continue
        used += size
        outcome["kept"] += 1
        outcome["bytes"] += size
        for reason in reasons:
            outcome["by_reason"][reason] = outcome["by_reason"].get(reason, 0) + 1
    return outcome


def corpus_status(root: Path, corpus_root: Path) -> dict:
    """What is held, against what the capture tree still offers."""
    root, corpus_root = Path(root), Path(corpus_root)
    captures = root / "captures"
    available = sum(
        1 for directory in (captures.iterdir() if captures.is_dir() else [])
        if directory.is_dir() and (directory / "survey.ci16").is_file())
    held, by_reason = 0, {}
    if corpus_root.is_dir():
        for entry in sorted(corpus_root.iterdir()):
            if not _held(entry):
                continue
            held += 1
            try:
                sidecar = json.loads((entry / "corpus.json").read_text())
            except (OSError, ValueError):
                continue
            for reason in sidecar.get("reasons") or []:
                by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"held": held, "available": available, "by_reason": by_reason,
            "bytes": held_bytes(corpus_root)}
