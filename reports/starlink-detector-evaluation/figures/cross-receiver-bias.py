"""What an epoch-blind differential does to the corpus's cross-receiver check.

`survey_scoring.cross_receiver_checks` marks two ports as agreeing when their
carrier offsets differ by the inter-receiver LNB bias, within
`CROSS_RECEIVER_CFO_HZ`. The check needs only that difference, which is exactly
what a differential calibration measures, so the design is sound. What is not
sound is that the calibration artifact carries one differential per *radio* with
no epoch, while the differential moved by hundreds of kilohertz when the LNBs
were swapped. Score a sweep from one epoch with the other epoch's bias and the
gate can no longer be satisfied by any real signal.

This measures that on the corpus as it stands, rather than arguing it: for each
radio and epoch it reads the bias actually applied out of the scored sidecars,
compares it against the differential measured in section 16, and counts how
often the check fired.

Read only.  python figures/cross-receiver-bias.py [--limit N]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPORT = Path(__file__).resolve().parent.parent
REPO = REPORT.parents[1]
sys.path.insert(0, str(REPO / "src"))

import matplotlib                                     # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402

from leo_tracker.radio.beacon.survey_scoring import (  # noqa: E402
    CROSS_RECEIVER_CFO_HZ)

CORPUS = Path("/mnt/qnap01/mouse9911/leo/surveys/corpus")
ABSCAL = REPORT / "figures" / "abscal-pipeline-abscal.json"

# The instant the LNB swap split the corpus, as it sorts inside an entry name.
# Section 14 fixes it at 2026-08-13T04:46:20Z from the step in both radios.
BOUNDARY = "20260813T044620Z"

PALETTE = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a"}


def epoch_of(stamp: str) -> str:
    return "gen1" if stamp < BOUNDARY else "gen2"


def entries(limit: int | None, seed: int):
    """Scored sidecars, sampled reproducibly when the corpus is large.

    A seeded sample rather than the newest N: scoring advances in corpus order,
    so the newest entries share a slice of the sky and would not represent the
    epochs evenly.
    """
    paths = sorted(CORPUS.glob("sync-*/scores.json"))
    if limit is not None and len(paths) > limit:
        random.Random(seed).shuffle(paths)
        paths = sorted(paths[:limit])
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="sample this many scored sidecars (default: all)")
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args(argv)

    truth = json.loads(ABSCAL.read_text())["differentials"]

    scanned = entries(args.limit, args.seed)
    census_before = len(list(CORPUS.glob("sync-*/scores.json")))

    groups: dict[tuple[str, str], dict] = {}
    for path in scanned:
        parts = path.parent.name.split("-")
        if len(parts) < 3:
            continue
        stamp, radio = parts[1], "-".join(parts[2:])
        key = (radio, epoch_of(stamp))
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        checks = document.get("cross_receiver") or []
        if not checks:
            continue
        slot = groups.setdefault(key, {
            "sweeps": 0, "checks": 0, "agree": 0, "applied_bias_hz": {}})
        slot["sweeps"] += 1
        slot["checks"] += len(checks)
        slot["agree"] += sum(1 for check in checks if check.get("agrees"))
        for check in checks:
            bias = round(float(check.get("bias_hz", 0.0)), 1)
            slot["applied_bias_hz"][str(bias)] = \
                slot["applied_bias_hz"].get(str(bias), 0) + 1

    rows = []
    for (radio, epoch), slot in sorted(groups.items()):
        measured = truth.get(f"{radio}|{epoch}", {}).get("median_hz")
        # The bias the scorer used, taken from the sidecars themselves rather
        # than from any calibration file: the scoring host's artifact need not
        # be the one on the share, and here it is not.
        applied = max(slot["applied_bias_hz"].items(), key=lambda kv: kv[1])[0]
        applied = float(applied)
        residual = None if measured is None else measured - applied
        rows.append({
            "radio": radio, "epoch": epoch,
            "sweeps": slot["sweeps"], "checks": slot["checks"],
            "agree": slot["agree"],
            "agree_fraction": slot["agree"] / slot["checks"],
            "applied_bias_hz": applied,
            "measured_differential_hz": measured,
            "residual_hz": residual,
            "within_gate": None if residual is None
                           else bool(abs(residual) <= CROSS_RECEIVER_CFO_HZ),
            "distinct_biases_seen": slot["applied_bias_hz"],
        })

    census_after = len(list(CORPUS.glob("sync-*/scores.json")))

    sidecar = {
        "schema": "cross-receiver-bias/1",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate_hz": float(CROSS_RECEIVER_CFO_HZ),
        "boundary": BOUNDARY,
        "sampled": len(scanned),
        "sample_seed": args.seed if args.limit else None,
        # Scoring is advancing while this runs, so the census is recorded either
        # side rather than claimed to be frozen.
        "census_scored_before": census_before,
        "census_scored_after": census_after,
        "rows": rows,
    }
    out = Path(__file__).with_suffix(".json")
    out.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")

    # ---- figure -----------------------------------------------------------
    fig, axis = plt.subplots(figsize=(9.0, 4.6), dpi=150)
    labels = [f"{row['radio']}\n{row['epoch']}" for row in rows]
    values = [row["agree_fraction"] * 100 for row in rows]
    colours = [PALETTE["aqua"] if row["within_gate"] else PALETTE["orange"]
               for row in rows]
    bars = axis.bar(labels, values, color=colours, width=0.55)
    for bar, row in zip(bars, rows):
        residual = row["residual_hz"]
        note = ("bias correct\nto %.1f kHz" % (abs(residual) / 1000)
                if row["within_gate"] else
                "bias wrong\nby %.0f kHz" % (abs(residual) / 1000))
        axis.annotate(f"{row['agree_fraction'] * 100:.2f}%\n{note}",
                      (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                      ha="center", va="bottom", fontsize=9,
                      xytext=(0, 4), textcoords="offset points")
    axis.set_ylabel("cross-receiver checks that agree  (%)")
    axis.set_ylim(0, max(values) * 1.45 if values else 1)
    axis.set_title("An epoch-blind differential silently disables the corpus's "
                   "own agreement check", fontsize=11, weight="bold")
    axis.spines[["top", "right"]].set_visible(False)
    axis.grid(axis="y", alpha=0.25)
    figure_note = (f"{sidecar['sampled']:,} scored sidecars; gate "
                   f"{CROSS_RECEIVER_CFO_HZ / 1000:.0f} kHz on "
                   f"|cfo difference − bias|")
    fig.text(0.01, 0.01, figure_note, fontsize=8, color="#666")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(Path(__file__).with_suffix(".png"))

    print(json.dumps({"wrote": str(out), "rows": len(rows),
                      "sampled": sidecar["sampled"],
                      "census": [census_before, census_after]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
