## 13. The same experiment, on two radios, with the answer known

[Section 11](#11-ground-truth-at-last-measured-detection-probability) used one
radio, where `RX1` and `RX2` share an oscillator and carry common-mode
noise — the *dependent* configuration this report argues elsewhere is unusable.
This section removes that objection. Two separate radios, each with its own
loopback, own oscillator, own clock and no RF path to the other, are driven from
one process on a shared occupancy schedule. They have in common exactly one
thing: whether a pilot frame was transmitted at instant *k*. That is what `f`
means, and here it is set rather than inferred.

Every structural property of the sky configuration is reproduced. The one
unknown becomes a dial.

### 13a. What the hardware actually returned

**A correction first, because it changes what this section is.** When these runs
were first written up, the figures were built from `results_dry.json` — the
output of `dryrun.py`, which exercises the analysis over `selftest.py`'s
**synthetic** scores with a hard-coded `PD` and no radio in the loop. The numbers
published here were therefore a simulation, not the two-radio rig this section
describes. The committed `runs.tar.gz` has since been driven through the same
`report.py` path and the hardware results are below. They are noisier, more
level-dependent, and at the lowest occupancy the estimator fails outright.

{{s13_cells_per_level}} cells per level, both radios, occupancy set by a seeded schedule:

| `f` set | `f` realised | Recovered across the eight | Brackets truth? | Spread |
|---:|---:|---|:--:|---:|
| {{s13_f_set_a}} | {{s13_f_realised_a}} | {{s13_recovered_lo_a}} – {{s13_recovered_hi_a}} | **no** — all eight read low | {{s13_spread_a}} |
| {{s13_f_set_b}} | {{s13_f_realised_b}} | {{s13_recovered_lo_b}} – {{s13_recovered_hi_b}} | yes | {{s13_spread_b}} |
| {{s13_f_set_c}} | {{s13_f_realised_c}} | {{s13_recovered_lo_c}} – {{s13_recovered_hi_c}} | yes | {{s13_spread_c}} |

At {{s13_f_set_b}} and {{s13_f_set_c}} the eight estimates bracket the realised occupancy. At {{s13_f_set_a}}
every one of them lands **below** it — the solver is not merely noisy there, it
is biased low. So the claim that the estimator recovers a known occupancy holds
at moderate and high occupancy and **fails at low occupancy**, which is the
regime the sky corpus mostly occupies.

![Recovered f against the f that was set](figures/injection/fig_d1_recovered_f.png)

*Rebuilt from the hardware `results.json`. At {{s13_f_set_a}} every one of the eight sits
below the truth line; at {{s13_f_set_b}} and {{s13_f_set_c}} they straddle it. Whiskers are 5th–95th
percentile over {{s13_draws}} cell resamples.*

### 13b. And the check certifies nothing, on any of them

The same runs, scored by the check the model uses to validate itself, on data
where the model is **true by construction**:

| `f` set | Verdict | Controls separate? |
|---:|---|---|
| {{s13_f_set_a}} | `{{s13_verdict_a}}` | no |
| {{s13_f_set_b}} | `{{s13_verdict_b}}` | no |
| {{s13_f_set_c}} | `{{s13_verdict_c}}` | no |

`certified: false` at all three. Three levels, three refusals, on hardware where
the answer is known and correct. **The check does not merely fail to detect a
broken model — it declines to certify a correct one, every time it is asked.**

(An earlier version of this section reported three *different* verdicts across
the three levels and read a mechanism into each. That came from the synthetic
run. The hardware is simpler and says the same thing more plainly.)

### The spread argument, corrected for sample size

This section previously argued that the injected spreads were **wider** than the
{{s13_sky_reference_spread}} seen on sky, and that the sky figure was therefore unusually tight. That
comparison was not like for like: the injected levels rest on **{{s13_injected_cells}} cells each**
against roughly **{{s13_sky_cells}}** for the sky join, and spread shrinks with sample size.

Matching n settles it. Resampling the sky join to {{s13_injected_cells}} cells by **sweep** — the
honest unit, since cells within a sweep share time, hardware and arm — over
{{s13_nmatch_draws}} draws:

| | Spread |
|---|---|
| Sky at n = {{s13_injected_cells}} | median **{{s13_sky_n500_median}}**, p05–p95 {{s13_sky_n500_p05}} – {{s13_sky_n500_p95}} |
| Hardware injected, n = {{s13_injected_cells}} | {{s13_inj_spread_a}}, {{s13_inj_spread_b}}, {{s13_inj_spread_c}} — percentiles **{{s13_inj_pct_a}}, {{s13_inj_pct_b}}, {{s13_inj_pct_c}}** |
| Sky at full n = {{s13_sky_cells}} | observed **{{s13_sky_full_observed}}**, percentile **{{s13_sky_full_percentile}}** of its own-n distribution |

And the shrinkage curve accounts for the entire original gap: median spread runs
{{s13_curve_128_p50}} at n = {{s13_curve_128_n}}, {{s13_curve_500_p50}} at {{s13_curve_500_n}}, {{s13_curve_2000_p50}} at {{s13_curve_2000_n}}, {{s13_curve_full_p50}} at {{s13_curve_full_n}}. **{{s13_sky_reference_spread}} at
n = {{s13_curve_full_n}} and the injected spreads at n = {{s13_injected_cells}} are the same number at two
sample sizes.**

So both directions of the original claim are dead. The sky's {{s13_sky_full_observed}} is
unremarkable for its own n, sitting at percentile {{s13_sky_full_percentile}}. The hardware spreads
straddle the sky distribution rather than exceeding it. What survives — and
survives on the synthetic and hardware sets alike — is the weaker and sufficient
claim: **a spread of {{s13_envelope_low}}–{{s13_envelope_high}} is simply what this estimator produces with eight
near-duplicate detectors at these sample sizes.** The consistency check rejected
a join whose spread is median for its size.

![Sky spread at matched n against the injected values](figures/injection/nmatch_spread.png)

*Sky f-spread resampled to n = {{s13_injected_cells}} by sweep, with the injected values marked;
the sky join at full size with its observed {{s13_sky_full_observed}}; and the spread-versus-n
shrinkage curve that explains the original discrepancy.*

### 13c. One earlier finding reverses under independence

The single-radio run found the solver reading `d` **low** against direct
measurement in {{s13_d2_prior_low}} of {{s13_d2_prior_cases}} cases, and this report carried that as a caveat: that
the published `d` values are floors rather than estimates.

On two independent radios the bias does not vanish — **it reverses.** It reads
low in only **{{s13_d2_reads_low}} of {{s13_d2_cases}}** cases, which is to say it reads *high* in {{s13_d2_reads_high}}, with a
median bias of **{{s13_d2_median_bias}}** and a worst case of **{{s13_d2_worst}}**. The direction is
systematic, not noise.

So the floors caveat is withdrawn: with the shared oscillator removed, the
solver **over-estimates** `d` *in this configuration*.

**It does not follow that the sky values are ceilings, and an earlier draft of
this section said they were.** This rig is homogeneous by construction — one
signal strength, one arm, one pair of receivers, one occupancy, one false-alarm
rate. The sky corpus is none of those things, and
[section 5b](#5b-what-the-model-assumes-and-what-this-corpus-already-contradicts) is an argument for why the
homogeneous model does not transfer to it. Carrying a bias measured here across
that gap is exactly the move that section warns against.

What stands: in the independent-radio loopback the solver reads `d` high. **No
direction of bias is established for the heterogeneous sky estimates**, which
remain model outputs — neither upper nor lower bounds.

![Recovered d against directly measured d](figures/injection/fig_d2_d_bias.png)

### 13d. The joint null

The model assumes false alarms are independent across chains and uses `p²` for
joint null firing, which has never been checked against a measured joint null.
With both radios silent over {{s13d_silent_instants}} instants, the measured joint rate tracks the
product of the marginals: **{{s13d_consistent}} of {{s13d_cases}}** algorithm-by-threshold cases are
consistent with independence, the smallest Fisher exact p-value among them being
{{s13d_min_fisher_p}}. The lowest-rate points rest on very few coincidences and should not be
read as a violation.

![Measured joint null against the p-squared assumption](figures/injection/fig_d5_joint_null.png)

### What this section still does not reach

Both rigs are cabled loopbacks: no LNB, no antenna, no sky, and no genuine
carrier offset, since each radio's transmit and receive share one reference. It
tests the estimator and the detectors, which is what was in question — but the
per-receiver −150 kHz LO errors of
[sections 12](#12-what-the-cliff-actually-is) and
[16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs), the water on the `lnb-c` and
`lnb-d` bias tees, and the LNB chain generally are all outside what a cable can
say anything about.
