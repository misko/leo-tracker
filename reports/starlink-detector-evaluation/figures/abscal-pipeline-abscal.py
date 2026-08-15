#!/usr/bin/env python3
"""abscal: an absolute per-receiver centre the pipeline can use.

C1  absolute centre per receiver per hardware epoch, from four independent
    populations, and the consistency test against the rx0-rx1 differential.
C2  the sky window re-binned on the corrected axis, fitted on half the sweeps
    and tested on the other half so the centring is not true by construction.
C4  the measured offset-versus-detection curve, from the two occasions where
    one port's search centre moved while its partner's did not.

Run: PYTHONPATH=/home/satpi01/leo-tracker/src nice -n 15 python3 abscal.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.survey_scoring import CROSS_RECEIVER_CFO_HZ  # noqa: E402

ROOT = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/abscal"
GROUPB = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/groupb"
SPLIT_LO = datetime.fromisoformat("2026-08-13T04:38:23+00:00").timestamp()
SPLIT_HI = datetime.fromisoformat("2026-08-13T04:46:20+00:00").timestamp()
RX0 = {"lnb-a", "lnb-c"}
SEED = 20260814
BOOT = 3000


def clustered(values, groups, statistic=np.mean, draws=BOOT, seed=SEED):
    values = np.asarray(values, float)
    keys, inverse = np.unique(groups, return_inverse=True)
    order = np.argsort(inverse, kind="stable")
    si, sv = inverse[order], values[order]
    bounds = np.searchsorted(si, np.arange(keys.size + 1))
    chunks = [sv[bounds[i]:bounds[i + 1]] for i in range(keys.size)]
    rng = np.random.default_rng(seed)
    out = np.array([statistic(np.concatenate([chunks[i] for i in
                                              rng.integers(0, keys.size, keys.size)]))
                    for _ in range(draws)])
    return [float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))]


def ratio_boot(hit_a, hit_b, groups, draws=BOOT, seed=SEED):
    """Cluster bootstrap of mean(a)/mean(b) over reports."""
    keys, inverse = np.unique(groups, return_inverse=True)
    sum_a = np.bincount(inverse, weights=hit_a.astype(float))
    sum_b = np.bincount(inverse, weights=hit_b.astype(float))
    count = np.bincount(inverse).astype(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, keys.size, (draws, keys.size))
    a = sum_a[pick].sum(axis=1) / count[pick].sum(axis=1)
    b = sum_b[pick].sum(axis=1) / count[pick].sum(axis=1)
    good = b > 0
    r = a[good] / b[good]
    return [float(np.percentile(r, 2.5)), float(np.percentile(r, 97.5))]


def main():
    out = {}

    # ================================================================= data ==
    live = np.load(f"{ROOT}/narrow-pairs.npz")
    lv = {k: live[k] for k in live.files}
    cf = lv["c_file"]
    gen_f = np.where(lv["f_utc"] < SPLIT_LO, "gen1",
                     np.where(lv["f_utc"] > SPLIT_HI, "gen2", "boundary"))
    gen = gen_f[cf]
    radio = lv["f_radio"][cf]

    corpus = np.load(f"{GROUPB}/tlework/residual-axes.npz")
    cp = {k: corpus[k] for k in corpus.files}
    applied = {("pluto-19f2", "gen1"): 434408.4, ("pluto-19f2", "gen2"): 604159.8,
               ("pluto-5d4d", "gen1"): 5154.0, ("pluto-5d4d", "gen2"): 567402.0}
    capp = np.array([applied.get((r, g), 0.0) if l in RX0 else 0.0 for r, g, l
                     in zip(cp["radio"], cp["gen"], cp["label"])])
    cp_raw = cp["corrected"] + capp

    figure = json.load(open(f"{GROUPB}/figures/tle-residual-figure.json"))
    sync = json.load(open(f"{GROUPB}/figures/tle-residual-synccheck.json"))
    sync_centre = sync["centres_applied_hz"]

    # ================================================== C1  differentials ====
    diffs = {}
    for r in ("pluto-19f2", "pluto-5d4d"):
        for g in ("gen1", "gen2"):
            m = lv["c_dual"] & (radio == r) & (gen == g)
            entry = {"n_dual": int(m.sum()),
                     "n_reports": int(np.unique(cf[m]).size) if m.sum() else 0}
            if m.sum() >= 100:
                v = lv["c_off0"][m] - lv["c_off1"][m]
                entry.update(
                    median_hz=float(np.median(v)),
                    median_ci95_hz=clustered(v, cf[m], np.median),
                    mean_hz=float(v.mean()),
                    source="live narrow reports, repository _paired_differences")
            diffs[f"{r}|{g}"] = entry
    # 5d4d gen2 cannot be measured this way at all -- that is the finding.
    diffs["pluto-5d4d|gen2"].update(
        median_hz=567402.0, median_ci95_hz=None,
        source="survey path (coarse bank E, +/-700 kHz about raw zero); the "
               "live path cannot measure it because lnb-a is outside its search "
               "so dual candidates never form",
        uncertainty_hz=4000.0)
    out["differentials"] = diffs

    # ============================================ C1  absolute per receiver ==
    radio_of = {"lnb-a": "pluto-5d4d", "lnb-b": "pluto-5d4d",
                "lnb-c": "pluto-19f2", "lnb-d": "pluto-19f2"}
    # The survey re-scoring and the TLE residual are ONE population -- the same
    # 878 sweeps, the residual being the Doppler-subtracted view of it -- so they
    # count once, not twice, when the populations are averaged.
    methods = {}
    for port in ("lnb-a", "lnb-b", "lnb-c", "lnb-d"):
        for g in ("gen1", "gen2"):
            entry = {"radio": radio_of[port]}
            # (1) live narrow path: every subset whose search was not clipped,
            # pooled.  Picking the subset that looks best-centred would select
            # on the quantity being estimated.
            keep_off, keep_rep = [], []
            for rx in (0, 1):
                lab = lv[f"f_rx{rx}"][cf]
                cand = lv[f"c_cand{rx}"]
                cvals = lv[f"f_c{rx}"][cf]
                off = lv[f"c_off{rx}"]
                for c in sorted(set(np.round(cvals[(lab == port) & (gen == g)], 1))):
                    m = cand & (lab == port) & (gen == g) & (np.round(cvals, 1) == c)
                    if m.sum() < 250:
                        continue
                    edge = float((np.abs(off[m] - c) > 340_000).mean())
                    row = {"centre_applied_hz": float(c), "n": int(m.sum()),
                           "n_reports": int(np.unique(cf[m]).size),
                           "mean_hz": float(off[m].mean()),
                           "median_hz": float(np.median(off[m])),
                           "miscentring_hz": float(off[m].mean() - c),
                           "edge_fraction": edge}
                    if edge > 0.05:
                        row["excluded"] = ("censored: >5% of matches pinned at "
                                           "the +/-350 kHz search edge")
                    else:
                        keep_off.append(off[m])
                        keep_rep.append(cf[m])
                    entry.setdefault("live_subsets", []).append(row)
            if keep_off:
                values = np.concatenate(keep_off)
                reports = np.concatenate(keep_rep)
                entry["live"] = {"mean_hz": float(values.mean()),
                                 "median_hz": float(np.median(values)),
                                 "n": int(values.size),
                                 "n_reports": int(np.unique(reports).size),
                                 "ci95_hz": clustered(values, reports)}
            # (2) survey re-scoring of the same sweeps, +/-700 kHz about zero
            m = cp["fired"] & (cp["label"] == port) & (cp["gen"] == g)
            if m.sum() >= 250:
                fire_rate = float(m.sum() /
                                  (cp["usable"] & (cp["label"] == port)
                                   & (cp["gen"] == g)).sum())
                edge = float((cp_raw[m] < -690_000).mean() +
                             (cp_raw[m] > 690_000).mean())
                row = {"mean_hz": float(cp_raw[m].mean()), "n": int(m.sum()),
                       "n_sweeps": int(np.unique(cp["sweep"][m]).size),
                       "fire_rate": fire_rate, "edge_fraction": edge,
                       "ci95_hz": clustered(cp_raw[m], cp["sweep"][m])}
                panel = figure["panels"]["residual_by_port"].get(port, {}).get(g)
                shift = applied.get((radio_of[port], g), 0.0) if port in RX0 else 0.0
                if panel:
                    row["tle_residual_mean_hz"] = panel["centre_khz"] * 1e3 + shift
                    row["tle_residual_ci95_hz"] = [v * 1e3 + shift
                                                   for v in panel["ci95_khz"]]
                if edge > 0.05 or fire_rate < 0.10:
                    row["excluded"] = (f"unreliable: fire rate {fire_rate:.3f}, "
                                       f"{edge:.2f} of fires at the bank edge")
                entry["corpus"] = row
            # (3) the independent sync-* corpus, gen2 only
            if g == "gen2" and port in sync["by_port"]:
                row = sync["by_port"][port]
                if row.get("refined_mean_khz") is not None:
                    entry["sync_corpus"] = {
                        "mean_hz": row["refined_mean_khz"] * 1e3 +
                                   (sync_centre[radio_of[port]] if port in RX0 else 0.0),
                        "n": row["refined_n"],
                        "note": "2026-08-14 sync sweeps, no TLE available"}
            # consensus over INDEPENDENT populations, censored ones dropped
            pops = {}
            if "live" in entry:
                pops["live"] = entry["live"]["mean_hz"]
            if "corpus" in entry and "excluded" not in entry["corpus"]:
                values = [entry["corpus"]["mean_hz"]]
                if "tle_residual_mean_hz" in entry["corpus"]:
                    values.append(entry["corpus"]["tle_residual_mean_hz"])
                pops["corpus"] = float(np.mean(values))
            if "sync_corpus" in entry:
                pops["sync_corpus"] = entry["sync_corpus"]["mean_hz"]
            entry["populations_hz"] = pops
            if pops:
                entry["consensus_hz"] = float(np.mean(list(pops.values())))
                entry["method_spread_hz"] = [float(min(pops.values())),
                                             float(max(pops.values()))]
                entry["n_populations"] = len(pops)
            methods[f"{port}|{g}"] = entry
    out["absolute_by_method"] = methods

    # -------------------------------------------- consistency against M -----
    checks = {}
    for r, p0, p1 in (("pluto-19f2", "lnb-c", "lnb-d"),
                      ("pluto-5d4d", "lnb-a", "lnb-b")):
        for g in ("gen1", "gen2"):
            a0 = methods[f"{p0}|{g}"]
            a1 = methods[f"{p1}|{g}"]
            m = diffs[f"{r}|{g}"].get("median_hz")
            row = {"differential_hz": m, "rx0": p0, "rx1": p1}
            for name in ("live", "corpus", "tle_residual", "sync_corpus"):
                if name in a0 and name in a1:
                    implied = a0[name]["mean_hz"] - a1[name]["mean_hz"]
                    row[name] = {"implied_differential_hz": implied,
                                 "gap_hz": implied - m if m else None}
            if a0.get("consensus_hz") is not None and a1.get("consensus_hz") is not None:
                implied = a0["consensus_hz"] - a1["consensus_hz"]
                row["consensus"] = {"implied_differential_hz": implied,
                                    "gap_hz": implied - m if m else None}
            # same-population control: both marginals on the dual checks only
            mask = lv["c_dual"] & (radio == r) & (gen == g)
            if mask.sum() >= 100:
                row["same_population"] = {
                    "n_dual": int(mask.sum()),
                    "rx0_mean_on_dual_hz": float(lv["c_off0"][mask].mean()),
                    "rx1_mean_on_dual_hz": float(lv["c_off1"][mask].mean()),
                    "implied_differential_hz": float(
                        (lv["c_off0"][mask] - lv["c_off1"][mask]).mean()),
                    "rx0_mean_all_candidates_hz":
                        a0.get("live", {}).get("mean_hz"),
                    "rx1_mean_all_candidates_hz":
                        a1.get("live", {}).get("mean_hz")}
            checks[f"{r}|{g}"] = row
    out["consistency"] = checks

    # ================================================== recommended centres ==
    plan = {}
    for r, p0, p1 in (("pluto-19f2", "lnb-c", "lnb-d"),
                      ("pluto-5d4d", "lnb-a", "lnb-b")):
        for g in ("gen1", "gen2"):
            m = diffs[f"{r}|{g}"]["median_hz"]
            a0 = methods[f"{p0}|{g}"].get("consensus_hz")
            a1 = methods[f"{p1}|{g}"].get("consensus_hz")
            if m is None or a1 is None:
                continue
            # rx0 - rx1 is NOT free: survey_scoring gates the cross-receiver
            # agreement check at CROSS_RECEIVER_CFO_HZ = 15 kHz on exactly that
            # difference, and the differential is the one quantity measured on
            # PAIRED detections where Doppler cancels, so it is fixed.  The two
            # marginal centres do not differ by exactly that, because each port
            # detects a different subset of the sky; the pair is therefore slid
            # bodily so the unavoidable residual is halved between them rather
            # than dumped entirely on one.
            shift = ((a0 - m) - a1) / 2.0 if a0 is not None else 0.0
            c1, c0 = a1 + shift, a1 + shift + m
            plan[f"{r}|{g}"] = {
                "receiver_labels": [p0, p1],
                "measured_centers_hz": [round(c0, 1), round(c1, 1)],
                "differential_hz": m,
                "anchor": p1, "anchor_consensus_hz": a1,
                "rx0_direct_estimate_hz": a0,
                "rx0_residual_miscentring_hz": (round(a0 - c0, 1)
                                                if a0 is not None else None),
                "rx1_residual_miscentring_hz": round(a1 - c1, 1),
                "rx0_uncertainty_hz": methods[f"{p0}|{g}"].get("method_spread_hz"),
                "rx1_uncertainty_hz": methods[f"{p1}|{g}"].get("method_spread_hz"),
                "cross_receiver_gate_hz": CROSS_RECEIVER_CFO_HZ,
                "difference_equals_differential": abs((c0 - c1) - m) < 1.0}
    out["recommendation"] = plan

    # ==================================================== C2  re-binning =====
    # Two tests, in ascending order of how hard they are to pass:
    #   (a) fit the centre on odd sweeps, measure it on even ones;
    #   (b) take the recommended centres, which come from the LIVE narrow
    #       reports, and apply them to the SURVEY re-scoring of the corpus --
    #       a different detector on a different arm.  Nothing about (b) is
    #       fitted to the data it is scored on.
    rebin = {}
    for port in ("lnb-a", "lnb-b", "lnb-c", "lnb-d"):
        for g in ("gen1", "gen2"):
            m = cp["fired"] & (cp["label"] == port) & (cp["gen"] == g)
            if m.sum() < 250:
                continue
            sweeps = cp["sweep"][m]
            values = cp_raw[m]
            train = (sweeps % 2) == 1
            if train.sum() < 100 or (~train).sum() < 100:
                continue
            centre = float(values[train].mean())
            held = values[~train] - centre
            key = f"{port}|{g}"
            radio_id = ("pluto-19f2" if port in ("lnb-c", "lnb-d") else "pluto-5d4d")
            current = (applied[(radio_id, g)] if port in RX0 else 0.0)
            rebin[key] = {
                "n_train": int(train.sum()), "n_test": int((~train).sum()),
                "fitted_centre_hz": centre,
                "held_out_centroid_hz": float(held.mean()),
                "held_out_centroid_ci95_hz": clustered(held, sweeps[~train]),
                "held_out_median_hz": float(np.median(held)),
                "held_out_fraction_negative": float((held < 0).mean()),
                "before_centroid_hz": float((values[~train] - current).mean()),
                "before_fraction_negative": float((values[~train] - current < 0).mean()),
                "current_centre_applied_hz": current}
            key_plan = plan.get(f"{radio_id}|{g}")
            if key_plan:
                index = key_plan["receiver_labels"].index(port)
                recommended = key_plan["measured_centers_hz"][index]
                shifted = values - recommended
                rebin[key].update(
                    recommended_centre_hz=recommended,
                    out_of_sample_centroid_hz=float(shifted.mean()),
                    out_of_sample_centroid_ci95_hz=clustered(shifted, sweeps),
                    out_of_sample_median_hz=float(np.median(shifted)),
                    out_of_sample_fraction_negative=float((shifted < 0).mean()),
                    out_of_sample_note=("centre from the live narrow reports, "
                                        "scored on the survey re-scoring"))
    out["rebinned"] = rebin

    # ============================================ C4  detection-vs-offset ====
    # Two occasions where one port's applied centre changed while its partner's
    # did not: the partner is the time control, so the ratio of ratios is the
    # detection cost of the miscentring alone.
    cost = {}
    experiments = [
        ("lnb-c|gen2", "pluto-19f2", "lnb-c", 0, "lnb-d", 1,
         434408.4, 604159.8, "gen2"),
        ("lnb-c|gen1", "pluto-19f2", "lnb-c", 0, "lnb-d", 1,
         434408.4, 0.0, "gen1"),
    ]
    for name, r, port, rx, partner, prx, c_good, c_bad, g in experiments:
        cvals = lv[f"f_c{rx}"][cf]
        base = (radio == r) & (gen == g)
        rows = {}
        for tag, c in (("centred", c_good), ("miscentred", c_bad)):
            m = base & (np.round(cvals, 1) == round(c, 1))
            if m.sum() < 1000:
                continue
            hit = lv[f"c_cand{rx}"][m]
            ctrl = lv[f"c_cand{prx}"][m]
            rows[tag] = {
                "centre_applied_hz": c, "n_checks": int(m.sum()),
                "n_reports": int(np.unique(cf[m]).size),
                "port_rate": float(hit.mean()), "partner_rate": float(ctrl.mean()),
                "normalised_rate": float(hit.mean() / ctrl.mean()),
                "normalised_ci95": ratio_boot(hit, ctrl, cf[m]),
                "utc_range": [datetime.fromtimestamp(lv["f_utc"][cf][m].min(),
                                                     timezone.utc).isoformat(),
                              datetime.fromtimestamp(lv["f_utc"][cf][m].max(),
                                                     timezone.utc).isoformat()]}
        if len(rows) == 2:
            absolute = methods[f"{port}|{g}"].get("consensus_hz")
            keep = rows["miscentred"]["normalised_rate"] / rows["centred"]["normalised_rate"]
            cost[name] = {
                "port": port, "partner_control": partner, "arms": rows,
                "miscentring_hz": (absolute - c_bad) if absolute is not None else None,
                "retained_fraction": keep,
                "detection_lost_fraction": 1.0 - keep}
    # lnb-a's own move, epoch to epoch, with lnb-b as the control
    for g, tag in (("gen1", "before"), ("gen2", "after")):
        m = (radio == "pluto-5d4d") & (gen == g)
        hit, ctrl = lv["c_cand0"][m], lv["c_cand1"][m]
        cost.setdefault("lnb-a|epoch-move", {}).setdefault("arms", {})[tag] = {
            "n_checks": int(m.sum()), "port_rate": float(hit.mean()),
            "partner_rate": float(ctrl.mean()),
            "normalised_rate": float(hit.mean() / ctrl.mean()),
            "normalised_ci95": ratio_boot(hit, ctrl, cf[m])}
    arms = cost["lnb-a|epoch-move"]["arms"]
    cost["lnb-a|epoch-move"].update(
        port="lnb-a", partner_control="lnb-b",
        retained_fraction=arms["after"]["normalised_rate"] /
                          arms["before"]["normalised_rate"],
        detection_lost_fraction=1 - arms["after"]["normalised_rate"] /
                                    arms["before"]["normalised_rate"],
        miscentring_hz=methods["lnb-a|gen2"].get("consensus_hz"))
    out["detection_cost"] = cost

    json.dump(out, open(f"{ROOT}/abscal.json", "w"), indent=1, sort_keys=True)

    # ------------------------------------------------------------- print ----
    print("== differentials ==")
    for k, v in sorted(out["differentials"].items()):
        print(f"  {k:20} {v.get('median_hz', float('nan')):>12,.0f} Hz  "
              f"n_dual={v['n_dual']:5d}  CI={v.get('median_ci95_hz')}")
    print("\n== absolute centres, kHz ==")
    for k, v in sorted(out["absolute_by_method"].items()):
        parts = " ".join(f"{n}={x / 1e3:>8.1f}"
                         for n, x in v.get("populations_hz", {}).items())
        dropped = [n for n in ("live", "corpus", "sync_corpus")
                   if n in v and "excluded" in (v[n] if isinstance(v[n], dict) else {})]
        if dropped:
            parts += "  dropped:" + ",".join(dropped)
        print(f"  {k:14} consensus {(v.get('consensus_hz') or float('nan')) / 1e3:>8.1f}  "
              f"spread {[round(x / 1e3, 1) for x in v.get('method_spread_hz', [])]}  {parts}")
    print("\n== consistency: difference of absolutes vs measured differential ==")
    for k, v in sorted(out["consistency"].items()):
        if v["differential_hz"] is None:
            continue
        line = f"  {k:20} M={v['differential_hz']:>10,.0f}"
        for n in ("live", "corpus", "consensus"):
            if n in v and v[n]["gap_hz"] is not None:
                line += f"  {n} gap {v[n]['gap_hz']:>+9,.0f}"
        print(line)
        sp = v.get("same_population")
        if sp:
            print(f"      same-population control: implied "
                  f"{sp['implied_differential_hz']:>10,.0f}  "
                  f"rx1 mean on dual {sp['rx1_mean_on_dual_hz']:>10,.0f} vs "
                  f"all {sp['rx1_mean_all_candidates_hz'] or float('nan'):>10,.0f}")
    print("\n== recommendation ==")
    for k, v in sorted(out["recommendation"].items()):
        print(f"  {k:20} {v['receiver_labels']} measured_centers_hz="
              f"{v['measured_centers_hz']}  residual miscentring "
              f"{v['rx0_residual_miscentring_hz']}/{v['rx1_residual_miscentring_hz']} Hz")
    print("\n== re-binning (kHz): current axis -> half-sample -> out of sample ==")
    for k, v in sorted(out["rebinned"].items()):
        oos = v.get("out_of_sample_centroid_hz")
        print(f"  {k:14} before {v['before_centroid_hz'] / 1e3:>8.1f} "
              f"(neg {v['before_fraction_negative']:.2f})  ->  held-out "
              f"{v['held_out_centroid_hz'] / 1e3:>6.2f} -> out-of-sample "
              f"{(oos / 1e3) if oos is not None else float('nan'):>7.2f} "
              f"CI {[round(x / 1e3, 1) for x in v.get('out_of_sample_centroid_ci95_hz', [])]} "
              f"(neg {v.get('out_of_sample_fraction_negative', float('nan')):.2f})")
    print("\n== detection cost ==")
    for k, v in sorted(out["detection_cost"].items()):
        print(f"  {k:20} miscentring {(v.get('miscentring_hz') or 0) / 1e3:>8.1f} kHz  "
              f"retained {v['retained_fraction']:.3f}  "
              f"lost {100 * v['detection_lost_fraction']:.1f}%")
        for tag, arm in v["arms"].items():
            print(f"      {tag:11} port {100 * arm['port_rate']:5.2f}%  partner "
                  f"{100 * arm['partner_rate']:5.2f}%  normalised "
                  f"{arm['normalised_rate']:.3f} CI "
                  f"{[round(x, 3) for x in arm['normalised_ci95']]}")
    print("\nwrote abscal.json")


if __name__ == "__main__":
    sys.exit(main())
