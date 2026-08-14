#!/usr/bin/env python3
"""Stream the scored sync corpus into one compact numpy cache.

Why not the JSON mirror ``extract_lite.py`` builds: the compact projection is
~73 KB per sidecar and there are 2,547 of them, which is ~180 MB.  /tmp on this
host is a 2 GB tmpfs shared with the compiled kernel cache and with five other
agents, and it has filled to 100% twice tonight -- which stops scoring dead.
So the scores are streamed straight into numpy arrays (~25 MB) and the nested
dict shape ``cross_radio`` expects is rebuilt on demand from those arrays.

Faithfulness, which is the whole point:
  * the per-radio ``entry`` dict is built by calling ``cross_radio._entry``
    itself, on the compact scores document, so every field the join and the
    thresholds read is produced by the module rather than by this file;
  * the rehydrated observation carries its points and per-method ``score``
    verbatim -- float64 in, float64 out, nothing rounded or aggregated;
  * ``pairs_from_cache`` replicates only the bookkeeping loop of
    ``cross_radio.load_pairs`` (group by paired sweep, require both radios,
    derive geometry).  ``verify_against_load_pairs.py`` builds a real on-disk
    mirror of a corpus slice, runs the true ``cross_radio.load_pairs`` on it,
    and asserts this cache reproduces it exactly.

Read-only: /mnt/qnap01 is never written.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "cache" / "heatmaps.npz"
CORPUS = Path("/mnt/qnap01/mouse9911/leo/surveys/corpus")
SNAPSHOT = HERE / "snapshot.json"

#: Entry-level keys ``cross_radio._entry`` reads off the scores document.
SCORE_KEYS = ("schema", "capture", "radio_id", "sample_rate_hz",
              "samples_per_tuning", "probe_ms", "pilot_guard_hz",
              "receiver_centers_hz", "truth_available", "calibration_applied")

#: Observation-level keys the join, the thresholds and the geometry read.
OBS_KEYS = ("arm", "template_edge", "null_direction", "iq_index", "channel",
            "region", "edge", "receiver", "receiver_label", "utc")


def compact(scores: dict) -> dict:
    """The projection of a scores document this analysis reads.

    ``observation_fires`` reads only ``methods[m]['score']`` and
    ``null_thresholds`` reads only the same field, so nothing else is kept.
    """
    out = {key: scores[key] for key in SCORE_KEYS if key in scores}
    observations = []
    for observation in scores.get("observations") or []:
        row = {key: observation.get(key) for key in OBS_KEYS}
        row["points"] = [
            {"methods": {method: {"score": values.get("score")}
                         for method, values in (point.get("methods") or {}).items()}}
            for point in observation.get("points") or []]
        observations.append(row)
    out["observations"] = observations
    return out


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build(names: list[str]) -> dict:
    methods: list[str] = []
    method_index: dict[str, int] = {}
    entries_meta: list[dict] = []
    o_entry, o_channel, o_iq = [], [], []
    o_arm, o_edge, o_region, o_rxlabel, o_receiver, o_utc = [], [], [], [], [], []
    o_pt_start, o_pt_count = [], []
    point_rows: list[np.ndarray] = []
    census = {"scanned": 0, "unreadable": 0, "no_manifest": 0,
              "other_schema": {}, "not_synchronised": 0}

    for position, name in enumerate(names):
        directory = CORPUS / name
        census["scanned"] += 1
        try:
            scores = json.loads((directory / cr.SCORES_FILENAME).read_text())
        except (OSError, ValueError):
            census["unreadable"] += 1
            continue
        schema = scores.get("schema")
        if schema != cr.SCORES_SCHEMA:
            label = str(schema) if schema else "no schema declared"
            census["other_schema"][label] = census["other_schema"].get(label, 0) + 1
            continue
        manifest_path = directory / cr.MANIFEST_FILENAME
        if not manifest_path.is_file():
            census["no_manifest"] += 1
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            census["unreadable"] += 1
            continue
        record = cr._synchronised(manifest)
        if not record or not record.get("paired_sweep"):
            census["not_synchronised"] += 1
            continue

        # The real module builds the entry, not this file.
        entry = cr._entry(directory, compact(scores), manifest)
        index = len(entries_meta)
        start_obs = len(o_entry)
        for observation in entry["scores"]["observations"]:
            o_entry.append(index)
            o_arm.append(observation.get("arm"))
            o_channel.append(-1 if observation.get("channel") is None
                             else int(observation["channel"]))
            o_iq.append(-1 if observation.get("iq_index") is None
                        else int(observation["iq_index"]))
            o_edge.append(observation.get("edge") or "")
            o_region.append(observation.get("region") or "")
            o_rxlabel.append(observation.get("receiver_label") or "")
            o_receiver.append(-1 if observation.get("receiver") is None
                              else int(observation["receiver"]))
            o_utc.append(observation.get("utc") or "")
            points = observation.get("points") or []
            o_pt_start.append(len(point_rows))
            o_pt_count.append(len(points))
            for point in points:
                for method in (point.get("methods") or {}):
                    if method not in method_index:
                        method_index[method] = len(methods)
                        methods.append(method)
                row = np.full(16, np.nan, dtype=np.float64)
                for method, values in (point.get("methods") or {}).items():
                    score = values.get("score")
                    if score is not None:
                        row[method_index[method]] = float(score)
                point_rows.append(row)
        entries_meta.append({
            "dirname": name,
            "capture": entry["capture"],
            "radio_id": entry["radio_id"],
            "receiver_labels": entry["receiver_labels"],
            "sample_rate_hz": entry["sample_rate_hz"],
            "probe_ms": entry["probe_ms"],
            "samples_per_tuning": entry["samples_per_tuning"],
            "pilot_guard_hz": entry["pilot_guard_hz"],
            "receiver_centers_hz": entry["receiver_centers_hz"],
            "sample_order": entry["sample_order"],
            "edge_order": entry["edge_order"],
            "arm_name": entry["arm_name"],
            "pilot_band_fits": entry["pilot_band_fits"],
            "matched_arm": entry["matched_arm"],
            "paired_sweep": entry["paired_sweep"],
            "peer_radio": entry["peer_radio"],
            "skew_ms": entry["skew_ms"],
            "obs_start": start_obs,
            "obs_count": len(o_entry) - start_obs,
        })
        if (position + 1) % 300 == 0:
            print(f"  {position + 1}/{len(names)}", file=sys.stderr, flush=True)

    scores_array = (np.vstack(point_rows) if point_rows
                    else np.zeros((0, 16), dtype=np.float64))
    scores_array = scores_array[:, :len(methods)]
    return {
        "methods": methods,
        "entries": entries_meta,
        "census": census,
        "p_scores": scores_array,
        "o_entry": np.array(o_entry, dtype=np.int32),
        "o_channel": np.array(o_channel, dtype=np.int16),
        "o_iq": np.array(o_iq, dtype=np.int16),
        "o_receiver": np.array(o_receiver, dtype=np.int16),
        "o_pt_start": np.array(o_pt_start, dtype=np.int64),
        "o_pt_count": np.array(o_pt_count, dtype=np.int16),
        "o_arm": np.array(o_arm), "o_edge": np.array(o_edge),
        "o_region": np.array(o_region), "o_rxlabel": np.array(o_rxlabel),
        "o_utc": np.array(o_utc),
    }


def save(built: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        meta=np.array(json.dumps({"methods": built["methods"],
                                  "entries": built["entries"],
                                  "census": built["census"]})),
        p_scores=built["p_scores"],
        o_entry=built["o_entry"], o_channel=built["o_channel"],
        o_iq=built["o_iq"], o_receiver=built["o_receiver"],
        o_pt_start=built["o_pt_start"], o_pt_count=built["o_pt_count"],
        o_arm=built["o_arm"], o_edge=built["o_edge"],
        o_region=built["o_region"], o_rxlabel=built["o_rxlabel"],
        o_utc=built["o_utc"])


# --------------------------------------------------------------------------
# read back
# --------------------------------------------------------------------------

class Cache:
    """The cache, rehydrated into the shapes ``cross_radio`` reads."""

    def __init__(self, path: Path = CACHE):
        blob = np.load(path, allow_pickle=False)
        meta = json.loads(str(blob["meta"]))
        self.methods = meta["methods"]
        self.entries_meta = meta["entries"]
        self.extract_census = meta["census"]
        self.p_scores = blob["p_scores"]
        self.o_entry = blob["o_entry"]
        self.o_channel = blob["o_channel"]
        self.o_iq = blob["o_iq"]
        self.o_receiver = blob["o_receiver"]
        self.o_pt_start = blob["o_pt_start"]
        self.o_pt_count = blob["o_pt_count"]
        self.o_arm = blob["o_arm"]
        self.o_edge = blob["o_edge"]
        self.o_region = blob["o_region"]
        self.o_rxlabel = blob["o_rxlabel"]
        self.o_utc = blob["o_utc"]

    def observation(self, index: int) -> dict:
        """One observation, in the exact shape the module's functions read."""
        start = int(self.o_pt_start[index])
        count = int(self.o_pt_count[index])
        points = []
        for row in self.p_scores[start:start + count]:
            methods = {}
            for position, method in enumerate(self.methods):
                value = row[position]
                methods[method] = {"score": None if np.isnan(value) else float(value)}
            points.append({"methods": methods})
        channel = int(self.o_channel[index])
        return {"arm": str(self.o_arm[index]),
                "channel": None if channel < 0 else channel,
                "iq_index": int(self.o_iq[index]),
                "edge": str(self.o_edge[index]) or None,
                "region": str(self.o_region[index]) or None,
                "receiver": int(self.o_receiver[index]),
                "receiver_label": str(self.o_rxlabel[index]) or None,
                "utc": str(self.o_utc[index]) or None,
                "points": points,
                "_row": index}

    def entry(self, index: int) -> dict:
        meta = dict(self.entries_meta[index])
        start, count = meta.pop("obs_start"), meta.pop("obs_count")
        meta.pop("dirname")
        observations = [self.observation(row) for row in range(start, start + count)]
        return {**meta, "path": "", "scores": {"observations": observations}}

    def pairs(self) -> tuple[list[dict], dict]:
        """``cross_radio.load_pairs``' grouping, over the cached entries."""
        census = {"scanned": self.extract_census["scanned"],
                  "read": len(self.entries_meta),
                  "unreadable": self.extract_census["unreadable"],
                  "no_manifest": self.extract_census["no_manifest"],
                  "other_schema": self.extract_census["other_schema"],
                  "not_synchronised": self.extract_census["not_synchronised"],
                  "unpaired_sweeps": 0, "irregular_geometry": 0,
                  "beyond_limit": 0}
        grouped: dict[str, list[int]] = defaultdict(list)
        for index, meta in enumerate(self.entries_meta):
            grouped[str(meta["paired_sweep"])].append(index)
        pairs = []
        for sweep in sorted(grouped):
            indices = sorted(grouped[sweep],
                             key=lambda i: self.entries_meta[i]["radio_id"])
            if len(indices) != 2:
                census["unpaired_sweeps"] += 1
                continue
            radios = [self.entry(i) for i in indices]
            left, right = radios
            geometry = cr.sweep_geometry(left["sample_order"], right["sample_order"])
            if geometry == "irregular":
                census["irregular_geometry"] += 1
                continue
            declared = ("same-edge" if left["edge_order"] == right["edge_order"]
                        else "opposite-edge")
            dropped: dict[str, int] = {}
            for entry in radios:
                for observation in entry["scores"]["observations"]:
                    label = observation.get("receiver_label")
                    if label in cr.DEAD_RECEIVERS:
                        dropped[label] = dropped.get(label, 0) + 1
            skew = left["skew_ms"] or right["skew_ms"]
            pairs.append({"paired_sweep": sweep, "radios": radios,
                          "geometry": geometry, "geometry_declared": declared,
                          "geometry_agrees": geometry == declared,
                          "matched_arm": left["matched_arm"] and right["matched_arm"],
                          "arms": sorted({e["arm_name"] for e in radios}),
                          "skew_ms": skew, "excluded_receivers": dropped})
        return pairs, census


def main() -> int:
    frozen = json.loads(SNAPSHOT.read_text())
    built = build(frozen["scored"])
    save(built, CACHE)
    size = CACHE.stat().st_size / 1e6
    print(json.dumps({
        "cache": str(CACHE), "megabytes": round(size, 1),
        "entries": len(built["entries"]),
        "observations": int(built["o_entry"].size),
        "points": int(built["p_scores"].shape[0]),
        "methods": built["methods"],
        "extract_census": built["census"],
        "snapshot_digest": frozen["scored_digest"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
