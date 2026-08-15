## 2. The hard part: nothing was injected

No signal of known amplitude was ever put into either front end at this site.
Without an injection there is no known input, so the obvious way to rank
detectors — count how often each one fires on sky — cannot work, and the
measurement shows why.

Across the eight detectors, how often a detector fires on sky tracks how often
it fires on a **measured** cross-edge target-code null: least squares over the eight points
gives slope {{s02_slope}}, r = {{s02_pearson_r}}, **r-squared = {{s02_r_squared}}**. Roughly two-thirds of the
between-detector spread in fire rate is just the null rate. That fit is eight
points and it moves with the corpus: recomputed independently at two census
sizes spanning one hour of scoring it ranges r-squared {{s02_r_squared_census_low}}–{{s02_r_squared_census_high}}, so read it as
"about two-thirds", not as {{s02_r_squared_pct}}. `full-frame-full`
fires most on sky ({{s02_ffull_fire}}) and also most on the null ({{s02_ffull_null}}); `glrt-32` fires
least on both ({{s02_glrt32_fire}} and {{s02_glrt32_null}}). Firing {{s02_fire_spread_pct}}% more often may mean {{s02_fire_spread_pct}}% more
sensitive or {{s02_fire_spread_pct}}% looser, and the count cannot say which.

| Detector | Fires on sky | Fires on the null, measured `p` | Excess | Rank by count | Rank by excess | Rank by model output `d` |
|---|---:|---:|---:|---:|---:|---:|
| `full-frame-full` | {{s02_ffull_fire}} | {{s02_ffull_null}} | {{s02_ffull_excess}} | {{s02_ffull_rank_count}} | {{s02_ffull_rank_excess}} | {{s02_ffull_rank_d}} |
| `full-frame-acquire` | {{s02_facq_fire}} | {{s02_facq_null}} | {{s02_facq_excess}} | {{s02_facq_rank_count}} | {{s02_facq_rank_excess}} | {{s02_facq_rank_d}} |
| `full-frame-verify` | {{s02_fver_fire}} | {{s02_fver_null}} | {{s02_fver_excess}} | {{s02_fver_rank_count}} | {{s02_fver_rank_excess}} | {{s02_fver_rank_d}} |
| `differential-32` | {{s02_diff32_fire}} | {{s02_diff32_null}} | {{s02_diff32_excess}} | {{s02_diff32_rank_count}} | {{s02_diff32_rank_excess}} | {{s02_diff32_rank_d}} |
| `differential-16` | {{s02_diff16_fire}} | {{s02_diff16_null}} | {{s02_diff16_excess}} | {{s02_diff16_rank_count}} | {{s02_diff16_rank_excess}} | {{s02_diff16_rank_d}} |
| `glrt-64` | {{s02_glrt64_fire}} | {{s02_glrt64_null}} | {{s02_glrt64_excess}} | {{s02_glrt64_rank_count}} | {{s02_glrt64_rank_excess}} | {{s02_glrt64_rank_d}} |
| `anchor-8` | {{s02_anchor8_fire}} | {{s02_anchor8_null}} | {{s02_anchor8_excess}} | {{s02_anchor8_rank_count}} | {{s02_anchor8_rank_excess}} | {{s02_anchor8_rank_d}} |
| `glrt-32` | {{s02_glrt32_fire}} | {{s02_glrt32_null}} | {{s02_glrt32_excess}} | {{s02_glrt32_rank_count}} | {{s02_glrt32_rank_excess}} | {{s02_glrt32_rank_d}} |

*n = {{s02_target_observations}} live target observations and {{s02_null_observations}} live cross-edge null
observations. The last column ranks by the mean of d_A and d_B from
`cross_radio.solve_coincidence` on {{s02_matched_arm_cells}} joined matched-arm cells — a model
output, from the model whose consistency check is shown to be incapable of
failing in [section 6](#6-did-it-work-the-negative-controls).*

Two of those three rankings are **measurements**, and they agree with each
other: Spearman rho = **{{s02_rho_count_excess}}** between rank-by-count and rank-by-excess.
Correcting for the measured cross-edge null rate nudges the order; it does not
overturn it. The one column that overturns it is the model output: rho =
**{{s02_rho_count_d}}** against the fire count and **{{s02_rho_excess_d}}** against the excess. `glrt-32`
fires least of the eight and comes out most sensitive.

So the honest statement has two halves, and both matter. **You cannot rank these
detectors by how often they fire.** And **the model that would reorder them has
not earned the right to**, because its own consistency check fails — that is
[section 6](#6-did-it-work-the-negative-controls).

![Fire rate against measured null rate, and three rankings of the same eight detectors](figures/fire-rate-problem.png)

***Figure 2 — a fire count cannot rank detectors when nothing is injected.***
*Left: raw fire rate on sky against the measured cross-edge null rate `p`, one point
per detector, bars are 95% marginal binomial intervals; all eight score the same
observations, so the differences are paired and better determined than the bars
alone suggest. The dashed line is least squares over the eight (r = {{s02_pearson_r}},
r-squared = {{s02_r_squared}}). `anchor-8` is the one clear departure: it false-alarms like
`differential-32` while firing like `glrt-64`. Right: the same eight ranked
three ways on the same observations. Columns one and two are measurements and
agree at rho = {{s02_rho_count_excess}}; column three is a model output and sits at rho = {{s02_rho_count_d}}
against column one. Population: {{s02_scored_in_pair}} scored sidecars in {{s02_paired_sweeps}} paired sweeps,
`lnb-a` excluded. Values in
[`figures/fire-rate-problem.json`](figures/fire-rate-problem.json).*

**Takeaway.** With no injection, a fire count measures the threshold, not the
detector. Ground truth has to come from somewhere else, and the only thing this
site has is a second radio.

---
