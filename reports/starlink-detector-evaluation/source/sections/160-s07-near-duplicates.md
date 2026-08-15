## 7. Why it failed: the detectors are near-duplicates

The negative control is not a surprise once you look at the detectors
themselves. Over **{{s07_observations}}** live target observations with all four receivers
included — every observation carrying all eight verdicts, none missing — every
pair of the eight makes the same fire / no-fire call at phi **{{s07_phi_min}}–{{s07_phi_max}}**.

| Pair | phi |
|---|---:|
| Loosest pair anywhere in the matrix: `{{s07_loosest_a}}` / `{{s07_loosest_b}}` | {{s07_phi_min}} |
| Tightest pair anywhere: `{{s07_tightest_a}}` / `{{s07_tightest_b}}` | {{s07_phi_max}} |
| `glrt-32` / `glrt-64` | {{s07_phi_glrt32_glrt64}} |
| `differential-16` / `differential-32` | {{s07_phi_diff16_diff32}} |
| Mean over all {{s07_pairs}} pairs | {{s07_phi_mean}} |
| Band previously reported, on {{s07_prev_observations}} observations | {{s07_prev_phi_min}}–{{s07_prev_phi_max}} |
| Band here, on {{s07_observations}} observations ({{s07_observation_ratio}}x) | **{{s07_phi_min}}–{{s07_phi_max}}** |

Note what this does *not* say. Detectors with statistically independent errors
are still positively correlated whenever both have skill, because they observe
the same latent occupancy: Cov(Y1,Y2) = f(1−f)(d1−p1)(d2−p2) > 0. A high phi is
therefore expected, and does not by itself prove shared errors — "one statistic
counted eight times" overstates it. What the band establishes is that the eight
produce **highly redundant binary verdicts on identical IQ** and cannot be
treated as eight independent witnesses for validating `f`. Separating shared
truth from shared error needs correlations on the null arm, conditional on arm,
receiver and time block, or against injected truth. At {{s07_observation_ratio_round}}x the observations the
band is **wider at the bottom** than previously reported, so this is not a
small-sample artefact being sanded down — it firms up. Grouping by family barely
matters: the same-family blocks hold the tightest pairs, but no pair anywhere in
the matrix falls below {{s07_phi_min}}.

That is the mechanism behind
[section 6](#6-did-it-work-the-negative-controls). Near-duplicate detectors are
**obliged** to return near-identical `f` whatever they are fed, including inputs
where the model being validated is known to be false. The consistency check was
never testing the model; it was testing whether eight copies of one statistic
agree with each other, and they do.

![Pairwise phi between the eight detectors on the same observations](figures/algorithm-correlation.png)

***Figure 8 — not eight opinions but one, counted eight times.*** *phi between
every pair of the eight confirmers on the same {{s07_observations}} live target observations
({{s07_paired_sweeps}} paired sweeps, {{s07_scored_sidecars_in_a_pair}} scored sidecars). An observation enters only if all
eight detectors returned a verdict, so every cell rests on one identical
population. Each detector is judged against the threshold drawn for its own
sample rate and probe length from the cross-edge null arms. Row order maximises
adjacent phi over all {{s07_detectors}}! = {{s07_orderings}} orderings, which is why the outlined
same-family blocks land on the diagonal. Values in
[`figures/algorithm-correlation.json`](figures/algorithm-correlation.json).*

**Takeaway.** "Which of these eight detectors is best" is not answerable at this
corpus size with this bank, and agreement among them carries no information
about the sky. Either add a genuinely different statistic, or report one
detector and its null.

---

