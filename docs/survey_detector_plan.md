# Survey detector: a staged plan

Revision 3. Revision 1 proposed building a full-frame coherent detector, which
already existed. Revision 2 fixed that but got the evaluation methodology wrong
in ways that would have produced pretty ROC curves and false conclusions. This
revision fixes the methodology, which is now the part most likely to waste a
month if it is wrong.

Status: proposed. Nothing here changes the capture host.

---

## The two findings the plan is built on

**The repository already contains a full-frame coherent acquisition detector.**
`matched_pilot_score` and `matched_pilot_control_scores` (pilots.py:349, 377)
FFT-correlate the entire 3333-sample replica — all 300 symbols x 8 subcarriers —
across *every* epoch, normalised by rolling energy, with a 17-symbol-rolled
negative control batched alongside.

**It is driven far too coarsely.** `coherent_grid_v1` feeds it a 25 kHz CFO grid
(acquisition.py:99-100) while a full-frame coherent correlation nulls at 750 Hz:
measured |corr| = 1.000 / 0.644 / 0.012 at 0 / 375 / 750 Hz of error. On a 25 kHz
grid the true CFO lands usefully about 3% of the time. This is *not* live —
both the watch script and kalman's analysis server pass `pilot_symbolwise_v3` —
but it is the CLI default (cli.py:2188, 2403) and `recovery.py`'s default, so it
is a trap set for any replay, backfill or recovery run.

Measured losses in the deployed survey statistic:

| loss | measured | note |
|---|---:|---|
| 3-point bank at 150 kHz residual | **8.8 dB** | worst case *inside* the searched span |
| 3-point bank at a 436 kHz offset | 7.16 dB | ~87% of it search coverage |
| half-sample epoch straddle | 1.62 dB | never corrected |
| anti-alias filter deleting a subcarrier at 436 kHz | 0.93 dB | the real Nyquist cost |
| Qin's measured channel response across the 8 pilots | 0.0-0.4 dB | negligible |
| aliasing itself | 0.02 dB | rotation and aliasing commute when sampled |

---

## What the first experiment established

Run against 122 recorded probes preserved from live capture. The four probes the
cheap survey scored highest, and the four it scored lowest, swept with
`matched_pilot_control_scores` at 375 Hz across ±200 kHz:

| | cheap p/med | anchors | coherent exact | 17-roll control | margin | CFO found |
|---|---:|---:|---:|---:|---:|---:|
| strongest 4 | 2.17-2.37 | 3-5 | 0.464-0.511 | 0.434-0.478 | 0.016-0.033 | -28 / +77 / +21 / +28 kHz |
| weakest 4 | 1.10-1.11 | 0 | 0.080-0.084 | 0.078-0.081 | 0.0007-0.0047 | -116 / -69 / -70 / +174 kHz |

**Coherent detection works on our data.** Mean score 0.486 against 0.082 — a
factor of 6, with no overlap between the groups. An independent full-frame
coherent detector confirms the cheap survey's ranking, which the survey's own
statistic could not establish about itself.

**It is about a thousand times too slow.** 89 ms per CFO offset; a ±200 kHz
sweep at 375 Hz costs 58-71 s per probe per tuning per receiver, so ~17 minutes
for one sweep of 8 tunings x 2 receivers. This is what makes the epoch shortlist
the centrepiece rather than an optimisation.

**Bank spacing is demoted.** The recovered CFOs cluster from -28 to +77 kHz,
where the 3-point bank costs <=1.8 dB — not the 8.8 dB worst case, which needs a
CFO near ±150 kHz. **8.8 dB is a worst case, not an expected gain.** Four samples
is not a conclusion, which is why measuring the CFO distribution now precedes
acting on it.

**The 17-roll control is not an independent null under an epoch search.** It
correlates about 0.91 with the exact template at a 17-symbol shift, so when
signal is present both score high and the margin stays small. The absolute score
is the discriminator; the margin only evidences code-specific structure.

---

## Stage 0 — Corpus and truth

**The only urgent item.** Survey IQ is written inside the capture directory and
both reclamation paths `rmtree` it. Measured: 88 probes on shared storage, oldest
3.0 hours, and `apply_retention` keeps 6 negative / 8 confirmed. **Every probe we
collect is deleted within hours.** Every experiment below depends on a corpus
that does not yet exist.

### Sampling

A `run_stage` entry in `scripts/starlink-analysis-server.sh` copies probes into
`surveys/corpus/`, recording the reason:

| stratum | selected by | bias |
|---|---|---|
| `strong` | the current detector fired hard | **biased toward what we already see** — kept because confident positives are scarce, labelled so it can be corrected |
| `predicted` | TLE says a catalogued Starlink was in view | independent of our detector |
| `random` | 1-in-N | none |

Collect across the **whole elevation range**, not above a cutoff. Qin does state
that "Starlink SVs typically do not transmit below an elevation of ~40°, which
limits |β| to below ~15 ppm" (p.6) — the citation is sound and it is why our
Doppler search need not exceed about ±172 kHz. But "typically" is a statement
about behaviour that can change, and gating collection on it would bake the
assumption into the evidence. Discover the working cutoff empirically instead.

At 12.8 MB per capture a 5,000-capture corpus is ~64 GB against 1.2 TB free.
Frozen and versioned.

### Three levels of truth, never conflated

Revision 2 treated "a catalogued satellite was predicted nearby" as the positive
class. That is circular: a satellite can be geometrically present and simply not
transmitting, and **frame occupancy is one of the things we are trying to
measure.** Optimising a detector against that label teaches it to find signals
that were never sent.

| level | what it is | what it can support |
|---|---|---|
| **geometry prior** | TLE+SGP4 predicted satellite and Doppler | enrichment, not detection probability |
| **verified positive** | exact pilot beats the 17-roll control, *and* lands near a predicted Doppler, *and* corroborates — other receiver, other edge, or continuation along the expected Doppler trajectory | a high-purity positive set |
| **synthetic injection** | the exact replica injected into *real captured noise* at known epoch, CFO, SNR and frame occupancy | Pd at fixed Pfa — the only hard number |

Report **injection Pd at fixed Pfa** as the headline, and **enrichment near
predictions** alongside it. Never call enrichment an absolute detection rate.

### False-alarm rate is defined at the whole-search level

Once a detector maximises over epochs, CFO hypotheses, frames, receivers and
channels, the extreme-value distribution changes whenever any of those dimensions
changes. The operative quantity is

```
P_FA,probe = P( max over (tau, f, frame, receiver) S  >  gamma  |  H0 )
```

This matters most for the very comparison Stage 2 wants to make: `max` over
frames will look better under signal **and** produce a higher noise maximum.
Comparing raw scores would flatter it. Every architecture gets its own threshold
calibrated on noise-only probes at its own search dimensionality.

### Split by pass, not by probe

Probes from one satellite pass — or one 15-second fixed assignment interval, over
which Qin notes the SV-to-cell beam assignment is constant — are strongly
related. A random probe-level split leaks siblings into validation and produces
flattering curves. Version metadata as `capture -> sweep -> pass -> FAI ->
satellite candidate`, and hold out **whole observation sessions**.

---

## Stage 1 — Characterise what exists

1. CFO response of `matched_pilot_score`; confirm the 750 Hz null on real probes.
2. Epoch response, including the half-sample straddle.
3. Cost as a function of CFO grid density — the only parameter that governs
   affordability.
4. Fix the `coherent_grid_v1` default so no replay silently runs a coherent
   matched filter on a 33x-too-coarse grid.

### The phase-ramp estimator is ambiguous, and needs a coarse stage in front

Qin gives the within-frame phase as `phi_mi = phi_m - 2*pi*i*Tsym*dbeta_c*Fc` —
linear in symbol index — so a transform across symbols is the matched estimator
for residual CFO. But pilot-symbol phase is sampled once per `Tsym = 4.4 us`, so
the estimate is **periodic in CFO with period 1/Tsym = 227.27 kHz**, giving an
unambiguous interval of only **±113.6 kHz**. It does not replace coarse search.

The architecture is therefore:

```
coarse bank  ->  epoch  ->  phase-ramp CFO refinement  ->  long coherent correlation
```

and the ≤120 kHz bank spacing proposed below is what makes it work: a worst-case
residual of ±60 kHz sits comfortably inside the unambiguous interval.

A refinement worth ablating: estimate CFO from **adjacent** symbols first for
unambiguous range, then from **widely separated** symbols for precision. Short
temporal baseline gives range; long baseline gives accuracy.

---

## Stage 2 — The cheap dB, in order of gain per unit of work

Each is an ablation on the frozen corpus, not a decision already taken.

| # | change | headroom | cost |
|---|---|---:|---|
| 0 | **re-centre each receiver's bank on its calibrated LNB bias** | may obviate most of (1) | free — `frequency_center_hz` already exists |
| 1 | bank hypothesis **spacing** <= 120 kHz | up to **8.8 dB** at the current worst case | linear in kernel count; new `NOISE_CEILING` needed |
| 2 | PRF-aware frame combining: max, top-K, trimmed sum | direction certain, magnitude to be measured | see caveat below |
| 3 | fractional epoch correction | **1.62 dB** claimed | see caveat below |
| 4 | one edge per channel at 160 ms instead of both at 80 ms | more frame opportunities | **-10% radio time** |
| 5 | full-frame coherent scoring at an epoch shortlist | up to ~24 dB coherent | ~30-50 ms/probe at K=32 |

**(0) comes before (1).** If one LNB sits +436 kHz from the other, that is a
*predictable receiver bias*, not satellite Doppler, and searching ±700 kHz around
zero to cover it is paying twice. Subtract the calibrated bias and search the
physical Doppler uncertainty around it — six bins around a correct centre may do
what thirteen around zero currently do. Note that a previous measurement found
re-centring *hurt* peak-to-median (1.41 vs 1.61 at −3 dB) because moving all
three hypotheses onto the signal lifts the median as much as the peak. That is
plausibly an artifact of a 3-point bank scored by peak/median, and must be
re-evaluated under the fixed-FAR corpus metric with a *denser* bank rather than
carried forward as established.

**(2) is not free in the current implementation.** The fused C kernel adds each
frame's contribution straight into the folded `agg` array; the per-frame
dimension is discarded inside the kernel. Stage 2 therefore begins with a slow
reference returning `S[frame, anchor, f_D, tau]`, and only optimises after the
combining law is chosen.

**(3) needs three variants ablated, not one.** Interpolating correlation
*magnitudes* after the fact estimates where the peak lies; it does not
reconstruct the correlation a properly fractionally-delayed replica would have
produced. Ablate: (a) quadratic interpolation of the magnitude, (b) an
oversampled or fractional-delay template, (c) a frequency-domain phase-ramp
delay. Only then is the 1.62 dB established as recoverable rather than merely
present.

**(4) is cheaper than what we do now**, because retune and refill overheads do
not scale with probe length: paying them four times instead of eight is
791 -> 716 ms of radio time per sweep at identical compute. The two edge bands sit
230.6 MHz apart (-115.430 and +115.195 MHz from channel centre), so they see
different LNB gain and filter response and slightly different Doppler; alternate
edges across sweeps. LNB calibration is unaffected — `measure_mismatch` compares
*receivers*, not edges.

### PRF: what is modelled and what must be measured

Kozhaya's PRF result is solid — frame occupancy is the fraction of frame
opportunities carrying OFDM, with a low-activity baseline near 2%. What is *not*
established is independence: the same paper documents transmission-mode changes
on a 15-second cadence, i.e. clustering.

So the following is a **first-order prediction under independent 2% occupancy**,
not expected field performance:

| probe | frame slots | P(>=1 ON) at 2%, if independent |
|---:|---:|---:|
| 20 ms | 15 | 0.26 |
| 80 ms | 60 | 0.70 |
| 160 ms | 120 | 0.91 |

Stage 2 measures the real thing from retained IQ: `P(ON at t+k | ON at t)`,
run-length distributions, and the empirical probability of at least one
transmitted frame at 20 / 40 / 80 / 160 ms. Do not adopt (4) on the strength of
the table above; adopt it on the measurement.

---

## Stage 3 — Ablation ladder

| component | levels |
|---|---|
| coherent span | 1, 4, 16, 64, 300 symbols |
| **symbol placement** | **adjacent vs spread across the frame, at equal symbol count** |
| frame combining | blind sum, max, top-K, trimmed sum, M-of-N |
| CFO estimation | grid search vs phase-ramp; grid density; adjacent-then-spread baselines |
| bank centre and spacing | Stage 2 (0) and (1) |
| epoch shortlist K | 8, 32, 128, all |
| fractional epoch | none, magnitude interpolation, fractional-delay template, frequency-domain |
| confirmation statistic | peak/median, anchor agreement, withheld pilot set, 17-roll control |
| cross-receiver combining | off, score fusion, coincidence |
| probe length | 20, 40, 80, 160 ms — by truncating stored IQ |

**Placement is a separate axis from count.** Sixteen adjacent symbols and sixteen
spread across a frame carry the same energy but behave completely differently
under CFO error: adjacent gives robust acquisition and weak frequency precision,
spread gives sharp CFO sensitivity and decorrelates easily. The attractive
staged pattern — 4-8 adjacent for acquisition, widely separated for CFO
refinement, 64-300 only once CFO is nailed — is a hypothesis this axis tests.

**Cross-receiver combining means score fusion, not coherent combining.** The two
LNBs have independent oscillators, so there is no common carrier-phase reference
even though the Pluto ADC channels share a clock. Combine as `S1 + S2` or as a
coincidence test *after each receiver has its own CFO correction* — never by
adding complex samples.

**The confirmation row settles the withheld-pilot proposal by measurement.** It
is closely related to `anchor_agreement`, which the repo already computes; the
real difference is that anchor agreement includes the anchors used for selection
and is therefore partly selection-contaminated, while a withheld set is clean.
Whether that matters is empirical.

---

## Stage 4 — Fast implementation

**Every optimised implementation must agree, within float tolerance, with a
simple obviously-correct reference implementation of the algorithm it
implements.** Not with `matched_pilot_score` specifically — that is the reference
only for full-frame exhaustive-grid correlation, and Stages 2-3 deliberately
contemplate changing the statistic. Each selected component gets its own slow
reference: `frame_max`, fractional-delay correlation, phase-ramp CFO, and so on.

That preserves the principle — sensitivity becomes agreement, which is decidable
— without freezing the design to today's statistic.

---

## Stage 5 — Deployment

A decision, not a foregone conclusion. Running only on kalman remains legitimate:
every future detector change can then be re-scored against the whole archive
instead of only affecting captures not yet taken.

---

## Testing

**Differential.** Optimised against reference, on every corpus probe. The
load-bearing test.

**Known answer.** `matched_pilot_score` normalises by
`|r^H s| / sqrt(|r|^2 |s|^2)`, so the replica against itself scores **1.0**, at
zero lag and zero Doppler — verified: 1.000000, sample_index 0, cfo 0.0. Not N.
Test the *unnormalised* correlation against template energy separately if a
conjugation/energy invariant is wanted.

**Invariance.** Score unchanged under gain scaling and global phase rotation;
shifted correspondingly under a circular time shift.

**Null calibration.** Pure noise must produce the designed whole-search
false-alarm rate. The current 1.33 threshold was characterised on synthetic
Gaussian noise and the field distribution sits close enough that its intended 1%
rate is doubtful — this test is what would have caught that.

**Injection.** Known epoch, Doppler, SNR and frame occupancy; assert recovery of
all of them. **At non-zero Doppler in both signs** — the repo carries an
unresolved positive-slope sign bug on ~25% of qualified Doppler tracks, and a
sign convention only ever tested at zero is not tested.

**Frame-period regression.** The frame period at 2.5 MS/s is 3333.333 samples,
not 3333. An integer grid drifts 20 samples over 60 frames and destroys the fold.
This mistake was made once during investigation and produced plausible-looking
numbers, which is why it needs a test rather than care.

**Corpus regression.** A frozen expected-results file; any change that moves a
detection explains itself in the commit.

---

## Not currently justified

**5 MS/s.** Aliasing itself costs 0.02 dB, because `build_bank` rotates an
already-sampled replica (fast_scan.py:280) so rotation and aliasing commute; the
only real cost is the anti-alias filter deleting energy, 0.93 dB. Widening the
bank recovers 5-6 dB for 1.67x kernel work and no radio time, against ~1 dB for
1.86x kernel work and 1.44x sweep time. **But the door stays open**: 5 MS/s also
halves the sampling interval from 400 to 200 ns, so if the 1.62 dB fractional-epoch
loss survives Stage 2 (3), the total benefit may exceed 0.93 dB. Reconsider after
the fractional-delay ablation, not before.

**Per-subcarrier equalisation.** Qin's edge roll-off is a 10-40 MHz feature; the
pilots span 1.64 MHz. Digitised from Qin Fig. 4 and run through `probe()`, the
measured cost is 0.0-0.4 dB. Across 8 *adjacent* subcarriers an amplitude tilt
costs `(mean w)^2 / mean(w^2)` — a 10:1 tilt is 1.20 dB — and a phase slope is a
pure delay the epoch search absorbs. Qin's own coarse acquisition is unequalised.

**`track_edge_pilots` for acquisition.** It requires `epoch_sample` and never
searches epoch; a one-sample (0.4 us) error drops its score from 0.9524 to 0.1081
against a control floor of 0.0958. Its statistic is `mean(|corr|^2)` —
noncoherent. It is a conditioned refiner.

**Coherent integration across frames.** Qin: the per-frame phase `theta_m` "has
so far resisted modeling".

**PSS/SSS acquisition.** The PSS spreads over 240 MHz; at 2.5 MHz we capture ~1%
of its energy, and Qin's bounds put narrowband PSS+SSS breakdown at +6.2 dB.

**T-codes and the reference template.** Qin's 48 dB needs all 1024 subcarriers
across the full channel. At 2.5 MHz, 34 dB is our ceiling.

**40 dB-Hz as a threshold.** That is Kozhaya's operating point for their
receiver's acquisition *and tracking* loops. Our scanner does not track.

---

## Execution order

0. **Save the corpus.** Nothing else matters until the IQ stops disappearing.
   122 probes preserved by hand; the automated sampler is the first thing built.
1. Evaluation harness: TLE as prior, injection as hard truth, whole-search FAR,
   pass-level held-out sessions.
2. **Measure before building.** The CFO distribution across the corpus, frame
   occupancy statistics, and the fractional-epoch response. Each of these decides
   whether a later stage is worth its cost, and the first experiment already
   demoted one stage that looked like the top priority.
3. Make coherent detection affordable: slow per-frame reference, then epoch
   shortlist, then coarse CFO -> phase-ramp -> long coherent.
4. Combining laws against measured occupancy, at fixed whole-search FAR.
5. Fractional-delay correction, three variants.
6. Bank centre and spacing, sized by the measured CFO histogram rather than by
   the worst case.
7. 160 ms single-edge sweeps, once burst statistics are known.
8. Only then reconsider 5 MS/s or channel equalisation.

The original working expectation was that CFO coverage would yield the first
obvious gain. The first experiment did not support that, and the ordering above
reflects the evidence rather than the expectation. What the experiment did
establish is that the coherent detector works and is unaffordable, so the whole
problem is now one of search cost rather than of detection principle.

---

## What this cannot tell us

Enrichment near TLE predictions inherits the prediction's errors and rewards only
detections where a catalogued satellite was expected; a real uncatalogued
detection scores as a false alarm. Injection Pd is a hard number but assumes the
injected replica matches the real transmission, which is exactly what a blind
beacon estimator would test and we are not doing.

---

## Confidence

The findings on the coherent primitive, Nyquist, channel response and CFO
scalloping were each established by a single investigation plus my own reading of
the code; **none received the adversarial audit planned for it** — that run was
stopped for time. The one-edge arithmetic, edge separation, the known-answer
value of 1.0, and the Qin 40° quotation I verified directly.

The load-bearing numbers most worth re-checking before anyone spends a week on
them are the **8.8 dB** detector-excess loss at 150 kHz and the **1.62 dB**
half-sample straddle, because the Stage 2 ordering rests on them.
