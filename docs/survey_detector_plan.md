# Survey detector: a staged plan

Revision 6. Revision 5 rewrote the architecture section as a bake-off. This
revision changes nothing about the plan's direction; it records that **step 1 is
built**, and corrects two claims that the work of building it measured and found
wrong. See "What building step 1 changed in this document" below.

Status: step 1 is **in the repository, not on the capture host**. No service has
been restarted, so the running survey is still the 3×8 bank at 1.33. Steps 2–6
are unstarted.

---

## The finding that reframes everything

**The deployed detector cannot detect a single transmitted frame anywhere in the
tested range.** With exactly one ON frame among 59 slots in an 80 ms probe, its
detection probability is ≤0.012 at every strength tested — −21, −18, −15, −12, −9
and −6 dB. Untested outside that range; the claim is bounded by the experiment. The
coherent full-frame detector finds that same frame with **Pd 0.94 at −21 dB**.

The deployed statistic folds ~60 frame slots non-coherently and needs roughly
**12–30 transmitted frames** in one probe to fire, at −9 to −6 dB against its own
1% false-alarm threshold. Starlink occupancy runs as low
as 2%. At that rate the fold sits at its false-alarm floor even on probes that do
contain a transmission.

So this was never primarily a tuning problem. It is the wrong detector for a
sparse signal, and everything below is either a cheap interim repair or the route
to replacing it.

---

## What is now measured

### Span — ±320 kHz before absolute-LO uncertainty, and we already have ±350

From a 3-day, 2-second sweep of the whole catalogue against our own site,
**63,035,467 satellite-instants**, with a vectorised propagator validated against
the repo's scalar path to 0.057 Hz:

```
Doppler p99.9, all-sky, top tuning (11.690 GHz)   279,059 Hz
LNB bias uncertainty (p99.9 of 2,865 pairs)        19,346
margin: p99.9 → population max → closed form       21,595
                                                 ==========
                                                  320,000 Hz  → ±320 kHz
```

`acquisition.py:99-100` already searches ±350 kHz. **Leave it alone.**

The 211.6 kHz figure earlier revisions treated as a bound is the **59th
percentile all-sky** — 41% of satellite-instants exceed it. It was the maximum of
one afternoon, and it landed near the *40°-gated* bound by coincidence.

Qin's ~15 ppm is low by ~27%, for reasons worth recording because they compound:
15 ppm corresponds to a **47.8° cutoff, not 40°**; our constellation median is
470.7 km, not 550; and **1,369 catalogued Starlinks are retrograde**, where
Earth's rotation adds to ground-relative speed. Correcting only the carrier
frequency — the 172 → 175.4 kHz fix an earlier revision made — addressed a 2%
error while a 19% one remained.

**Doppler rate** bounds coherent integration at T ≤ 15.2 ms (all-sky p99.9),
13.4 ms gated, **8.0 ms worst case**, under the criterion that the offset must not
walk one frequency bin across the integration: `R·T ≤ 1/T`, so `T ≤ 1/√R`. Our frame is 1.33 ms, safe by 6×. Note that
gating at 40° *multiplies* the rate 4.7×: high elevation is the fast part.

### Spacing — ≤125 kHz, and the requirement is SNR-dependent

From 14,608 injected trials over CFO 0–900 kHz × SNR −16…−4 dB. The table gives
**hypothesis-to-hypothesis spacing**; worst residual mismatch to the nearest
hypothesis is half of it. Largest spacing still holding Pd ≥ 0.9 everywhere:

| SNR | max spacing |
|---|---|
| −6 dB | 300 kHz |
| −10 dB | 200 kHz |
| **−12 dB** | **150 kHz** |
| −14 dB | none — even a matched hypothesis only reaches 0.47 |

Spacing tightens ~25 kHz per dB. The kernel penalty is *fixed* — 150 kHz mismatch
costs 6.6 dB at every SNR — what shrinks is the headroom. So ≤125 kHz is right
not because 125 is special but because it is the first step below what −12 dB
demands, and −12 dB is where the detector stops working at all.

`Pd < 0.5 at worst CFO`: **A −5.4 dB, E −13.3 dB** — a 7.9 dB gap.

### Occupancy — the loss is real but it is not where anyone said

Conditional on an ON frame existing, 100% → 10% occupancy costs:

| | coherent | deployed |
|---|---|---|
| 80 ms | 2.2–3.5% | **40–56%** |

The "~5% loss" an earlier revision quoted was correct — **for the coherent
detector only**. It does not transfer to what we run.

Clustering does **not** uniformly hurt. At equal mean occupancy, bursts make the
coherent detector worse (80 ms, 2%: Pd 0.699 → 0.235) and the deployed fold
*better* (0.006 → 0.076), because bursts deliver ~5.7 frames together instead of
1.7 scattered, and the fold needs them together.

**Probe length is a third-order fix.** At 2% occupancy, 9×20 ms, 4×40 ms and
2×80 ms all cost 160–180 ms of air for 90% cumulative detection; 80 ms wins only
by amortising ~137 ms of fixed per-probe overhead. At Kozhaya's 15 s mode cadence
probe length becomes irrelevant entirely and **revisit spacing is the only lever**
— about 28 minutes of wall clock for 90%.

### Thresholds — the current one is not what it claims

Calibrated on windows that are **target-pilot-free by construction**: a *lower*-edge
bank scored on an *upper*-edge tuning, whose pilot codes sit 230 MHz away. No
screening on the statistic being calibrated, so no circularity. It is free of the
*target pilot*, not of RF energy in general, and 230 MHz away the interference
environment is not matched — hence the second null below.

| | A | E | G |
|---|---|---|---|
| 1% threshold | 1.289 | **1.255** | 1.261 |

Earlier work in this repository — including revision 3's own experiments —
calibrated on lower-edge windows, of which 8–26% hold real pilots. That charges
real detections to the false-alarm budget and inflates every threshold.

1,456 clean realisations support false-alarm rates down to ≈0.2% and no further.

**Reproduced independently** while building step 1, on 106 corpus captures whose
manifests record `sample_order`, 1,696 realisations, 80 ms windows: config E
**1.2524** both directions and **1.2547** in exactly the direction stated above,
against 1.255; config A 1.2785 against 1.289. Bootstrap 90% interval on E's 99th
percentile [1.2444, 1.2570].

**Corrected.** This section previously read "the deployed 1.33 realises
8.11% / 2.72% / 2.11% false alarms at 20 / 40 / 80 ms, not 1%". On the clean
cross-edge null at 80 ms, 1.33 realises **0.06%**. The sentence also contradicted
the paragraph above it: contamination *inflates* a threshold, and an inflated
threshold gives fewer false alarms, not more. What 8.11% appears to be is the
*fire rate on same-edge windows* — detections and false alarms together, which is
the very conflation the clean null exists to undo. Config A at 1.33 fires on 7.8%
of same-edge windows in this reproduction. So the deployed threshold was about
**seventeen times too strict**, not too lax, and adopting the measured 1% point is
a sensitivity gain in its own right, separate from the bank.

### Real sky, no injection

Each bank at its own 1% false-alarm rate, on **lnb-a and lnb-b — zero-bias ports
whose pilots lie entirely inside A's span**, so span cannot explain the result:

```
A fires on  9.0% / 7.0%          E fires on  30.6% / 29.2%
```

A 3–4× detection gain from spacing alone, on the sky.

**Reproduced**, same 106 captures, each bank at its own reproduced 1% point:

```
A fires on  9.7% / 7.6%          E fires on  24.3% / 20.4%      gain 2.5× / 2.7×
```

The A figures land on top of the originals; the E figures are lower and the gain
is 2.5–2.7× rather than 3–4×. Different corpus, different sky, same direction and
same order. The biased port is where it is largest: lnb-c goes 3.1% → 11.8%, a
3.8× gain, which is what a span argument predicts and a spacing argument does not.

---

## What the measurements retired

**"Invisible however strong."** No bank goes blind. The 11-tap filter settles onto
a sidelobe shelf 8–11 dB down that persists to 900 kHz; at −4 dB the deployed bank
detects at ≥0.84 at *every* offset measured. Coverage is a sensitivity statement,
never a boundary.

**"A hole at 150 kHz."** It is a notch, not a cliff — Pd 0.28 at exactly 150 kHz
and 1.00 at both 125 and 175 kHz. A 50 kHz measurement grid rendered one column as
a wall. It *becomes* a wall by −8 dB.

**"Recentring is the cheap win."** Config G is a **per-port** configuration, not a
fleet one: lnb-c (+434 kHz bias) has 100% of its pilots inside G and 0% inside A;
the other three ports are exactly the reverse. It is correct only for lnb-c and
only while its bias is tracked.

**"Sparse transmission matters less than feared."** True for the coherent
detector, false for the deployed one, where both terms fail at once.

---

## Decided

**Adopt config E** — 13×8 over ±700 kHz, 104 kernels, threshold 1.255. Span and
spacing come from independent measurements and both point here. Deploy in shadow:
the survey never gates a capture.

*Cost, measured on building it:* **3.76×** the scoring compute and **121 ms** per
80 ms probe, against the ~2× and ~50 ms estimated here. The estimate was low
because it counted kernels against the wrong baseline; the ratio is below the
4.33× the kernel count alone gives, because a quarter of a 24-kernel probe is
fixed cost. Still host time only, and still preamble to a 120 s dwell.

**~~Adopt two nulls, not one.~~ Retracted — the second null does not exist.**
The reasoning was right and the construction was wrong. A same-tuning
wrong-code null *would* be worth having: the cross-edge null is target-pilot-free
by construction, which is what exposed the contaminated thresholds, but it sits
230 MHz away, so its interference environment is not ours.

The proposed construction — score the 17-symbol-rolled pilot sequence on the same
raw IQ — **is not a wrong code.** Every symbol occupies exactly one symbol period,
so rolling the code sequence by *r* symbols produces the same waveform shifted by
*r* symbol periods: measured coherence **0.909** between the 17-roll and the plain
frame circularly shifted 187 samples, which is exactly 17 × 11. A statistic that
*searches* the epoch simply re-finds the true pilot at the shifted epoch. Measured:

```
rolled-bank winning epoch = true epoch − r × 11 samples, exactly, for r = 1,5,17,29
on the corpus:  rolled p99 1.851   cross-edge p99 1.252   matched p99 1.943
                correlation with the matched score: rolled 0.967, cross-edge 0.825
```

A threshold calibrated on the rolled population would be 1.851, at which the
survey would fire on 1.8% of the sky instead of 21%. It would have looked like a
sober, conservative, distribution-matched null and it would have made the detector
nearly blind.

`control_symbol_roll=17` remains correct where the repository actually uses it —
`conditioned_pilot_score`, which **holds the epoch fixed** and separates the same
signal 0.585 to 0.019. The rule is that a symbol roll is a control for a
conditioned statistic and never for a searching one. Note that
`matched_pilot_control_scores`, which searches `sample_index`, has the same defect
and was not part of this work.

A genuine same-tuning null still wants building. It needs a code that is not a
time shift of the target — the other edge's code evaluated on this tuning is the
obvious candidate and is *not* the same thing as the cross-edge null, because the
window is the operational one.

---

## Not measured, and load-bearing

**The anchor port's absolute LO error.** `receiver_centers` anchors one port at 0
and places the other by the measured *difference*, so the common term cancels and
was never determined. `measured_centers_hz` is absent from the calibration
artifact. Every span above is centred on an imperfectly located origin. Only a
direct absolute sweep closes this, and it needs radio time.

**~~The timing stage searches ±300 kHz~~ — closed.** It now searches
±320 kHz, at 106.7 kHz for PSS and 80 kHz for pilot-epoch, both *finer* than the
150 kHz and 100 kHz they replaced because `acquisition_centers` rounds the step
down to fit the span. Measured over seven CFOs by three seeds, the median
exact-minus-control margin moves from 0.021 to 0.277 and epoch-plus-CFO recovery
from 15/21 to 16/21; 240 kHz went from picking the *wrong epoch* to exact.

It is not uniformly better, and that is worth recording: 290 kHz sat 10 kHz from
an old hypothesis and sits 30 kHz from a new one, and loses lock. Which exposes
something the spacing analysis above does not cover — this stage's full-frame
templates have an effective capture range of roughly ±10–20 kHz around a
hypothesis, not the ±40 kHz half-spacing an 80 kHz grid implies. The ±320 kHz
requirement is met; the grid is still coarse relative to what this stage can pull
in, and closing that is a separate measurement.

**Whether the expensive coherent detector is needed at all.** Its full-frame
template tolerates ~±375 Hz residual CFO, so a brute-force search over the span
would need ~800 hypotheses against the deployed bank's 3. That is the cost of *one
implementation*, not a property of the signal — see the relative-phase section.

---

## Relative phase — external, unverified, and the reason Stage 3 is now a bake-off

**Provenance: this work was not done in this repository and none of it has been
verified here.** It arrived as a handoff describing experiments on 96 real
tuning/receiver observations — 26 previously sequence-confirmed against 70
unconfirmed. Its own author states the caveat plainly: the positive group was
labelled using the previous coherent detector, so these are *exploratory
separation ratios on a corpus with a contaminated label source*, not Pd/FAR
measurements. The referenced artifacts are not reachable from this machine. It is
recorded here as hypotheses with claimed support, and the injection harness is how
they get settled.

### The algebra, which needs no experiment

For frame `m` and pilot symbol `i`, `z_{m,i} ≈ A_{m,i}·exp(j(φ_m + 2π·Δf·t_i))`.
The differential product

```
d_{m,i} = z_{m,i+1} · conj(z_{m,i}) ≈ A_{m,i+1}·A_{m,i}·exp(j·2π·Δf·T_sym)
```

**cancels φ_m exactly.** Equivalently `S(f) = |Σ_i z_i·exp(-j2π f t_i)|²` is
invariant to a common phase on all `z_i`. This is why the old blanket rule was too
blunt, and it is checkable on paper rather than by measurement.

Two consequences follow directly. Evidence *can* be accumulated across frames with
unrelated absolute phases, provided the phase is cancelled first. And residual CFO
can be *estimated* in symbol space — `Δf = ∠D / (2π·T_sym)` over ~32 complex
values — rather than *searched* over ~800 hypotheses in sample space. If that
holds, the coherent detector's search cost stops being the architectural problem
revision 4 called it.

The ambiguity is the catch: adjacent-symbol phase is sampled every 4.4 µs, so the
estimate is periodic at 227.3 kHz and unique only within **±113.6 kHz**. A coarse
stage remains mandatory. Config E's 116.7 kHz spacing gives a worst nearest-bin
residual of **58.3 kHz**, comfortably inside that window — so E is a good front end
for relative phase, which is a second and independent reason to adopt it.

### Claimed results, unverified

Exploratory separation (lowest confirmed ÷ highest unconfirmed) on those 96
observations:

| candidate | claimed gap | claimed 26/70 split |
|---|---:|---|
| current 3×8 | 0.97× (overlap) | 23/26, 12/70 |
| config E 13×8 | 0.96× (overlap) | 25/26, 9/70 |
| **8-anchor relative phase** | **1.77×** | 26/26, 0/70 |
| differential 16 + control | 2.91× | 26/26, 0/70 |
| differential 32 + control | 3.58× | 26/26, 0/70 |
| **GLRT32 + control** | **11.6×** | 26/26, 3/70 above one ceiling |
| all-pair 32 + control | 16.3× | — |
| GLRT64 + control | 21.7× | — |
| 300-symbol coherent | 2.80× | 26/26, 1/70 |
| withheld 300-symbol | 3.43× | 26/26, 0/70 |

Also claimed: symbol count mostly drives the *noise* population down rather than
lifting the signal (highest unconfirmed 0.147 → 0.106 → 0.064 → 0.018 at 8 → 16 →
32 → 300 symbols), which is why 16–32 may suffice; amplitude weighting beats
phase-only; and all-pair-32 CFO agrees with the existing refiner to ~91 Hz median.

Reported as *not* promising, and worth not rediscovering: multiple epoch
candidates, epoch dithering, periodicity as a primary detector, per-subcarrier
differential processing, and phase-only weighting.


---

## Next steps

**1 — ~~Repair what is deployed.~~ Built, not deployed.** Config E with a
cross-edge-calibrated `NOISE_CEILING` (1.252 over 1,696 clean realisations); the
timing stage widened to ±320 kHz at 106.7 kHz for PSS and 80 kHz for pilot-epoch,
both finer than they were. E is doubly justified: it is also the right coarse
front end for anything relative-phase, since its worst residual sits inside the
±113.6 kHz ambiguity window. Reaching the capture host is a deployment, and
nothing here has restarted a service.

**2 — Measure the absolute LO error.** The one gap no stored data can close. Until
it exists, every span is a width about an unknown centre.

**3 — Run the architecture bake-off.** This replaces revision 4's assumption that
the destination is a 300-symbol full-frame detector. The question is no longer
*"how do we afford 300-symbol coherent acquisition?"* but:

> **What is the cheapest phase-invariant known-code statistic that reaches the
> required Pd at fixed whole-search false-alarm rate?**

All candidates share the config-E coarse stage and are scored on the injection
harness — known epoch, CFO, SNR and occupancy — at a *common* whole-search FAR,
with CPU milliseconds per 80 ms probe recorded beside every number:

```
                        13×8 coarse bank
                                |
                      candidate epoch + CFO
                                |
        +-----------+-----------+-----------+-----------+
        v           v           v           v           v
   8-anchor    differential  differential  GLRT 32   64-symbol
   relative     16 + ctrl     32 + ctrl    + ctrl    ordinary
      phase                                          coherent
        +-----------+-----------+-----------+-----------+
                                |
                   fixed-FAR comparison, Pd per ms
                                |
                    borderline candidates only
                                v
                 300-symbol withheld confirmation
```

The coarse stage's success criterion changes with it. Do not ask whether its score
crosses a detection threshold; ask **whether the true epoch and CFO land in the top
K candidates closely enough for the confirmation stage to recover them.** That is a
much weaker and much cheaper requirement.

Two candidates are worth naming as priors, on the external evidence above:
`8-anchor relative phase` because it reuses anchors the survey already computes and
its only structural change is *not* replacing each complex correlation with its
magnitude; and `GLRT32 + wrong-code control` as the claimed performance/cost
leader. Both remain hypotheses until the harness says otherwise.

**4 — Build the winner**, gated on agreeing with a slow reference implementation of
the same algorithm. The 300-symbol detector becomes the slow reference and optional
final validator rather than the presumed production detector.

**5 — Test whether relative phase attacks sparse occupancy directly.** The
hypothesis: differential phasors from transmitted frames add directionally while
noise-only opportunities have random phase and partially cancel — unlike the `Σ|z|²`
fold, where noise can only add. If true, sparse occupancy is addressable
algorithmically rather than only through revisit cadence, which would change
conclusion (5) below. The occupancy harness already exists and can settle it.

**6 — Revisit cadence, not probe length**, if (5) fails. At a 15 s mode cadence the
lever is how often we return to a channel, not how long we look. That is a
scheduler question and it has not been asked yet.

Stage 0 is complete: the corpus samples automatically, geometry priors and
injection ground truth both exist, and every measurement above came from them.
Stage 0.4 — a whole-search false-alarm harness over a null population large enough
to pin 1% — remains the prerequisite for any *production* threshold, as opposed to
the comparison thresholds used here.

---

## What building step 1 changed in this document

Four things, all of them measurements the build had to make and none of them
changing where the plan is going.

**The threshold reproduced.** Config E's 1% point came out at 1.2547 in exactly
the construction this document specifies and 1.2524 over both cross-edge
directions, against 1.255. Config A came out 1.2785 against 1.289. The real-sky
fire rates for A on lnb-a and lnb-b came out 9.7% and 7.6% against 9.0% and 7.0%.
E came out lower than claimed, 24.3% and 20.4% against 30.6% and 29.2%.

**The 8.11% / 2.72% / 2.11% sentence was wrong** and is corrected above. The
deployed 1.33 realises 0.06% on a clean null, not 2.11%.

**The second null does not exist** as specified, and the retraction above says why.
This is the one place where the plan would have made things worse if followed.

**The cost model is affine, not proportional.** 104 kernels against 24 costs
**3.76×**, not 4.33×, because about a quarter of a 24-kernel probe is fixed cost —
the energy normaliser and the running-power cumsum run over the whole window
before a single kernel is touched. Measured on a Pi 5, 80 ms probes, three threads,
best of nine: 14.7 ms at 8 kernels, 28.2 at 24, 60.2 at 56, 106.2 at 104. The
field anchors the level: 234 deployed surveys report 400.4 ms per probe-second at
24 kernels. The survey sweep goes from a field-measured 1.70 s to a projected
3.1 s.

**One gap the build exposed and did not close.** The statistic depends on how many
frames it folds, so a threshold belongs to a probe length: on synthetic noise this
bank's 99th percentile runs 1.310 at 20 ms, 1.189 at 40 ms and 1.137 at 80 ms.
`verify_presence` applies the 80 ms ceiling to whatever dwell chunk it is handed
and cannot tell the difference. Recorded in the code, not fixed.

---

## Principles, unchanged

**The capture host records; the analysis host decides.**

**Nothing is included without measured evidence.** Four proposals died this
revision — 5 MS/s, per-subcarrier equalisation, recentring as a fleet default, and
probe lengthening — each on a measurement rather than an argument.

**Truth comes from outside the detector.** Injection gives Pd under the injection
model; TLE proximity gives enrichment, whose slices cover 74% of the search space
and are therefore ~2:1 evidence at best. Neither is field Pd.

**Every optimised implementation must agree with a slow, obviously-correct
reference of the same algorithm.** This is what stops optimisation becoming
silent redesign.

---

## Testing

Differential against a reference; known-answer (the replica against itself scores
**1.0**, not N — the normalisation is `|r^H s| / sqrt(|r|² |s|²)`); invariance
under gain, global phase and circular shift; null calibration at whole-search FAR
using the cross-edge construction; injection at non-zero Doppler **in both signs**,
because this repository carries an unresolved positive-slope sign bug and a
convention only ever tested at zero is not tested; frame-period shift invariance on a wrapped or guarded synthetic capture and joint
signal/epoch translation away from the boundaries — a finite `valid` correlation is
*not* invariant to an arbitrary circular shift; a frame-period regression pinning
**3333.333 samples, not 3333**; an order-mapping regression on
`sample_order`; and a corpus regression against frozen expectations.

---

## Deliberately not doing

No **direct complex-amplitude** integration across frames — absolute inter-frame
carrier phase is unmodelled, so raw frame amplitudes must not be summed.
Phase-invariant statistics are explicitly permitted: normalised coherent energy,
differential products and other relative-phase quantities cancel the unknown frame
phase and may be accumulated across frame opportunities. No PSS/SSS acquisition — at 2.5 MHz we capture ~1% of its energy. No
T-codes — they need all 1024 subcarriers. No 5 MS/s — aliasing costs 0.02 dB, the
door stays open only if the fractional-epoch loss survives. No per-subcarrier
equalisation — 0.0–0.4 dB measured ceiling. No 40° elevation gate on the corpus —
it would bake a "typically" statement into the evidence, and the measurements
above show the gated and ungated populations differ by 35%.
