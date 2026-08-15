## 9. What survives: the instrument

Two findings that do not depend on the coincidence model, do not depend on the
detector bank being independent, and are directly actionable.

### 9a. A detection cliff near {{s09_edge_350}}–{{s09_edge_400}} kHz — later shown not to be a tolerance at all

> **Superseded.** This section reports the observation. Injection against a
> *known* imposed offset
> ([section 11c](#11c-the-350400-khz-cliff-is-not-an-intrinsic-detector-or-hardware-limit))
> shows every detector is flat straight through this band, and
> [section 12](#12-what-the-cliff-actually-is) identifies the feature as the
> folded far edge of a one-sided window, not a symmetric tolerance. The
> observation below stands; the word "tolerance" does not.

The pipeline computes a bias-corrected frequency offset for every candidate.
Binned on that axis, `differential-32`'s detection rate collapses in the
**{{s09_edge_350}}–{{s09_edge_400}} kHz bin at every sample rate whose pilot band fits**:

| MS/s | {{s09_edge_0}}–{{s09_edge_50}} | {{s09_edge_50}}–{{s09_edge_100}} | {{s09_edge_100}}–{{s09_edge_150}} | {{s09_edge_150}}–{{s09_edge_200}} | {{s09_edge_200}}–{{s09_edge_250}} | {{s09_edge_250}}–{{s09_edge_300}} | {{s09_edge_300}}–{{s09_edge_350}} | **{{s09_edge_350}}–{{s09_edge_400}}** | {{s09_edge_400}}–{{s09_edge_500}} |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| {{s09_rate_250}} | {{s09_r250_b0}} | {{s09_r250_b1}} | {{s09_r250_b2}} | {{s09_r250_b3}} | {{s09_r250_b4}} | {{s09_r250_b5}} | {{s09_r250_b6}} | **{{s09_r250_b7}}** | {{s09_r250_b8}} |
| {{s09_rate_500}} | {{s09_r500_b0}} | {{s09_r500_b1}} | {{s09_r500_b2}} | {{s09_r500_b3}} | {{s09_r500_b4}} | {{s09_r500_b5}} | {{s09_r500_b6}} | **{{s09_r500_b7}}** | {{s09_r500_b8}} |
| {{s09_rate_1000}} | {{s09_r1000_b0}} | {{s09_r1000_b1}} | {{s09_r1000_b2}} | {{s09_r1000_b3}} | {{s09_r1000_b4}} | {{s09_r1000_b5}} | {{s09_r1000_b6}} | **{{s09_r1000_b7}}** | {{s09_r1000_b8}} |

*Detection percentage by bias-corrected offset bin; n per bin runs {{s09_bin_n_min}}–{{s09_bin_n_max}}.
{{s09_points_plotted}} live target points plotted from {{s09_points_read}} candidate points read, over
{{s09_paired_sweeps}} paired sweeps.*

The pilot guard bands at those three rates are {{s09_guard_250}}, {{s09_guard_500}} and
{{s09_guard_1000}} kHz — a **{{s09_guard_spread}}x range** — and the cliff does not move. That rules the
guard band out. What is left is a detection cliff near {{s09_edge_350}}–{{s09_edge_400}} kHz of corrected offset — **since shown by
injection not to be a detector tolerance at all
([11c](#11c-the-350400-khz-cliff-is-not-an-intrinsic-detector-or-hardware-limit)),
and since measured to be the deployed coarse bank's own search span
([16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs))**. The
guess recorded here when the mechanism was open — that it belonged to the
survey scorer's ±{{s09_survey_bank_khz}} kHz bank rather than to the narrower
acquisition-path constant — had the right kind of cause and the wrong bank.

The cliff is robust to disaggregation. All nine (rate, probe length) cells
collapse in the same bin — {{s09_robust_250_80_before}}% to {{s09_robust_250_80_after}}% at {{s09_rate_250}} MS/s / {{s09_probe_80}} ms, down to {{s09_robust_1000_640_before}}% to
{{s09_robust_1000_640_after}}% at {{s09_rate_1000_int}} MS/s / {{s09_probe_640}} ms — so it is not an artefact of pooling probe lengths
inside a rate.

Two limits belong with it, neither visible in the figure:

- **The {{s09_rate_250}} MS/s cliff point is one port.** Its {{s09_edge_350}}–{{s09_edge_400}} kHz bin holds n = {{s09_cliff250_n}},
  of which {{s09_cliff250_lnbc}} ({{s09_cliff250_share}}%) are `lnb-c`. The concentration is worse at the full corpus
  than the {{s09_cliff_review_share}}% found at review, so that row alone would not carry the argument.
  {{s09_rate_500}} MS/s ({{s09_cliff500_lnbb}} / {{s09_cliff500_lnbc}} / {{s09_cliff500_lnbd}} for b / c / d) and {{s09_rate_1000_int}} MS/s ({{s09_cliff1000_lnbb}} / {{s09_cliff1000_lnbc}} / {{s09_cliff1000_lnbd}}) carry
  all three ports through the cliff and all three fall.
- **The guard is a raw-axis quantity plotted beside a corrected axis.** On the
  raw axis at {{s09_rate_250}} MS/s there is no cliff at all: the rate *rises* through its own
  guard — {{s09_raw250_b5}}% ({{s09_edge_250}}–{{s09_edge_300}} kHz), {{s09_raw250_b6}}% ({{s09_edge_300}}–{{s09_edge_350}}), {{s09_raw250_b7}}% ({{s09_edge_350}}–{{s09_edge_400}}), {{s09_raw250_b8}}%
  ({{s09_edge_400}}–{{s09_edge_500}}), {{s09_raw250_b9}}% ({{s09_edge_500}}–{{s09_edge_600}}). The corrected axis is the right one for asking
  about detection; the guard does not live on it, and in the figure it is an
  axis-anchored tick rather than a bound on the plotted quantity.

![Detection rate against bias-corrected offset for three sample rates](figures/cfo-cliff.png)

***Figure 11 — the cliff does not move when the guard moves {{s09_guard_spread}}x.***
*`differential-32`, fired against its own (sample rate, probe length)
cross-edge-null {{s09_false_alarm_pct}}% threshold and binned on the pipeline's bias-corrected offset.
The three guards span {{s09_guard_spread}}x, and two of them are never reached at all — the
largest corrected offset anywhere in the corpus is {{s09_max_corrected_khz}} kHz, well inside
both. Lower panel is n per bin. Values in
[`figures/cfo-cliff.json`](figures/cfo-cliff.json).*

### 9b. One port carries a large LO offset and is the best receiver on site

`lnb-c` has `receiver_centers_hz` = **{{s09_lnbc_recorded_centre}} Hz**, applied at scoring time.
That is well past the ~{{s09_edge_350}} kHz tolerance above, which is enough to make a
healthy port look dead. On the raw axis at {{s09_rate_500_int}} MS/s it reads {{s09_lnbc_raw_rate}}% (n = {{s09_lnbc_raw_n}}) in
the {{s09_pb_edge_100}}–{{s09_pb_edge_200}} kHz bin where the other two ports peak, because its entire response
has moved out to {{s09_pb_edge_400}}–{{s09_pb_edge_500}} kHz ({{s09_lnbc_raw_peak_rate}}%, n = {{s09_lnbc_raw_peak_n}}).

Bias-corrected, it is the strongest port on site:

| Port | {{s09_rate_500_int}} MS/s, corrected offset {{s09_pb_edge_100}}–{{s09_pb_edge_200}} kHz | n |
|---|---:|---:|
| `lnb-c` | **{{s09_pub_lnbc_rate}}%** | {{s09_pub_lnbc_n}} |
| `lnb-d` | {{s09_pub_lnbd_rate}}% | {{s09_pub_lnbd_n}} |
| `lnb-b` | {{s09_pub_lnbb_rate}}% | {{s09_pub_lnbb_n}} |

`lnb-b` and `lnb-d` carry zero bias, so their raw and corrected panels are
identical by construction and only `lnb-c` moves. The {{s09_port_ratio}}x port difference
survives the correction rather than being created by it.

![Detection against raw and bias-corrected offset for all four ports](figures/port-bias.png)

***Figure 12 — two ports are shifted, not deaf.*** *`differential-32`, {{s09_pb_paired_sweeps}}
paired sweeps, {{s09_pb_points}} points, **all four ports**. `lnb-a` carries its
**measured** {{s09_lnba_centre}} Hz ± {{s09_lnba_uncertainty_khz}} kHz centre from
[section 14](#14-the-dead-port-and-the-stale-calibration-are-one-fault), marked
on the figure as measured rather than recorded; the third panel shows its live
window moving from {{s09_ghost_low}}…{{s09_ghost_high}} kHz onto {{s09_after_low}}…{{s09_after_high}} kHz with the stale centre drawn as
a ghost. On the raw axis `lnb-a` reads **{{s09_lnba_raw_rate}}%** (n = {{s09_lnba_raw_n}}) in the {{s09_pb_edge_100}}–{{s09_pb_edge_200}} kHz
bin where `lnb-b` and `lnb-d` peak — just as `lnb-c` reads {{s09_lnbc_raw_rate}}% — and with its
own centre removed it peaks at **{{s09_now_lnba}}%** there against `lnb-c` {{s09_now_lnbc}}, `lnb-d`
{{s09_now_lnbd}} and `lnb-b` {{s09_now_lnbb}}. All four sit at {{s09_centroid_lnba}} to {{s09_centroid_lnbc}} kHz rather than zero — the
offset of [section 12](#12-what-the-cliff-actually-is), which
[section 16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs)
resolves into four independent per-receiver oscillator errors. Those per-port centroids are
computed on this figure's own population and statistic and differ by up to {{s09_centroid_survey_gap}} kHz
from the survey-path values in `hardware/epochs.json`; the conclusion — every
port about 150 kHz below zero, none near it — is the same either way. Values in
[`figures/port-bias.json`](figures/port-bias.json).*

***Audit note, now resolved — and the exclusion was wrong.*** *Every figure
above was produced with `lnb-a` excluded, because `cross_radio.DEAD_RECEIVERS`
recorded it as flat ~1.19 at every tuning since 2026-08-13 04:44 UTC. Scored on
`differential-32`, this corpus never reproduced that: `lnb-a` fires on
{{s09_lnba_fire_pct}}% of its {{s09_lnba_target_n}} target points against
`lnb-b`'s {{s09_lnbb_fire_pct}}% of {{s09_lnbb_target_n}}, and its cross-edge
null is not silence — median {{s09_lnba_null_median}} and p99
{{s09_lnba_null_p99}}, against `lnb-b`'s {{s09_lnbb_null_median}} and
{{s09_lnbb_null_p99}}. [Section 14](#14-the-dead-port-and-the-stale-calibration-are-one-fault)
explains the flat reading — the oscillator moved out of the **narrow** search
grid, which the survey path this report scores does not use — and
`DEAD_RECEIVERS` is now empty. The exclusion cost twice over, because it took
the port's null with it and so moved every threshold drawn beside it. **The
figures in this section still carry it**; they were computed before the
withdrawal and have not been recomputed, which is the honest state rather than
the tidy one.*

### The surviving findings, in one place

| Finding | The number it rests on | Status |
|---|---|---|
| ~~A rate-independent ~{{s09_edge_350}} kHz offset tolerance~~ **superseded — an observation, not a tolerance ([11c](#11c-the-350400-khz-cliff-is-not-an-intrinsic-detector-or-hardware-limit), [12](#12-what-the-cliff-actually-is))** | collapse in the {{s09_edge_350}}–{{s09_edge_400}} kHz bin at {{s09_rate_250}}, {{s09_rate_500}} and {{s09_rate_1000_int}} MS/s, in all nine (rate, probe) cells, while the guard moves {{s09_guard_spread}}x | **stands**; mechanism **now measured** — the coarse bank's own search span, not bandwidth ([16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs)) |
| ~~`lnb-c` needs a {{s09_lnbc_applied_khz}} configured centre correction~~ **superseded — {{s09_lnbc_applied_khz}} is itself {{s09_lnbc_too_high_khz}} too high and costs {{s09_lnbc_lost}} of its detections ([16c](#16c-miscentring-costs-detections-and-this-is-measured-not-modelled))** | its measured absolute centre is **{{s09_lnbc_measured_centre}}**; the direction of the original finding — `lnb-c` needs a large positive correction, and corrected it has the highest fire rate in the slice — **stands** | **corrected value** |
| The {{s09_rate_125}} MS/s arm cannot capture the full unaliased pilot allocation and is the weakest arm; extra dwell cannot restore missing bandwidth | a {{s09_pilot_band_mhz}} MHz band in a {{s09_rate_125}} MHz capture; guard {{s09_guard_125}}, `pilot_band_fits` false; f {{s09_axis_low_f}} on {{s09_axis_low_cells}} cells against {{s09_axis_high_f}} | **stands** |
| The eight detectors produce highly redundant verdicts on identical IQ | phi {{s09_phi_min}}–{{s09_phi_max}} over {{s09_observations}} observations | **stands** |
| A per-point {{s09_nominal_per_point_pct}}% threshold yields {{s09_percell_low}}–{{s09_percell_high}}% per cell after maximising over ~{{s09_points_per_cell}} candidates, as expected | {{s09_percell_low}}–{{s09_percell_high}}% across the eight, on {{s09_null_observations}} null observations | **stands** |
| Recorded skew is a lower bound and is blind to geometry | barrier-release stamp; {{s09_skew_same}} against {{s09_skew_opposite}} ms across geometries whose true offsets differ ~{{s09_skew_geometry_factor}}x | **stands** |
| ~~`lnb-a` is excluded as a dead port~~ **withdrawn — its LO moved {{s09_lnba_move_khz}} kHz out of the search grid ([14](#14-the-dead-port-and-the-stale-calibration-are-one-fault))** | `DEAD_RECEIVERS`; not reproduced on `differential-32` ({{s09_lnba_fire_pct}}% of {{s09_lnba_target_n}} against `lnb-b`'s {{s09_lnbb_fire_pct}}% of {{s09_lnbb_target_n}}) | **withdrawn in code**; figures above predate it |
| Cross-radio beats within-radio | not shown: no committed artefact isolates a radio-boundary effect on phi | **not established** |

**Takeaway.** The instrument findings are the practical payoff of this corpus:
apply epoch-aware **per-receiver absolute centres** while preserving each
radio's measured differential, choose the coarse search span deliberately
against the offset population it has to reach rather than inheriting it, and stop
pooling the {{s09_rate_125}} MS/s arm with arms whose pilot band fits.

---
