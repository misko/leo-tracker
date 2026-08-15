#!/usr/bin/env python3
"""Per-epoch live differentials and per-port live absolute centres."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import numpy as np

ROOT = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/abscal"
SPLIT_LO = datetime.fromisoformat("2026-08-13T04:38:23+00:00").timestamp()
SPLIT_HI = datetime.fromisoformat("2026-08-13T04:46:20+00:00").timestamp()
SEED = 20260814
BOOT = 2000


def boot_stat(values, groups, fn=np.mean, draws=BOOT, seed=SEED):
    """Cluster bootstrap over reports, for any statistic."""
    values = np.asarray(values, float)
    if values.size < 10:
        return None
    keys, inverse = np.unique(groups, return_inverse=True)
    order = np.argsort(inverse, kind="stable")
    sorted_inverse = inverse[order]
    sorted_values = values[order]
    bounds = np.searchsorted(sorted_inverse, np.arange(keys.size + 1))
    chunks = [sorted_values[bounds[i]:bounds[i + 1]] for i in range(keys.size)]
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(draws):
        pick = rng.integers(0, keys.size, keys.size)
        out.append(fn(np.concatenate([chunks[i] for i in pick])))
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]


def main():
    z = np.load(f"{ROOT}/narrow-live.npz")
    d = {k: z[k] for k in z.files}
    utc = d["f_utc"]
    gen = np.where(utc < SPLIT_LO, "gen1",
                   np.where(utc > SPLIT_HI, "gen2", "boundary"))
    radio = d["f_radio"]

    print("reports by radio/gen:")
    for r in np.unique(radio):
        for g in ("gen1", "gen2", "boundary"):
            m = (radio == r) & (gen == g)
            if not m.sum():
                continue
            print(f"  {r} {g}: {m.sum():5d} reports  "
                  f"{datetime.fromtimestamp(utc[m].min(), timezone.utc):%m-%dT%H:%M} .. "
                  f"{datetime.fromtimestamp(utc[m].max(), timezone.utc):%m-%dT%H:%M}  "
                  f"centres applied rx0 {sorted(set(np.round(d['f_c0'][m], 1)))} "
                  f"rx1 {sorted(set(np.round(d['f_c1'][m], 1)))}")

    result = {"differentials": {}, "absolute_live": {}}

    # ---------------------------------------------------- differentials ----
    pf, pd = d["p_file"], d["p_diff"]
    print("\nlive differential rx0-rx1 (repo _paired_differences, median):")
    for r in np.unique(radio):
        for g in ("gen1", "gen2"):
            m = (radio[pf] == r) & (gen[pf] == g)
            if m.sum() < 40:
                continue
            v = pd[m]
            ci = boot_stat(v, pf[m], np.median)
            key = f"{r}|{g}"
            result["differentials"][key] = {
                "median_hz": float(np.median(v)), "n_pairs": int(m.sum()),
                "n_reports": int(np.unique(pf[m]).size),
                "p10_hz": float(np.percentile(v, 10)),
                "p90_hz": float(np.percentile(v, 90)),
                "median_ci95_hz": ci,
                "trimmed_mean_hz": float(np.mean(
                    v[(v > np.percentile(v, 10)) & (v < np.percentile(v, 90))])),
            }
            e = result["differentials"][key]
            print(f"  {key:20} median {e['median_hz']:>10.1f} Hz  "
                  f"CI {np.round(ci, 1) if ci else None}  n={e['n_pairs']:5d} "
                  f"reports={e['n_reports']:4d}  p10/p90 "
                  f"{e['p10_hz']:>10.1f}/{e['p90_hz']:>10.1f}")

    # ------------------------------------------------ absolute per port ----
    sf, srx, soff, scand = d["s_file"], d["s_rx"], d["s_off"], d["s_cand"]
    label = np.where(srx == 0, d["f_rx0"][sf], d["f_rx1"][sf])
    centre = np.where(srx == 0, d["f_c0"][sf], d["f_c1"][sf])
    print("\nlive per-port absolute offset, candidates only "
          "(search is centre +/- 350 kHz):")
    for p in ("lnb-a", "lnb-b", "lnb-c", "lnb-d"):
        for g in ("gen1", "gen2"):
            m = scand & (label == p) & (gen[sf] == g)
            if m.sum() < 40:
                continue
            v = soff[m]
            rel = v - centre[m]
            ci = boot_stat(v, sf[m], np.mean)
            key = f"{p}|{g}"
            result["absolute_live"][key] = {
                "n": int(m.sum()), "n_reports": int(np.unique(sf[m]).size),
                "radio": str(radio[sf][m][0]),
                "mean_hz": float(v.mean()), "median_hz": float(np.median(v)),
                "mean_ci95_hz": ci,
                "p10_hz": float(np.percentile(v, 10)),
                "p90_hz": float(np.percentile(v, 90)),
                "centres_applied_hz": sorted(set(np.round(centre[m], 1))),
                "rel_to_search_centre_mean_hz": float(rel.mean()),
                "frac_at_low_edge": float((rel < -340_000).mean()),
                "frac_at_high_edge": float((rel > 340_000).mean()),
            }
            e = result["absolute_live"][key]
            print(f"  {key:12} n={e['n']:6d} rep={e['n_reports']:4d} "
                  f"mean {e['mean_hz']:>10.1f}  med {e['median_hz']:>10.1f}  "
                  f"CI {np.round(ci, 0) if ci else None}  "
                  f"rel {e['rel_to_search_centre_mean_hz']:>9.1f}  "
                  f"edge lo/hi {e['frac_at_low_edge']:.3f}/{e['frac_at_high_edge']:.3f} "
                  f"C={e['centres_applied_hz']}")

    json.dump(result, open(f"{ROOT}/live-stats.json", "w"), indent=1, sort_keys=True)
    print("\nwrote live-stats.json")


if __name__ == "__main__":
    sys.exit(main())
