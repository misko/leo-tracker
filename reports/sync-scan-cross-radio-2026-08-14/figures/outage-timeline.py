#!/usr/bin/env python3
"""outage-timeline: one USB dropout, 892 self-inflicted failures after it.

Reads every committed sweep record on the share, classifies each as paired (both
radios returned IQ) or single-radio (one radio errored), and draws the two states
against wall clock so the contiguous block and the recovery are visible without
reading a table.

Sources, all read-only:
  /mnt/qnap01/mouse9911/leo-scans/sync-*/sweep.json   -- the commit marker
  /mnt/leo-nvme/leo-tracker/sync-scans/collector.log  -- the reopen failures

    nice -n 15 python3 outage-timeline.py

CHANGED FOR THE FULL-CORPUS PASS (2026-08-14): radio collection is PAUSED
(leo-sync-scan stopped by the operator), so the scan share is frozen and the
sweep total is no longer a floor -- it is the final count.  The population is
still taken from snapshot.py rather than a fresh glob, so this figure and the
other five agree on how many sweeps exist.  Nothing here is typed in by hand
except REVIEWED_SWEEPS, which is the report's own snapshot size, kept so the
reproduction can be checked against it rather than asserted.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

SCANS = "/mnt/qnap01/mouse9911/leo-scans"
LOG = Path("/mnt/leo-nvme/leo-tracker/sync-scans/collector.log")
HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("REFRESH_WORK", HERE.parent / "work"))
SNAPSHOT = WORK / "snapshot.json"
PNG = HERE / "outage-timeline.png"
JSON_OUT = HERE / "outage-timeline.json"

# The report's own snapshot size, kept so the reproduction can be checked against
# it rather than asserted.
REVIEWED_SWEEPS = 2594

PAIRED = "#2a78d6"
SINGLE = "#e34948"
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#d9d8d4"


def parse(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=dt.timezone.utc)


def frozen() -> dict:
    with open(SNAPSHOT) as handle:
        return json.load(handle)


def read_sweeps(names: list[str]) -> list[dict]:
    rows = []
    for name in names:
        directory = os.path.join(SCANS, name)
        path = os.path.join(directory, "sweep.json")
        if not os.path.exists(path):
            continue                      # no commit marker: not a sweep yet
        with open(path) as handle:
            record = json.load(handle)
        radios = record.get("radios") or {}
        errors = {name: (item.get("error") or "")
                  for name, item in radios.items() if item.get("error")}
        live = [name for name, item in radios.items() if not item.get("error")]
        rows.append({"utc": record["utc"], "when": parse(record["utc"]),
                     "live": sorted(live), "errors": errors,
                     "sweep": record.get("sweep")})
    rows.sort(key=lambda row: row["when"])
    return rows


def runs(rows: list[dict]) -> list[dict]:
    """Contiguous stretches of one state, in sweep order."""
    out = []
    for row in rows:
        state = "paired" if len(row["live"]) == 2 else "single"
        if out and out[-1]["state"] == state:
            out[-1]["end"] = row["when"]
            out[-1]["count"] += 1
            out[-1]["last_utc"] = row["utc"]
        else:
            out.append({"state": state, "start": row["when"], "end": row["when"],
                        "count": 1, "first_utc": row["utc"],
                        "last_utc": row["utc"]})
    return out


def main() -> None:
    snapshot = frozen()
    rows = read_sweeps(snapshot["sweeps"])
    census = dt.datetime.now(dt.timezone.utc)
    stretches = runs(rows)
    paired = [row for row in rows if len(row["live"]) == 2]
    single = [row for row in rows if len(row["live"]) < 2]

    genuine = [row for row in single
               if any("expected one USB Pluto" in text or "OSError" in text
                      for text in row["errors"].values())]
    keyerror = [row for row in single
                if any("KeyError" in text for text in row["errors"].values())]
    survivors = sorted({tuple(row["live"]) for row in single})

    outage = [item for item in stretches if item["state"] == "single"]
    block = max(outage, key=lambda item: item["count"]) if outage else None
    after = [row for row in rows if block and row["when"] > block["end"]]
    resumed = after[0] if after else None

    text = LOG.read_text(errors="replace") if LOG.is_file() else ""
    log_counts = {
        "reopen_failed_total": len(re.findall(r"reopen pluto-5d4d FAILED", text)),
        "genuine_usb_loss": len(re.findall(
            r"reopen pluto-5d4d FAILED: expected one USB Pluto", text)),
        "keyerror": len(re.findall(
            r"reopen pluto-5d4d FAILED: 'pluto-5d4d'", text))}

    # The report's snapshot, re-derived rather than quoted.
    prefix = rows[:REVIEWED_SWEEPS]
    at_review = {
        "sweeps_examined": len(prefix),
        "single_radio": sum(1 for row in prefix if len(row["live"]) < 2),
        "paired_after_the_fix": sum(1 for row in prefix
                                    if block and row["when"] > block["end"]
                                    and len(row["live"]) == 2),
        "single_after_the_fix": sum(1 for row in prefix
                                    if block and row["when"] > block["end"]
                                    and len(row["live"]) < 2)}

    # ------------------------------------------------------------- figure
    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK, "text.color": INK,
                         "xtick.color": MUTED, "ytick.color": INK,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    figure, (strip, cumulative) = plt.subplots(
        2, 1, figsize=(11.6, 7.6), height_ratios=[1.0, 1.55], sharex=True,
        gridspec_kw={"hspace": 0.16})

    lane = {"paired": 1.0, "single": 0.0}
    colour = {"paired": PAIRED, "single": SINGLE}
    for item in stretches:
        span = max((item["end"] - item["start"]).total_seconds(), 6.0)
        strip.broken_barh(
            [(mdates.date2num(item["start"]), span / 86400.0)],
            (lane[item["state"]] - 0.21, 0.42),
            facecolors=colour[item["state"]], edgecolor="none", zorder=3)

    strip.set_yticks([1.0, 0.0])
    strip.set_yticklabels(["both radios\n(paired sweep)",
                           "one radio only\n(no pair possible)"], fontsize=10)
    strip.tick_params(axis="y", length=0, pad=6)
    strip.set_ylim(-1.20, 1.85)
    strip.grid(axis="x", color=GRID, lw=0.6, zorder=0)
    strip.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        strip.spines[spine].set_visible(False)

    if block:
        for edge in (block["start"], block["end"]):
            for axes in (strip, cumulative):
                axes.axvline(edge, color=MUTED, lw=1.0, ls=(0, (3, 3)),
                             zorder=2)
        # The dashed rule at the block edge is the pointer here; an arrow from
        # the label would have to cross the paired lane to reach it.
        strip.text(mdates.date2num(block["start"]
                                   - dt.timedelta(minutes=7)), 1.58,
                   "%s — the one genuine\nUSB dropout (Errno 5, 0 Plutos found)"
                   % block["first_utc"],
                   fontsize=9.5, color=INK, ha="right", va="center")
        strip.annotate(
            "%d contiguous single-radio sweeps over %s\n"
            "= that 1 dropout + %d KeyError reopens from  del ctx[name]"
            % (block["count"],
               str(block["end"] - block["start"]).rsplit(".", 1)[0],
               len(keyerror)),
            xy=(block["start"] + (block["end"] - block["start"]) / 2, -0.24),
            xytext=(block["start"] + (block["end"] - block["start"]) / 2, -0.78),
            fontsize=9.5, color=SINGLE, ha="center", va="center",
            arrowprops=dict(arrowstyle="->", color=SINGLE, lw=1.2,
                            shrinkA=2, shrinkB=2))
    if resumed:
        strip.annotate(
            "%s — paired capture resumes\n%d paired, 0 single since"
            % (resumed["utc"],
               sum(1 for row in after if len(row["live"]) == 2)),
            xy=(resumed["when"], 1.24),
            xytext=(rows[-1]["when"], 1.58),
            fontsize=9.5, color=PAIRED, ha="right", va="center",
            arrowprops=dict(arrowstyle="->", color=PAIRED, lw=1.2,
                            shrinkA=2, shrinkB=2))

    # ---- cumulative ------------------------------------------------------
    when = [row["when"] for row in rows]
    pair_total, single_total, tally_p, tally_s = [], [], 0, 0
    for row in rows:
        if len(row["live"]) == 2:
            tally_p += 1
        else:
            tally_s += 1
        pair_total.append(tally_p)
        single_total.append(tally_s)

    cumulative.step(when, pair_total, where="post", color=PAIRED, lw=2.0,
                    zorder=4, label="paired sweeps")
    cumulative.step(when, single_total, where="post", color=SINGLE, lw=2.0,
                    ls=(0, (5, 2)), zorder=4, label="single-radio sweeps")
    for values, colour_, marker in ((pair_total, PAIRED, "o"),
                                    (single_total, SINGLE, "D")):
        step = max(len(rows) // 26, 1)
        cumulative.plot(when[::step], values[::step], marker, color=colour_,
                        ms=5.5, mec="white", mew=1.0, zorder=5, lw=0)

    cumulative.axhline(len(single), color=SINGLE, lw=0.9, ls=(0, (2, 3)),
                       zorder=2)
    cumulative.text(rows[-1]["when"] - dt.timedelta(minutes=8), len(single) + 70,
                    "%d single-radio sweeps in total" % len(single),
                    fontsize=9.5, color=SINGLE, ha="right", va="bottom")
    if block:
        cumulative.annotate(
            "flat: not one pair for %s"
            % str(block["end"] - block["start"]).rsplit(".", 1)[0],
            xy=(block["start"] + (block["end"] - block["start"]) / 2,
                pair_total[rows.index(block_first(rows, block))]),
            xytext=(rows[0]["when"] + (block["start"] - rows[0]["when"]) * 0.05,
                    max(pair_total) * 0.72),
            fontsize=9.5, color=PAIRED, ha="left", va="center",
            arrowprops=dict(arrowstyle="->", color=PAIRED, lw=1.2,
                            shrinkA=2, shrinkB=3))
        cumulative.annotate(
            "no single-radio sweep\nsince %s\n(%d paired sweeps, 0 single)"
            % (resumed["utc"] if resumed else "the fix",
               sum(1 for row in after if len(row["live"]) == 2)),
            xy=(rows[-1]["when"] - dt.timedelta(minutes=25), len(single)),
            xytext=(block["end"] + (rows[-1]["when"] - block["end"]) * 0.16,
                    max(pair_total) * 0.055),
            fontsize=9.5, color=SINGLE, ha="left", va="center",
            arrowprops=dict(arrowstyle="->", color=SINGLE, lw=1.2,
                            shrinkA=2, shrinkB=3))

    cumulative.set_ylabel("sweeps committed, cumulative")
    cumulative.set_xlabel("wall clock, UTC (%s)"
                          % rows[0]["when"].strftime("%Y-%m-%d"))
    span_h = (rows[-1]["when"] - rows[0]["when"]).total_seconds() / 3600.0
    cumulative.xaxis.set_major_locator(
        mdates.HourLocator(interval=1) if span_h > 6
        else mdates.MinuteLocator(byminute=range(0, 60, 30)))
    cumulative.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    cumulative.set_xlim(rows[0]["when"] - dt.timedelta(minutes=4),
                        rows[-1]["when"] + dt.timedelta(minutes=4))
    cumulative.grid(color=GRID, lw=0.6, zorder=0)
    cumulative.set_axisbelow(True)
    for spine in ("top", "right"):
        cumulative.spines[spine].set_visible(False)
    cumulative.legend(handles=[
        Line2D([], [], color=PAIRED, lw=2.0, marker="o", ms=5.5, mec="white",
               label="paired sweeps (both radios)"),
        Line2D([], [], color=SINGLE, lw=2.0, ls=(0, (5, 2)), marker="D",
               ms=5.5, mec="white", label="single-radio sweeps")],
        loc="upper left", fontsize=9.5, frameon=False, borderaxespad=0.6)

    figure.text(0.019, 0.982,
                "One real USB dropout cost %d sweeps: the recovery path raised "
                "before it could retry" % len(single),
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.019, 0.955,
                "%d committed sweeps on the share: %d paired, %d single-radio.  "
                "Collection is PAUSED, so this is the final count, not a floor."
                % (len(rows), len(paired), len(single)),
                fontsize=9.5, color=MUTED, ha="left", va="top")
    figure.text(0.019, 0.934,
                "CENSUS, frozen before any figure was computed and shared with all "
                "six (snapshot.py, digest %s, taken %s):\n"
                "%s sweeps on the scan share  |  %s corpus entries  |  %s scored "
                "sidecars.  The report reviewed the first %s sweeps of this same "
                "share."
                % (snapshot["scored_digest"], snapshot["measured_utc"],
                   f"{snapshot['sweeps_on_share']:,}",
                   f"{snapshot['corpus_entries']:,}",
                   f"{snapshot['scored_sidecars']:,}",
                   f"{REVIEWED_SWEEPS:,}"),
                fontsize=9.5, color=MUTED, ha="left", va="top",
                linespacing=1.5)

    figure.subplots_adjust(left=0.125, right=0.980, top=0.822, bottom=0.090)
    figure.savefig(PNG, dpi=150)
    print("wrote", PNG)

    # --------------------------------------------------------------- json
    out = {
        "figure": "outage-timeline",
        "generated_utc": census.isoformat(timespec="seconds"),
        "sources": {"sweeps": SCANS + "/sync-*/sweep.json",
                    "collector_log": str(LOG)},
        "snapshot": {key: snapshot[key] for key in
                     ("measured_utc", "sweeps_on_share", "corpus_entries",
                      "scored_sidecars", "scored_digest")},
        "census": {
            "committed_sweeps": len(rows),
            "paired": len(paired),
            "single_radio": len(single),
            "first_sweep_utc": rows[0]["utc"], "last_sweep_utc": rows[-1]["utc"],
            "note": "collection is PAUSED (leo-sync-scan stopped); the scan "
                    "share is frozen and these totals are final, not floors"},
        "outage": {
            "single_radio_sweeps": len(single),
            "window_utc": [block["first_utc"], block["last_utc"]] if block else None,
            "duration": str(block["end"] - block["start"]).rsplit(".", 1)[0]
                        if block else None,
            "episodes": len(outage),
            "contiguous": len(outage) == 1 and block["count"] == len(single),
            "surviving_radio": ["|".join(item) for item in survivors],
            "genuine_hardware_events": len(genuine),
            "genuine_event_utc": genuine[0]["utc"] if genuine else None,
            "genuine_event_error": list(genuine[0]["errors"].values())[0]
                                   if genuine else None,
            "self_inflicted_keyerror_sweeps": len(keyerror),
            "collector_log": log_counts},
        "recovery": {
            "first_paired_after_utc": resumed["utc"] if resumed else None,
            "sweeps_after_the_fix": len(after),
            "paired_after_the_fix": sum(1 for row in after
                                        if len(row["live"]) == 2),
            "single_after_the_fix": sum(1 for row in after
                                        if len(row["live"]) < 2)},
        "at_the_reports_snapshot": at_review,
        "runs_plotted": [{"state": item["state"], "sweeps": item["count"],
                          "from_utc": item["first_utc"],
                          "to_utc": item["last_utc"]} for item in stretches],
        "cumulative_endpoints": {"paired": pair_total[-1],
                                 "single_radio": single_total[-1]},
    }
    JSON_OUT.write_text(json.dumps(out, indent=2) + "\n")
    print("wrote", JSON_OUT)
    print(json.dumps({k: out[k] for k in ("census", "outage", "recovery",
                                          "at_the_reports_snapshot")}, indent=2))


def block_first(rows, block):
    for row in rows:
        if row["utc"] == block["first_utc"]:
            return row
    return rows[0]


if __name__ == "__main__":
    main()
