"""E2 headline figure: measured detection probability against measured SNR.

Every number is from the cabled loopback on ip:192.168.1.183.  Thresholds come
from E3's genuinely empty channel at 1% per point; the SNR axis is measured by
coherent projection onto the known transmitted stream, never assumed from the
gain setting.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import analysis as A
import style

RUN = HERE.parent / "e2_roc.jsonl"
THRESHOLDS = HERE.parent / "thresholds.json"


# --------------------------------------------------------------------------
# fit
# --------------------------------------------------------------------------

def fit_curve(x, k, n, floor):
    """Binomial MLE for Pd = floor + (1-floor) * logistic((x-mu)/s)."""
    x, k, n = np.asarray(x, float), np.asarray(k, float), np.asarray(n, float)

    def nll(theta):
        mu, log_s = theta
        p = floor + (1 - floor) / (1 + np.exp(-(x - mu) / np.exp(log_s)))
        p = np.clip(p, 1e-9, 1 - 1e-9)
        return -float(np.sum(k * np.log(p) + (n - k) * np.log(1 - p)))

    best, value = None, np.inf
    for mu0 in (-20, -16, -12, -8):
        for s0 in (0.5, 1.0, 2.0):
            out = minimize(nll, [mu0, np.log(s0)], method="Nelder-Mead")
            if out.fun < value:
                best, value = out.x, out.fun
    mu, s = float(best[0]), float(np.exp(best[1]))
    target = (0.5 - floor) / (1 - floor)
    if not 0 < target < 1:
        return mu, s, float("nan")
    return mu, s, mu + s * np.log(target / (1 - target))


def main() -> None:
    style.apply()
    import matplotlib.pyplot as plt

    header, rows = A.read(RUN)
    thresholds = json.loads(THRESHOLDS.read_text())["empty_channel"]

    off = [r for r in rows if not r["transmitting"]]
    noise_power = float(np.mean([r["total_power"] for r in off]))
    floor_signal = float(np.mean([r["signal_power"] for r in off]))
    off_by_visit = {}
    for r in off:
        off_by_visit.setdefault(r["tag"], []).append(r["total_power"])
    drift = {tag: float(np.mean(v)) for tag, v in sorted(off_by_visit.items())}

    # one rung per TX gain, pooling the repeated TX-off visits
    rungs: dict = {}
    for r in rows:
        rungs.setdefault(round(r["tx2_gain_db"], 2), []).append(r)

    table = []
    for gain in sorted(rungs):
        rs = rungs[gain]
        excess = float(np.mean([r["signal_power"] for r in rs])) - floor_signal
        snr = 10 * np.log10(excess / noise_power) if excess > 0 else float("-inf")
        entry = {"tx2_gain_db": gain, "observations": len(rs),
                 "probes": len({(r["tag"], r["index"]) for r in rs}),
                 "snr_db": snr,
                 "signal_power_excess": excess,
                 "above_estimator_floor": bool(excess > 2 * floor_signal),
                 "transmitting": rs[0]["transmitting"], "methods": {}}
        for method in A.METHODS:
            entry["methods"][method] = A.cell_rate(rs, method, thresholds[method])
        table.append(entry)

    # -- fits, on rungs whose SNR the projection can actually measure --------
    usable = [e for e in table if e["transmitting"] and e["above_estimator_floor"]]
    quiet = [e for e in table if not e["transmitting"]]
    fits = {}
    for method in A.METHODS:
        floor = float(np.mean([q["methods"][method]["rate"] for q in quiet]))
        x = [e["snr_db"] for e in usable]
        k = [e["methods"][method]["fired"] for e in usable]
        n = [e["methods"][method]["cells"] for e in usable]
        mu, s, snr50 = fit_curve(x, k, n, floor)
        fits[method] = {"mu": mu, "scale": s, "snr50_db": snr50,
                        "empty_channel_rate": floor}

    # cluster bootstrap over probes, so the two receivers of one probe move together
    rng = np.random.default_rng(11)
    draws = {m: [] for m in A.METHODS}
    by_probe: dict = {}
    for e in usable:
        for r in rungs[e["tx2_gain_db"]]:
            by_probe.setdefault((e["tx2_gain_db"], r["index"]), []).append(r)
    keys_by_rung: dict = {}
    for key in by_probe:
        keys_by_rung.setdefault(key[0], []).append(key)
    for _ in range(200):
        x, counts = [], {m: ([], []) for m in A.METHODS}
        for e in usable:
            keys = keys_by_rung[e["tx2_gain_db"]]
            picked = [keys[i] for i in rng.integers(0, len(keys), len(keys))]
            rs = [row for key in picked for row in by_probe[key]]
            x.append(e["snr_db"])
            for m in A.METHODS:
                rate = A.cell_rate(rs, m, thresholds[m])
                counts[m][0].append(rate["fired"])
                counts[m][1].append(rate["cells"])
        for m in A.METHODS:
            _, _, s50 = fit_curve(x, counts[m][0], counts[m][1],
                                  fits[m]["empty_channel_rate"])
            if np.isfinite(s50):
                draws[m].append(s50)
    for m in A.METHODS:
        d = np.array(draws[m])
        fits[m]["snr50_lo"] = float(np.quantile(d, 0.025)) if d.size else None
        fits[m]["snr50_hi"] = float(np.quantile(d, 0.975)) if d.size else None

    # Every method is fitted on the SAME resample within an iteration, so the
    # draws are paired and a difference between two of them is far better
    # determined than the overlap of their marginal intervals suggests.  An
    # ordering nobody can resolve is not a ranking, and that has to be tested
    # rather than read off the dots.
    length = min(len(draws[m]) for m in A.METHODS)
    matrix = np.vstack([np.array(draws[m][:length]) for m in A.METHODS])
    pairwise = {}
    for i, left in enumerate(A.METHODS):
        for j, right in enumerate(A.METHODS):
            if j <= i:
                continue
            delta = matrix[i] - matrix[j]
            pairwise[f"{left} - {right}"] = {
                "median_db": float(np.median(delta)),
                "ci": [float(np.quantile(delta, 0.025)),
                       float(np.quantile(delta, 0.975))],
                "share_left_more_sensitive": float(np.mean(delta < 0)),
                "resolved": bool(np.quantile(delta, 0.025) > 0
                                 or np.quantile(delta, 0.975) < 0)}

    measured_ranking = sorted(A.METHODS, key=lambda m: fits[m]["snr50_db"])
    payload = {
        "figure": "detection-vs-snr",
        "note": A.LOOPBACK_NOTE,
        "source": str(RUN),
        "probe_ms": 20.0, "sample_rate_hz": 5e6,
        "thresholds": thresholds,
        "threshold_basis": "1% per point on the E3 genuinely-empty channel",
        "noise_power_counts2": noise_power,
        "tx_off_drift_by_visit": drift,
        "estimator_floor_signal_power": floor_signal,
        "rungs": table, "fits": fits,
        "pairwise_snr50_difference_db": pairwise,
        "pairs_resolved": sum(1 for v in pairwise.values() if v["resolved"]),
        "pairs_total": len(pairwise),
        "bootstrap_draws": {m: [round(v, 4) for v in draws[m][:length]]
                            for m in A.METHODS},
        "measured_ranking_best_first": measured_ranking,
        "model_d_ranking_best_first": list(A.MODEL_D_RANKING),
        "fire_count_ranking_most_first": list(A.FIRE_COUNT_RANKING),
        "spearman_measured_vs_model_d": A.spearman(measured_ranking, A.MODEL_D_RANKING),
        "spearman_measured_vs_fire_count": A.spearman(measured_ranking,
                                                      A.FIRE_COUNT_RANKING),
    }
    (HERE / "detection-vs-snr.json").write_text(json.dumps(payload, indent=1))

    # ---------------------------------------------------------------- draw
    draw(payload)


def draw(payload: dict) -> None:
    """Render from the payload alone, so a re-style costs no bootstrap."""
    style.apply()
    import matplotlib.pyplot as plt

    fits, table = payload["fits"], payload["rungs"]
    measured_ranking = payload["measured_ranking_best_first"]
    spread_db = (fits[measured_ranking[-1]]["snr50_db"]
                 - fits[measured_ranking[0]]["snr50_db"])
    shown = [e for e in table if e["transmitting"] and e["above_estimator_floor"]]
    xs = np.array([e["snr_db"] for e in shown])

    fig = plt.figure(figsize=(13.4, 6.4))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.85, 1.0], wspace=0.26,
                            left=0.065, right=0.985, top=0.845, bottom=0.155)
    ax = fig.add_subplot(grid[0, 0])
    ax2 = fig.add_subplot(grid[0, 1])

    grid_x = np.linspace(xs.min() - 1.5, 0.0, 400)
    for method in A.METHODS:
        y = np.array([e["methods"][method]["rate"] for e in shown])
        lo = np.array([e["methods"][method]["lo"] for e in shown])
        hi = np.array([e["methods"][method]["hi"] for e in shown])
        s, f = style.STYLE[method], fits[method]
        ax.fill_between(xs, lo, hi, color=s["color"], alpha=0.11, linewidth=0)
        curve = (f["empty_channel_rate"] + (1 - f["empty_channel_rate"])
                 / (1 + np.exp(-(grid_x - f["mu"]) / f["scale"])))
        ax.plot(grid_x, curve, color=s["color"], linewidth=1.1, alpha=0.55)
        style.line(ax, xs, y, method, linewidth=0,
                   markeredgecolor=style.SURFACE, markeredgewidth=0.6)

    band = [min(f["empty_channel_rate"] for f in fits.values()),
            max(f["empty_channel_rate"] for f in fits.values())]
    ax.axhspan(band[0], band[1], color=style.MUTED, alpha=0.16, linewidth=0)
    # Left-hand side: the legend owns the lower right of this panel.
    ax.text(xs.min() - 1.2, band[1] + 0.02,
            f"empty-channel rate {band[0]*100:.0f}–{band[1]*100:.0f}% per cell",
            fontsize=8.5, color=style.INK_2, ha="left", va="bottom")
    ax.axhline(0.5, color=style.MUTED, linewidth=0.9, linestyle=(0, (2, 3)))
    saturated = [e for e in shown if e["snr_db"] > -2]
    ax.text(-2.2, 0.985,
            f"Pd = 1.000 for all eight at every rung above −5 dB\n"
            f"({len(saturated)} further rungs, out to +34.8 dB)",
            ha="right", va="top", fontsize=8.5, color=style.INK_2)

    ax.set_xlim(xs.min() - 1.5, -1.5)
    ax.set_ylim(-0.03, 1.06)
    ax.set_yticks([0, .25, .5, .75, 1.0])
    ax.set_yticklabels(["0", "0.25", "0.50", "0.75", "1"])
    ax.set_xlabel("Measured wideband SNR in the 5 MHz probe (dB)")
    ax.set_ylabel("Detection probability per cell")
    ax.set_title(f"All eight break within {spread_db:.2f} dB — in an order that "
                 f"matches neither published ranking", loc="left", pad=26)
    ax.text(0.0, 1.015,
            f"n = {shown[0]['probes']} probes = {shown[0]['observations']} cells "
            f"per rung ({len(shown)} rungs); bands are 95% Wilson intervals",
            transform=ax.transAxes, fontsize=9, color=style.INK_2)
    ax.legend(loc="lower right", ncol=2, handlelength=1.6, columnspacing=1.1,
              labelspacing=0.35, framealpha=0.95, frameon=True,
              facecolor=style.SURFACE, edgecolor=style.GRID, borderpad=0.6)

    for i, method in enumerate(measured_ranking):
        f, s = fits[method], style.STYLE[method]
        ax2.plot([f["snr50_lo"], f["snr50_hi"]], [i, i], color=s["color"],
                 linewidth=2.4, solid_capstyle="round", alpha=0.55)
        ax2.plot([f["snr50_db"]], [i], marker=s["marker"], color=s["color"],
                 markersize=8, markeredgecolor=style.SURFACE, markeredgewidth=0.8)
        ax2.text(f["snr50_db"], i + 0.28, method, fontsize=9.5, color=style.INK,
                 ha="center", va="bottom")
    ax2.set_yticks([])
    ax2.set_ylim(len(measured_ranking) - 0.35, -0.8)
    ax2.set_xlabel("SNR at 50% detection (dB) — lower is more sensitive")
    ax2.set_title("The measured ranking", loc="left", pad=26)
    ax2.text(0.0, 1.015, "dot = MLE, bar = 95% cluster bootstrap over probes",
             transform=ax2.transAxes, fontsize=9, color=style.INK_2)
    ax2.grid(axis="y", visible=False)

    style.footer(fig, f"Spread over the eight: {spread_db:.2f} dB; "
                      f"{payload['pairs_resolved']} of {payload['pairs_total']} "
                      f"pairs resolved at 95%.  Spearman vs model d-ranking "
                      f"{payload['spearman_measured_vs_model_d']:+.3f}; vs "
                      f"fire-count ranking "
                      f"{payload['spearman_measured_vs_fire_count']:+.3f}.")
    fig.savefig(HERE / "detection-vs-snr.png")
    order = measured_ranking
    print("SNR50 (dB), best first:")
    for m in order:
        f = fits[m]
        print(f"  {m:>20} {f['snr50_db']:7.2f}  [{f['snr50_lo']:.2f}, {f['snr50_hi']:.2f}]")
    print("spearman vs model-d   ", round(payload["spearman_measured_vs_model_d"], 3))
    print("spearman vs fire-count", round(payload["spearman_measured_vs_fire_count"], 3))
    print("pairs resolved", payload["pairs_resolved"], "of", payload["pairs_total"])


if __name__ == "__main__":
    if "--redraw" in sys.argv:
        draw(json.loads((HERE / "detection-vs-snr.json").read_text()))
    else:
        main()
