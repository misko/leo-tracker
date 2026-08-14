# Cross-radio synchronised scan — retraction and corrected record

Date: 2026-08-14 UTC
Status: **apparatus works and the corpus is sound; every scientific claim made
from it has been withdrawn**

## Executive summary

Two Pluto SDRs were driven from one process, two threads, and a
`threading.Barrier` at every tuning, so both radios sat on the same tuning at
the same instant. The apparatus does what it was built to do: 2,442 committed
paired sweeps were captured to NVMe, shipped to QNAP in byte-verified batches,
imported to corpus form on a timer, and scored by eight detectors. That part
of the work stands.

The findings drawn from it do not. An earlier version of this report claimed
three results. Two independent adversarial reviews returned `SERIOUS_ISSUES`,
and all three claims are withdrawn here:

- **Withdrawn — "six of eight methods agree on sky occupancy f = 0.2278 ±
  0.007, validating the cross-radio coincidence model."** The agreement is not
  evidence. A negative control that joins radio A of one sweep to radio B of a
  *different* sweep, minutes apart, passes the same check. The eight detectors
  correlate with each other at φ 0.82–0.94, so they are near-duplicates and
  their f estimates must agree whatever the input.
- **Withdrawn — "`differential-16` and `differential-32` are miscalibrated;
  implied detection probability d > 1 is impossible."** That rested entirely on
  assuming the nominal 1% false-alarm rate. Measured from the cross-edge null
  arms the rate is ≈6.5%, and at the measured value every method lands in
  physical range. The differentials were never shown to be broken.
- **Withdrawn in its reasoning — "no detection cliff at the pilot guard band;
  only 1.25 MS/s is handicapped."** Re-binned on the bias-corrected offset,
  all three live ports collapse onto one curve and a cliff appears between 350
  and 400 kHz at 2.5, 5.0 *and* 10 MS/s. The guard bands at those rates are
  +312.5, +1562.5 and +4062.5 kHz — a 13× range that does not move the cliff.
  It is a rate-independent ≈350 kHz offset tolerance, which is the CFO search
  span, not the pilot band.

What survives is smaller and mostly instrumental: an LO bias on `lnb-c` large
enough to have been mistaken for a weak receiver, a genuinely handicapped
1.25 MS/s arm, a dead port, a collector bug that cost 893 sweeps, and
synchronisation that is far looser than the analysis assumed. Those are stated
below with the numbers they rest on.

Two facts govern everything that follows, and neither was properly stated in
the withdrawn version:

1. **There is no signal injection at this site.** Every detection probability
   in this report is a *model output* inferred from a coincidence model, never
   measured against a known input. `survey_comparison` says this in its own
   module docstring — "there is no injection here, so there is no Pd, and this
   file refuses to imply one" — and the withdrawn report implied one anyway.
2. **The cross-radio estimator was not shown to beat the within-radio one.**
   Independence of the two radios remains an assumption, not a measurement.

## What was built

One collector process (`/mnt/leo-nvme/leo-tracker/bin/synccollect.py`, run by
`leo-sync-scan.service`) opens both radios and runs one thread per radio with a
`threading.Barrier` at every tuning. Twelve arms cross probe length
{80, 160, 640} ms with sample rate {1.25, 2.5, 5.0, 10.0} MS/s, drawn uniformly
per sweep. Both radios take the same arm with p = 0.9; the remaining 10% put two
different configurations on the same sky at the same instant. Each radio draws
its edge order (`L` or `U`) independently every sweep, so about half of all
sweeps have the two radios on opposite edges of the same channel simultaneously
and about half have them on the identical tuning.

| Stage | Unit | Function |
|---|---|---|
| Capture | `leo-sync-scan.service` | both radios, one process, barrier per tuning, IQ to NVMe |
| Transfer | `leo-sync-drain.service` | batched rsync to QNAP; a directory appears only after every file matched byte for byte |
| Import | `leo-sync-import.timer` (5 min) | hard-links sweeps into survey-corpus entries |
| Scoring | survey scorer | eight detectors per observation |

`sweep.json` is written last, after both IQ files are closed, so its presence is
the commit marker. All 2,442 sweep directories in the census carried one; there
were zero incomplete directories.

Raw data and the format README are at `/mnt/qnap01/mouse9911/leo-scans`.

### Corpus census

Census taken 2026-08-14T05:36Z. The collector was still running, so every total
here is a floor — the share held 2,485 sweeps three minutes later.

| Quantity | Measured |
|---|---:|
| Sweep directories, all with `sweep.json` | 2,442 |
| Paired sweeps (both radios produced IQ) | 1,549 |
| Single-radio sweeps | 893 |
| `matched_arm` fraction | 0.9066 |
| Distinct arms present | 12 of 12 |
| Declared IQ volume | 347.84 GB |
| Corpus entries imported from these sweeps | 3,959 |
| — from `pluto-19f2` / `pluto-5d4d` | 2,426 / 1,533 |
| Corpus entries scored at census | 320 |
| Scored observations | 7,680 (5,120 target, 2,560 cross-edge null) |

The 2,426 − 1,533 = 893 entry deficit on `pluto-5d4d` is exactly the
single-radio outage described below; the two counts agree independently.

The eight scoring methods present on every observation are `anchor-8`,
`coarse-A`, `coarse-E`, `differential-16`, `differential-32`, `full-frame-300`,
`glrt-32` and `glrt-64`.

## Retraction 1 — cross-detector agreement on f is not evidence

The withdrawn claim was that six of eight methods agreeing on f = 0.2278 ±
0.007 validated the cross-radio coincidence model. Two controls kill it.

| Join | f range | spread |
|---|---|---:|
| Real join: radio A and radio B of the same sweep | — | 0.050 |
| Radio A of one sweep to radio B of a *different* sweep, minutes apart | 0.637–0.713 | 0.075 |
| Radio B shifted by two instants within one sweep | — | 0.045 |

The second row is a join for which the coincidence model is *definitionally
false* — the two radios are looking at sky minutes apart — and the check
passes on it. The third row is *tighter* than the real join. A test that
cannot fail is not a test.

The root cause is that the eight detectors are not eight independent opinions.
Over 2,160 observations they correlate with each other at φ 0.82–0.94. They
are near-duplicates, so their f estimates must agree whatever the input,
including on inputs where the model being validated is known to be wrong.

The corollary is uncomfortable and is stated rather than buried: **"which
detector is best" may be the wrong question at this corpus size.** At φ
0.82–0.94 the eight are not distinguishable, and any ranking among them is
reading noise.

## Retraction 2 — the differentials were never shown to be miscalibrated

The withdrawn claim was that `differential-16` and `differential-32` implied a
detection probability d > 1, which is impossible, and that their effective
false-alarm rate was 2–3× the nominal 1%.

That inference assumed the nominal rate. The repository carries it as a
default, not a measurement — `DEFAULT_FALSE_ALARM_RATE = 0.01` in
`src/leo_tracker/radio/beacon/survey_comparison.py` — and the same module
already warns that "a threshold from a finite null bounds a false-alarm rate;
it does not pin one."

Measured from the cross-edge null arms, the effective rate is ≈6.5%. At the
measured value every method, including both differentials, lands in physical
range. The impossible d > 1 was an artefact of the assumed denominator. The
differentials are not exonerated by this — nothing here shows them to be good
— but the specific charge against them is withdrawn.

## Retraction 3 — there is a cliff, and it is the CFO search span

The withdrawn claim was that there is no detection cliff at the pilot guard
band and that only 1.25 MS/s is handicapped. The conclusion that 1.25 MS/s is
handicapped survives (see below); the reasoning about the guard band does not.

The pipeline already computes a bias-corrected offset — `residual_cfo_hz`,
populated on 38,400 certificates in the scored census — and it had never been
used as a binning axis. Re-binned on it, all three live ports collapse onto one
curve and a cliff appears at every rate whose pilot band fits.

`differential-32` detection percentage by corrected-offset bin (bins 50 kHz
wide, 0–450 kHz):

| MS/s | 0–50 | 50–100 | 100–150 | 150–200 | 200–250 | 250–300 | 300–350 | 350–400 | 400–450 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2.5 | 10.6 | 25.3 | 36.4 | 45.3 | 39.0 | 40.0 | 26.7 | 0.0 | 1.2 |
| 5.0 | 17.0 | 33.5 | 43.6 | 55.9 | 31.0 | 19.7 | 12.1 | 2.0 | 1.9 |
| 10.0 | 24.1 | 49.0 | 50.6 | 60.4 | 38.9 | 28.6 | 10.8 | 1.6 | 1.8 |

The collapse falls between the 300–350 and 350–400 kHz bins in all three rows.
The pilot guard bands at those rates, read from the scored records, are:

| Sample rate | `pilot_guard_hz` | `pilot_band_fits` |
|---:|---:|---|
| 1.25 MS/s | −312,500 | false |
| 2.5 MS/s | +312,500 | true |
| 5.0 MS/s | +1,562,500 | true |
| 10.0 MS/s | +4,062,500 | true |

The guard spans a 13× range across the three live rates and the cliff does not
move. A rate-independent ≈350 kHz tolerance is the CFO search span, not the
pilot band. The withdrawn report reached a partly correct destination by an
argument the data does not support, which is worse than being wrong, because it
would have survived review unexamined.

## What survives and is actionable

### `lnb-c` carries a large LO bias and is the best port on site

The scored records apply `receiver_centers_hz` at scoring time. Two distinct
values appear across the census, one per radio:

| Radio | Receivers | `receiver_centers_hz` |
|---|---|---|
| `pluto-19f2` | `lnb-c`, `lnb-d` | `[604159.8, 0.0]` |
| `pluto-5d4d` | `lnb-a`, `lnb-b` | `[1170.0, 0.0]` |

`lnb-c` therefore sits +604.2 kHz off. That is well past the ≈350 kHz tolerance
found above, which is enough to make a healthy port look dead. It is **not** a
weak receiver. Bias-corrected it is the best port on site:

| Port | 5 MS/s, corrected offset 100–200 kHz | n |
|---|---:|---:|
| `lnb-c` | 61.4% | 347 |
| `lnb-b` | 41.7% | 458 |

**Correction to the brief that produced this report.** `hardware/epochs.json`
records `lnb-c` at +430,700 Hz (n = 13, SE 12,600), and that was offered as
independent corroboration of the +604.2 kHz figure. It is not. That measurement
belongs to epoch `pluto-19f2.lnb-cd.gen1`, which ended 2026-08-13T04:38:23Z; the
units were replaced and epoch `gen2` begins 2026-08-13T04:46:20Z with
`"measured": null` and a pending action to run `lo_sweep` once ~40 post-swap
probes exist. These scans are all `gen2`. The +430,700 Hz number describes a
*different physical unit*. The epochs file itself warns about exactly this
trap: "Receiver labels are reused across hardware changes… Split on capture
start time, not on label." The +604.2 kHz value stands on its own — it is
recorded directly in the corpus and was applied at scoring time — but it has
one measurement behind it, not two.

Operational fix: correct the bias, or widen the CFO search past 350 kHz.
**Do not abandon 2.5 MS/s.** That was the earlier, wrong recommendation and it
followed from the mis-binned analysis in Retraction 3.

### 1.25 MS/s is genuinely poor

f = 0.015 on the 160 ms arm. The mechanism is not subtle: the eight edge-pilot
subcarriers span 1.875 MHz and only 1.25 MHz is sampled, so `pilot_guard_hz` is
negative — −312,500 Hz, exactly the (1.875 − 1.25)/2 overhang — and
`pilot_band_fits` is `false` on all 1,263 radio-arms at that rate. The radio
returns healthy-looking IQ regardless. These arms must not be pooled with arms
that fit.

### `lnb-a` is dead

`lnb-a` (rx0 on `pluto-5d4d`) died at 2026-08-13 04:44 UTC, returning a flat
≈1.19 peak-to-median at every tuning — no signal path rather than a weak one.
It is excluded from both the target and the null arms throughout. That
exclusion is load-bearing: a dead port's null is silence, and leaving it in
would drag every threshold down and inflate every detection rate scored against
those thresholds.

One coincidence is recorded without a claim attached. The `lnb-c`/`lnb-d` swap
on the *other* radio has an uncertainty window of 04:38:23Z–04:46:20Z on
2026-08-13 in `hardware/epochs.json`, and `lnb-a` died at 04:44 UTC — inside
it, on a radio the swap did not touch. Physical work on site at that moment is
a plausible common cause and should be checked against the bench log before
`lnb-a` is written off as a component failure.

### The collector bug cost 893 sweeps, not ~400

A USB dropout wedged the collector's reopen path. The reopen block deleted the
context with `del ctx[name]`, which raised before the retry below it could ever
run, turning one dropped USB device into a permanently dead radio. It is fixed
by using `ctx.pop(name, None)`; the live collector now carries the fix and the
comment explaining it.

The measured extent is larger and earlier than the brief for this report
stated, and the corrected figures are:

| | Stated in brief | Measured |
|---|---|---|
| Failed reopens | 888 | 893 |
| Single-radio sweeps | ~400 | 893 |
| Window | 04:18 – 05:07 UTC | 03:24:15Z – 05:07:41Z |
| Episodes | 1 | 1 (contiguous, no gaps) |

`grep -c` on `/mnt/leo-nvme/leo-tracker/sync-scans/collector.log` gives 893
failed reopens, of which **exactly one** is the genuine hardware event —
`reopen pluto-5d4d FAILED: expected one USB Pluto with serial …5d4d, found 0` —
and the other **892** are `reopen pluto-5d4d FAILED: 'pluto-5d4d'`, the
`KeyError` from the bug. One real USB dropout produced 892 self-inflicted
failures. The surviving radio was `pluto-19f2` in all 893 cases.

Those 893 sweeps are single-radio and cannot form pairs. Paired capture is
restored and verified: the single-radio run is one contiguous block and every
sweep after 2026-08-14T05:07:41Z is paired.

### Synchronisation is looser than the analysis assumed

Skew is recorded at **barrier release**, not at first sample, and is therefore
a **lower bound** on the true sample-start offset. The collector computes it as
`1000*abs(x-y)` over the two radios' barrier arrival times.

Two independent measurements, both reported because they disagree:

| Population | median | max | over 0.054 ms |
|---|---:|---:|---|
| First 87 sweeps (the withdrawn analysis) | 0.0436 ms | 8.3409 ms | 200 of 696 (28.7%) |
| All 1,549 paired sweeps (this census) | 0.0460 ms | 11.5125 ms | 4,116 of 12,392 (33.2%) |

The whole-corpus recheck is worse than the subset in both tail and failure
rate. Per-sweep medians give 0.0449 ms; p90 is 0.0881 ms and p99 is 2.0620 ms.

Two caveats belong with these numbers.

**Which specification.** Against the 0.054 ms bound used by the coincidence
analysis, a third of all paired tunings fail. Against the operator target of
0.2–0.5 s recorded in the share README, every tuning passes by four orders of
magnitude. The bound the coincidence model actually requires has not been
derived, so "worse than specified" is true of one specification and false of
the other. Deriving the required bound is a prerequisite for interpreting any
of this.

**The recorded skew does not see the dominant term.** Same-order and
opposite-order sweeps have almost identical barrier-release skew — median
0.0455 ms (n = 5,952 tunings) against 0.0463 ms (n = 6,440). The share README
states that the true sample-start offset is ≈0.2–0.8 ms on same-order sweeps
and ≈4 ms on opposite-order sweeps, because the two radios write different
local-oscillator frequencies after the barrier releases and those writes take
different times. A ≈5× difference in the real offset appears as a 2% difference
in the recorded one. The recorded number is measuring the barrier, not the
radios.

### Simultaneity appears to matter — as a hypothesis, not a result

Cells within the skew bound give f 0.310–0.372; cells beyond it give f
0.220–0.288. The intervals do not overlap. Given everything above — that the
detectors are near-duplicates, that f is not behaving like a sky parameter, and
that the recorded skew is a lower bound blind to the dominant term — this is
the next experiment, not a finding.

### f is not behaving like a sky parameter

If f were sky occupancy it should be roughly invariant across the instrument
and vary with the sky. It does the opposite. It moves more across instrumental
factors than across the detectors that are supposed to be estimating it:

| Factor | Movement in f |
|---|---|
| Receiver pair | +0.072, same sign in 8 of 8 |
| Arm | 0.015 to 0.510 |
| Channel | 0.211 to 0.436 |
| Algorithm | smallest of the four |

A parameter that is stable across estimators and unstable across the hardware
is a property of the hardware.

## What this corpus cannot decide

Stated plainly, because the withdrawn version did not state it.

**There is no signal injection at this site.** No detection probability in this
report was measured against a known input. Every one is a model output inferred
from a coincidence model whose assumptions are the thing in question. Where
this report writes "detection %", read "rate at which this detector fired,
under a threshold calibrated on a finite null, on sky whose contents are
unknown."

**Cross-radio was not shown to beat within-radio.** This was the entire
motivation for building the two-radio apparatus, and the corpus does not
support it:

| Estimator | cells | f spread | φ |
|---|---:|---:|---|
| Within radio (`lnb-c` \| `lnb-d`) | 720 | 0.077 | 0.49–0.59 |
| Cross radio | 1,440 | 0.050 | 0.52–0.58 |

The spreads overlap, and cross-radio has twice the cells, which accounts for
most of the apparent difference. The physical argument for independence —
separate LNBs, separate Plutos, separate USB controllers on separate buses —
remains sound as an argument. It remains an **assumption**, not a measurement,
and the substitute-for-injection role the share README assigns to cross-radio
agreement is not yet earned.

**The detectors are not distinguishable at this corpus size.** φ 0.82–0.94.

## What to run next

1. **Measure the `gen2` LO offsets.** `hardware/epochs.json` has a pending
   action to run `lo_sweep` on epoch `pluto-19f2.lnb-cd.gen2` once ~40 post-swap
   probes exist. The corpus now has far more than 40. Do this first: it turns
   the +604.2 kHz figure from one number into a measurement with an error bar,
   and it is the only item here that is cheap, decisive, and blocked on nothing.
2. **Widen the CFO search past 350 kHz and re-score one arm.** If the cliff is
   the search span, widening it should move the cliff and lift `lnb-c` further.
   If the cliff does not move, the ≈350 kHz explanation is wrong and should be
   abandoned rather than patched.
3. **Fix the skew measurement before using skew as a covariate.** Move the
   retune before the barrier so the recorded number is the sample-start offset
   rather than the barrier-release offset, and bump the schema version so
   records say which build produced them. Until then the
   within-bound/beyond-bound split above cannot be interpreted.
4. **Derive the skew bound the coincidence model actually needs.** 0.054 ms and
   0.2–0.5 s cannot both be the requirement. Until one of them is derived from
   the model, "synchronisation is adequate" is not a checkable statement.
5. **Stop treating the eight detectors as eight votes.** At φ 0.82–0.94 they
   are close to one detector counted eight times. Either pick a genuinely
   different statistic — the `frame_max` combiner already carried beside every
   conditioned score is the obvious candidate — or report one detector and its
   null, and drop the agreement framing entirely.
6. **Settle injection.** Every limitation in this report traces back to having
   no known input. A single injected tone of known amplitude at a known offset
   would convert the whole corpus from model output to measurement. This is the
   highest-value item on the list and the only one that changes what the
   apparatus is capable of proving.
7. **Check the bench log for 2026-08-13 04:38–04:46 UTC** before writing
   `lnb-a` off as a component failure.

## Provenance and reproduction

Raw sweeps and the format README: `/mnt/qnap01/mouse9911/leo-scans`
(read-only from the analysis host). Corpus entries:
`/mnt/qnap01/mouse9911/leo/surveys/corpus`. Collector, drain and import
implementations: `/mnt/leo-nvme/leo-tracker/bin/`.

Every count in the census section was read from the share at 2026-08-14T05:36Z
by walking `sweep.json`. The census is reproducible with:

```bash
# committed sweeps, paired vs single, matched-arm fraction, skew
nice -n 15 python3 - <<'PY'
import json, glob, os, statistics
rows = []
for d in sorted(glob.glob("/mnt/qnap01/mouse9911/leo-scans/sync-*")):
    p = os.path.join(d, "sweep.json")
    if not os.path.exists(p):
        continue
    s = json.load(open(p))
    live = [r for r, v in s["radios"].items() if not v.get("error")]
    rows.append((s["utc"], len(live), s))
print("committed", len(rows))
print("paired", sum(1 for r in rows if r[1] == 2),
      "single", sum(1 for r in rows if r[1] < 2))
print("matched_arm", sum(1 for r in rows if r[2].get("matched_arm")) / len(rows))
per = [v for r in rows if r[1] == 2 for v in r[2]["skew_ms"]["per_tuning"]]
print("skew median %.4f ms  max %.4f ms  over 0.054 ms %d/%d"
      % (statistics.median(per), max(per),
         sum(1 for v in per if v > 0.054), len(per)))
PY

# the outage: one genuine USB loss, 892 self-inflicted KeyErrors
nice -n 15 grep -c "FAILED: expected one USB Pluto" \
  /mnt/leo-nvme/leo-tracker/sync-scans/collector.log
nice -n 15 grep -c "FAILED: 'pluto-5d4d'" \
  /mnt/leo-nvme/leo-tracker/sync-scans/collector.log

# the LO bias, as applied at scoring time
nice -n 15 python3 -c "import json; print(json.load(open(
  '/mnt/qnap01/mouse9911/leo/surveys/corpus'
  '/sync-20260814T000315Z-pluto-19f2/scores.json'))['receiver_centers_hz'])"
```

The collector, drain and import services were running throughout and were not
stopped, started or restarted for this report; the radios were not touched. All
share and NVMe paths were read only.

## Status of the withdrawn report

The superseded version of these findings should not be cited. Its three
headline claims are withdrawn in full, and its recommendation to abandon
2.5 MS/s is withdrawn with them. The apparatus, the transfer chain, the corpus
and the format documentation are unaffected by the retraction and remain
usable. The corrected record is this document.
