## 15. Three experiments that revise the ranking

### 15a. Conditional independence holds — and the prior finding was never significant

The coincidence model needs P(AB | T=1) = d_A·d_B on **occupied** cells, and only
the empty-cell version had ever been tested. On two independent radios at a
common {{s15a_far_pct}}% per-cell false-alarm rate, {{s15a_occupied_cells}} occupied cells, Pd {{s15a_mean_pd_a}} and {{s15a_mean_pd_b}}:

| Detector | Two-radio excess | 95% CI | Single-radio prior |
|---|---:|---|---:|
| `anchor-8` | {{s15a_excess_a8}} | [{{s15a_ci_lo_a8}}, {{s15a_ci_hi_a8}}] | {{s15a_prior_a8}} |
| `differential-16` | {{s15a_excess_d16}} | [{{s15a_ci_lo_d16}}, {{s15a_ci_hi_d16}}] | {{s15a_prior_d16}} |
| `differential-32` | {{s15a_excess_d32}} | [{{s15a_ci_lo_d32}}, {{s15a_ci_hi_d32}}] | {{s15a_prior_d32}} |
| `glrt-32` | {{s15a_excess_g32}} | [{{s15a_ci_lo_g32}}, {{s15a_ci_hi_g32}}] | {{s15a_prior_g32}} |
| `full-frame-verify` | {{s15a_excess_ffv}} | [{{s15a_ci_lo_ffv}}, {{s15a_ci_hi_ffv}}] | {{s15a_prior_ffv}} |

Mean excess **{{s15a_mean_excess}}**; **{{s15a_significant_bonferroni}} of {{s15a_detectors}}** intervals exclude zero under Fisher,
permutation, or a within-sweep stratified permutation with Bonferroni; empty-cell
control {{s15a_empty_mean_excess}}. **The assumption holds.**

But the single-radio prior this was meant to explain **was never significant on
its own data.** Recomputing it from the archived records reproduces {{s15a_prior_a8}} /
{{s15a_prior_d16}} / {{s15a_prior_d32}} exactly — at n = {{s15a_prior_cells}}, where the standard error is {{s15a_prior_se_mean}} and
**{{s15a_prior_covering}} of {{s15a_detectors}} intervals already covered zero.** So an effect of that size can now be
ruled out, but attributing the original number to the shared oscillator was
reading signal into small-sample noise. This report did that, and it was wrong to.

![Occupied-cell excess with intervals, two radios against the single-radio prior](figures/injection/a1-occupied-cell-independence.png)

### 15b. At equal false-alarm cost the ranking changes, and the head dissolves

[Section 11a](#11a-measured-ranking-under-one-condition--20-ms-5-mss-cabled-loopback)
ranked the eight at {{s15b_far_point_pct}}% per *point*, which left their per-*cell* rates spread over
{{s15b_cell_far_old_min}}–{{s15b_cell_far_old_max}}% — so they were not compared at equal operational cost. Redrawing
thresholds on the ladder's own TX-off rungs puts all eight at exactly **{{s15b_far_cell_new}} per
cell**. The pipeline reproduces the published SNR50 to **{{s15b_snr50_reproduction}} dB**, so only the
threshold changed:

| | Order |
|---|---|
| Published ({{s15b_far_point_pct}}% per point) | `glrt-32`, `ffv`, `ffa`, `fff`, `glrt-64` \| `anchor-8`, `diff-32`, `diff-16` |
| **Equal {{s15b_far_cell_new_pct}}% per cell** | **`ffv` {{s15b_snr50_new_ffv}}, `fff` {{s15b_snr50_new_fff}}, `glrt-32` {{s15b_snr50_new_g32}}, `ffa` {{s15b_snr50_new_ffa}}, `glrt-64` {{s15b_snr50_new_g64}}** \| `anchor-8` {{s15b_snr50_new_a8}}, `diff-32` {{s15b_snr50_new_d32}}, `diff-16` {{s15b_snr50_new_d16}} |

**`glrt-32` falls from first to third.** And the resolution collapses: the
published **{{s15b_pairs_marginal_old}} of {{s15_pairs_total}}** pairs becomes **{{s15b_pairs_familywise_old}} of {{s15_pairs_total}}** under a max-t family-wise band,
and {{s15b_pairs_familywise_new}} of {{s15_pairs_total}} under the new calibration. The defensible statement is a partial
order with an unordered head:

> `{ffv, fff, glrt-32, ffa, glrt-64}` — {{s15b_leading_spread}} dB apart, **no internal pair
> resolved** — beats `{anchor-8, differential-32}`, which beats `differential-16`.

So "`glrt-32` is the most sensitive detector" was never supported. What is
supported is a leading group of five, a trailing pair, and `differential-16` last.

![SNR50 at a common per-cell false-alarm rate](figures/injection/a2-common-false-alarm-ranking.png)

### 15c. And it is condition-dependent

| Arm | Spread | Pairs resolved |
|---|---:|---:|
| 80 ms / 2.5 MS/s | {{s15c_spread_25}} dB | {{s15c_pairs_25}}/{{s15_pairs_total}} |
| 80 ms / 1.25 MS/s | {{s15c_spread_125}} dB | {{s15c_pairs_125}}/{{s15_pairs_total}} |
| 640 ms / 2.5 MS/s | {{s15c_spread_640}} dB | {{s15c_pairs_640}}/{{s15_pairs_total}} |
| **160 ms / 5 MS/s** | **{{s15c_spread_160}} dB** | **{{s15c_pairs_160}}/{{s15_pairs_total}}** |

The two-group split survives in all three arms that resolve anything, and
`glrt-32` sits at rank {{s15c_glrt32_rank_baseline}}, {{s15c_glrt32_rank_125}} and {{s15c_glrt32_rank_25}} — consistent with an unordered head. **1.25 MS/s
costs {{s15c_rate_cost}} dB** against 2.5 MS/s at the same probe, confirming the pilot-band
argument in [section 4](#4-the-dataset-twelve-arms-and-two-geometries-for-free)
by measurement.

The most useful row is the last. At 160 ms / 5 MS/s **nothing resolves at all** —
{{s15c_pairs_160}} of {{s15_pairs_total}} pairs separate. **As probe length grows, detector
choice stops being an operational choice.** For a survey that can afford the
dwell, this is the finding that matters: pick any of them.

![SNR50 by arm, showing the ranking is condition-dependent](figures/injection/a3-ranking-across-arms.png)

*Caveats carried from the run: the 640 ms / 10 MS/s corner was unreachable on
probe time and capture size, so 640 ms / 2.5 MS/s was substituted; that arm's
trailing-group SNR50 is extrapolated past the ladder top and is not trustworthy,
though only {{s15c_pairs_640}} of {{s15_pairs_total}} pairs resolved there anyway. Absolute SNR50 is not comparable
across sample rates because the noise bandwidth changes — only the within-arm
order is clean.*
