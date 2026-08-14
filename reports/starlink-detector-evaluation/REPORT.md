# Evaluating Starlink edge-pilot detectors with two radios

Date: 2026-08-14 UTC
Status: **the apparatus works; the method proposed for deciding which detector
is better does not, and the test that shows it is the main result**

## Executive summary

Eight algorithms were built to decide whether a Starlink downlink channel is
lit, by looking for the eight known pilot subcarriers at the channel's band
edge. To rank them you need ground truth. Nothing is injected at this site, so
there is none, and the substitute chosen was two independent radios watching the
same channel at the same instant: if both fire more often than chance, something
was there, and a coincidence model turns the three counted rates into a sky
occupancy `f` and a per-chain detection probability `d`.

Two radios were driven from one process with a barrier at every tuning. 7,054
paired sweeps were captured, shipped byte-verified, imported, and scored. That
part works and is documented in
[section 3](#3-the-apparatus-two-radios-one-instant).

The method it was built to serve does not. The validation offered for the
coincidence model was that the eight algorithms agree on `f`. Run the identical
estimator on two joins where the model is **definitionally false** — radio B
taken two instants later, and radio A joined to a different sweep entirely — and
the eight agree just as tightly. More tightly: spread 0.036 on the scrambled
join against 0.042 on the real one, each at or below its own sampling noise. The
check has no failure mode
([section 6](#6-did-it-work-the-negative-controls)).

The reason is measurable. Over 30,192 observations every pair of the eight
detectors makes the same fire / no-fire call at phi 0.847–0.945. They are one
statistic counted eight times, and near-duplicates are obliged to return
near-identical `f` whatever they are fed
([section 7](#7-why-it-failed-the-detectors-are-near-duplicates)).

Three things survive. The sky is structured, and the structure is not spectral:
a channel's own two edges agree at phi 0.453 while any two different channels
agree at 0.118, and that term does not fade with channel separation. Agreement
between two chains is costed one factor at a time, and **changing the LNB is the
entire effect** — the edge costs phi −0.0000 and the radio boundary +0.0046,
both indistinguishable from zero, while swapping the receiver costs −0.0868
([section 8](#8-what-the-sky-looks-like)). And the instrument yields two hard,
reusable numbers: a rate-independent ~350 kHz offset tolerance, and a
+604.2 kHz local-oscillator offset on one port that makes a healthy receiver
look deaf ([section 9](#9-what-survives-the-instrument)).

Every `d` in this report is a **model output**, never a measurement. It is
inferred from a model whose own consistency check is the one shown above to be
incapable of failing. That caveat is repeated wherever a `d` appears, because it
is load-bearing every time.

## How to read this report

Each numbered section is one idea, one figure, one takeaway, and is meant to be
readable on its own. The detailed experimental record — every intermediate
result, every disagreement between snapshots, the full claim ledger — is
[`reports/sync-scan-cross-radio-2026-08-14/REPORT.md`](../sync-scan-cross-radio-2026-08-14/REPORT.md)
and is not superseded by this document. This is the summary.

**One census note, stated once and then not revisited.** Radio collection was
paused by the operator at 2026-08-14T14:49Z, so sweep and pair counts are fixed
and do not move. Scoring has no timer and ran throughout, so the count of scored
sidecars does move. Every figure freezes its own list of scored sidecars before
computing and uses only that list; the four new figure groups froze at 2,462,
2,544, 2,547 and 2,554 scored sidecars (for the two joining groups, 2,514 and
2,516 of those sit inside a pair), and the three figures carried over from the
detailed record froze earlier still, at 2,339. A population size quoted in one
section can therefore differ from a neighbouring section by a percent or two.
Nothing here turns on that, no figure mixes lists, and the range is 2,339–2,554
throughout.

**One limitation that does matter, and it constrains how every absolute number
here should be read.** Scoring runs in corpus order, so the scored set is a
*chronological prefix* of the campaign, not a sample of it. The share spans
00:03:15Z to 14:49:22Z; scoring has reached 03:54:56Z. Of the 2,948 entries at
or before that instant, 2,840 are scored; of the 10,267 after it, **none are**.
Every figure in this report therefore describes the first quarter of the
observing window.

That window is not stationary. Split into equal quartiles, the fire rate climbs
steadily through it:

| Window (UTC) | n | `anchor-8` | `full-frame-full` | `glrt-32` | any method |
|---|---:|---:|---:|---:|---:|
| 00:03–01:07 | 8,016 | 0.2586 | 0.2823 | 0.2516 | 0.3519 |
| 01:07–02:00 | 8,016 | 0.3326 | 0.3500 | 0.3280 | 0.4228 |
| 02:00–02:44 | 8,016 | 0.3230 | 0.3462 | 0.3225 | 0.4178 |
| 02:44–03:24 | 8,016 | 0.3527 | 0.3721 | 0.3542 | 0.4452 |

The swing across 3.3 hours is 0.094 — roughly **four times the 0.024 spread
between the eight detectors** that section 2 is about. So: every *absolute* rate
in this report is a property of 00:03–03:55 UTC and should not be read as a
property of the sky in general. Every *comparison* — between algorithms, between
geometries, between joins — is paired inside that window and is unaffected,
which is why the results in sections 6, 7 and 8 stand regardless. It also
explains why the census numbers above drift monotonically rather than randomly:
each later batch is later sky, and later sky was busier.

## The corpus

| Quantity | Measured | Moves? |
|---|---:|---|
| Sweep directories on the scan share | 7,054 | fixed — collection paused |
| — captured with both radios, so pairable | 6,161 | fixed |
| — single-radio, the collector outage | 893 | fixed |
| Corpus entries imported | 13,215 | fixed |
| Scored sidecars at figure-freeze time | 2,339 – 2,554 | grows; scoring is live |
| Paired sweeps the analysis could use | 1,261 at the widest freeze | grows with scoring |
| — same-edge / opposite-edge | 603 / 658 | |
| Matched-arm cells behind the coincidence estimate | 18,176 | |
| Live target observations behind the correlation matrix | 30,192 | |
| Live cross-edge null observations behind the measured false-alarm rate | 15,072 | |
| `lnb-a` observations excluded as a dead port | 15,104 | |

Four receivers exist; three are live. `lnb-c` and `lnb-d` are the two inputs of
`pluto-19f2`; `lnb-b` and `lnb-a` are the two inputs of `pluto-5d4d`. `lnb-a`
returns a flat ~1.19 peak-to-median at every tuning since 2026-08-13 04:44 UTC
and is excluded from both the target and the null arms throughout — an exclusion
whose stated reason this corpus does not reproduce, recorded in
[section 9](#9-what-survives-the-instrument).

Only 1,261 of the 6,161 pairable sweeps have been scored. That gap is the
cheapest available improvement to every number in this report, and it is the
first item in [section 10](#10-what-real-ground-truth-would-take).

---

## 1. Why edge pilots, and why nothing here is visible

Each Starlink downlink channel is 240 MHz wide and carries eight known pilot
subcarriers just inside each band edge. They sit at fixed, published offsets, so
a receiver that can decide whether they are present can decide whether that
channel is lit — and at what frequency offset — without decoding anything. That
is the whole motivation: a cheap, blind channel-occupancy sensor built out of
one known waveform feature.

**The pilots are not visible in any spectrum in this corpus.** Ranked over all
2,136 target-arm observations in the 640 ms / 10 MS/s arm — the longest probe at
the widest band, ranked by the deployed survey bank's own peak-to-median — the
top-scoring capture puts the 1.875 MHz pilot band **+0.0588 dB** above its own
shoulder, 1 sigma 0.0040 dB over 769 bins. The repository's own noiseless pilot
frame, measured the same way on the same axis, gives **+16.24 dB**. The sky is
about 16 dB below what a spectrum could show. And the next shoulder out sits at
−0.12 dB, so +0.0588 dB is inside this receiver's own passband curvature, not
above it.

There is also almost nothing to resolve even in principle. A 4.4 us OFDM symbol
spreads each pilot to a ~227 kHz main lobe against 234.375 kHz subcarrier
spacing, so adjacent pilots merge. With the noise removed entirely the on-pilot
minus between-pilot contrast in 117.2 kHz windows is **+0.39 dB** — a comb
exists, but a 0.39 dB one; on the real capture it is −0.05 dB. Treat the eight
edge pilots as a 1.875 MHz **block** rather than a resolvable comb: the comb is
real and is simply far too shallow to find a signal by, which is a statement
about this geometry and not about noise.

| Quantity | Value | Source |
|---|---:|---|
| Pilot band | 1.875 MHz | `PILOT_BANDWIDTH_HZ` |
| Pilot subcarriers per edge | 8 | `STARLINK_EDGE_PILOT_SUBCARRIERS` |
| Subcarrier spacing | 234.375 kHz | `STARLINK_SUBCARRIER_SPACING_HZ` |
| OFDM symbol duration | 4.4 us, a 227.3 kHz single-symbol lobe | `OFDM_SYMBOL_DURATION_S` |
| Band lift, best of 2,136 real captures | **+0.0588 dB** (1 sigma 0.0040, n = 769 bins) | `edge-pilots.json` |
| Band lift, noiseless reference | **+16.235 dB** | `edge-pilots.json` |
| Next shoulder out, same capture | −0.121 dB | `edge-pilots.json` |
| Comb contrast, noiseless reference | +0.389 dB | `edge-pilots.json` |
| Comb contrast, best real capture | −0.046 dB | `edge-pilots.json` |

![Best real capture and the noiseless reference frame on one axis, showing a flat band where the pilots are](figures/edge-pilots.png)

***Figure 1 — there is no picture of the thing being detected.*** *Panel (a):
capture `sync-20260814T010257Z-pluto-5d4d`, tuning slot 3, `lnb-b`, channel 2
lower edge, 640 ms at 10.00 MS/s — 6,400,000 samples, 4096-point transforms
averaged over 1,562 segments at 2441.4 Hz per bin. It is rank 1 of 2,136 target
observations in that arm by the survey bank's own coarse statistic (1.835,
against that arm's own 1% null bar of 1.132 over n = 1,048 null windows). The
shaded 1.875 MHz pilot span is flat. Panel (b): the same band with
`leo_tracker`'s own noiseless `pilots.edge_pilot_frame` overlaid — a +16.24 dB
block with no internal comb, against the sky's +0.06 dB. Dotted verticals are
the eight published lower-edge pilot subcarriers (528–535). `lnb-a` excluded
from target and null. Every plotted value is in
[`figures/edge-pilots.json`](figures/edge-pilots.json).*

**Takeaway.** Every detection in this report is statistical inference beneath
the noise floor. You cannot look at a capture and see whether a detector was
right, which is exactly why ground truth has to be manufactured — the subject of
the next two sections.

---

## 2. The hard part: nothing was injected

No signal of known amplitude was ever put into either front end at this site.
Without an injection there is no known input, so the obvious way to rank
detectors — count how often each one fires on sky — cannot work, and the
measurement shows why.

Across the eight detectors, how often a detector fires on sky tracks how often
it fires on a **measured** empty-sky null: least squares over the eight points
gives slope 1.99, r = 0.84, **r-squared = 0.70**. Roughly two-thirds of the
between-detector spread in fire rate is just the null rate. That fit is eight
points and it moves with the corpus: recomputed independently at three census
sizes spanning one hour of scoring it ranges r-squared 0.64–0.70, so read it as
"about two-thirds", not as 70.0%. `full-frame-full`
fires most on sky (33.30%) and also most on the null (6.74%); `glrt-32` fires
least on both (30.89% and 5.47%). Firing 8% more often may mean 8% more
sensitive or 8% looser, and the count cannot say which.

| Detector | Fires on sky | Fires on the null, measured `p` | Excess | Rank by count | Rank by excess | Rank by model output `d` |
|---|---:|---:|---:|---:|---:|---:|
| `full-frame-full` | 33.30% | 6.74% | 26.56% | 1 | 2 | 7 |
| `full-frame-acquire` | 33.29% | 6.73% | 26.56% | 2 | 3 | 8 |
| `full-frame-verify` | 33.19% | 6.69% | 26.50% | 3 | 4 | 6 |
| `differential-32` | 33.09% | 6.30% | 26.79% | 4 | 1 | 5 |
| `differential-16` | 32.70% | 6.35% | 26.35% | 5 | 5 | 4 |
| `glrt-64` | 31.75% | 6.10% | 25.64% | 6 | 6 | 2 |
| `anchor-8` | 31.18% | 6.30% | 24.89% | 7 | 8 | 3 |
| `glrt-32` | 30.89% | 5.47% | 25.41% | 8 | 7 | 1 |

*n = 30,168 live target observations and 15,072 live cross-edge null
observations. The last column ranks by the mean of d_A and d_B from
`cross_radio.solve_coincidence` on 18,176 joined matched-arm cells — a model
output, from the model whose consistency check is shown to be incapable of
failing in [section 6](#6-did-it-work-the-negative-controls).*

Two of those three rankings are **measurements**, and they agree with each
other: Spearman rho = **+0.833** between rank-by-count and rank-by-excess.
Correcting for the measured empty-sky rate nudges the order; it does not
overturn it. The one column that overturns it is the model output: rho =
**−0.952** against the fire count and **−0.762** against the excess. `glrt-32`
fires least of the eight and comes out most sensitive.

So the honest statement has two halves, and both matter. **You cannot rank these
detectors by how often they fire.** And **the model that would reorder them has
not earned the right to**, because its own consistency check fails — that is
[section 6](#6-did-it-work-the-negative-controls).

![Fire rate against measured null rate, and three rankings of the same eight detectors](figures/fire-rate-problem.png)

***Figure 2 — a fire count cannot rank detectors when nothing is injected.***
*Left: raw fire rate on sky against the measured empty-sky rate `p`, one point
per detector, bars are 95% marginal binomial intervals; all eight score the same
observations, so the differences are paired and better determined than the bars
alone suggest. The dashed line is least squares over the eight (r = 0.84,
r-squared = 0.70). `anchor-8` is the one clear departure: it false-alarms like
`differential-32` while firing like `glrt-64`. Right: the same eight ranked
three ways on the same observations. Columns one and two are measurements and
agree at rho = +0.833; column three is a model output and sits at rho = −0.952
against column one. Population: 2,514 scored sidecars in 1,257 paired sweeps,
`lnb-a` excluded. Values in
[`figures/fire-rate-problem.json`](figures/fire-rate-problem.json).*

**Takeaway.** With no injection, a fire count measures the threshold, not the
detector. Ground truth has to come from somewhere else, and the only thing this
site has is a second radio.

---

## 3. The apparatus: two radios, one instant

One collector process opens both Plutos and runs one thread per radio, with a
`threading.Barrier` at every tuning — eight tunings per sweep. Both radios
therefore sit on the same tuning at the same instant. IQ is written straight to
local NVMe, copied to the QNAP share in byte-for-byte verified batches, and
`sweep.json` is written last so its presence is the commit marker.

The design rationale is independence: separate LNBs, separate Plutos, separate
USB controllers on separate buses. Different radios fail independently, so
cross-radio agreement can stand in for the injection this site does not have.
The two receivers *inside* one Pluto share an ADC clock and a bus and do not
qualify — a distinction that turns out to matter in
[section 8](#8-what-the-sky-looks-like).

**How well the two radios are actually aligned is not known, and the recorded
number cannot tell you.** `skew_ms` is stamped at **barrier release** — before
the two threads write their different local-oscillator frequencies, and those
writes take different times. Every recorded value is therefore a **lower bound**
on the sample-start offset that matters.

| Quantity | Measured |
|---|---:|
| Paired tunings measured | 9,712, in 1,214 scored pairs |
| Median | 0.0441 ms |
| p90 / p99 / max | 0.0769 / 2.0203 / 8.3409 ms |
| Beyond the 0.054 ms design bound | 2,820 (29.04%), in 1,015 of 1,214 sweeps |
| Median, same-edge sweeps (n = 4,624) | 0.0437 ms |
| Median, opposite-edge sweeps (n = 5,088) | 0.0446 ms |
| Ratio between the two geometries, as recorded | **1.02x** |
| True sample-start offset, per the share README | 0.2–0.8 ms same-order, ~4 ms opposite-order — **~5x** |
| Manifest / scan-share copies of every per-tuning skew that disagree | 0 |

The last three rows are the important ones. The recorded skew is **blind to the
very axis it would be used to stratify on**: barrier-release skew differs
between the two geometries by 1.02x, while the README puts the true offset
between them at about 5x. Any analysis that splits cells on "within the design
bound" against "beyond it" cannot see the effect it means to bound, and no such
split appears in this report.

The provenance of that claim is worth stating plainly too: `leo_tracker`'s own
`synchronised_scan.sweep_skew_event()` **refuses to certify** the basis for this
corpus. It raises on all 1,214 paired sweeps, because the collector wrote
`leo-tracker.interim-synchronised-scan/v1` — a schema that names no `skew.event`
and is not one of the two versions the function recognises. 2,428 paired
manifests carry a `skew_basis` field that says barrier release, and the share
README says so in prose. The claim rests on those, not on code.

![Schematic of the two-thread barrier design, and the measured skew distribution](figures/apparatus.png)

***Figure 3 — the apparatus works, and the number that would prove it measures
the wrong event.*** *Panel (a) is a schematic — no measured data — showing where
`skew_ms` is stamped (barrier release) against where the offset that matters
begins (first sample); the gap between them is never stamped on this build.
Panel (b) is every paired tuning in the scored corpus, log-binned, split by
geometry. It reproduces the authoritative full-corpus run (2,702 of 9,336
tunings, 28.9%, median 0.0440 ms, max 8.3409 ms) on a corpus grown by 47 pairs
since: 2,820 of 9,712, 29.0%, median 0.0441 ms, max 8.3409 ms — the worst tuning
is 154x the design bound. Values in
[`figures/apparatus.json`](figures/apparatus.json).*

**Takeaway.** The apparatus does put two radios on one tuning at one instant and
records it reproducibly. The one number that would say how tightly is measuring
the rendezvous, not the radios, and cannot distinguish the two geometries the
experiment was built to compare.

---

## 4. The dataset: twelve arms, and two geometries for free

Twelve arms cross probe length {80, 160, 640} ms with sample rate
{1.25, 2.5, 5.0, 10.0} MS/s, drawn uniformly per sweep. Both radios take the
same arm with design probability 0.9; the remaining tenth put two different
configurations on the same sky at the same instant.

**The randomisation came out flat.** 6,364 of 7,054 captured sweeps put both
radios on the same arm — 90.2% against a design 0.9. Per-arm counts run 508–575
against a uniform expectation of 530.3; chi-square 6.99 on 11 degrees of
freedom, Monte-Carlo p = **0.80** over 20,000 draws. No arm was starved.

| arm | probe (ms) | rate (MS/s) | sweeps, both radios | solo captures | samples/tuning | IQ bytes per radio | pilot guard (kHz) | pilot band fits | imported pairs | scored pairs |
|---|---:|---:|---:|---:|---:|---:|---:|:--:|---:|---:|
| 80ms-1.25MSps | 80 | 1.25 | 540 | 106 | 100,000 | 6,400,000 | -312.5 | NO | 469 | 103 |
| 80ms-2.50MSps | 80 | 2.5 | 536 | 133 | 200,000 | 12,800,000 | +312.5 | yes | 465 | 87 |
| 80ms-5.00MSps | 80 | 5 | 523 | 105 | 400,000 | 25,600,000 | +1562.5 | yes | 447 | 109 |
| 80ms-10.00MSps | 80 | 10 | 517 | 103 | 800,000 | 51,200,000 | +4062.5 | yes | 451 | 88 |
| 160ms-1.25MSps | 160 | 1.25 | 528 | 109 | 200,000 | 12,800,000 | -312.5 | NO | 460 | 96 |
| 160ms-2.50MSps | 160 | 2.5 | 527 | 118 | 400,000 | 25,600,000 | +312.5 | yes | 475 | 96 |
| 160ms-5.00MSps | 160 | 5 | 575 | 116 | 800,000 | 51,200,000 | +1562.5 | yes | 509 | 106 |
| 160ms-10.00MSps | 160 | 10 | 542 | 121 | 1,600,000 | 102,400,000 | +4062.5 | yes | 477 | 95 |
| 640ms-1.25MSps | 640 | 1.25 | 541 | 121 | 800,000 | 51,200,000 | -312.5 | NO | 460 | 97 |
| 640ms-2.50MSps | 640 | 2.5 | 508 | 124 | 1,600,000 | 102,400,000 | +312.5 | yes | 453 | 73 |
| 640ms-5.00MSps | 640 | 5 | 517 | 107 | 3,200,000 | 204,800,000 | +1562.5 | yes | 447 | 96 |
| 640ms-10.00MSps | 640 | 10 | 510 | 117 | 6,400,000 | 409,600,000 | +4062.5 | yes | 437 | 94 |

One column is handicapped, and it is handicapped by physics rather than by
sampling. A 1.25 MS/s capture is only 1.25 MHz wide, so the 1.875 MHz band the
detectors correlate against cannot fit inside it at any probe length: the pilot
guard is −312.5 kHz and `pilot_band_fits` is false at all three probe lengths.
That column was drawn as often as any other — 540, 528 and 541 sweeps. The
authoritative full-corpus run puts 1.25 MS/s at 80 ms at the bottom of the arm
axis, f = 0.120 on 1,504 cells with only 6 of 8 algorithms solvable, against
f = 0.525 for 10 MS/s at 80 ms, over an arm axis spanning f 0.12–0.525 on 16,864
cells.

![The twelve arms as a grid, with sweep counts, sample budgets and the pilot-band verdict](figures/arm-matrix.png)

***Figure 4 — the draw was flat; one column is beaten by arithmetic.*** *Each
cell carries the sweeps captured with both radios on that arm, the solo captures
beside them, the sample and byte budget per radio, and the pilot-band verdict.
Colour encodes the sweep count only, on a scale spanning the uniform expectation
530.3 ± 3 sigma. The three hatched cells are the 1.25 MS/s column: guard =
rate/2 − 937.5 kHz, negative at that rate at every probe length, and the
collector's own `pilot_band_fits` flag agrees with the recomputed guard at all
four rates. Values in [`figures/arm-matrix.json`](figures/arm-matrix.json).*

**Two geometries came free.** Each radio draws its edge order (`L` or `U`)
independently every sweep, so the pair lands in one of two geometries by chance.
**Same-edge** puts both radios on one tuning at every instant: replication — do
two chains agree about this tuning? **Opposite-edge** splits them across one
channel's two edges at every instant: simultaneity across a channel — was the
whole channel live at this instant? Neither question can be asked of the other's
sweeps, and opposite-edge cannot replicate, because the chains never share a
tuning, so a disagreement there is not evidence that either chain is wrong.

| Population | n | same-edge | opposite-edge | same-edge share |
|---|---:|---:|---:|---:|
| Sweeps captured by the collector | 7,054 | 3,461 | 3,593 | 49.06% |
| Pairs imported to the corpus | 6,161 | 3,002 | 3,159 | 48.73% |
| Pairs scored — the analysable set | 1,261 | 603 | 658 | 47.82% |
| Cross-radio cells, scored | 20,176 | 9,648 | 10,528 | |

The draw really is a coin flip: 49.06% same-edge over 7,054 sweeps, two-sided
Monte-Carlo p = **0.12** against a fair 50% over 20,000 draws. Per-radio
edge-order counts are 3,539 / 3,515 and 3,516 / 3,538. Geometry derived from the
recorded sample orders agrees with the declared `edge_order` letter on **6,161
of 6,161** pairs, with exactly two distinct sample orders present in the corpus,
8 instants per sweep and 2 live receiver pairs per pair.

![Schematics of the two geometries and the counts that land in each](figures/geometry.png)

***Figure 5 — half the sweeps replicate; the other half ask a different
question.*** *Left panels are schematics of the two sample orders that actually
occur in the corpus (verified: there are exactly two). Right panels are the
counts at each stage — captured, imported, scored — with the geometry mixture
unmoved at every stage, and a verification against the authoritative run's
1,167 / 558 / 609 line: same filter, 94 more pairs, same-edge share 47.81% then
and 47.82% now. Values in [`figures/geometry.json`](figures/geometry.json).*

**Takeaway.** The dataset is balanced across arms and geometries by
construction rather than by luck, and the two geometries are the only lever this
corpus has on simultaneity — the recorded skew, from
[section 3](#3-the-apparatus-two-radios-one-instant), is not one.

---

## 5. Ground truth by coincidence: the model

Two chains that share only the sky are the substitute for injection. Let a sky
cell — one channel edge at one instant — be occupied with probability `f`. Chain
A detects an occupied cell with probability d_A, chain B with d_B, and either
chain still fires on an *empty* cell with probability `p`. Then:

```
P(A)  = f * d_A + (1 - f) * p
P(B)  = f * d_B + (1 - f) * p
P(AB) = f * d_A * d_B + (1 - f) * p^2
```

`P(A)`, `P(B)` and `P(AB)` are **counted** on the corpus. `p` is **measured**,
per cell, on the cross-edge null arm. Three equations, three unknowns, and out
come `f`, d_A and d_B: a detection probability with no known input anywhere.
That is the substitute for the injection this corpus never had — a substitute,
not an equal. Injection measures `d` against a signal you put there; this
**infers** `d` from an assumption that the two chains fail independently. If
that assumption is wrong, so is every `d` beside it.

The model carries its own consistency check, and it is sharp: **`f` is a
property of the sky.** All eight detectors read the same sky at the same
instant, so all eight must return the same `f`. The spread across the eight is
the check.

| Detector | measured `p` | `f` | d_A | d_B |
|---|---:|---:|---:|---:|
| `anchor-8` | 6.30% | 0.3388 | 0.8135 | 0.7609 |
| `glrt-32` | 5.47% | 0.3391 | 0.8208 | 0.7681 |
| `glrt-64` | 6.10% | 0.3470 | 0.8185 | 0.7616 |
| `differential-16` | 6.35% | 0.3659 | 0.8037 | 0.7428 |
| `differential-32` | 6.30% | 0.3733 | 0.8009 | 0.7402 |
| `full-frame-verify` | 6.69% | 0.3745 | 0.7938 | 0.7363 |
| `full-frame-full` | 6.74% | 0.3781 | 0.7888 | 0.7334 |
| `full-frame-acquire` | 6.73% | 0.3788 | 0.7831 | 0.7376 |
| **range over the eight** | **5.47–6.74%** | **0.3388–0.3788, spread 0.0400** | **0.783–0.821** | **0.733–0.768** |

*18,176 joined matched-arm cells; `p` measured per detector on 15,072 live
cross-edge null observations; `lnb-a` excluded from target and null. **Every d_A
and d_B in this table is a model output, never checked against a known input.**
The model's own consistency check is
[section 6](#6-did-it-work-the-negative-controls), and it fails.*

Two things are worth reading off that table before going further.

**The empty-sky rate is measured, and it is not the nominal 1%.** It is
5.47–6.74% across the eight, drawn from the cross-edge null arms. Every `d`
above depends on that denominator; assuming the nominal value instead pushes
methods out of physical range for reasons that have nothing to do with the
methods.

**The one sky parameter does not come out as one number.** Pooled, the eight
return `f` 0.3388–0.3788, a spread of 0.0400. That is the model's own check,
already unmet. And the extremes move with geometry rather than sitting on one
misbehaving algorithm: on opposite-edge cells the minimum is `glrt-32` at 0.3442
(spread 0.0429 over 9,568 cells); on same-edge cells the minimum is `anchor-8`
at 0.3301 (spread 0.0408 over 8,608 cells). Near-identical spread, different
argument minimum. The invariance failure is a property of the estimate, not of
one algorithm.

The solver reproduces the authoritative full-corpus run closely on the
opposite-edge cells: `anchor-8` f 0.3467 against 0.346 reported, `glrt-32`
0.3442 against 0.344, `full-frame-full` 0.3871 against 0.388 — every `f` within
0.001.

![The coincidence model as a schematic, and the f, dA and dB it returns per detector](figures/coincidence-model.png)

***Figure 6 — three equations recover `d`, if the one sky they assume is really
there.*** *Left panel is a schematic — no data — of the model and of what is
counted, measured and solved. Right panel is the solution per detector, split by
geometry (filled = opposite-edge, open = same-edge): sky occupancy `f` in the
narrow band on the left, and d_A / d_B at 0.73–0.82 on the right. Every point on
the right half of that panel is a model output. n = 18,176 joined matched-arm
cells (9,568 opposite-edge, 8,608 same-edge) from 1,136 matched-arm sweeps of
1,257 paired. Values in
[`figures/coincidence-model.json`](figures/coincidence-model.json).*

**Takeaway.** The model is solvable and returns physically plausible numbers.
Whether it is *entitled* to is a separate question, and it has a direct
experimental answer.

---

## 6. Did it work? The negative controls

This is the test that decides the report, and it is cheap: build joins where the
coincidence model is **definitionally false**, run the identical estimator, and
see whether the consistency check notices.

Two controls, alongside the real join:

- **Shifted** — radio B taken two instants later within the same sweep. The two
  sides are then on different tunings, so there is no shared sky cell and the
  model cannot hold.
- **Scrambled** — radio A of one sweep joined to radio B of a *different* sweep,
  matched on arm and geometry, with only the pairing broken. Partners are a
  median 4,847 s apart (minimum 3,651 s, maximum 7,130 s), so the two sides
  never saw the sky at the same time.

Thresholds and the empty-sky rate `p` are drawn **once** from the cross-edge
null arms and held fixed across all three joins, so the join is the only thing
that changes.

| Join | Model can hold? | Cells | `f` range | mean `f` | Spread across the eight | Resample p05–p95 | Spread over its own noise median |
|---|---|---:|---|---:|---:|---|---:|
| **Real** — radio A and radio B of one sweep, same instant | yes | 16,560 | 0.334–0.376 | 0.358 | **0.042** | 0.039–0.049 | 0.97x |
| **Shifted** — radio B at instant *i*+2 | **no** | 16,560 | 0.657–0.697 | 0.679 | **0.040** | 0.031–0.053 | 0.97x |
| **Scrambled** — radio A joined to another sweep | **no** | 16,560 | 0.782–0.818 | 0.798 | **0.036** | 0.025–0.057 | 0.95x |

Read the three spread columns. The quantity offered as validation — the
tightness of the eight algorithms' agreement on `f` — is **smallest on the join
where the model is most obviously false**. Every join's observed spread sits at or below
its own sampling-noise median. There is no failure mode: whatever you feed this
check, it passes.

The result is not that the estimator returns the same answer three times. `f`
itself moves **3x** across the joins, 0.358 to 0.679 to 0.798, so these really
are different data and the estimator really is responding to them. What does not
move is the thing being used as evidence.

![Sky occupancy f across three joins, and each join's spread against its own sampling noise](figures/negative-control.png)

***Figure 7 — the consistency check cannot fail.*** *Same estimator, same 16,560
matched-arm cells in each of the three joins, same thresholds and same empty-sky
rate; only the pairing changes. Left: `f` moves 3x across the joins while all
eight algorithms move together, so every cluster stays tight — including on the
two joins the model forbids. Right: each join's observed spread against the
p05–p95 band of 200 joint resamples of that same join, with the noise median
marked; all three land at or below their own noise median, the scrambled join
furthest below at 0.95x. Population: 1,035 matched-arm paired sweeps, 2,292
scored sidecars in a pair. Estimator
`leo_tracker.radio.beacon.cross_radio`, unmodified. Values in
[`figures/negative-control.json`](figures/negative-control.json).*

**Takeaway.** Cross-detector agreement on `f` is not evidence for the
coincidence model, and the cross-radio apparatus has not been shown to deliver
the ground truth it was built to deliver. Every `d` anywhere in this report is a
model output from this model. Most detector comparisons never run this control.
This one did, and it came back negative — that is the contribution.

---

## 7. Why it failed: the detectors are near-duplicates

The negative control is not a surprise once you look at the detectors
themselves. Over **30,192** live target observations — every observation
carrying all eight verdicts, none missing, `lnb-a` excluded — every pair of the
eight makes the same fire / no-fire call at phi **0.847–0.945**.

| Pair | phi |
|---|---:|
| Loosest pair anywhere in the matrix: `anchor-8` / `differential-16` | 0.847 |
| Tightest pair anywhere: `full-frame-full` / `full-frame-verify` | 0.945 |
| `glrt-32` / `glrt-64` | 0.936 |
| `differential-16` / `differential-32` | 0.917 |
| Mean over all 28 pairs | 0.888 |
| Band previously reported, on 2,160 observations | 0.82–0.94 |
| Band here, on 30,192 observations (14.0x) | **0.847–0.945** |

Eight independent opinions would sit near phi = 0. At 14x the observations the
band is **higher at both ends** than previously reported, so this is not a
small-sample artefact being sanded down — it firms up. Grouping by family barely
matters: the same-family blocks hold the tightest pairs, but no pair anywhere in
the matrix falls below 0.85.

That is the mechanism behind
[section 6](#6-did-it-work-the-negative-controls). Near-duplicate detectors are
**obliged** to return near-identical `f` whatever they are fed, including inputs
where the model being validated is known to be false. The consistency check was
never testing the model; it was testing whether eight copies of one statistic
agree with each other, and they do.

![Pairwise phi between the eight detectors on the same observations](figures/algorithm-correlation.png)

***Figure 8 — not eight opinions but one, counted eight times.*** *phi between
every pair of the eight confirmers on the same 30,192 live target observations
(1,258 paired sweeps, 2,516 scored sidecars). An observation enters only if all
eight detectors returned a verdict, so every cell rests on one identical
population. Each detector is judged against the threshold drawn for its own
sample rate and probe length from the cross-edge null arms. Row order maximises
adjacent phi over all 8! = 40,320 orderings, which is why the outlined
same-family blocks land on the diagonal. Values in
[`figures/algorithm-correlation.json`](figures/algorithm-correlation.json).*

**Takeaway.** "Which of these eight detectors is best" is not answerable at this
corpus size with this bank, and agreement among them carries no information
about the sky. Either add a genuinely different statistic, or report one
detector and its null.

---

## 8. What the sky looks like

Two independent readings of the same corpus. Neither depends on the coincidence
model; both use only counted fire / no-fire decisions, so both survive
everything above.

### 8a. Channels are not independent, but the structure is not spectral

Take one receiver chain's pass over one sweep as the unit of observation — it
visits all eight tunings (4 channels x 2 edges) exactly once. There are 3,774
such units, identical in every cell of the matrix.

- A channel's **own two edges** agree at phi **0.441–0.466**, mean **0.453**.
- Any **two different channels** agree at phi **0.072–0.161**, mean **0.118** —
  3.8x weaker, but not zero.
- And the cross-channel term **does not fade with separation**: phi 0.103 at
  channel distance 1, 0.130 at distance 2, 0.141 at distance 3. If anything it
  rises.

The thin margin is worth stating rather than rounding away. The weakest
cross-channel cell is phi 0.072 against a shuffled null whose 99th percentile of
abs(phi) is 0.062. Every cross-channel cell clears the null, but the weakest clears
it by 0.010. The null shuffles each tuning column independently across units,
destroying every between-tuning association while preserving each tuning's own
fire rate; its mean abs(phi) is 0.013 over 200 draws.

The pattern is consistent across all three live receivers: same-channel means
0.425 (`lnb-c`), 0.461 (`lnb-b`), 0.468 (`lnb-d`); cross-channel means 0.102,
0.115, 0.121.

A satellite lighting one channel would produce a cross-channel term that decays
with frequency separation. A flat one that does not decay is a **sweep-wide
common mode** sitting under every tuning.

![Phi between all eight tunings, and the cross-channel term against channel separation](figures/channel-edge-correlation.png)

***Figure 9 — the only strong structure is a channel's own two edges.*** *phi
between every pair of the eight tunings, on 3,774 units in every cell (1,258
paired sweeps x 3 live receivers). Axis order was declared in advance — ch1
lower, ch1 upper, ch2 lower, and so on — so a channel's own edges are adjacent;
the four outlined diagonal blocks are a finding, not a layout. Fire / no-fire is
`cross_radio._any_method_fires`, each of the eight judged against its own
(sample rate, probe length) threshold. Lower panel: the cross-channel term
against channel-number distance, with the shuffled null's 99th percentile of
abs(phi) shaded. Values in
[`figures/channel-edge-correlation.json`](figures/channel-edge-correlation.json).*

### 8b. What costs agreement is the receiver, not the timing and not the edge

The obvious reading of "two chains agree" is that simultaneity matters.
Decompose it one factor at a time and it does not. Four rungs, each changing
exactly one thing relative to its neighbour:

| Rung | What is different | n pairs | sweeps | mean phi across the eight | bootstrap 95% CI |
|---|---|---:|---:|---:|---|
| Same receiver, both edges of one channel | one dwell apart in its own scan order: **a time gap, one LNB, one clock** | 15,096 | 1,258 | **0.605** | 0.590–0.620 |
| Two receivers, one radio, same tuning | **different LNB**, shared clock and bus, simultaneous | 10,064 | 1,258 | **0.518** | 0.502–0.535 |
| Two radios, **same** edge, same tuning | **plus the radio boundary**: two clocks, two buses | 9,632 | 602 | **0.523** | 0.497–0.546 |
| Two radios, **opposite** edges | **plus the edge**: both edges of one channel at one instant | 10,496 | 656 | **0.523** | 0.500–0.546 |

The contrasts between adjacent rungs are the result:

| Contrast | What it isolates | delta phi | 95% CI | Crosses zero? |
|---|---|---:|---|:--:|
| opposite-edge minus same-edge, two radios | **the edge alone**, at fixed hardware and zero time gap | **−0.0000** | −0.035 … +0.037 | yes |
| two radios same-edge minus two receivers one radio | **the radio boundary**, at a fixed edge | **+0.0046** | −0.021 … +0.030 | yes |
| two receivers one radio minus same receiver both edges | **changing the LNB**, with the time gap removed | **−0.0868** | −0.101 … −0.070 | **no** |

*Bootstrap over paired sweeps, 400 draws — not over cells, because a sweep's
eight tunings and two receiver pairs are not independent draws.*

**Receiver-to-receiver variation is the entire effect. Timing is free and the
edge is free.** Removing the scan gap makes agreement *worse* — 0.605 down to
0.523 — and the reason is not that simultaneity hurts; it is that you had to
change LNBs to get simultaneity, and changing LNBs costs 0.087 on its own with
the gap already removed. The ordering is the same in all four channels — channel
by channel, 0.612 / 0.618 / 0.610 / 0.581 for the same-receiver rung against
0.510 / 0.549 / 0.525 / 0.504 for two radios on opposite edges.

One asymmetry in that ladder is worth flagging: only `pluto-19f2` can supply the
second rung, because the other port of `pluto-5d4d` is the dead `lnb-a`. That
rung is therefore one radio's pair of LNBs rather than both radios', which is
why its sweep count matches the first rung's while its pair count does not.

The two readings in this section cross-check each other. Under the any-method
fire rule, the top rung's per-channel agreement is phi 0.448 / 0.466 / 0.459 /
0.441 — the same four numbers as the outlined diagonal blocks in Figure 9, from
a different script and a different unit of observation.

![Four rungs of agreement, isolating the gap, the receiver, the radio boundary and the edge](figures/edge-agreement.png)

***Figure 10 — the edge costs nothing; the LNB costs everything.*** *Upper
panel: the four rungs, with one open circle per algorithm, a bar for the
bootstrap 95% CI over paired sweeps, and a heavy rule at the mean phi across the
eight. The bracketed contrasts on the right are the three isolations tabled
above. Lower panel: the same two extreme cases per channel, with the pooled
values as dashed lines; every channel orders them the same way. Census frozen at
2,547 scored sidecars, 1,258 paired sweeps, `lnb-a` excluded. Values in
[`figures/edge-agreement.json`](figures/edge-agreement.json).*

**Takeaway.** Whatever these detectors are responding to is a **per-receiver
property first**. That is the same conclusion sections 6 and 7 reach from a
different direction, and it is why what survives from this corpus is
instrumental.

---

## 9. What survives: the instrument

Two findings that do not depend on the coincidence model, do not depend on the
detector bank being independent, and are directly actionable.

### 9a. A rate-independent ~350 kHz offset tolerance, and it is not the guard band

The pipeline computes a bias-corrected frequency offset for every candidate.
Binned on that axis, `differential-32`'s detection rate collapses in the
**350–400 kHz bin at every sample rate whose pilot band fits**:

| MS/s | 0–50 | 50–100 | 100–150 | 150–200 | 200–250 | 250–300 | 300–350 | **350–400** | 400–500 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2.5 | 9.4 | 25.8 | 36.0 | 50.4 | 38.7 | 33.1 | 35.4 | **2.6** | 1.3 |
| 5.0 | 19.1 | 37.5 | 48.8 | 60.5 | 39.4 | 25.5 | 17.5 | **2.1** | 1.4 |
| 10.0 | 23.8 | 43.6 | 52.2 | 63.8 | 41.7 | 25.2 | 14.6 | **2.0** | 1.1 |

*Detection percentage by bias-corrected offset bin; n per bin runs 616–9,057.
178,399 live target points plotted from 363,004 candidate points read, over
1,146 paired sweeps.*

The pilot guard bands at those three rates are +312.5, +1,562.5 and
+4,062.5 kHz — a **13x range** — and the cliff does not move. That rules the
guard band out. What is left is a rate-independent ~350 kHz offset tolerance
whose mechanism is open: the survey scorer's own bank spans ±700 kHz, and the
±350 kHz constant elsewhere in the codebase belongs to the acquisition path,
which this scoring path does not use.

The cliff is robust to disaggregation. All nine (rate, probe length) cells
collapse in the same bin — 30.2% to 4.3% at 2.5 MS/s / 80 ms, down to 9.7% to
1.9% at 10 MS/s / 640 ms — so it is not an artefact of pooling probe lengths
inside a rate.

Two limits belong with it, neither visible in the figure:

- **The 2.5 MS/s cliff point is one port.** Its 350–400 kHz bin holds n = 616,
  of which 552 (90%) are `lnb-c`. The concentration is worse at the full corpus
  than the 86% found at review, so that row alone would not carry the argument.
  5.0 MS/s (844 / 530 / 641 for b / c / d) and 10 MS/s (753 / 298 / 573) carry
  all three ports through the cliff and all three fall.
- **The guard is a raw-axis quantity plotted beside a corrected axis.** On the
  raw axis at 2.5 MS/s there is no cliff at all: the rate *rises* through its own
  guard — 15.5% (250–300 kHz), 32.5% (300–350), 62.9% (350–400), 77.3%
  (400–500), 95.1% (500–600). The corrected axis is the right one for asking
  about detection; the guard does not live on it, and in the figure it is an
  axis-anchored tick rather than a bound on the plotted quantity.

![Detection rate against bias-corrected offset for three sample rates](figures/cfo-cliff.png)

***Figure 11 — the cliff does not move when the guard moves 13x.***
*`differential-32`, fired against its own (sample rate, probe length)
cross-edge-null 1% threshold and binned on the pipeline's bias-corrected offset.
The three guards span 13x, and two of them are never reached at all — the
largest corrected offset anywhere in the corpus is 1,417.4 kHz, well inside
both. Lower panel is n per bin. Values in
[`figures/cfo-cliff.json`](figures/cfo-cliff.json).*

### 9b. One port carries a large LO offset and is the best receiver on site

`lnb-c` has `receiver_centers_hz` = **+604,159.8 Hz**, applied at scoring time.
That is well past the ~350 kHz tolerance above, which is enough to make a
healthy port look dead. On the raw axis at 5 MS/s it reads 1.5% (n = 2,050) in
the 100–200 kHz bin where the other two ports peak, because its entire response
has moved out to 400–500 kHz (61.0%, n = 3,619).

Bias-corrected, it is the strongest port on site:

| Port | 5 MS/s, corrected offset 100–200 kHz | n |
|---|---:|---:|
| `lnb-c` | **65.9%** | 3,352 |
| `lnb-d` | 51.3% | 4,817 |
| `lnb-b` | 43.8% | 4,791 |

`lnb-b` and `lnb-d` carry zero bias, so their raw and corrected panels are
identical by construction and only `lnb-c` moves. The 1.5x port difference
survives the correction rather than being created by it.

![Detection against raw and bias-corrected offset for the three live ports](figures/port-bias.png)

***Figure 12 — `lnb-c` is shifted, not deaf.*** *5 MS/s, `differential-32`,
1,146 paired sweeps, `lnb-a` excluded. Values in
[`figures/port-bias.json`](figures/port-bias.json).*

***Audit note, recorded and unresolved.*** *`lnb-a` is excluded throughout
because `cross_radio.DEAD_RECEIVERS` records it as flat ~1.19 at every tuning
since 2026-08-13 04:44 UTC. Scored on `differential-32`, this corpus does not
reproduce that: `lnb-a` fires on 15.52% of its 60,095 target points against
`lnb-b`'s 17.98% of 59,604, and its cross-edge null is not silence — median
0.0180 and p99 0.0814, against `lnb-b`'s 0.0187 and 0.0854. The exclusion is
applied everywhere in this report regardless; the disagreement belongs in the
record.*

### The surviving findings, in one place

| Finding | The number it rests on | Status |
|---|---|---|
| A rate-independent ~350 kHz offset tolerance | collapse in the 350–400 kHz bin at 2.5, 5.0 and 10 MS/s, in all nine (rate, probe) cells, while the guard moves 13x | **stands**; mechanism open |
| `lnb-c` carries a +604.2 kHz LO offset and is the best port once corrected | `receiver_centers_hz` = +604,159.8, applied at scoring; 65.9% (n = 3,352) against `lnb-b` 43.8% (n = 4,791) | **stands** |
| The 1.25 MS/s arm cannot work at any probe length | a 1.875 MHz band in a 1.25 MHz capture; guard −312.5 kHz, `pilot_band_fits` false; f 0.120 on 1,504 cells against 0.525 | **stands** |
| The detector bank is one statistic counted eight times | phi 0.847–0.945 over 30,192 observations | **stands** |
| The measured empty-sky rate is ~6%, not the nominal 1% | 5.47–6.74% across the eight, on 15,072 null observations | **stands** |
| Recorded skew is a lower bound and is blind to geometry | barrier-release stamp; 0.0437 against 0.0446 ms across geometries whose true offsets differ ~5x | **stands** |
| `lnb-a` is excluded as a dead port | `DEAD_RECEIVERS`; not reproduced on `differential-32` (15.52% of 60,095 against `lnb-b`'s 17.98% of 59,604) | applied, **unexplained** |
| Cross-radio beats within-radio | not shown: the radio boundary costs phi +0.0046, CI −0.021…+0.030 | **not established** |

**Takeaway.** The instrument findings are the practical payoff of this corpus:
correct the `lnb-c` bias or widen the offset search past 350 kHz, and stop
pooling the 1.25 MS/s arm with arms whose pilot band fits.

---

## 10. What real ground truth would take

The single sentence: **every limitation above traces back to having no known
input.** One injected tone of known amplitude at a known offset converts the
whole corpus from model output to measurement, and it is the only item on this
list that changes what the apparatus is capable of proving.

Short of that, in order of cost:

| Step | Cost | What it buys |
|---|---|---|
| **Score the rest of the pairable corpus** | none — no new collection, no new code | every interval in this report tightens; ~5x more data, already on disk |
| Fix the skew stamp: retune before the barrier, bump the schema | one collector change | makes `skew_ms` the sample-start offset, and lets `sweep_skew_event()` certify it |
| Derive the skew bound the coincidence model actually needs | analysis only | 0.054 ms and the README's 0.2–0.5 s operator target cannot both be the requirement |
| Measure the `gen2` LO offsets with `lo_sweep` | one sweep | turns +604.2 kHz from one number into a measurement with an error bar |
| Widen the offset search past 350 kHz and re-score one arm | one arm | if the cliff moves, the search span explains it; if not, that explanation is wrong |
| Add a genuinely different statistic to the bank | development | at phi 0.847–0.945 the eight are one detector; agreement among them is not information |
| **Inject a known signal** | hardware | replaces every model-output `d` in this report with a measurement |

**The cheapest item is first, and it is very cheap.** 7,054 sweeps were
captured; 893 of them lost `pluto-5d4d` entirely to a collector fault, leaving
**6,161 pairable** sweeps. That loss is not geometry-selective — 459 same-edge
against 434 opposite-edge — so it does not bias the comparison. But only
**1,261** of those 6,161 pairs are scored. **There is roughly 5x more pairable
data already captured than has been analysed**, sitting on the share, needing no
radio time, and scoring has no timer driving it. Every number in this report was
computed on about a fifth of the available pairs; the counts at each stage are
in [Figure 5](#4-the-dataset-twelve-arms-and-two-geometries-for-free).

**And a definition of success, so the next attempt is checkable.** A working
test of the coincidence model is one where a join the model forbids produces a
*visibly worse* consistency check than the real join. Today it produces a better
one ([Figure 7](#6-did-it-work-the-negative-controls)). Until a negative control
fails, cross-radio agreement is not evidence, and no ranking of these eight
detectors — by fire count, by excess over the measured null, or by model output
`d` — should be reported as a result.

---

## Figures and provenance

Every figure is computed from the read-only corpus at
`/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/` and the read-only scan share
at `/mnt/qnap01/mouse9911/leo-scans/`. No value in any of them is typed in by
hand. Each PNG ships with the script that produced it and a JSON sidecar holding
every value it plots, so any number here can be re-derived — or contradicted —
without re-running anything.

| # | Figure | Sidecar | Frozen at | Population behind it |
|---:|---|---|---:|---|
| 1 | [`edge-pilots.png`](figures/edge-pilots.png) | [`edge-pilots.json`](figures/edge-pilots.json) | 2,462 | one capture, ranked over 2,136 target observations in the 640 ms / 10 MS/s arm |
| 2 | [`fire-rate-problem.png`](figures/fire-rate-problem.png) | [`fire-rate-problem.json`](figures/fire-rate-problem.json) | 2,544 | 30,168 target and 15,072 null observations; 1,257 paired sweeps |
| 3 | [`apparatus.png`](figures/apparatus.png) | [`apparatus.json`](figures/apparatus.json) | 2,462 | 9,712 paired tunings in 1,214 scored pairs |
| 4 | [`arm-matrix.png`](figures/arm-matrix.png) | [`arm-matrix.json`](figures/arm-matrix.json) | 2,554 | 7,054 captured sweeps, 6,364 matched-arm |
| 5 | [`geometry.png`](figures/geometry.png) | [`geometry.json`](figures/geometry.json) | 2,554 | 7,054 captured, 6,161 imported pairs, 1,261 scored pairs |
| 6 | [`coincidence-model.png`](figures/coincidence-model.png) | [`coincidence-model.json`](figures/coincidence-model.json) | 2,544 | 18,176 joined matched-arm cells from 1,136 matched-arm sweeps |
| 7 | [`negative-control.png`](figures/negative-control.png) | [`negative-control.json`](figures/negative-control.json) | 2,339 | 16,560 matched-arm cells in each of three joins; 1,035 matched-arm sweeps |
| 8 | [`algorithm-correlation.png`](figures/algorithm-correlation.png) | [`algorithm-correlation.json`](figures/algorithm-correlation.json) | 2,547 | 30,192 live target observations; 1,258 paired sweeps |
| 9 | [`channel-edge-correlation.png`](figures/channel-edge-correlation.png) | [`channel-edge-correlation.json`](figures/channel-edge-correlation.json) | 2,547 | 3,774 receiver-chain passes (1,258 pairs x 3 live receivers) |
| 10 | [`edge-agreement.png`](figures/edge-agreement.png) | [`edge-agreement.json`](figures/edge-agreement.json) | 2,547 | 15,096 / 10,064 / 9,632 / 10,496 pairs across four rungs |
| 11 | [`cfo-cliff.png`](figures/cfo-cliff.png) | [`cfo-cliff.json`](figures/cfo-cliff.json) | 2,339 | 178,399 live target points from 1,146 paired sweeps |
| 12 | [`port-bias.png`](figures/port-bias.png) | [`port-bias.json`](figures/port-bias.json) | 2,339 | the same 1,146 paired sweeps, 5 MS/s only |

Figures 1–6 and 8–10 are new. Figures 7, 11 and 12 are carried unchanged from
[the detailed record](../sync-scan-cross-radio-2026-08-14/REPORT.md) together
with their scripts and sidecars, which is why they carry an earlier freeze.

All twelve scripts, their sidecars and the extractors that feed them are in
[`figures/`](figures/). Each figure runs in two steps — an extractor that
streams the corpus into a compact local cache, because the capture host has 4 GB
of RAM, then the figure script itself. The extractors are prefixed by the group
they belong to: `opening-pipeline-*` feeds Figures 1 and 3,
`firerate-pipeline-*` feeds Figures 2 and 6, `heatmaps-pipeline-*` feeds Figures
8 to 10, and `carried-pipeline-*` feeds Figures 7, 11 and 12. Figures 4 and 5
build their own cache on first run. The scripts import the repository's own
estimator from `/home/satpi01/leo-tracker/src`; change that path if the checkout
moves. The caches they build are not committed — they are large, and each is one
command to rebuild.

The census used by each figure is frozen by a snapshot step *before* the figure
computes and re-measured afterwards; both readings, and the list of sidecars
that landed in between, are recorded in each sidecar. Across every run behind
this report the recheck showed **0 sweeps added and 0 sidecars removed** — only
scoring advanced. Collection stayed paused throughout, the collector, drain and
import services were neither stopped nor restarted, the radios were not touched,
and all share and NVMe paths were read only.

Raw sweeps and the format README are at `/mnt/qnap01/mouse9911/leo-scans`;
corpus entries at `/mnt/qnap01/mouse9911/leo/surveys/corpus`; collector, drain
and import implementations at `/mnt/leo-nvme/leo-tracker/bin/`. The
authoritative full-corpus estimator run that this report checks itself against
is preserved beside the detailed record as
[`review-full-corpus.txt`](../sync-scan-cross-radio-2026-08-14/review-full-corpus.txt).
