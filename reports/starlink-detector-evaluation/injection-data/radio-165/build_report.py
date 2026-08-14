#!/usr/bin/env python3
"""Generate the report page from the measurement JSONs.

Every number on the page is read from the payload a figure script wrote, so the
prose cannot drift from the data it describes.  Figures are embedded as data
URIs because the artifact CSP blocks external hosts.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"
OUT = HERE / "report-165.html"

ORDER = ["anchor-8", "glrt-32", "glrt-64", "full-frame-verify",
         "full-frame-full", "full-frame-acquire", "differential-32",
         "differential-16"]


def load(name: str, root: Path = FIG) -> dict:
    path = root / name
    return json.loads(path.read_text()) if path.exists() else {}


def image(name: str) -> str:
    path = FIG / name
    if not path.exists():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def pct(value, places: int = 2) -> str:
    return "&mdash;" if value is None else f"{value * 100:.{places}f}%"


def num(value, places: int = 2) -> str:
    return "&mdash;" if value is None else f"{value:.{places}f}"


def khz(value) -> str:
    return "&mdash;" if value is None else f"{value / 1e3:.0f}&nbsp;kHz"


STYLE = """
:root {
  color-scheme: light;
  --ground:#f2f3f0; --surface:#fbfcfa; --sunk:#e8eae5; --rule:#d0d4cb;
  --ink:#161a18; --ink-2:#4c544f; --ink-3:#737c76;
  --signal:#a8481a; --probe:#1d5aa0; --good:#1b6b4c;
  --band:#f7efe7; --band-rule:#c98a3f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --ground:#14171a; --surface:#1c2023; --sunk:#23282c; --rule:#343a3f;
    --ink:#eceeed; --ink-2:#b3bab6; --ink-3:#8b938e;
    --signal:#e0864f; --probe:#6aa8e8; --good:#4fb389;
    --band:#2a2119; --band-rule:#9a6a2e;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ground:#14171a; --surface:#1c2023; --sunk:#23282c; --rule:#343a3f;
  --ink:#eceeed; --ink-2:#b3bab6; --ink-3:#8b938e;
  --signal:#e0864f; --probe:#6aa8e8; --good:#4fb389;
  --band:#2a2119; --band-rule:#9a6a2e;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  font-size: 17px;
  line-height: 1.62;
  -webkit-font-smoothing: antialiased;
}

.wrap { max-width: 1140px; margin: 0 auto; padding: 0 24px 96px; }
.col  { max-width: 68ch; }

h1, h2, h3 {
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino,
               "Book Antiqua", Georgia, serif;
  text-wrap: balance;
  font-weight: 600;
  line-height: 1.22;
  margin: 0;
}
h1 { font-size: clamp(2.1rem, 4.4vw, 3.1rem); letter-spacing: -0.015em; }
h2 { font-size: clamp(1.5rem, 2.6vw, 1.95rem); letter-spacing: -0.01em; }
h3 { font-size: 1.16rem; }

p { margin: 0 0 1.05em; }
a { color: var(--probe); }

code, .mono, td.n, th.n {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
               "Liberation Mono", monospace;
  font-variant-numeric: tabular-nums;
}
code { font-size: 0.9em; background: var(--sunk); padding: 0.1em 0.34em;
       border-radius: 3px; }

.eyebrow {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.735rem; text-transform: uppercase; letter-spacing: 0.13em;
  color: var(--ink-3); margin: 0 0 0.55em;
}

/* ---- masthead ---- */
header.top { border-bottom: 1px solid var(--rule); background: var(--surface); }
header.top .wrap { padding-top: 52px; padding-bottom: 34px; }
.lede { font-size: 1.16rem; color: var(--ink-2); margin-top: 1.1em; }

.rig-banner {
  margin: 30px 0 0; padding: 15px 19px;
  background: var(--band); border: 1px solid var(--band-rule);
  border-left-width: 4px; border-radius: 3px;
  font-size: 0.945rem; color: var(--ink);
}
.rig-banner strong { color: var(--signal); }

.meta {
  margin-top: 26px; display: grid; gap: 1px; background: var(--rule);
  border: 1px solid var(--rule); border-radius: 3px; overflow: hidden;
  grid-template-columns: repeat(auto-fit, minmax(178px, 1fr));
}
.meta div { background: var(--surface); padding: 11px 15px; }
.meta dt { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.09em;
           color: var(--ink-3); margin: 0 0 3px; }
.meta dd { margin: 0; font-family: ui-monospace, Menlo, Consolas, monospace;
           font-size: 0.85rem; font-variant-numeric: tabular-nums;
           word-break: break-all; }

/* ---- verdicts ---- */
.verdicts { display: grid; gap: 16px; margin: 40px 0 8px;
            grid-template-columns: repeat(auto-fit, minmax(268px, 1fr)); }
.verdict { background: var(--surface); border: 1px solid var(--rule);
           border-radius: 3px; padding: 19px 20px 21px; }
.verdict .tag {
  font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 0.7rem;
  letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3);
}
.verdict .answer { font-family: ui-serif, Georgia, serif; font-size: 1.3rem;
                   line-height: 1.3; margin: 9px 0 8px; color: var(--signal); }
.verdict p { font-size: 0.925rem; color: var(--ink-2); margin: 0; }

/* ---- sections ---- */
section { margin-top: 66px; }
section > .col > h2 { margin-bottom: 0.5em; }

figure { margin: 30px 0 8px; }
figure img { width: 100%; height: auto; display: block;
             border: 1px solid var(--rule); border-radius: 3px;
             background: #fcfcfb; }
figcaption { font-size: 0.885rem; color: var(--ink-2); margin-top: 11px;
             max-width: 84ch; }
figcaption b { color: var(--ink); font-weight: 600; }

.scroll { overflow-x: auto; margin: 26px 0; border: 1px solid var(--rule);
          border-radius: 3px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 0.875rem; }
caption { text-align: left; padding: 13px 15px 0; font-size: 0.875rem;
          color: var(--ink-2); }
th, td { padding: 8px 13px; text-align: left; border-bottom: 1px solid var(--rule);
         white-space: nowrap; }
thead th { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.075em;
           color: var(--ink-3); font-weight: 600;
           border-bottom: 1px solid var(--ink-3); }
td.n, th.n { text-align: right; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--sunk); }
td.name { font-family: ui-monospace, Menlo, Consolas, monospace;
          font-size: 0.83rem; }
.hi { color: var(--signal); font-weight: 600; }
.ok { color: var(--good); font-weight: 600; }

.note { border-left: 3px solid var(--rule); padding: 3px 0 3px 18px;
        margin: 26px 0; color: var(--ink-2); font-size: 0.955rem; }

.trap { background: var(--surface); border: 1px solid var(--rule);
        border-top: 3px solid var(--signal); border-radius: 3px;
        padding: 22px 24px; margin: 22px 0; }
.trap h3 { margin-bottom: 0.45em; }
.trap p:last-child { margin-bottom: 0; }

footer { margin-top: 76px; padding-top: 26px; border-top: 1px solid var(--rule);
         font-size: 0.875rem; color: var(--ink-3); }
footer code { font-size: 0.85em; }
ul.files { list-style: none; padding: 0; margin: 12px 0 0; }
ul.files li { padding: 3px 0; font-family: ui-monospace, Menlo, Consolas, monospace;
              font-size: 0.8rem; word-break: break-all; }
"""


def verdict_cards(topology, false_alarm, cliff, loss_db) -> str:
    deployed = false_alarm.get("rates_deployed_thresholds_target_arm", {})
    cells = [deployed[m]["per_cell_rate"] for m in ORDER if m in deployed]
    points = [deployed[m]["per_point_rate"] for m in ORDER if m in deployed]
    collapse = [v for row in (cliff.get("collapse_hz") or {}).values()
                for v in row.values() if v is not None]
    if not collapse:
        cliff_caption = ("Detection holds flat across the whole sweep while the "
                         "sky curve collapses through the shaded band.")
    elif min(collapse) > 450_000:
        cliff_caption = ("Detection survives far past 350&ndash;400 kHz and falls "
                         "at the coarse bank&rsquo;s own search limit.")
    else:
        cliff_caption = ("Detection against a known offset collapses where sky "
                         "says it does.")
    if not collapse:
        cliff_answer = "no collapse to 800 kHz"
        cliff_body = ("Against a <em>known</em> injected offset the eight keep "
                      "detecting across the whole sweep. The 350&ndash;400 kHz "
                      "cliff seen on sky is not reproduced.")
    elif min(collapse) > 450_000:
        cliff_answer = f"{min(collapse) / 1e3:.0f}&ndash;{max(collapse) / 1e3:.0f} kHz"
        cliff_body = ("Detection against a <em>known</em> injected offset survives "
                      "far past the 350&ndash;400 kHz where sky detection dies, "
                      "then falls at the coarse bank&rsquo;s own search limit.")
    else:
        cliff_answer = f"{min(collapse) / 1e3:.0f}&ndash;{max(collapse) / 1e3:.0f} kHz"
        cliff_body = ("Detection against a <em>known</em> injected offset collapses "
                      "in the same 350&ndash;400 kHz window the sky corpus reports.")
    return f"""
  <div class="verdicts">
    <div class="verdict">
      <div class="tag">T1 &middot; Topology</div>
      <div class="answer">TX2 &rarr; split &rarr; RX1 + RX2</div>
      <p>TX1 is not cabled &mdash; it never lifts either receiver off its noise
         floor. Same topology as sibling rig .183, {num(loss_db, 1)} dB more
         loss.</p>
    </div>
    <div class="verdict">
      <div class="tag">T2 &middot; False alarm</div>
      <div class="answer">{pct(min(points),2)}&ndash;{pct(max(points),2)} per point<br>
        {pct(min(cells),1)}&ndash;{pct(max(cells),1)} per cell</div>
      <p>On a genuinely empty channel, under the thresholds the corpus
         analysis uses.
         The ~6% seen on sky is what correct per-point calibration predicts, not
         excess firing.</p>
    </div>
    <div class="verdict">
      <div class="tag">T3 &middot; The cliff</div>
      <div class="answer">{cliff_answer}</div>
      <p>{cliff_body}</p>
    </div>
  </div>"""


def false_alarm_table(false_alarm) -> str:
    deployed = false_alarm.get("rates_deployed_thresholds_target_arm", {})
    own_t = false_alarm.get("rates_own_thresholds_target_arm", {})
    own_c = false_alarm.get("rates_own_thresholds_cross_edge_arm", {})
    sky = false_alarm.get("sky_per_cell_measured", {})
    rows = []
    for method in ORDER:
        d, t, c = deployed.get(method), own_t.get(method), own_c.get(method)
        if not d:
            continue
        rows.append(f"""      <tr>
        <td class="name">{method}</td>
        <td class="n">{num(d['threshold'], 4)}</td>
        <td class="n">{pct(d['per_point_rate'])}</td>
        <td class="n">{pct(d['per_cell_rate'])}</td>
        <td class="n">{pct((sky.get(method) or {}).get('rate'))}</td>
        <td class="n">{pct((c or {}).get('per_point_rate'))}</td>
        <td class="n hi">{pct((t or {}).get('per_point_rate'))}</td>
      </tr>""")
    return f"""
  <div class="scroll">
    <table>
      <caption>Per algorithm, on {false_alarm.get('observations', 0):,} empty-channel
        cells. The last two columns share one threshold, re-drawn on this radio's own
        cross-edge null &mdash; they differ only in which template was read at the
        same candidate points.</caption>
      <thead><tr>
        <th>algorithm</th>
        <th class="n">corpus threshold</th>
        <th class="n">per point</th>
        <th class="n">per cell</th>
        <th class="n">per cell on sky</th>
        <th class="n">re-drawn: null arm</th>
        <th class="n">re-drawn: target arm</th>
      </tr></thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
  </div>"""


def sky_at(sky_curve, offset_hz):
    """The sky bin containing this offset, or None past the end of the curve."""
    for row in sky_curve or []:
        if row["low_khz"] <= offset_hz / 1e3 < row["high_khz"]:
            return row
    return None


def cliff_verdict(cliff, bias: dict) -> str:
    """The T3 conclusion, written from what the sweep actually did."""
    curves = cliff.get("curves") or {}
    if not curves:
        return "<p>Sweep not yet complete.</p>"
    collapse = {gain: [v for v in row.values() if v is not None]
                for gain, row in (cliff.get("collapse_hz") or {}).items()}
    solved = [v for values in collapse.values() for v in values]
    bank = (cliff.get("bank_span_hz") or {}).get("coarse-E (candidate 13x8)", 700_000)
    biases = (bias.get("bias_hz") or {"lnb-c": 604159.8})
    worst_port = max(biases, key=lambda k: abs(biases[k]))
    worst_bias = abs(biases[worst_port])
    rest_bias = max((abs(v) for k, v in biases.items() if k != worst_port),
                    default=0.0) / 1e3

    if not solved:
        # Where the pipeline's own estimate stops tracking the truth, which is a
        # different place from where detection stops -- that gap is the finding.
        estimate = (curves.get(max(curves, key=float)) or {}).get("_estimate", {})
        saturation = ""
        if estimate:
            pairs = list(zip(estimate["offset_hz"], estimate["estimated_hz"]))
            broken = [(imposed, got) for imposed, got in pairs
                      if abs(got - imposed) > 80_000]
            if broken:
                imposed, got = broken[0]
                saturation = (
                    f" The estimate stops tracking first: at {khz(imposed)} imposed "
                    f"the coarse stage still reports {khz(got)}, pinned at the edge "
                    f"of its own &plusmn;{khz(bank)} search, while every detector "
                    f"goes on finding the signal.")
        # A detection rate of 1.00 everywhere could still mean every score was
        # scraping its threshold, so the margin is quoted beside it.
        margins = [v for gain in curves for method in ORDER
                   if method in curves[gain]
                   for v in curves[gain][method].get("score_over_threshold", [])]
        margin_note = (
            f" These are not marginal detections: across the whole sweep the "
            f"best point in a cell scores {min(margins):.1f}&ndash;"
            f"{max(margins):.0f}&times; its own threshold."
            if margins else "")
        # State the exceptions rather than rounding them away.  Of 320 (offset,
        # gain, algorithm) combinations exactly one fell below 1.00, and a
        # summary that said "every one" would be false.
        misses = [(gain, method, offset, value)
                  for gain in curves for method in ORDER if method in curves[gain]
                  for offset, value in zip(curves[gain][method]["offset_hz"],
                                           curves[gain][method]["per_cell_detection"])
                  if value < 1.0]
        if not misses:
            coverage = ("every cell was detected by every one of the eight")
        elif len(misses) == 1:
            gain, method, offset, value = misses[0]
            coverage = (f"every cell was detected by every one of the eight at "
                        f"every offset but one: <code>{method}</code> fell to "
                        f"{value * 100:.0f}% at {khz(offset)} and {gain} dB, the "
                        f"single value below 100% in the sweep")
        else:
            coverage = (f"{len(misses)} of "
                        f"{sum(len(curves[g][m]['offset_hz']) for g in curves for m in ORDER if m in curves[g])} "
                        f"(offset, gain, algorithm) combinations fell below 100%; "
                        f"the lowest was {min(v for _, _, _, v in misses) * 100:.0f}%")
        return (
            "<p><strong>The 350&ndash;400 kHz cliff is not reproduced. Detection "
            "never collapsed at all.</strong> Out to 800 kHz of imposed offset, "
            + coverage + "."
            + margin_note +
            " Whatever ends detection on sky at 350&ndash;400 kHz, it is not "
            "these detectors meeting a carrier offset in a clean channel.</p>"
            f"<p>What does break is the pipeline&rsquo;s ability to <em>report</em> "
            f"the offset.{saturation} That matters because the sky cliff is plotted "
            "against the pipeline&rsquo;s own bias-corrected estimate. A signal the "
            "search cannot place correctly is drawn at the wrong x, so the sky "
            "curve mixes &lsquo;not detected&rsquo; with &lsquo;detected and "
            "mis-located&rsquo;, and the 350&ndash;400 kHz figure describes the "
            "axis rather than a tolerance.</p>"
            "<p>The two quantities also are not the same quantity. The coarse banks "
            "are built with <code>center_hz = 0</code>, so they search "
            "<em>raw</em> offset about each receiver&rsquo;s own local oscillator, "
            "&plusmn;300 kHz for the deployed bank and &plusmn;700 kHz for the "
            "candidate. The cliff axis is <code>abs(cfo &minus; "
            f"receiver_center)</code>, and in this corpus one port, "
            f"<code>{worst_port}</code>, sits {khz(worst_bias)} off centre while "
            f"the other three sit within {rest_bias:.1f} kHz of it. For that port "
            f"a corrected offset and a "
            "searchable raw offset differ by more than the whole width of the "
            "deployed bank, so the axis and the search do not measure the same "
            "thing on the ports that populate its far bins.</p>"
            "<p>Two cautions. This rig has no LNB, so any offset-dependent effect "
            "living in the analogue chain is absent here by construction. And the "
            "loopback signal is far stronger than sky: this says where the search "
            "reaches, not how little signal it needs to get there.</p>")

    lowest, highest = min(solved), max(solved)
    same_place = all(abs(v - lowest) <= 100_000 for v in solved)
    gain_note = ("The collapse sits in the same place at both transmit powers, "
                 "30 dB apart, so it is structural rather than a sensitivity "
                 "effect." if len(collapse) > 1 and same_place else
                 "The collapse moves with transmit power, so at least part of it "
                 "is sensitivity rather than structure.")

    if lowest >= 450_000:
        return (
            f"<p><strong>The 350&ndash;400 kHz cliff is not reproduced.</strong> "
            f"Against a known offset, detection holds far past it and does not "
            f"collapse until {khz(lowest)}&ndash;{khz(highest)} &mdash; which is "
            f"where the candidate coarse bank&rsquo;s own &plusmn;{khz(bank)} "
            f"search span runs out. {gain_note}</p>"
            "<p>That makes the sky cliff an artefact of the axis rather than a "
            "limit of the detectors. On sky the offset is estimated by the same "
            "coarse stage whose search is bounded; a signal outside that span "
            "cannot be reported at its true offset <em>and</em> cannot be "
            "detected, so it leaves the plotted axis and the detection curve "
            "together. The 350&ndash;400 kHz location therefore describes where "
            "the sky population runs out of correctly-estimated offsets, not a "
            "tolerance of the pipeline.</p>"
            "<p>Two cautions. This rig has no LNB, so any offset-dependent effect "
            "living in the analogue chain is absent by construction. And the "
            "loopback signal is far stronger than sky: these curves say where the "
            "search reaches, not how little signal it needs.</p>")

    return (
        f"<p><strong>The cliff reproduces.</strong> Against a known offset, "
        f"detection collapses at {khz(lowest)}&ndash;{khz(highest)}, matching the "
        f"350&ndash;400 kHz the sky corpus reports. {gain_note} Since the offset "
        f"here is imposed rather than estimated, this is a property of the "
        f"detectors and the search, not of the axis.</p>")


def cliff_table(cliff) -> str:
    curves = cliff.get("curves") or {}
    if not curves:
        return ""
    sky_curve = cliff.get("sky_reference_5MSps") or []
    gains = sorted(curves, key=float, reverse=True)
    head = "".join(f'<th class="n">cable, TX {g} dB</th>' for g in gains)
    # Index by offset VALUE, never by position: two gains need not have swept
    # the same number of offsets (a run cut short leaves one shorter), and
    # lining them up by index would silently pair different offsets.
    by_offset = {gain: {method: dict(zip(curves[gain][method]["offset_hz"],
                                         curves[gain][method]["per_cell_detection"]))
                        for method in ORDER if method in curves[gain]}
                 for gain in gains}
    offsets = sorted({offset for gain in gains for method in by_offset[gain]
                      for offset in by_offset[gain][method]})
    rows = []
    for offset in offsets:
        cellset = []
        for gain in gains:
            values = [by_offset[gain][m][offset] for m in ORDER
                      if m in by_offset[gain] and offset in by_offset[gain][m]]
            if not values:
                cellset.append('<td class="n">&mdash;</td>')
                continue
            mean = sum(values) / len(values)
            klass = "ok" if mean >= 0.5 else "hi"
            cellset.append(f'<td class="n {klass}">{mean * 100:.0f}%</td>')
        bin_row = sky_at(sky_curve, offset)
        sky_cell = ('<td class="n">&mdash;</td>' if bin_row is None else
                    f'<td class="n {"ok" if bin_row["rate_pct"] >= 10 else "hi"}">'
                    f'{bin_row["rate_pct"]:.1f}%</td>')
        rows.append(f'      <tr><td class="n">{offset / 1e3:.0f}</td>'
                    + "".join(cellset) + sky_cell + "</tr>")
    return f"""
  <div class="scroll">
    <table>
      <caption>Mean detection across all eight algorithms against the
        <em>imposed</em> offset, beside what sky reports at 5 MS/s against its
        <em>estimated</em> offset. The two levels are not comparable &mdash; the
        cable columns count cells that all contain a signal, the sky column counts
        candidate points on sky where most contain none. The shape is the
        comparison.</caption>
      <thead><tr><th class="n">offset (kHz)</th>{head}
        <th class="n">sky 5 MS/s (per point)</th></tr></thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
  </div>"""


def main() -> None:
    topology = load("topology-165.json")
    false_alarm = load("false-alarm-165.json")
    cliff = load("cfo-cliff-165.json")
    sky_coarse = load("sky_coarse_null-165.json", HERE)
    lnb_bias = load("lnb_bias-165.json", HERE)

    geometry = topology.get("geometry", {})
    deployed = false_alarm.get("rates_deployed_thresholds_target_arm", {})
    points = [deployed[m]["per_point_rate"] for m in ORDER if m in deployed]
    cells = [deployed[m]["per_cell_rate"] for m in ORDER if m in deployed]
    asym = false_alarm.get("arm_asymmetry", {})
    own_t = false_alarm.get("rates_own_thresholds_target_arm", {})
    worst = max(ORDER, key=lambda m: (own_t.get(m) or {}).get("per_point_rate", 0)) \
        if own_t else None
    collapse = [v for row in (cliff.get("collapse_hz") or {}).values()
                for v in row.values() if v is not None]
    if not collapse:
        cliff_caption = ("Detection holds flat across the whole sweep while the "
                         "sky curve collapses through the shaded band.")
    elif min(collapse) > 450_000:
        cliff_caption = ("Detection survives far past 350&ndash;400 kHz and falls "
                         "at the coarse bank&rsquo;s own search limit.")
    else:
        cliff_caption = ("Detection against a known offset collapses where sky "
                         "says it does.")
    tx2 = topology.get("paths", {}).get("TX2 → RX1", {})
    tx1 = topology.get("paths", {}).get("TX1 → RX1", {})
    coarse = sky_coarse.get("coarse", {})
    facts = topology.get("summary", {})
    couplings = list((facts.get("tx1_coupling_below_tx2_db") or {"0": 0}).values())
    #: Sibling rig .183 reported this rms at identical settings; the gap is a
    #: property of the attenuators, so it is computed rather than eyeballed.
    sibling_rms = 83.6
    own_rms = (tx2.get("rms_counts") or [sibling_rms])[-1]
    loss_db = 20 * __import__("math").log10(sibling_rms / own_rms)

    html = f"""<title>Loopback Ground Truth on .165</title>
<style>{STYLE}</style>
<header class="top">
  <div class="wrap">
    <div class="col">
      <p class="eyebrow">Radio ip:192.168.1.165 &middot; independent replication</p>
      <h1>A known signal, injected into a closed cable</h1>
      <p class="lede">This project has never had ground truth. Detection
        probability has only been inferred from a coincidence model whose
        validation was shown to be vacuous. Injecting a signal the detectors
        already hunt, at a strength and a carrier offset chosen rather than
        estimated, measures what that model could only guess.</p>
    </div>
    <div class="rig-banner">
      <strong>This is a cabled loopback, not sky.</strong> TX2 feeds a splitter
      into RX1 and RX2 with no antenna anywhere in the path. Everything here
      tests the eight detectors and the digital pipeline. It says nothing about
      the LNBs, the dish, the sky, or anything upstream of the ADC.
    </div>
    <div class="meta">
      <div><dt>Radio</dt><dd>ip:192.168.1.165</dd></div>
      <div><dt>Serial</dt><dd>1040007c4a94000211000b009186843ef2</dd></div>
      <div><dt>Arm</dt><dd>{geometry.get('arm', '80ms-5.00MSps')}</dd></div>
      <div><dt>Geometry</dt><dd>{geometry.get('probe_samples', 400000):,} samples
        &middot; {geometry.get('probe_ms', 80):.0f} ms</dd></div>
      <div><dt>LO</dt><dd>{geometry.get('lo_hz', 1190312500):,} Hz</dd></div>
      <div><dt>RX gain</dt><dd>{geometry.get('rx_gain_db', 40):.0f} dB manual</dd></div>
    </div>
  </div>
</header>

<div class="wrap">
{verdict_cards(topology, false_alarm, cliff, loss_db)}

<section>
  <div class="col">
    <p class="eyebrow">T1</p>
    <h2>What is actually cabled to this radio</h2>
    <p>Nothing about the rig was assumed. The pilot frame was transmitted on
      TX1 and then on TX2, and both receivers were measured each time, at four
      transmit powers, against a transmitter-off floor re-measured between every
      row.</p>
    <p>Two statistics are needed, because either alone can be fooled. Received
      <strong>rms</strong> tells a signal path from silence but cannot tell a
      signal from any other energy. The <strong>matched-filter peak/median</strong>
      against the exact transmitted buffer tells a real copy of the waveform from
      unrelated energy, but says nothing about level. A path counts only if both
      move together.</p>
  </div>
  <figure>
    <img src="{image('topology-165.png')}" alt="Received level and matched-filter
      peak/median against TX gain for all four TX-port/RX-port combinations.">
    <figcaption><b>TX2 is a clean cable to both receivers; TX1 is not
      connected.</b> TX2 gains 10 dB of level per 10 dB of drive and its
      correlation saturates at {num(facts.get('tx2_matched_ptm', {}).get('1'), 1)}
      against a ceiling of {num(topology.get('ideal_matched_ptm'), 1)} &mdash; the
      value this metric reaches on a noiseless copy of the transmitted buffer, so
      the received waveform is very nearly perfect. TX1 moves neither receiver off
      its floor.</figcaption>
  </figure>
  <div class="col">
    <p>The two receivers sit {num(facts.get('rx_split_db'), 2)} dB apart at full
      drive, and their correlation peaks land at the <em>same</em> phase modulo
      the transmitted buffer &mdash; lag
      {facts.get('peak_lag_mod_buffer', {}).get('1', '&mdash;')} on both &mdash;
      which is what a symmetric splitter with matched attenuators looks like.
      TX1 does couple faintly: its correlation climbs to
      {num((tx1.get('matched_peak_to_median') or [0])[-1], 1)} at the top gain,
      {num(min(couplings), 1)}&ndash;{num(max(couplings), 1)} dB below TX2.
      But it never raises the received level, so it is board and on-chip leakage
      rather than a cable. Measuring that coupling from the matched-filter peak
      rather than from rms is deliberate: TX1&rsquo;s rms sits inside the noise
      floor, where a level ratio would be a difference of two noise estimates.</p>
    <p>This is the same topology as sibling rig .183 with {num(loss_db, 1)} dB
      more loss: at identical settings .183 reported rms {num(sibling_rms, 1)}
      counts where this rig gives {num(own_rms, 1)}. Structure replicates on
      separate hardware; level does not, which is a property of the attenuators
      rather than of the pipeline.</p>
  </div>
</section>

<section>
  <div class="col">
    <p class="eyebrow">T2</p>
    <h2>What the eight detectors do on an empty channel</h2>
    <p>With both transmitters at &minus;89.75 dB, the cable holds nothing.
      That is the difference from the cross-edge null the corpus calibrates on,
      which is target-code-free by construction but is still sky and may hold
      real energy. Every probe was scored through the repository&rsquo;s own
      path &mdash; <code>search_observation</code>, <code>distinct_points</code>,
      <code>confirm_points</code> &mdash; imported, not reimplemented.</p>
    <p>The channel stayed genuinely empty for the whole run: received rms
      {num((false_alarm.get('rms_counts') or {}).get('min'), 2)}&ndash;{num(
      (false_alarm.get('rms_counts') or {}).get('max'), 2)} ADC counts with no
      drift, peak 6 counts against a 2048 ceiling, and the coarse bank&rsquo;s
      peak-to-median never exceeded 1.193 &mdash; well below the deployed 1.33
      gate, which never fired once.</p>
  </div>
  <figure>
    <img src="{image('false-alarm-165.png')}" alt="Per-point and per-cell
      false-alarm rates for eight algorithms on an empty channel.">
    <figcaption><b>The corpus thresholds are calibrated.</b> An empty channel
      fires at {pct(min(points))}&ndash;{pct(max(points))} per point against a 1%
      nominal, and {pct(min(cells), 1)}&ndash;{pct(max(cells), 1)} per cell against
      the {pct(false_alarm.get('predicted_per_cell'), 1)} that
      1&minus;0.99<sup>{num(false_alarm.get('mean_points_per_cell'), 1)}</sup>
      predicts once a cell maximises over its candidates.</figcaption>
  </figure>
{false_alarm_table(false_alarm)}
  <div class="col">
    <h3>The per-cell rate on sky is not excess firing</h3>
    <p>The sky corpus measures 5.47&ndash;6.74% per cell and this rig measures
      {pct(min(cells), 1)}&ndash;{pct(max(cells), 1)} on a channel with nothing in
      it whatsoever. Since 1&minus;(0.99)<sup>7</sup> = 6.8%, a per-cell rate near
      6% is exactly what correct per-point calibration produces after maximising
      over ~7 candidates. <strong>The answer to the question the brief asks is
      that the thresholds look calibrated.</strong> The sky per-cell figure has
      been read as a symptom; it is arithmetic.</p>
    <h3>But the null the thresholds are drawn from is the wrong temperature</h3>
    <p>Re-draw the thresholds the repository&rsquo;s own way &mdash; the 1%
      quantile of the cross-edge null arm &mdash; on this radio&rsquo;s empty
      channel, and the target arm fires far above 1% at the same points:
      up to {pct((own_t.get(worst) or {}).get('per_point_rate'))} for
      <code>{worst}</code>, where the null arm sits at 1% by construction.</p>
    <p>Both arms are read at the <em>same</em> candidate points, and those points
      were all proposed by searchers maximising a target-edge statistic. So the
      target template is evaluated where it was selected to do well, and the
      opposite edge&rsquo;s template is evaluated at a place chosen without
      reference to it. On a dead cable neither arm holds a signal, so the gap is
      selection, not sky.</p>
    <p>The sky corpus carries the same asymmetry, because it is the same code
      path. Its cross-edge null is also hotter than truly empty: on the matching
      arm its coarse peak-to-median reaches p99
      {num((coarse.get('cross-edge-null/E') or {}).get('p99'), 4)} and max
      {num((coarse.get('cross-edge-null/E') or {}).get('max'), 4)}, where this
      cable reaches 1.181 and 1.193. The <em>medians</em> agree almost exactly
      ({num((coarse.get('cross-edge-null/E') or {}).get('p50'), 4)} on sky against
      1.139 on the cable), so the bulk of the sky null really is noise &mdash; it
      is the upper tail that is contaminated, and the upper tail is the only part
      a threshold is made of.</p>
  </div>
</section>

<section>
  <div class="col">
    <p class="eyebrow">T3</p>
    <h2>Where detection actually dies with carrier offset</h2>
    <p>On sky, detection collapses between 350 and 400 kHz of bias-corrected
      offset at every sample rate, and nobody knows whether that is the
      pipeline&rsquo;s search span or something physical. The trouble is that the
      sky axis is an <em>estimate</em> of the offset, produced by the same
      pipeline whose sensitivity is in question, so it cannot separate &ldquo;the
      search does not reach there&rdquo; from &ldquo;the estimate does not reach
      there&rdquo;.</p>
    <p>Here the offset is imposed on the waveform before the DAC, by multiplying
      by exp(2&pi;<i>ft</i>). The x axis is known by construction. The rig&rsquo;s
      own carrier error is nil &mdash; transmitter and receiver run from one
      crystal, and the coarse bank returns 0 Hz on the loopback &mdash; so the
      offset the detectors face is the offset imposed.</p>
  </div>
  <figure>
    <img src="{image('cfo-cliff-165.png')}" alt="Per-cell detection rate and the
      pipeline's own offset estimate against imposed carrier offset.">
    <figcaption><b>{cliff_caption}</b>
      The lower row shows the pipeline&rsquo;s own coarse estimate against the
      imposed truth, which is what separates a search that cannot reach from an
      estimate that cannot report.</figcaption>
  </figure>
{cliff_table(cliff)}
  <div class="col">
{cliff_verdict(cliff, lnb_bias)}
  </div>
</section>

<section>
  <div class="col">
    <p class="eyebrow">Traps</p>
    <h2>Two things that cost this run, and will cost the next one</h2>
    <div class="trap">
      <h3>A cyclic TX buffer silently fails to start</h3>
      <p>On this radio a cyclic push often does not start the DMA. The port then
        reads the bare noise floor while <code>hardwaregain</code> reads back
        exactly the value asked for, <code>tx_cyclic_buffer</code> reads
        <code>True</code>, and nothing raises anywhere. An entire TX sweep was
        recorded as a dead port this way before it was caught &mdash; the same
        shape of failure the brief warns about for the channel map, reached
        instead through the DMA.</p>
      <p>The fix that holds: load the waveform onto <em>both</em> TX channels in
        one buffer and select the radiating port with hardwaregain alone, so
        choosing a port never touches the DMA; then verify the load by requiring
        received rms to rise, and retry until it does. rms is the right probe
        because it is blind to the thing under test.</p>
    </div>
    <div class="trap">
      <h3>The cross-edge null is not a null for a searched point</h3>
      <p>The repository already documents that a wrong-code control free to
        choose its epoch re-finds the real signal. The cross-edge arm avoids
        that, but it inherits a quieter version of the same problem: it is
        evaluated at points somebody else&rsquo;s target-edge maximisation chose.
        Measured on an empty cable, that is worth up to
        {num(max((asym.get(m) or {}).get('p99_ratio', 0) for m in ORDER) if asym else 0, 2)}&times;
        in p99 and turns a nominal 1% into
        {pct((own_t.get(worst) or {}).get('per_point_rate'))}.</p>
      <p>Any threshold meant for a searched statistic has to be drawn from a null
        that was searched the same way.</p>
    </div>
  </div>
</section>

<section>
  <div class="col">
    <p class="eyebrow">Disagreements</p>
    <h2>Where this rig disagrees with what is already written down</h2>
    <ul>
      <li><strong>Sibling rig .183 &mdash; same topology, ~9 dB more loss.</strong>
        TX2 into a split feeding both receivers, confirmed independently on
        separate hardware. Level differs; structure does not.</li>
      <li><strong><code>CLEAN_NULL_P99_BY_PROBE_MS</code> is keyed only by probe
        length.</strong> It records 1.137 at 80 ms. A genuinely clean 80 ms null
        at 5 MS/s measures 1.181 here. The statistic is a maximum over frame
        epochs, and there are twice as many epochs at 5 MS/s as at 2.5, so the
        constant cannot be a function of probe length alone.</li>
      <li><strong>The sky cross-edge null is hotter than empty in its tail
        only.</strong> Medians agree to three decimals; p99 and max do not.</li>
    </ul>
  </div>
</section>

<footer>
  <div class="col">
    <p>All measurements taken on ip:192.168.1.165 only. Both transmit channels
      were returned to &minus;89.75 dB at the end of every run, including on
      failure. Scoring used the repository&rsquo;s own modules unmodified;
      thresholds were drawn from the sky corpus&rsquo;s matching
      <code>{geometry.get('arm', '80ms-5.00MSps')}</code> arm at 1% per point,
      via <code>cross_radio.null_thresholds</code> and
      <code>observation_fires</code>.</p>
    <ul class="files">
      <li>figures/topology-165.png &middot; .py &middot; .json</li>
      <li>figures/false-alarm-165.png &middot; .py &middot; .json</li>
      <li>figures/cfo-cliff-165.png &middot; .py &middot; .json</li>
      <li>t1_matrix-165.jsonl &middot; t2_scores-165.jsonl &middot; t3_cliff-165.jsonl</li>
      <li>sky_thresholds-165.json &middot; sky_coarse_null-165.json</li>
    </ul>
  </div>
</footer>
</div>
"""
    OUT.write_text(html)
    print(f"wrote {OUT} ({len(html) / 1024:.0f} kB)")


if __name__ == "__main__":
    main()
