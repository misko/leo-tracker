"""Build the standalone results page, with every figure embedded as a data URI."""
from __future__ import annotations

import base64
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"
OUT = HERE / "injection-results.html"

S = json.loads((HERE / "summary.json").read_text())


def img(name: str) -> str:
    data = base64.b64encode((FIG / name).read_bytes()).decode()
    return f"data:image/png;base64,{data}"


def fig_block(name: str, caption: str) -> str:
    return (f'<figure class="plate">\n'
            f'  <div class="plate-scroll"><img src="{img(name)}" alt="{caption}"></div>\n'
            f'  <figcaption>{caption}</figcaption>\n</figure>')


e2, e3, e4, e5 = S["E2_roc"], S["E3_false_alarm"], S["E4_offset"], S["E5_coincidence"]
e1 = S["E1_smoke"]

knee = [p for p in e4["passes"] if p["snr_db"] < 0][0]
knee_cuts = [v for v in knee["collapse_hz_at_pd_50"].values() if v]
xe_lo, xe_hi = (min(e3["per_cell_at_conditioned_cross_edge_threshold"].values()),
                max(e3["per_cell_at_conditioned_cross_edge_threshold"].values()))
na_lo, na_hi = (min(e3["per_cell_at_null_arm_threshold"].values()),
                max(e3["per_cell_at_null_arm_threshold"].values()))

RANK_ROWS = "".join(
    f"<tr><td class='rk'>{i+1}</td><td class='mono'>{m}</td>"
    f"<td class='num'>{e2['snr50_db'][m]:.2f}</td>"
    f"<td class='num dim'>{e2['snr50_ci'][m][0]:.2f}, {e2['snr50_ci'][m][1]:.2f}</td>"
    f"<td class='num'>{S['E5_coincidence']['f_recovered'][m]:.3f}</td></tr>"
    for i, m in enumerate(e2["measured_ranking_best_first"]))

HTML = f"""<title>Ground Truth on the Bench</title>
<style>
  :root {{
    --ground:#f5f7f8; --surface:#ffffff; --sunk:#eef1f3;
    --ink:#10151b; --ink-2:#525d69; --ink-3:#7d8894;
    --rule:#dde2e6; --rule-2:#c8d0d7;
    --accent:#2a78d6; --accent-soft:#e8f0fb;
    --good:#0f7a53; --good-soft:#e3f3ec;
    --warn:#b0521a; --warn-soft:#fbeee3;
    --alert:#bd382e; --alert-soft:#fbe9e7;
    --display:ui-serif,Georgia,"Times New Roman",serif;
    --body:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    --data:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ground:#0e1216; --surface:#151a20; --sunk:#1b222a;
      --ink:#e9edf1; --ink-2:#9aa5b1; --ink-3:#77828e;
      --rule:#242c35; --rule-2:#333d48;
      --accent:#5b9bea; --accent-soft:#16283f;
      --good:#3fae83; --good-soft:#12291f;
      --warn:#d08a4f; --warn-soft:#2b1f14;
      --alert:#e2776c; --alert-soft:#2d1815;
    }}
  }}
  :root[data-theme="dark"] {{
    --ground:#0e1216; --surface:#151a20; --sunk:#1b222a;
    --ink:#e9edf1; --ink-2:#9aa5b1; --ink-3:#77828e;
    --rule:#242c35; --rule-2:#333d48;
    --accent:#5b9bea; --accent-soft:#16283f;
    --good:#3fae83; --good-soft:#12291f;
    --warn:#d08a4f; --warn-soft:#2b1f14;
    --alert:#e2776c; --alert-soft:#2d1815;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--ground); color:var(--ink);
    font-family:var(--body); font-size:16px; line-height:1.62;
    -webkit-font-smoothing:antialiased;
  }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:0 28px 96px; }}
  h1,h2,h3 {{ font-family:var(--display); font-weight:600; text-wrap:balance; }}
  .mono {{ font-family:var(--data); font-size:.9em; }}
  .num {{ font-family:var(--data); font-variant-numeric:tabular-nums; }}
  .dim {{ color:var(--ink-2); }}

  header.masthead {{ padding:64px 0 34px; border-bottom:2px solid var(--ink); }}
  .kicker {{ font-family:var(--data); font-size:11.5px; letter-spacing:.14em;
    text-transform:uppercase; color:var(--accent); margin:0 0 18px; }}
  h1 {{ font-size:clamp(34px,5vw,52px); line-height:1.08; margin:0 0 20px;
    letter-spacing:-.015em; }}
  .standfirst {{ font-size:19px; line-height:1.55; color:var(--ink-2);
    max-width:66ch; margin:0 0 26px; }}
  .caveat {{ display:flex; gap:12px; align-items:flex-start; padding:14px 16px;
    background:var(--warn-soft); border-left:3px solid var(--warn);
    color:var(--ink); font-size:14.5px; line-height:1.5; max-width:74ch; }}
  .caveat strong {{ color:var(--warn); }}

  .runmeta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
    gap:1px; background:var(--rule); border:1px solid var(--rule);
    margin:30px 0 0; }}
  .runmeta div {{ background:var(--surface); padding:12px 14px; }}
  .runmeta dt {{ font-family:var(--data); font-size:10.5px; letter-spacing:.1em;
    text-transform:uppercase; color:var(--ink-3); margin:0 0 5px; }}
  .runmeta dd {{ margin:0; font-family:var(--data); font-size:13.5px;
    font-variant-numeric:tabular-nums; }}

  section.exp {{ display:grid; grid-template-columns:64px 1fr; gap:0 22px;
    padding:52px 0 8px; border-bottom:1px solid var(--rule); }}
  .spine {{ position:relative; }}
  .eid {{ font-family:var(--data); font-size:13px; font-weight:600;
    color:var(--surface); background:var(--ink); width:44px; height:44px;
    display:flex; align-items:center; justify-content:center; }}
  .spine .tick {{ position:absolute; left:21px; top:52px; bottom:-8px;
    width:1px; background:var(--rule-2); }}
  section.exp:last-of-type .tick {{ display:none; }}

  .chip {{ display:inline-block; font-family:var(--data); font-size:11px;
    letter-spacing:.1em; text-transform:uppercase; padding:4px 9px;
    margin:0 0 12px; }}
  .chip.ok {{ background:var(--good-soft); color:var(--good); }}
  .chip.new {{ background:var(--accent-soft); color:var(--accent); }}
  .chip.bad {{ background:var(--alert-soft); color:var(--alert); }}
  section.exp h2 {{ font-size:27px; line-height:1.2; margin:0 0 14px;
    letter-spacing:-.01em; }}
  section.exp p {{ max-width:68ch; margin:0 0 15px; }}
  section.exp p.lede {{ font-size:17px; color:var(--ink); }}

  .readout {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(184px,1fr));
    gap:1px; background:var(--rule); border:1px solid var(--rule);
    margin:22px 0 26px; }}
  .readout div {{ background:var(--surface); padding:15px 16px; }}
  .readout .k {{ font-family:var(--data); font-size:10.5px; letter-spacing:.09em;
    text-transform:uppercase; color:var(--ink-3); margin:0 0 7px; }}
  .readout .v {{ font-family:var(--data); font-size:23px; font-weight:600;
    font-variant-numeric:tabular-nums; letter-spacing:-.02em; line-height:1.15; }}
  .readout .v.pos {{ color:var(--good); }}
  .readout .v.neg {{ color:var(--alert); }}
  .readout .s {{ font-size:12.5px; color:var(--ink-2); margin-top:5px;
    line-height:1.4; }}

  .plate {{ margin:26px 0 10px; }}
  .plate-scroll {{ overflow-x:auto; border:1px solid var(--rule);
    background:#fcfcfb; }}
  .plate img {{ display:block; width:100%; min-width:880px; height:auto; }}
  .plate figcaption {{ font-size:13px; color:var(--ink-2); margin-top:10px;
    max-width:74ch; line-height:1.5; }}

  table.rank {{ border-collapse:collapse; width:100%; max-width:600px;
    margin:6px 0 24px; font-size:14px; }}
  table.rank th {{ font-family:var(--data); font-size:10.5px; letter-spacing:.08em;
    text-transform:uppercase; color:var(--ink-3); text-align:left;
    padding:8px 10px; border-bottom:1px solid var(--rule-2); font-weight:500; }}
  table.rank td {{ padding:7px 10px; border-bottom:1px solid var(--rule); }}
  table.rank td.num, table.rank th.num {{ text-align:right;
    font-variant-numeric:tabular-nums; }}
  table.rank td.rk {{ font-family:var(--data); color:var(--ink-3); width:34px; }}

  .files {{ margin-top:52px; padding-top:28px; border-top:2px solid var(--ink); }}
  .files h2 {{ font-size:20px; margin:0 0 14px; }}
  .files ul {{ list-style:none; padding:0; margin:0; columns:2; column-gap:32px; }}
  .files li {{ font-family:var(--data); font-size:12.5px; color:var(--ink-2);
    padding:3px 0; break-inside:avoid; }}
  .path {{ font-family:var(--data); font-size:12.5px; color:var(--ink-3);
    word-break:break-all; margin-top:14px; }}
  @media (max-width:720px) {{
    section.exp {{ grid-template-columns:1fr; gap:0; }}
    .spine {{ display:flex; align-items:center; gap:12px; margin-bottom:14px; }}
    .spine .tick {{ display:none; }}
    .files ul {{ columns:1; }}
  }}
</style>

<div class="wrap">
<header class="masthead">
  <p class="kicker">Cabled loopback · PlutoSDR 192.168.1.183 · 14 Aug 2026</p>
  <h1>The detectors, measured against a signal we put there</h1>
  <p class="standfirst">This project has never had ground truth: detection probability was
  inferred from a coincidence model, never measured. Injecting the repository's own pilot
  waveform through a closed cable at a known power settles four questions and reopens one.</p>
  <p class="caveat"><strong>Read this first.</strong> Everything below is a
  <strong>cabled loopback</strong> — TX2 → SMA tee → 2×30&nbsp;dB → RX1, RX2, no antenna.
  It measures the eight detectors and the digital pipeline. It says nothing about the LNBs,
  the dish, or real sky, and the capture radios were never touched.</p>
  <dl class="runmeta">
    <div><dt>Waveform</dt><dd>edge_pilot_frame</dd></div>
    <div><dt>Sample rate</dt><dd>5 MS/s</dd></div>
    <div><dt>Probe</dt><dd>20 ms · 15 frames</dd></div>
    <div><dt>Scored cells</dt><dd>8,060</dd></div>
    <div><dt>Detectors</dt><dd>8</dd></div>
    <div><dt>Scoring path</dt><dd>repository's own</dd></div>
  </dl>
</header>

<section class="exp">
  <div class="spine"><div class="eid">E1</div><div class="tick"></div></div>
  <div>
    <span class="chip ok">Reproduced</span>
    <h2>The rig matches the bench notes</h2>
    <p class="lede">Before anything is believed, the injected pilot has to be demonstrably
    arriving and the receiver demonstrably far from saturation.</p>
    <div class="readout">
      <div><p class="k">Correlation peak/median</p><p class="v pos">{e1['measured_peak_to_median']:.1f}</p>
        <p class="s">bench note 60.3; stop-work floor was 8</p></div>
      <div><p class="k">Receiver rms</p><p class="v">{e1['measured_rms_counts']:.1f}</p>
        <p class="s">bench note 83.6 counts</p></div>
      <div><p class="k">Transmitter off</p><p class="v">{e1['transmitter_off_peak_to_median']:.1f}</p>
        <p class="s">the same statistic with no signal</p></div>
      <div><p class="k">Peak headroom</p><p class="v pos">17 dB</p>
        <p class="s">{e1['highest_rx_peak_counts']:.0f} counts against a 1500 ceiling</p></div>
    </div>
    <p>The bench notes fixed every analog setting but not the digital drive amplitude, and at
    full scale the rig came out 9.4&nbsp;dB hot. Fixing the drive at 0.305 of full scale put the
    received level on the noted 83.6 counts. The correlation statistic — the one that actually
    says the pilot is there — reproduced to within 3%.</p>
    {fig_block("smoke-test.png",
               "E1. Left: correlation peak-to-median against transmitter gain, RX1 and RX2. "
               "Right: receiver level against the 12-bit rail; the whole ladder stays a "
               "factor of seven below the safety ceiling.")}
  </div>
</section>

<section class="exp">
  <div class="spine"><div class="eid">E2</div><div class="tick"></div></div>
  <div>
    <span class="chip bad">Overturns both published rankings</span>
    <h2>Detection probability against measured SNR</h2>
    <p class="lede">Eighteen power rungs, 160 cells each, every probe scored by all eight
    detectors through the repository's own path, each held at a threshold calibrated on a
    genuinely empty channel.</p>
    <div class="readout">
      <div><p class="k">Spread over the eight</p><p class="v">{e2['spread_db']:.2f} dB</p>
        <p class="s">SNR at 50% detection, best to worst</p></div>
      <div><p class="k">Pairs resolved</p><p class="v">{e2['pairs_resolved']}/{e2['pairs_total']}</p>
        <p class="s">95% paired bootstrap — a real order</p></div>
      <div><p class="k">vs model d-ranking</p><p class="v neg">{e2['spearman_vs_model_d']:+.3f}</p>
        <p class="s">Spearman — no relationship</p></div>
      <div><p class="k">vs fire-count ranking</p><p class="v neg">{e2['spearman_vs_fire_count']:+.3f}</p>
        <p class="s">Spearman — no relationship</p></div>
    </div>
    <p>The eight are close but not interchangeable: 21 of 28 pairwise differences are resolved
    at 95%, so there genuinely is an order. It is not either of the two orders in circulation.
    The coincidence model ranks <span class="mono">glrt-32</span> best and the three
    full-frame variants last; measurement agrees about <span class="mono">glrt-32</span> and puts
    the full-frame variants <em>second, third and fourth</em>. The two adjacent-differential
    detectors, which the model ranks mid-field, measure worst — and
    <span class="mono">differential-16</span> is significantly worse than all seven others.</p>
    <table class="rank">
      <thead><tr><th>#</th><th>Detector</th><th class="num">SNR&#8325;&#8320; dB</th>
      <th class="num">95% CI</th><th class="num">f (E5)</th></tr></thead>
      <tbody>{RANK_ROWS}</tbody>
    </table>
    {fig_block("detection-vs-snr.png",
               "E2. Detection probability per cell against SNR measured by coherent "
               "projection onto the known transmitted stream — never assumed from the gain "
               "setting. Curves are binomial-MLE fits with the empty-channel rate as a floor.")}
    {fig_block("detector-ranking.png",
               "E2. The same eight detectors in three orders. Only the left column is a "
               "measurement against a known input. The middle and right columns are almost "
               "exact reverses of each other; neither predicts the left.")}
  </div>
</section>

<section class="exp">
  <div class="spine"><div class="eid">E3</div><div class="tick"></div></div>
  <div>
    <span class="chip ok">Confirms the report's null</span>
    <span class="chip bad">Flags a second one</span>
    <h2>False alarms on a genuinely empty channel</h2>
    <p class="lede">The transmitter at its minimum, everything else identical. Unlike the
    cross-edge null used so far, this channel is not merely target-code-free — there is nothing
    in it at all, and a dark arm with the DAC buffer cancelled entirely confirms that.</p>
    <div class="readout">
      <div><p class="k">Realised per point</p><p class="v pos">{e3['per_point_rate_range'][0]*100:.2f}–{e3['per_point_rate_range'][1]*100:.2f}%</p>
        <p class="s">against a requested 1%, held out</p></div>
      <div><p class="k">Realised per cell</p><p class="v">{e3['per_cell_rate_range'][0]*100:.1f}–{e3['per_cell_rate_range'][1]*100:.1f}%</p>
        <p class="s">{e3['points_per_cell']:.1f} points per cell</p></div>
      <div><p class="k">Sky null, for comparison</p><p class="v">{e3['sky_per_cell_range'][0]*100:.2f}–{e3['sky_per_cell_range'][1]*100:.2f}%</p>
        <p class="s">measured on sky, per cell</p></div>
      <div><p class="k">Conditioned cross-edge</p><p class="v neg">{xe_lo*100:.0f}–{xe_hi*100:.0f}%</p>
        <p class="s">same empty channel, other null</p></div>
    </div>
    <p>Calibrated on truly empty input the thresholds behave exactly as advertised: 0.6–1.1% per
    point, and 4.0–7.7% per cell once the roughly seven candidate points per cell are maximised
    over. That per-cell band brackets the 5.47–6.74% the report measures on sky, which means
    <strong>the on-sky null rate is fully explained by candidate multiplicity and needs no
    residual sky energy to account for it.</strong></p>
    <p>But the repository builds <em>two</em> different cross-edge nulls, and they do not behave
    alike. <span class="mono">cross_radio.null_thresholds</span> — the one the published
    <span class="mono">f</span>, <span class="mono">d</span> and 5.47–6.74% figures rest on —
    runs the opposite edge as its own target with its own bank and its own candidate points. On
    an empty channel it lands within {na_lo*100:.0f}–{na_hi*100:.0f}% per cell, right on the
    truth. It is sound. The other one,
    <span class="mono">survey_comparison.conditioned_comparison</span>, thresholds on the
    opposite-edge template evaluated at points the <em>target-edge</em> detectors selected — an
    unselected draw compared against a maximised one. Its thresholds run as low as half the
    honest value, and on this empty cable they fire on {xe_lo*100:.0f}–{xe_hi*100:.0f}% of cells
    for five of the eight detectors.</p>
    {fig_block("false-alarm-empty-channel.png",
               "E3. Left: per-point and per-cell false-alarm rates on empty input, against the "
               "nominal 1% and the sky-measured band. Right: the same probes and the same "
               "detectors, judged by thresholds from three different null populations.")}
  </div>
</section>

<section class="exp">
  <div class="spine"><div class="eid">E4</div><div class="tick"></div></div>
  <div>
    <span class="chip new">Settled</span>
    <h2>The 350–400 kHz cliff is not in the pipeline</h2>
    <p class="lede">On sky, detection collapses between 350 and 400 kHz of bias-corrected
    offset at every sample rate, and it was unknown whether that was the search span or
    something physical. Here the offset is multiplied onto the waveform before transmission, so
    it is imposed rather than estimated.</p>
    <div class="readout">
      <div><p class="k">Pd through 350–400 kHz</p><p class="v pos">1.00</p>
        <p class="s">all eight, far above threshold</p></div>
      <div><p class="k">At the detection knee</p><p class="v pos">0.80–0.83</p>
        <p class="s">same as its neighbours — no step</p></div>
      <div><p class="k">Actual 50% crossing</p><p class="v">{min(knee_cuts)/1e3:.0f}–{max(knee_cuts)/1e3:.0f} kHz</p>
        <p class="s">at the knee, all eight together</p></div>
      <div><p class="k">Received power there</p><p class="v">+0.08 dB</p>
        <p class="s">not the analog filter</p></div>
    </div>
    <p>At +4.7&nbsp;dB SNR every detector holds Pd = 1.00 out to 750&nbsp;kHz. Repeating the
    sweep at the detection knee, where the eight sit at 0.98 with no offset — so that offset
    tolerance cannot simply be bought with SNR — gives the same answer with a much sharper edge:
    flat through 350–400&nbsp;kHz, then a hard fall between 700 and 800&nbsp;kHz. That is the
    coarse-E bank's own ±700&nbsp;kHz search span, and the received power is flat across it, so
    the receiver's analog filter is not what ends the curve.</p>
    <p><strong>The pipeline's offset limit is 700–800 kHz, not 350–400 kHz.</strong> Whatever
    produces the on-sky cliff is not the detectors, not the coarse banks, and not the search
    span — it lives in something this cable does not contain: the sky, the LNBs, or the
    estimation and bias-correction of the offset itself, which here was never estimated.</p>
    {fig_block("offset-cliff.png",
               "E4. Detection against an imposed carrier offset at two SNRs, on a shared axis. "
               "The red band is where sky says detection collapses. The bottom strip is the "
               "measured received power — the control that separates a search-span limit from "
               "an analog rolloff.")}
  </div>
</section>

<section class="exp">
  <div class="spine"><div class="eid">E5</div><div class="tick"></div></div>
  <div>
    <span class="chip new">The check cannot pass</span>
    <h2>The coincidence model, fed an occupancy it was never told</h2>
    <p class="lede">RX1 and RX2 see one injected signal through the tee with their own noise —
    structurally two radios on one sky, except that a seeded coin decided the occupancy. The
    repository's own solver was then asked to recover it.</p>
    <div class="readout">
      <div><p class="k">True occupancy</p><p class="v">{e5['f_true']:.3f}</p>
        <p class="s">set by a seeded Bernoulli draw</p></div>
      <div><p class="k">Recovered</p><p class="v">{min(e5['f_recovered'].values()):.3f}–{max(e5['f_recovered'].values()):.3f}</p>
        <p class="s">every 95% interval covers the truth</p></div>
      <div><p class="k">Spread over the eight</p><p class="v neg">{e5['f_spread_measured']:.3f}</p>
        <p class="s">where f is one number by construction</p></div>
      <div><p class="k">Report's on-sky spread</p><p class="v">{e5['report_sky_f_spread']:.3f}</p>
        <p class="s">offered as the model's failed check</p></div>
    </div>
    <p>The estimator works: all eight recover an occupancy of 0.283–0.331 against a true 0.278,
    and every bootstrap interval covers the truth. Conditional independence — the model's
    load-bearing assumption — also survives direct inspection, which only injection makes
    possible: on cells known to be empty, P(AB) differs from P(A)P(B) by at most 0.007.</p>
    <p>What does not survive is the model's <em>consistency check</em>. The report treats the
    spread of <span class="mono">f</span> across the eight detectors as a test the model must
    pass, because occupancy is a property of the sky and all eight read the same sky; on sky
    that spread is 0.040 and is reported as the check going unmet. Here occupancy is identical
    for all eight <em>by construction</em>, and the eight still disagree by
    {e5['f_spread_measured']:.3f} — larger than the on-sky figure, and larger than it in
    {e5['probability_loopback_spread_exceeds_sky']*100:.0f}% of bootstrap resamples.
    <strong>A spread of that size is what a correct model looks like.</strong> The on-sky
    spread is not evidence that anything is wrong.</p>
    <p>One systematic does show up: the solver reads detection probability low against the
    directly measured value in 15 of 16 cases — by up to 0.10 on dB — so the published d values
    are more likely floors than estimates.</p>
    {fig_block("coincidence-recovery.png",
               "E5. Left: occupancy recovered by the repository's solver against the occupancy "
               "that was set; the shaded band is the spread the model's own consistency check "
               "asks to be zero. Right: solver dA against dA read straight off the occupied "
               "probes.")}
  </div>
</section>

<div class="files">
  <h2>What was written</h2>
  <ul>
    <li>summary.json — every headline number</li>
    <li>figures/smoke-test.{{py,json,png}}</li>
    <li>figures/detection-vs-snr.{{py,json,png}}</li>
    <li>figures/detector-ranking.{{py,json,png}}</li>
    <li>figures/false-alarm-empty-channel.{{py,json,png}}</li>
    <li>figures/offset-cliff.{{py,json,png}}</li>
    <li>figures/coincidence-recovery.{{py,json,png}}</li>
    <li>thresholds.json — the calibration E2/E4/E5 use</li>
    <li>e1_smoke · e2_roc · e3_off · e3_dark · e3b_nullarm</li>
    <li>e4_offset · e4_offset_68 · e5_occupancy (raw scores, JSONL)</li>
    <li>rig.py · runner.py · pipeline.py · analysis.py</li>
    <li>palette_check.py — the figure palette, revalidated</li>
  </ul>
  <p class="path">/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/inject/</p>
  <p class="path">Radio returned to −89.75 dB on both transmit chains. Collection remained
  paused throughout; leo-sync-drain and leo-sync-import.timer were not touched.</p>
</div>
</div>
"""

OUT.write_text(HTML)
print(f"wrote {OUT} ({OUT.stat().st_size/1e6:.2f} MB)")
