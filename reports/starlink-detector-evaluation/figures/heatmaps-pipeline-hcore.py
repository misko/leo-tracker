#!/usr/bin/env python3
"""Shared spine for the three correlation heatmaps.

Loads the verified cache once, builds the pair list, draws the thresholds with
``cross_radio.null_thresholds``, and lays every live observation out as a table
so the three figures compute phi over one identical population.

phi is the repo's own: ``cross_radio.solve_coincidence`` normalises the
coincidence covariance by what two binary variables of those marginals could
reach, and that value is computed from the three rates before any part of the
occupancy fit runs, so it is available whether or not the model fits.  This
module calls that function rather than restating the algebra; ``phi_from_counts``
is checked against ``numpy.corrcoef`` in ``selftest``.

Nothing here decides anything: every fire / no-fire call is
``cross_radio.observation_fires`` against the threshold
``cross_radio.null_thresholds`` drew for that observation's own
(sample rate, probe length) from the cross-edge null arms.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, "/home/satpi01/leo-tracker/src")
sys.path.insert(0, str(HERE))
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402

import extract_heatmaps as ex  # noqa: E402

FIGURES = HERE.parent / "figures"
SNAPSHOT = HERE / "snapshot.json"
DRIFT = HERE / "drift.json"


def drift_block() -> dict | None:
    """How far the corpus moved while these figures were computed, if measured."""
    if not DRIFT.is_file():
        return None
    return json.loads(DRIFT.read_text())


def drift_caption() -> str:
    """One line naming the drift, for the foot of a figure."""
    block = drift_block()
    if not block:
        return ""
    delta = block["delta"]["scored_sidecars"]
    end = block["measured_at_end"]
    if not delta and block["identical"]:
        return ("DRIFT: none — the corpus was identical when this figure "
                "finished.")
    return (f"DRIFT while computing: scored sidecars "
            f"{block['frozen_at_start']['scored_sidecars']:,} → "
            f"{end['scored_sidecars']:,} ({delta:+,}), "
            f"{block['scored_removed']} removed, "
            f"{block['sweeps_added']} new sweeps (collection paused).  "
            "This figure uses the FROZEN list only.")

#: The four channels by two edges, ordered so a channel's own two edges are
#: adjacent.  Stated in every caption that uses it.
TUNINGS = [(channel, edge) for channel in (1, 2, 3, 4)
           for edge in ("lower", "upper")]
TUNING_LABELS = [f"ch{channel} {edge}" for channel, edge in TUNINGS]


# --------------------------------------------------------------------------
# phi
# --------------------------------------------------------------------------

def phi_from_counts(usable: int, fired_a: int, fired_b: int, both: int):
    """phi for two binary decisions, via the module's own normalisation."""
    if not usable:
        return None
    solved = cr.solve_coincidence(fired_a / usable, fired_b / usable,
                                  both / usable, 0.0)
    return solved["phi"]


def phi_of_columns(left: np.ndarray, right: np.ndarray):
    """phi between two boolean columns over the rows where both are defined."""
    usable = int(left.size)
    return phi_from_counts(usable, int(left.sum()), int(right.sum()),
                           int(np.logical_and(left, right).sum()))


def phi_matrix(columns: np.ndarray) -> np.ndarray:
    """Symmetric phi matrix over boolean columns of one identical population."""
    width = columns.shape[1]
    out = np.full((width, width), np.nan, dtype=float)
    for i in range(width):
        out[i, i] = 1.0
        for j in range(i + 1, width):
            value = phi_of_columns(columns[:, i], columns[:, j])
            out[i, j] = out[j, i] = np.nan if value is None else value
    return out


# --------------------------------------------------------------------------
# the corpus, as one table
# --------------------------------------------------------------------------

class Corpus:
    def __init__(self):
        self.cache = ex.Cache()
        self.pairs, self.census = self.cache.pairs()
        if not self.pairs:
            raise SystemExit("no paired sweeps in the cache")
        self.entries = [entry for pair in self.pairs for entry in pair["radios"]]
        self.methods = cr.methods_in(self.entries)
        self.thresholds = cr.null_thresholds(self.entries)
        self.snapshot = json.loads(SNAPSHOT.read_text())
        self._table = None

    # -- census ---------------------------------------------------------
    def frozen_census(self) -> dict:
        return {key: self.snapshot[key] for key in
                ("measured_utc", "sweeps_on_share", "corpus_entries",
                 "scored_sidecars", "scored_digest")}

    def census_block(self) -> dict:
        geometry = {"same-edge": 0, "opposite-edge": 0}
        for pair in self.pairs:
            geometry[pair["geometry"]] = geometry.get(pair["geometry"], 0) + 1
        return {
            "frozen_at_start": self.frozen_census(),
            "paired_sweeps": len(self.pairs),
            "paired_sweeps_by_geometry": geometry,
            "matched_arm_paired_sweeps": sum(1 for p in self.pairs
                                             if p["matched_arm"]),
            "scored_sidecars_in_a_pair": len(self.entries),
            "load_pairs_census": self.census,
            "excluded_receivers": self.excluded_receivers(),
            "threshold_keys": len(self.thresholds),
        }

    def excluded_receivers(self) -> dict:
        dropped: dict[str, int] = {}
        for pair in self.pairs:
            for label, count in pair["excluded_receivers"].items():
                dropped[label] = dropped.get(label, 0) + count
        return dropped

    # -- the observation table ------------------------------------------
    def table(self) -> dict:
        """Every live TARGET observation, with all eight verdicts.

        An observation enters only if every method returned a verdict on it, so
        every column of every matrix below rests on one identical population.
        """
        if self._table is not None:
            return self._table
        fired, meta, partial = [], [], 0
        for pair in self.pairs:
            for entry in pair["radios"]:
                key = cr.threshold_key(entry)
                for observation in entry["scores"]["observations"]:
                    if observation.get("arm") != "target":
                        continue
                    if not cr._live(observation):
                        continue
                    verdicts = [cr.observation_fires(
                        observation, method,
                        cr._threshold_for(self.thresholds, method, key))
                        for method in self.methods]
                    if any(v is None for v in verdicts):
                        partial += 1
                        continue
                    fired.append([bool(v) for v in verdicts])
                    meta.append((entry["capture"], entry["radio_id"],
                                 observation.get("receiver_label"),
                                 observation.get("channel"),
                                 observation.get("edge"),
                                 observation.get("iq_index"),
                                 pair["paired_sweep"], pair["geometry"],
                                 entry["sample_rate_hz"], entry["probe_ms"]))
        self._table = {
            "fired": np.array(fired, dtype=bool),
            "capture": np.array([m[0] for m in meta]),
            "radio": np.array([m[1] for m in meta]),
            "receiver": np.array([m[2] for m in meta]),
            "channel": np.array([m[3] for m in meta], dtype=np.int16),
            "edge": np.array([m[4] for m in meta]),
            "instant": np.array([m[5] for m in meta], dtype=np.int16),
            "sweep": np.array([m[6] for m in meta]),
            "geometry": np.array([m[7] for m in meta]),
            "rate": np.array([m[8] for m in meta], dtype=float),
            "probe": np.array([m[9] for m in meta], dtype=float),
            "observations_with_a_missing_verdict": partial,
        }
        return self._table

    def any_method_column(self) -> np.ndarray:
        """The repo's channel-level rule: any of the scored methods fires.

        ``channel_instant_verdicts`` answers channel questions this way, so the
        two channel figures use the same rule rather than inventing one.
        """
        return self.table()["fired"].any(axis=1)


# --------------------------------------------------------------------------
# helpers the figures share
# --------------------------------------------------------------------------

def write_outputs(name: str, data: dict, figure) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    (FIGURES / f"{name}.json").write_text(json.dumps(data, indent=2,
                                                     default=_plain) + "\n")
    figure.savefig(FIGURES / f"{name}.png", dpi=150,
                   facecolor=figure.get_facecolor())
    print(f"wrote {FIGURES / f'{name}.png'}")
    print(f"wrote {FIGURES / f'{name}.json'}")


def _plain(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.bool_,)):
        return bool(value)
    raise TypeError(repr(value))


def selftest() -> None:
    """phi here must equal Pearson on the same binary columns."""
    generator = np.random.default_rng(20260814)
    for _ in range(200):
        size = int(generator.integers(30, 400))
        left = generator.random(size) < generator.random()
        right = np.where(generator.random(size) < 0.7, left,
                         generator.random(size) < 0.5)
        if left.std() == 0 or right.std() == 0:
            continue
        mine = phi_of_columns(left, right)
        pearson = float(np.corrcoef(left.astype(float), right.astype(float))[0, 1])
        assert abs(mine - pearson) < 1e-9, (mine, pearson)
    print("selftest: phi == numpy.corrcoef on binary columns, 200 cases")


if __name__ == "__main__":
    selftest()
    corpus = Corpus()
    table = corpus.table()
    print(json.dumps({
        "census": corpus.census_block(),
        "methods": corpus.methods,
        "live_target_observations": int(table["fired"].shape[0]),
        "observations_with_a_missing_verdict":
            table["observations_with_a_missing_verdict"],
        "fire_rate": {m: float(table["fired"][:, i].mean())
                      for i, m in enumerate(corpus.methods)},
        "any_method_fire_rate": float(corpus.any_method_column().mean()),
        "receivers": {label: int((table["receiver"] == label).sum())
                      for label in sorted(set(table["receiver"].tolist()))},
        "channels": {int(c): int((table["channel"] == c).sum())
                     for c in sorted(set(table["channel"].tolist()))},
        "edges": {e: int((table["edge"] == e).sum())
                  for e in sorted(set(table["edge"].tolist()))},
    }, indent=2, default=_plain))
