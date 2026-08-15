## 11. Ground truth at last: measured detection probability

Every number before this section is inferred. This one is measured.

Two bench radios carry a closed loopback — `TX2` into a splitter and fixed
attenuation, into `RX1` and `RX2`, with no antenna. Transmitting the
repository's own `pilots.edge_pilot_frame` puts the exact signal the eight
detectors hunt onto a channel whose occupancy, amplitude and carrier offset are
set rather than guessed. That converts every question in this report from a
model output into a measurement — for the detectors and the digital pipeline,
though **not** for the LNBs, the antenna or the sky, none of which the cable
contains.

### 11a. Measured ranking under one condition — {{s11_probe_ms}} ms, {{s11_sample_rate_msps}} MS/s, cabled loopback

> **Superseded in part.** The ranking below compares the eight at {{s11_far_per_point_pct}}% per
> *candidate point*, which leaves their per-*cell* false-alarm rates spread over
> {{s11_cell_far_old_min}}–{{s11_cell_far_old_max}}% — not equal operational cost. Redrawing thresholds to a common {{s11_far_per_cell_new_pct}}% per
> cell moves `glrt-32` from first to third and collapses the resolved pairs from
> {{s11_pairs_resolved_old}} to {{s11_pairs_resolved_familywise_old}} of {{s11_pairs_total}}. See
> [section 15b](#15b-at-equal-false-alarm-cost-the-ranking-changes-and-the-head-dissolves).
> The SNR50 values themselves reproduce to {{s11_snr50_reproduction}} dB; only the threshold changed.

SNR at 50% detection, {{s11_probe_ms}} ms probes at {{s11_sample_rate_msps}} MS/s, thresholds set to {{s11_far_per_point_pct}}% per point on
a genuinely empty channel, {{s11_cells_per_rung}} cells per rung across {{s11_transmitting_rungs}} rungs:

| Rank | Detector | SNR at Pd = 0.5 | | Rank | Detector | SNR at Pd = 0.5 |
|---:|---|---:|---|---:|---|---:|
| 1 | `glrt-32` | **{{s11_snr50_glrt32}} dB** | | 5 | `glrt-64` | {{s11_snr50_glrt64}} dB |
| 2 | `full-frame-verify` | {{s11_snr50_ffverify}} dB | | 6 | `anchor-8` | {{s11_snr50_anchor8}} dB |
| 3 | `full-frame-acquire` | {{s11_snr50_ffacquire}} dB | | 7 | `differential-32` | {{s11_snr50_diff32}} dB |
| 4 | `full-frame-full` | {{s11_snr50_fffull}} dB | | 8 | `differential-16` | {{s11_snr50_diff16}} dB |

Against the model's `d` ranking, Spearman rho = **{{s11_spearman_model_d}}**. Against the raw
fire-count ranking, **{{s11_spearman_fire_count}}**. Both are indistinguishable from zero on eight
points: **neither ranking in this report carries information about which
detector was more sensitive under this condition.**

Read the measured order as a **partial** one. The spread is only {{s11_snr50_spread}} dB; {{s11_pairs_resolved}} of
{{s11_pairs_total}} pairs resolve under a paired bootstrap, but that is {{s11_pairs_total}} comparisons from one
bootstrap ensemble with no family-wise correction, and rank uncertainty is not
propagated into the Spearman figures. What survives that caution is: `glrt-32`
and the three full-frame variants form the leading group, `differential-16` is
materially worst, and several internal orderings — including `glrt-32` against
`full-frame-acquire` — are unresolved.

**And the comparison is not yet at equal operational cost.** Every threshold is
calibrated to 1% per candidate *point*, but the methods' measured empty-channel
*cell* rates differ ({{s11_cell_far_min}}–{{s11_cell_far_max}}%), so they are not being compared at a common
false-alarm rate. Re-running at fixed cell-level FAR is the right form of this
experiment and has not been done.

This ranking holds for {{s11_probe_ms}} ms probes at {{s11_sample_rate_msps}} MS/s, on a cabled loopback, at one
occupancy schedule, with near-zero natural carrier offset. It should not be read
as a general statement across {{s11_arm_probe_min_ms}}–{{s11_arm_probe_max_ms}} ms, {{s11_arm_rate_low_msps}}–{{s11_arm_rate_high_msps}} MS/s, sparse occupancy, Doppler
rate, LO drift, timing error, or anything involving an LNB or an antenna. The model's second most
confident claim — `full-frame-acquire` worst of eight — is exactly inverted: it
measures third, and its gap from `glrt-32` is not resolvable. The differentials
the model placed mid-field measure worst, and `differential-16` is significantly
worse than all seven others.

![Detection probability against measured SNR, one curve per detector](figures/injection/detection-vs-snr.png)

*Pd against measured SNR on the cabled loopback. SNR is estimated empirically at
each rung from signal-present against TX-off power in the same configuration,
not assumed from the gain setting.*

![The three rankings side by side](figures/injection/detector-ranking.png)

*The measured ranking against the model's and the fire count's. Spearman {{s11_spearman_model_d}}
and {{s11_spearman_fire_count}} respectively.*

### 11b. The coincidence estimator works. Its consistency check never could.

With occupancy set by hand to `f_true` = {{s11_f_true}} at an SNR where Pd ~ {{s11_pd_operating}}:

- The eight point estimates span **{{s11_f_recovered_min}}–{{s11_f_recovered_max}}** — and every one of them lies
  **above** the true {{s11_f_true}}. They do not bracket it. Mean bias is **{{s11_mean_bias}}**,
  and all eight read high in {{s11_all_high_share}} of resamples. The plotted intervals are
  central bootstrap percentile intervals.
- **Null** false alarms are approximately independent: on known-empty cells,
  P(AB) − P(A)P(B) <= {{s11_empty_excess_max}}. That is *not* conditional independence, which the
  model also requires on **occupied** cells, where P(AB | T=1) must equal
  d_A·d_B. On this single-radio rig the occupied-cell excess is materially
  negative — about {{s11_occ_excess_anchor8}} for `anchor-8`, {{s11_occ_excess_diff16}} for `differential-16`, {{s11_occ_excess_diff32}}
  for `differential-32` — consistent with the shared oscillator biasing the
  recovered `d`. The occupied-cell test on independent radios has not been run.
- And the eight algorithms disagree about `f` by **{{s11_f_spread_loopback}} on data where `f` is
  one number by construction** — *larger* than the {{s11_f_spread_sky}} spread this report
  observes on sky, and larger in {{s11_spread_exceeds_sky}} of bootstrap resamples.

That last line reframes [section 6](#6-did-it-work-the-negative-controls). The
negative controls showed the agreement check cannot fail. This shows the
quantity it measures was never diagnostic: **a spread of that size is what a
correct model produces.** The solver partly works under these conditions — it
brackets truth at {{s11_d1_level_mid}} and {{s11_d1_level_high}} and reads low at {{s11_d1_level_low}} — while the instrument used
to police it returns `VACUOUS` at every level. Neither statement extends to the sky
corpus, whose heterogeneity in `p`, `d`, arm, receiver and time is listed in
[section 5b](#5b-what-the-model-assumes-and-what-this-corpus-already-contradicts).

One systematic bias appeared here — the solver reading `d` **low** against
direct measurement in {{s11_d_low_cases}} of {{s11_d_prior_cases}} cases — but it does **not** survive independent
radios and is withdrawn in
[section 13c](#13c-one-earlier-finding-reverses-under-independence). It was a
property of this rig's shared oscillator, not of the estimator.

![Recovered f against the f that was set](figures/injection/coincidence-recovery.png)

*Recovered occupancy against known truth, with the across-algorithm spread on
the same axis.*

### 11c. The {{s11_sky_cliff_lo_khz}}–{{s11_sky_cliff_hi_khz}} kHz cliff is not an intrinsic detector or hardware limit

Injecting at a **known** imposed offset removes the circularity of plotting
against an offset the pipeline itself estimates. At {{s11_snr_high}} dB SNR all eight hold
Pd = {{s11_pd_high_offset}} out to {{s11_offset_high_khz}} kHz. Repeated at the detection knee, so tolerance cannot
simply be bought with signal:

| Imposed offset | Pd, all eight |
|---|---|
| {{s11_offset_flat_lo_khz}} – {{s11_offset_flat_hi_khz}} kHz | flat, including straight through {{s11_sky_cliff_lo_khz}}–{{s11_sky_cliff_hi_khz}} kHz ({{s11_pd_knee_min}}–{{s11_pd_knee_max}}) |
| {{s11_offset_flat_hi_khz}} – {{s11_offset_fall_hi_khz}} kHz | hard fall, 50% crossing at **{{s11_collapse_min_khz}}–{{s11_collapse_max_khz}} kHz**, all eight together |

{{s11_collapse_min_khz}}–{{s11_collapse_max_khz}} kHz is the coarse-E bank's own ±{{s11_bank_span_khz}} kHz span. Received power is flat to
{{s11_received_flatness}} dB across the transition, so it is not the analogue filter. The second
radio reproduces this independently: per-cell detection {{s11_165_per_cell_pct}}% at every offset out
to {{s11_165_offset_max_khz}} kHz, with one exception.

**So the on-sky cliff is not an intrinsic tolerance of the detectors or of the
hardware.** The `~{{s11_sky_cliff_lo_khz}} kHz tolerance` framing in
[section 9](#9-what-survives-the-instrument) is withdrawn.

**But note what this experiment could and could not separate, because an
earlier draft of this section overreached.** It ran one bank — coarse-E — so a
detector limit and that bank's own geometry are perfectly confounded in it. The
50% crossing landing on coarse-E's ±{{s11_bank_span_khz}} kHz span is not
incidental to that; it is the clue. What this rules out is an *intrinsic*
{{s11_sky_cliff_lo_khz}} kHz limit, which is real and worth having: the
detectors sail straight through the sky's cliff at a known offset.

What it does **not** rule out is search geometry, and the earlier draft claimed
it did. [Section 16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs)
settles it by running both banks against the same signal: change the bank and
the knee moves with it.

![Detection against imposed offset](figures/injection/offset-cliff.png)

*Detection against a known imposed carrier offset. The fall is at the bank
edge, not at {{s11_sky_cliff_lo_khz}}–{{s11_sky_cliff_hi_khz}} kHz.*

### 11d. The thresholds are calibrated — and one null in the repository is not

Measured on truly empty input, independently on both radios:

| | radio `.183` | radio `.165` | nominal |
|---|---|---|---|
| per point | {{s11_183_point_min}}–{{s11_183_point_max}}% | {{s11_165_point_min}}–{{s11_165_point_max}}% | {{s11_far_per_point_pct}}% |
| per cell | {{s11_cell_far_min}}–{{s11_cell_far_max}}% | {{s11_165_cell_min}}–{{s11_165_cell_max}}% | {{s11_predicted_cell_183}}–{{s11_predicted_cell_165}}% predicted by 1 − 0.99^k |

Both bracket the on-sky {{s11_sky_cell_min}}–{{s11_sky_cell_max}}%. **The sky null rate is fully explained by
candidate multiplicity; no residual sky energy is required to account for it.**
This is the measurement behind the correction in
[section 2](#2-the-hard-part-nothing-was-injected).

One caveat the two radios do not fully agree on: `.165` finds the sky null
hotter than a cable null in the **tail only** — its median {{s11_sky_null_p50_165}} sits below the
cable null's, its p99 {{s11_sky_null_p99_165}} above — while `.183` finds the empty-channel band
brackets sky outright. The tail is what a threshold is made of, so this is
worth resolving.

**A defect that should be fixed.** The repository contains two cross-edge nulls
and only one is sound:

| | What it does | On empty input |
|---|---|---|
| `cross_radio.null_thresholds` | runs the opposite edge as its own target, with its own bank and points | thresholds {{s11_null_arm_ratio_min}}–{{s11_null_arm_ratio_max}}× truth — **valid**, and this is what the published `f` and `d` rest on |
| `survey_comparison.conditioned_comparison` | scores the opposite template at points the *target-edge* detectors selected | thresholds to **{{s11_conditioned_ratio_min}}×** truth; fires on **{{s11_conditioned_cell_min_pct}}–{{s11_conditioned_cell_max_pct}}% of cells** for five of eight |

The second compares an unselected draw against a maximised one. Its docstring
claims no screening on the statistic being calibrated; the screening is in the
point selection rather than the template. The bias is concentrated in the GLRT
and full-frame families, with `differential-*` and `anchor-8` near-unbiased.

### What this section does not establish

The loopback shares one oscillator between transmit and receive, so the injected
signal arrives at a negligible offset and the detectors never had to search
frequency — which is why 11c had to impose offset explicitly, and why nothing
here speaks to LO drift or to the water on the `lnb-c`/`lnb-d` bias tees. `RX1`
and `RX2` share an LO and carry common-mode noise power; it produced no
correlated false alarms, but two radios on sky are not quite this pair. One
probe length, one sample rate, and a single occupancy and SNR for 11b. And no
LNB, antenna or sky anywhere in the path.


