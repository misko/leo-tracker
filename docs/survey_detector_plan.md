# Survey detector: a staged plan

Revision 4. Revisions 1–3 argued about the design. This one is mostly measured:
three parallel investigations produced 63 million satellite-instants of geometry,
14,608 injected trials across a CFO×SNR surface, and 18,250 trials across frame
occupancy. Most of what those measurements found was not what the earlier
revisions predicted.

Status: proposed. Nothing here has changed the capture host.

---

## The finding that reframes everything

**The deployed detector cannot detect a single transmitted frame — at any
strength.** With exactly one ON frame among 59 slots in an 80 ms probe, its
detection probability is ≤0.012 at −21, −18, −15, −12, −9 *and* −6 dB. The
coherent full-frame detector finds that same frame with **Pd 0.94 at −21 dB**.

The deployed statistic folds ~60 frame slots non-coherently and needs roughly
**12–30 transmitted frames** in one probe to fire. Starlink occupancy runs as low
as 2%. At that rate the fold sits at its false-alarm floor even on probes that do
contain a transmission.

So this was never primarily a tuning problem. It is the wrong detector for a
sparse signal, and everything below is either a cheap interim repair or the route
to replacing it.

---

## What is now measured

### Span — ±320 kHz, and we already have ±350

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
13.4 ms gated, **8.0 ms worst case**. Our frame is 1.33 ms, safe by 6×. Note that
gating at 40° *multiplies* the rate 4.7×: high elevation is the fast part.

### Spacing — ≤125 kHz, and the requirement is SNR-dependent

From 14,608 injected trials over CFO 0–900 kHz × SNR −16…−4 dB. Largest mismatch
still holding Pd ≥ 0.9:

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

`Pd < 0.5 at worst CFO`: **A −5.4 dB, E −13.3 dB** — a 7.5 dB gap.

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

Calibrated on windows that are signal-free **by construction**: a *lower*-edge
bank scored on an *upper*-edge tuning, whose pilot codes sit 230 MHz away. No
screening on the statistic being calibrated, so no circularity.

| | A | E | G |
|---|---|---|---|
| 1% threshold | 1.289 | **1.255** | 1.261 |

Earlier work in this repository — including revision 3's own experiments —
calibrated on lower-edge windows, of which 8–26% hold real pilots. That charges
real detections to the false-alarm budget and inflates every threshold. The
deployed 1.33 realises **8.11% / 2.72% / 2.11%** false alarms at 20 / 40 / 80 ms,
not 1%.

1,456 clean realisations support false-alarm rates down to ≈0.2% and no further.

### Real sky, no injection

Each bank at its own 1% false-alarm rate, on **lnb-a and lnb-b — zero-bias ports
whose pilots lie entirely inside A's span**, so span cannot explain the result:

```
A fires on  9.0% / 7.0%          E fires on  30.6% / 29.2%
```

A 3–4× detection gain from spacing alone, on the sky.

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
spacing come from independent measurements and both point here. Roughly 2× the
scoring compute, ~50 ms per 80 ms probe. Deploy in shadow: the survey never gates
a capture.

**Adopt the cross-edge null** as the calibration method throughout. Getting
signal-free windows by construction rather than by screening is better than
anything currently in the repo, and it is what exposed the contaminated
thresholds.

---

## Not measured, and load-bearing

**The anchor port's absolute LO error.** `receiver_centers` anchors one port at 0
and places the other by the measured *difference*, so the common term cancels and
was never determined. `measured_centers_hz` is absent from the calibration
artifact. Every span above is centred on an imperfectly located origin. Only a
direct absolute sweep closes this, and it needs radio time.

**The timing stage searches ±300 kHz** (`acquisition.py:105-116`), below the
298.4 kHz that Doppler-p99.9 + bias requires — and every later stage inherits its
candidate CFO from there. That is the tight link in the chain, not the ±350 kHz
grid.

**The coherent detector's search cost.** Its full-frame template tolerates only
~±375 Hz residual CFO, so covering the span needs on the order of 800 hypotheses
against the deployed bank's 3. Its occupancy immunity is free; its search is not.
This is the whole of the remaining problem.

---

## Next steps

Ordered by evidence, not by appetite.

**1 — Repair what is deployed.** Config E with a cross-edge-calibrated
`NOISE_CEILING`; widen the timing stage past ±320 kHz. Both are small, both are
backed by a measured 3–4× on real sky, and neither waits on anything.

**2 — Measure the absolute LO error.** The one gap that cannot be closed from
stored data. Until it exists, every span is a width about an unknown centre.

**3 — Price the coherent search.** The only open question that decides the
architecture. Measure, on the corpus: how many CFO hypotheses are actually needed
once a coarse stage narrows the range; how well a cheap first pass *ranks* the
true epoch into a shortlist, which is a far weaker requirement than winning; and
what the phase-ramp estimator costs given it is ambiguous outside ±113.6 kHz and
therefore needs a coarse stage in front of it.

**4 — Then build it**, if and only if (3) says it fits: coarse bank → epoch
shortlist → phase-ramp CFO → full-frame coherent confirmation. Gated on agreeing
with a slow reference implementation of the same algorithm.

**5 — Revisit cadence, not probe length.** If occupancy really is mode-driven on a
15 s cadence, the lever is how often we return to a channel, not how long we look.
That is a scheduler question and it has not been asked yet.

Stage 0 is complete: the corpus samples automatically, geometry priors and
injection ground truth both exist, and every measurement above came from them.
Stage 0.4 — a whole-search false-alarm harness over a null population large enough
to pin 1% — remains the prerequisite for any *production* threshold, as opposed to
the comparison thresholds used here.

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
convention only ever tested at zero is not tested; a frame-period regression
pinning **3333.333 samples, not 3333**; an order-mapping regression on
`sample_order`; and a corpus regression against frozen expectations.

---

## Deliberately not doing

No coherent integration across frames — the per-frame phase is unmodelled in the
literature. No PSS/SSS acquisition — at 2.5 MHz we capture ~1% of its energy. No
T-codes — they need all 1024 subcarriers. No 5 MS/s — aliasing costs 0.02 dB, the
door stays open only if the fractional-epoch loss survives. No per-subcarrier
equalisation — 0.0–0.4 dB measured ceiling. No 40° elevation gate on the corpus —
it would bake a "typically" statement into the evidence, and the measurements
above show the gated and ungated populations differ by 35%.
