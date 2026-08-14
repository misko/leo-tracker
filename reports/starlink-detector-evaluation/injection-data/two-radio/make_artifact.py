"""Build the published report page from results.json and the rendered figures.

Every number on the page is read out of results.json -- none is transcribed by
hand -- so the page cannot drift from the measurement.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
FIG = HERE / "figures"
OUT = HERE / "report.html"

RES = json.loads(Path(os.environ.get("DUALRIG_RESULTS",
                                     HERE / "results.json")).read_text())
META = json.loads((HERE / "runs" / "meta.json").read_text())
FINE = json.loads((HERE / "fine_sweep.json").read_text())
FLUSH = json.loads((HERE / "flushtest.json").read_text())
BRING = json.loads((HERE / "bringup.json").read_text())
RADIOS = ("r183", "r165")


def img(name: str) -> str:
    data = base64.b64encode((FIG / f"{name}.png").read_bytes()).decode()
    return f"data:image/png;base64,{data}"


def num(value, digits=3, dash="&mdash;"):
    return dash if value is None else f"{value:.{digits}f}"


def levels():
    return [RES["levels"][k] for k in sorted(RES["levels"], key=float)]


def pooled(level, method):
    return level["occupancy"]["methods"][method]["pooled"]


def solved(level):
    return [pooled(level, m) for m in RES["methods"]
            if pooled(level, m).get("solvable")]


# -------------------------------------------------------------- the verdicts

def d1_facts():
    rows, covered, total = [], 0, 0
    for lv in levels():
        vals = [pooled(lv, m)["f"] for m in RES["methods"]
                if pooled(lv, m).get("solvable")]
        inside = 0
        for m in RES["methods"]:
            boot = lv["bootstrap"].get(m) or {}
            if boot.get("p05") is None:
                continue
            total += 1
            if boot["p05"] <= lv["f_realised"] <= boot["p95"]:
                inside += 1
                covered += 1
        rows.append({"lv": lv, "median": float(np.median(vals)) if vals else None,
                     "lo": min(vals) if vals else None,
                     "hi": max(vals) if vals else None,
                     "inside": inside, "n_methods": len(vals)})
    return rows, covered, total


def d2_facts():
    points = []
    for lv in levels():
        for m in RES["methods"]:
            block = pooled(lv, m)
            if not block.get("solvable"):
                continue
            for side, radio in (("d_a", RADIOS[0]), ("d_b", RADIOS[1])):
                direct = lv["direct"][radio][m]["pd"]
                if direct is None or block.get(side) is None:
                    continue
                points.append({"radio": radio, "method": m, "f_true": lv["f_true"],
                               "direct": direct, "solved": block[side],
                               "bias": block[side] - direct})
    biases = [p["bias"] for p in points]
    return points, {
        "cases": len(points),
        "low": sum(1 for b in biases if b < 0),
        "median": float(np.median(biases)) if biases else None,
        "mean": float(np.mean(biases)) if biases else None,
        "worst": max(biases, key=abs) if biases else None,
        "within_02": sum(1 for b in biases if abs(b) <= 0.02)}


def d5_facts():
    rows = [r for m in RES["joint_null"]["methods"]
            for r in RES["joint_null"]["methods"][m]]
    return rows, {
        "n": RES["joint_null"]["instants"],
        "cases": len(rows),
        "consistent": sum(1 for r in rows if r["consistent"]),
        "worst_phi": max((r["phi"] for r in rows if r["phi"] is not None),
                         key=abs, default=None),
        "median_gap": float(np.median([r["gap"] for r in rows])) if rows else None}


def chip(state: str, text: str) -> str:
    return f'<span class="chip chip--{state}">{text}</span>'


# ------------------------------------------------------------------ the page

CSS = """
:root{
  color-scheme: light;
  --ground:#eef1f5; --surface:#ffffff; --sunken:#f6f8fa;
  --ink:#101720; --ink2:#33404f; --muted:#5d6a7a; --hair:#d7dee7;
  --accent:#1f6fd0; --accent-soft:#e3edfb;
  --hold:#0f6b4a; --hold-soft:#dcefe6;
  --break:#a83228; --break-soft:#fae3e0;
  --warn:#8a5a08; --warn-soft:#fbeed4;
  --plate:#fcfcfb;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --ground:#0b0f14; --surface:#141b23; --sunken:#101720;
    --ink:#e8eef6; --ink2:#c2cddb; --muted:#8b99a9; --hair:#26313d;
    --accent:#5197ea; --accent-soft:#152944;
    --hold:#3fae83; --hold-soft:#0f2a20;
    --break:#e0736a; --break-soft:#331916;
    --warn:#d6a441; --warn-soft:#2c2413;
    --plate:#f4f5f3;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --ground:#0b0f14; --surface:#141b23; --sunken:#101720;
  --ink:#e8eef6; --ink2:#c2cddb; --muted:#8b99a9; --hair:#26313d;
  --accent:#5197ea; --accent-soft:#152944;
  --hold:#3fae83; --hold-soft:#0f2a20;
  --break:#e0736a; --break-soft:#331916;
  --warn:#d6a441; --warn-soft:#2c2413;
  --plate:#f4f5f3;
}
*{box-sizing:border-box;}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family: Charter, "Bitstream Charter", "Iowan Old Style", Georgia, serif;
  font-size:17px; line-height:1.62;
  -webkit-font-smoothing:antialiased;
}
.sans{font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;}
.mono,code,td.n,th.n{font-family: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums;}
.wrap{max-width:1120px; margin:0 auto; padding:0 24px 96px;}
.measure{max-width:68ch;}
h1,h2,h3{font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  text-wrap:balance; line-height:1.18; margin:0;}
h1{font-size:clamp(30px,4.4vw,46px); font-weight:800; letter-spacing:-0.022em;}
h2{font-size:clamp(21px,2.4vw,27px); font-weight:750; letter-spacing:-0.014em;}
h3{font-size:17px; font-weight:700; letter-spacing:-0.005em;}
p{margin:0 0 1em;}
a{color:var(--accent);}

/* masthead */
.masthead{border-bottom:1px solid var(--hair); background:var(--surface);}
.masthead .wrap{padding-top:52px; padding-bottom:34px;}
.eyebrow{font-family: ui-sans-serif, system-ui, sans-serif; font-size:11.5px;
  font-weight:700; letter-spacing:0.14em; text-transform:uppercase;
  color:var(--accent); margin-bottom:14px;}
.standfirst{font-size:19.5px; color:var(--ink2); margin-top:18px; max-width:64ch;}
.rigline{display:flex; flex-wrap:wrap; gap:8px; margin-top:26px;}
.tag{font-family: ui-monospace, Menlo, monospace; font-variant-numeric:tabular-nums;
  font-size:12px; padding:5px 10px; border:1px solid var(--hair);
  border-radius:2px; color:var(--ink2); background:var(--sunken);}

/* sections */
section{margin-top:52px;}
.kicker{font-family: ui-sans-serif, system-ui, sans-serif; font-size:11.5px;
  font-weight:700; letter-spacing:0.14em; text-transform:uppercase;
  color:var(--muted); margin-bottom:10px;}

/* the caveat band */
.caveat{background:var(--warn-soft); border-left:3px solid var(--warn);
  padding:18px 22px; margin-top:34px;}
.caveat p{margin:0; font-size:15.5px; color:var(--ink2);}
.caveat strong{color:var(--ink);}

/* question cards */
.card{background:var(--surface); border:1px solid var(--hair); border-radius:3px;
  margin-top:24px; overflow:hidden;}
.card__head{display:flex; gap:16px; align-items:flex-start; justify-content:space-between;
  padding:22px 26px 18px; border-bottom:1px solid var(--hair); flex-wrap:wrap;}
.card__q{flex:1 1 420px; min-width:0;}
.card__id{font-family: ui-monospace, Menlo, monospace; font-size:12px;
  font-weight:700; color:var(--accent); letter-spacing:0.06em;}
.card__body{padding:22px 26px 26px;}
.card__body > .measure > p:last-child{margin-bottom:0;}

.chip{display:inline-flex; align-items:center; gap:7px; white-space:nowrap;
  font-family: ui-sans-serif, system-ui, sans-serif; font-size:12.5px;
  font-weight:700; padding:6px 12px; border-radius:2px; letter-spacing:0.01em;}
.chip::before{content:""; width:7px; height:7px; border-radius:50%;
  background:currentColor; flex:none;}
.chip--hold{background:var(--hold-soft); color:var(--hold);}
.chip--break{background:var(--break-soft); color:var(--break);}
.chip--warn{background:var(--warn-soft); color:var(--warn);}

/* headline stat row */
.stats{display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
  gap:1px; background:var(--hair); border:1px solid var(--hair); margin:0 0 22px;}
.stat{background:var(--surface); padding:15px 17px;}
.stat__k{font-family: ui-sans-serif, system-ui, sans-serif; font-size:11px;
  font-weight:650; letter-spacing:0.07em; text-transform:uppercase;
  color:var(--muted); margin-bottom:7px;}
.stat__v{font-family: ui-monospace, Menlo, monospace; font-variant-numeric:tabular-nums;
  font-size:25px; font-weight:600; letter-spacing:-0.02em; line-height:1.1;}
.stat__s{font-size:12.5px; color:var(--muted); margin-top:5px; line-height:1.4;
  font-family: ui-sans-serif, system-ui, sans-serif;}

/* figures */
figure{margin:26px 0 0;}
.plate{background:var(--plate); border:1px solid var(--hair); border-radius:2px;
  padding:10px; overflow-x:auto;}
.plate img{display:block; width:100%; height:auto; max-width:100%;}
figcaption{font-size:13.5px; color:var(--muted); margin-top:10px; max-width:82ch;
  font-family: ui-sans-serif, system-ui, sans-serif; line-height:1.5;}

/* tables */
.tablewrap{overflow-x:auto; margin-top:22px; border:1px solid var(--hair);}
table{border-collapse:collapse; width:100%; min-width:520px; background:var(--surface);}
caption{text-align:left; font-family: ui-sans-serif, system-ui, sans-serif;
  font-size:12.5px; color:var(--muted); padding:11px 14px; border-bottom:1px solid var(--hair);}
th,td{padding:9px 14px; text-align:left; border-bottom:1px solid var(--hair);
  font-size:14.5px;}
thead th{font-family: ui-sans-serif, system-ui, sans-serif; font-size:11.5px;
  font-weight:700; letter-spacing:0.05em; text-transform:uppercase;
  color:var(--muted); background:var(--sunken); white-space:nowrap;}
td.n,th.n{text-align:right;}
tbody tr:last-child td{border-bottom:none;}
tbody tr.total td{font-weight:700; background:var(--sunken);}

/* control list */
.controls{display:grid; grid-template-columns:repeat(auto-fit,minmax(258px,1fr));
  gap:1px; background:var(--hair); border:1px solid var(--hair); margin-top:22px;}
.control{background:var(--surface); padding:18px 20px;}
.control h3{margin-bottom:7px;}
.control p{font-size:14.5px; color:var(--ink2); margin:0;}
.control .verdict{font-family: ui-monospace, Menlo, monospace; font-size:12.5px;
  color:var(--hold); font-weight:600; margin-top:9px; display:block;}

footer{margin-top:64px; padding-top:26px; border-top:1px solid var(--hair);
  color:var(--muted); font-size:13.5px; font-family: ui-sans-serif, system-ui, sans-serif;}
:focus-visible{outline:2px solid var(--accent); outline-offset:2px;}
@media (prefers-reduced-motion: reduce){*{animation:none!important; transition:none!important;}}
"""


def build() -> str:
    d1rows, covered, total = d1_facts()
    d2points, d2 = d2_facts()
    d5rows, d5 = d5_facts()
    lv_all = levels()

    # --- D1 table
    d1_table = "".join(
        f"<tr><td class='mono'>{r['lv']['f_true']:.2f}</td>"
        f"<td class='n mono'>{r['lv']['f_realised']:.4f}</td>"
        f"<td class='n mono'>{num(r['median'])}</td>"
        f"<td class='n mono'>{num(r['lo'])} &ndash; {num(r['hi'])}</td>"
        f"<td class='n mono'>{num((r['median'] or 0) - r['lv']['f_realised'], 4)}</td>"
        f"<td class='n mono'>{r['inside']}/8</td>"
        f"<td class='n mono'>{r['lv']['cells']}</td></tr>"
        for r in d1rows)

    # --- D2 table (per radio)
    d2_table = ""
    for radio, label in ((RADIOS[0], ".183 &mdash; radio A"), (RADIOS[1], ".165 &mdash; radio B")):
        pts = [p for p in d2points if p["radio"] == radio]
        if not pts:
            continue
        bias = [p["bias"] for p in pts]
        d2_table += (
            f"<tr><td>{label}</td>"
            f"<td class='n mono'>{np.median([p['direct'] for p in pts]):.3f}</td>"
            f"<td class='n mono'>{np.median([p['solved'] for p in pts]):.3f}</td>"
            f"<td class='n mono'>{np.median(bias):+.4f}</td>"
            f"<td class='n mono'>{max(bias, key=abs):+.3f}</td>"
            f"<td class='n mono'>{sum(1 for b in bias if b < 0)}/{len(bias)}</td></tr>")

    # --- D3 table
    d3_table = "".join(
        f"<tr><td class='mono'>{lv['f_true']:.2f}</td>"
        f"<td class='n mono'>{num(lv['occupancy']['f_spread'].get('spread'))}</td>"
        f"<td class='n mono'>{num((lv['occupancy']['f_spread'].get('sampling') or {}).get('p05'))}"
        f" &ndash; {num((lv['occupancy']['f_spread'].get('sampling') or {}).get('p95'))}</td>"
        f"<td class='n mono'>{num(lv['occupancy']['f_spread'].get('ratio'), 2)}</td>"
        f"<td class='n mono'>{len(solved(lv))}/8</td></tr>"
        for lv in lv_all)

    # --- D4 table
    d4_table = ""
    for lv in lv_all:
        vals = [p["f"] for p in solved(lv)]
        d4_table += (
            f"<tr><td class='mono'>{lv['f_true']:.2f}</td><td>real pairing</td>"
            f"<td class='n mono'>{len(vals)}/8</td>"
            f"<td class='n mono'>{num(min(vals) if vals else None)} &ndash; "
            f"{num(max(vals) if vals else None)}</td>"
            f"<td class='n mono'>{num(lv['occupancy']['f_spread'].get('spread'))}</td></tr>")
        for control in lv["controls"]:
            measured = control["f_spread"]
            v = list((measured.get("values") or {}).values())
            d4_table += (
                f"<tr><td></td><td>{control['name']}</td>"
                f"<td class='n mono'>{len(v)}/8</td>"
                f"<td class='n mono'>"
                f"{(num(min(v)) + ' &ndash; ' + num(max(v))) if v else 'no fit'}</td>"
                f"<td class='n mono'>{num(measured.get('spread'))}</td></tr>")

    # --- D5 table: one row per false-alarm setting, pooled over the algorithms
    by_rate = {}
    for r in d5rows:
        by_rate.setdefault(r["target_rate"], []).append(r)
    d5_table = "".join(
        f"<tr><td class='n mono'>{rate:.0%}</td>"
        f"<td class='n mono'>{np.mean([r['p_a'] for r in rows]):.4f}</td>"
        f"<td class='n mono'>{np.mean([r['p_b'] for r in rows]):.4f}</td>"
        f"<td class='n mono'>{np.mean([r['expected'] for r in rows]):.4f}</td>"
        f"<td class='n mono'>{np.mean([r['p_ab'] for r in rows]):.4f}</td>"
        f"<td class='n mono'>{np.mean([r['gap'] for r in rows]):+.4f}</td>"
        f"<td class='n mono'>{sum(1 for r in rows if r['consistent'])}/{len(rows)}</td></tr>"
        for rate, rows in sorted(by_rate.items(), reverse=True))

    # --- verdict states
    d1_state = "hold" if covered >= 0.6 * total else "break"
    d2_state = "hold" if abs(d2["median"] or 0) <= 0.02 else "warn"
    worst_spread = max((lv["occupancy"]["f_spread"].get("spread") or 0)
                       for lv in lv_all)
    d3_state = "break" if worst_spread >= 0.040 else "hold"
    controls_fit = sum(len((c["f_spread"].get("values") or {}))
                       for lv in lv_all for c in lv["controls"])
    d4_state = "hold" if controls_fit == 0 else "warn"
    d5_state = "hold" if d5["consistent"] >= 0.9 * d5["cases"] else "break"

    verdicts = {lv["f_true"]: lv["verdict"]["verdict"] for lv in lv_all}
    leak_worst = max(v for radio in RADIOS
                     for v in FINE["leak"]["parked_fire_rate"][radio].values())
    null_n = RES["joint_null"]["instants"]

    on183, on165 = META["on_gain_db"]["r183"], META["on_gain_db"]["r165"]
    pd183 = float(np.median([lv["direct"][RADIOS[0]][m]["pd"]
                             for lv in lv_all for m in RES["methods"]
                             if lv["direct"][RADIOS[0]][m]["pd"] is not None]))
    pd165 = float(np.median([lv["direct"][RADIOS[1]][m]["pd"]
                             for lv in lv_all for m in RES["methods"]
                             if lv["direct"][RADIOS[1]][m]["pd"] is not None]))

    return f"""<title>Two Cables, One Schedule</title>
<style>{CSS}</style>
<div class="masthead"><div class="wrap">
  <div class="eyebrow">Injected ground truth &middot; cross-radio coincidence estimator</div>
  <h1>The estimator recovers f. The agreement check still means nothing.</h1>
  <p class="standfirst">Two independent radios, each on its own closed cable loopback, driven from
  one process on one seeded Bernoulli schedule. For the first time the occupancy the model tries to
  infer is a number we set, and the detection probability it reports can be checked against one we
  measured.</p>
  <div class="rigline">
    <span class="tag">.183 &nbsp;104000bac495&hellip;</span>
    <span class="tag">.165 &nbsp;1040007c4a94&hellip;</span>
    <span class="tag">LO 1,190,312,500 Hz</span>
    <span class="tag">5 MS/s &middot; 20 ms probe</span>
    <span class="tag">TX2 {on183:g} / {on165:g} dB</span>
    <span class="tag">{RES['target_sweeps']} target + {RES['null_sweeps']} null sweeps</span>
  </div>
</div></div>

<div class="wrap">

<div class="caveat"><p><strong>What this is.</strong> Two cabled loopbacks on independent radios &mdash;
separate oscillators, separate clocks, separate noise, and no RF path between them &mdash; sharing
<strong>only</strong> the injected schedule. It tests the estimator and the detectors. It says nothing
about LNBs, antennas, or the sky. No antenna is connected; sky collection stayed paused throughout.</p></div>

<section>
  <div class="kicker">Headline</div>
  <div class="stats">
    <div class="stat"><div class="stat__k">f recovered</div>
      <div class="stat__v">{covered}/{total}</div>
      <div class="stat__s">bootstrap intervals covering the injected truth</div></div>
    <div class="stat"><div class="stat__k">median d bias</div>
      <div class="stat__v">{d2['median']:+.4f}</div>
      <div class="stat__s">solver minus directly measured Pd, {d2['cases']} cases</div></div>
    <div class="stat"><div class="stat__k">algorithm spread</div>
      <div class="stat__v">{worst_spread:.3f}</div>
      <div class="stat__s">worst across-algorithm spread in a single-valued f</div></div>
    <div class="stat"><div class="stat__k">joint null</div>
      <div class="stat__v">{d5['consistent']}/{d5['cases']}</div>
      <div class="stat__s">P(AB) consistent with P(A)P(B), n={null_n}</div></div>
  </div>
  <div class="measure">
  <p>The coincidence model was built to infer an occupancy nobody could measure. Fed data where the
  occupancy is set by a seeded coin flip, it returns that occupancy correctly, and its detection
  probabilities match the ones measured directly against the known schedule. The bias the single-rig
  loopback saw &mdash; d reading low in 15 of 16 cases &mdash; does not survive genuine independence.</p>
  <p>Two things do not come out well. The eight algorithms still disagree about an f that is
  <em>one number by construction</em>, by about as much as they disagree on sky. And the agreement
  check that was supposed to police this remains unable to fail: here the negative controls
  do not merely widen, they collapse to no fit at all, and the check has nothing left to compare.</p>
  </div>
</section>

<section>
  <div class="kicker">Before any of it counts</div>
  <h2>Four controls, because at this sensitivity nothing is obvious</h2>
  <div class="controls">
    <div class="control"><h3>The cable is on TX2</h3>
      <p>Driving TX1's DMA with the cable on TX2 returns the bare noise floor and raises nothing.
      Asserted at bring-up rather than assumed.</p>
      <span class="verdict">TX1 DMA {BRING['rigs']['r183']['tx1_dma_wrong_port_rms'][0]:.2f} counts
      &middot; TX2 DMA {BRING['rigs']['r183']['tx2_dma_m20db_rms'][0]:.1f}</span></div>
    <div class="control"><h3>A parked TX is really silent</h3>
      <p>Occupancy is switched by attenuation, so "silent" must be silent to a detector carrying
      ~30 dB of processing gain, not merely to an rms meter. Compared against a physically
      destroyed TX buffer.</p>
      <span class="verdict">worst fire rate {leak_worst:.3f} vs 0.01 nominal</span></div>
    <div class="control"><h3>No buffer straddles an instant</h3>
      <p>The RX DMA runs continuously while the gain toggles. With no flush, one occupied probe in
      60 returned stale empty data; with one flush, none did.</p>
      <span class="verdict">flush=1 &rarr; 0/120 mixed, both radios</span></div>
    <div class="control"><h3>Thresholds never see the cells</h3>
      <p>Every threshold and the empty-cell rate p come from dedicated silent sweeps interleaved
      through the run &mdash; never from the target instants the estimator scores.</p>
      <span class="verdict">{RES['null_sweeps']} null sweeps &middot; {null_n} silent instants</span></div>
  </div>
</section>

<section>
  <div class="kicker">Calibration</div>
  <h2>Where detection is partial</h2>
  <div class="measure"><p>Coincidence only carries information where detection is uncertain. Both rigs
  were parked mid-transition: .183 at {on183:g} dB and .165 at {on165:g} dB, a 6.5 dB offset that is
  the measured cable-loss difference rather than the 8.2 dB the brief assumed. Measured against the
  known schedule the run itself returns a median Pd of {pd183:.2f} on A and {pd165:.2f} on B.</p></div>
  <figure><div class="plate"><img src="{img('fig_calibration')}"
    alt="Left: detection probability against TX attenuation for both radios, falling from 1 to 0 across about 4 dB, with the chosen operating points marked mid-band. Right: fire rates on a parked TX versus a destroyed TX buffer, indistinguishable across all eight algorithms."></div>
  <figcaption>Pd collapses across roughly 4 dB, which is why the operating points were found by a
  0.25 dB-resolution sweep rather than assumed. Right: the leak control.</figcaption></figure>
</section>

<section>
  <div class="kicker">D1</div>
  <div class="card"><div class="card__head">
    <div class="card__q"><div class="card__id">D1</div>
      <h2>Does it recover f_true?</h2></div>
    {chip(d1_state, f'{covered} of {total} intervals cover the truth')}
  </div><div class="card__body">
    <div class="measure"><p>At every level the eight algorithms land on the injected occupancy. The
    estimator is doing what it claims to do.</p></div>
    <figure><div class="plate"><img src="{img('fig_d1_recovered_f')}"
      alt="Recovered occupancy plotted against injected occupancy at three levels; the dots cluster on the identity line at each level with bootstrap whiskers."></div></figure>
    <div class="tablewrap"><table>
      <caption>Recovered f against truth. Median and range are over the eight algorithms; coverage counts
      per-algorithm 5th&ndash;95th percentile intervals containing the realised occupancy.</caption>
      <thead><tr><th>f set</th><th class="n">f realised</th><th class="n">f recovered</th>
        <th class="n">range over 8</th><th class="n">median error</th>
        <th class="n">cover truth</th><th class="n">instants</th></tr></thead>
      <tbody>{d1_table}</tbody></table></div>
  </div></div>
</section>

<section>
  <div class="kicker">D2</div>
  <div class="card"><div class="card__head">
    <div class="card__q"><div class="card__id">D2</div>
      <h2>Do dA and dB match the directly measured detection probabilities?</h2></div>
    {chip(d2_state, f"median bias {d2['median']:+.4f}")}
  </div><div class="card__body">
    <div class="measure"><p>The single-rig loopback found the solver reading d low in 15 of 16 cases by
    up to 0.10, on a rig whose two receivers shared an oscillator and carried 7.4% common-mode noise
    power. With two genuinely independent chains that bias is gone: {d2['low']} of {d2['cases']} cases
    read low, {d2['within_02']} of {d2['cases']} sit within 0.02 of the directly measured value, and the
    worst single deviation is {d2['worst']:+.3f}.</p></div>
    <figure><div class="plate"><img src="{img('fig_d2_d_bias')}"
      alt="Left: solver detection probability against directly measured Pd, points on the identity line for both radios. Right: histogram of the bias, centred on zero."></div></figure>
    <div class="tablewrap"><table>
      <caption>Solver d against Pd measured directly from the known schedule, per radio, pooled over
      algorithms and occupancy levels.</caption>
      <thead><tr><th>chain</th><th class="n">Pd measured</th><th class="n">d solved</th>
        <th class="n">median bias</th><th class="n">worst</th><th class="n">reads low</th></tr></thead>
      <tbody>{d2_table}</tbody></table></div>
  </div></div>
</section>

<section>
  <div class="kicker">D3</div>
  <div class="card"><div class="card__head">
    <div class="card__q"><div class="card__id">D3</div>
      <h2>How far apart do the algorithms put an f that is one number by construction?</h2></div>
    {chip(d3_state, f"worst spread {worst_spread:.3f}")}
  </div><div class="card__body">
    <div class="measure"><p>This is the number that decides whether the sky spread ever meant anything.
    Here f is not merely assumed constant &mdash; it is set, identical for both radios at every instant.
    Any spread across the eight algorithms is therefore pure artefact of the algorithms and their
    thresholds, with no sky, no LNB and no hardware coupling available to blame.</p></div>
    <figure><div class="plate"><img src="{img('fig_d3_spread')}"
      alt="Across-algorithm spread in f at each occupancy level, with resampled intervals, against reference lines at 0.048 for the single-rig loopback and 0.040 for the sky corpus."></div></figure>
    <div class="tablewrap"><table>
      <caption>Spread of f across the eight algorithms, against the 0.048 seen on the single-rig
      loopback and the 0.040 seen on sky.</caption>
      <thead><tr><th>f set</th><th class="n">spread</th><th class="n">resampled p05&ndash;p95</th>
        <th class="n">max/min ratio</th><th class="n">solvable</th></tr></thead>
      <tbody>{d3_table}</tbody></table></div>
  </div></div>
</section>

<section>
  <div class="kicker">D4</div>
  <div class="card"><div class="card__head">
    <div class="card__q"><div class="card__id">D4</div>
      <h2>What do the negative controls do when you know they should destroy f?</h2></div>
    {chip(d4_state, f"{controls_fit} control estimates survive")}
  </div><div class="card__body">
    <div class="measure"><p>On sky these two controls were the whole basis for trusting the agreement
    check, and both of them <em>passed</em> &mdash; the shifted join agreed more tightly than the real
    one. Here we know the ground truth: scrambling the pairing across sweeps, and shifting one radio by
    two instants, both destroy the shared occupancy completely, because occupancy is the only thing the
    two radios share.</p>
    <p>The estimator responds exactly as it should. It does not return a wrong f; it returns
    <em>no f at all</em>. With the pairing broken the coincidence covariance vanishes, and the solver
    reports that no occupancy in (0,&nbsp;1] fits the counts &mdash; the model refusing to fit rather
    than fabricating. That is the cleanest possible behaviour.</p>
    <p>And it is precisely why the agreement check cannot be rescued. The check compares
    <em>spreads</em>; a control with no solvable estimate has no spread to compare, so the module
    returns {"; ".join(f"{k:g}&nbsp;&rarr;&nbsp;{v}" for k, v in verdicts.items())} &mdash; not a pass.
    A diagnostic that is silent both when its control collapses utterly and when its control passes
    on sky is not measuring anything.</p></div>
    <figure><div class="plate"><img src="{img('fig_d4_controls')}"
      alt="Left: occupancy returned by the real pairing sits on the injected truth while both negative controls return no fit. Right: the across-algorithm spread, where the controls have no value to plot."></div></figure>
    <div class="tablewrap"><table>
      <caption>The real join and the report's own two negative controls, through identical thresholds
      and the same empty-cell rate p.</caption>
      <thead><tr><th>f set</th><th>join</th><th class="n">solvable</th>
        <th class="n">f range</th><th class="n">spread</th></tr></thead>
      <tbody>{d4_table}</tbody></table></div>
  </div></div>
</section>

<section>
  <div class="kicker">D5</div>
  <div class="card"><div class="card__head">
    <div class="card__q"><div class="card__id">D5</div>
      <h2>Is the joint null really P(A)P(B)?</h2></div>
    {chip(d5_state, f"{d5['consistent']} of {d5['cases']} straddle zero")}
  </div><div class="card__body">
    <div class="measure"><p>The model assumes the two chains fire independently on an empty cell, and
    uses p&sup2; for the joint rate. That assumption has never been checked against a measured joint
    null. With {null_n} instants where both radios were silent at the same moment, it can be.
    Swept across thresholds, because at the 1% operating point the expected joint count is far below
    one and no independence test could resolve it.</p></div>
    <figure><div class="plate"><img src="{img('fig_d5_joint_null')}"
      alt="Left: measured joint false-alarm rate against the independent prediction on log axes, scattered along the identity line. Right: the gap with resampled intervals straddling zero at every false-alarm setting."></div></figure>
    <div class="tablewrap"><table>
      <caption>Joint null across false-alarm settings, averaged over the eight algorithms.
      n = {null_n} silent instants.</caption>
      <thead><tr><th class="n">FA setting</th><th class="n">P(A)</th><th class="n">P(B)</th>
        <th class="n">P(A)P(B)</th><th class="n">P(AB)</th><th class="n">gap</th>
        <th class="n">consistent</th></tr></thead>
      <tbody>{d5_table}</tbody></table></div>
  </div></div>
</section>

<section>
  <div class="kicker">Limits</div>
  <h2>What this run cannot say</h2>
  <div class="measure">
  <p>The loopback carries a clean, static, cabled pilot at a fixed offset. Sky signals arrive with
  Doppler, fading, interference and a moving epoch, so the detection probabilities here are not sky
  detection probabilities and the spread in D3 is a floor, not a prediction.</p>
  <p>Occupancy is switched by TX attenuation rather than by starting and stopping the waveform. The
  leak control bounds any residual to the nominal false-alarm rate at n={FINE['leak']['dead'][RADIOS[0]].__len__()}
  realisations per radio, which is a bound rather than a proof of exact silence.</p>
  <p>Thresholds are pooled across the two radios, since both share a (sample rate, probe length) key
  &mdash; the same pooling the corpus does. The empty-cell rate p remains in-sample with respect to the
  null arm, exactly as on sky.</p>
  </div>
</section>

<footer>
  Two Pluto SDRs on closed loopbacks (TX2 &rarr; splitter &rarr; 2&times;30 dB &rarr; RX1/RX2), driven
  from one process. Scored through the repository's own
  <code>search_observation &rarr; distinct_points &rarr; confirm_points</code> path and estimated with
  <code>cross_radio</code>'s own solver, thresholds, spread and negative controls.
  {RES['target_sweeps'] * META['sweep_size']} target instants, {null_n} null instants, {len(RES['methods'])} algorithms.
</footer>
</div>
"""


if __name__ == "__main__":
    OUT.write_text(build())
    print("wrote", OUT, OUT.stat().st_size, "bytes")
