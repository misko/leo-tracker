## 4. The dataset: twelve arms, and two geometries for free

Twelve arms cross probe length {{{s04_probe_80}}, {{s04_probe_160}}, {{s04_probe_640}}} ms with sample rate
{{{s04_rate_125}}, {{s04_rate_250}}, {{s04_rate_500_axis}}, {{s04_rate_1000_axis}}} MS/s, drawn uniformly per sweep. Both radios take the
same arm on most sweeps by design; the rest put two different
configurations on the same sky at the same instant.

**The randomisation came out flat.** {{s04_matched_arm_sweeps}} of {{s04_sweeps_captured}} captured sweeps put both
radios on the same arm — {{s04_matched_arm_pct}}. Per-arm counts run {{s04_arm_count_min}}–{{s04_arm_count_max}}
against a uniform expectation of {{s04_expected_per_arm}}; chi-square {{s04_chi_square}} on {{s04_chi_df}} degrees of
freedom, Monte-Carlo p = **{{s04_chi_p_mc}}** over {{s04_chi_mc_draws}} draws. No arm was starved.

| arm | probe (ms) | rate (MS/s) | sweeps, both radios | solo captures | samples/tuning | IQ bytes per radio | pilot guard (kHz) | pilot band fits | imported pairs | scored pairs |
|---|---:|---:|---:|---:|---:|---:|---:|:--:|---:|---:|
| {{s04_arm0_name}} | {{s04_arm0_probe}} | {{s04_arm0_rate}} | {{s04_arm0_sweeps}} | {{s04_arm0_solo}} | {{s04_arm0_samples}} | {{s04_arm0_bytes}} | {{s04_arm0_guard_khz}} | NO | {{s04_arm0_imported}} | {{s04_arm0_scored}} |
| {{s04_arm1_name}} | {{s04_arm1_probe}} | {{s04_arm1_rate}} | {{s04_arm1_sweeps}} | {{s04_arm1_solo}} | {{s04_arm1_samples}} | {{s04_arm1_bytes}} | {{s04_arm1_guard_khz}} | yes | {{s04_arm1_imported}} | {{s04_arm1_scored}} |
| {{s04_arm2_name}} | {{s04_arm2_probe}} | {{s04_arm2_rate}} | {{s04_arm2_sweeps}} | {{s04_arm2_solo}} | {{s04_arm2_samples}} | {{s04_arm2_bytes}} | {{s04_arm2_guard_khz}} | yes | {{s04_arm2_imported}} | {{s04_arm2_scored}} |
| {{s04_arm3_name}} | {{s04_arm3_probe}} | {{s04_arm3_rate}} | {{s04_arm3_sweeps}} | {{s04_arm3_solo}} | {{s04_arm3_samples}} | {{s04_arm3_bytes}} | {{s04_arm3_guard_khz}} | yes | {{s04_arm3_imported}} | {{s04_arm3_scored}} |
| {{s04_arm4_name}} | {{s04_arm4_probe}} | {{s04_arm4_rate}} | {{s04_arm4_sweeps}} | {{s04_arm4_solo}} | {{s04_arm4_samples}} | {{s04_arm4_bytes}} | {{s04_arm4_guard_khz}} | NO | {{s04_arm4_imported}} | {{s04_arm4_scored}} |
| {{s04_arm5_name}} | {{s04_arm5_probe}} | {{s04_arm5_rate}} | {{s04_arm5_sweeps}} | {{s04_arm5_solo}} | {{s04_arm5_samples}} | {{s04_arm5_bytes}} | {{s04_arm5_guard_khz}} | yes | {{s04_arm5_imported}} | {{s04_arm5_scored}} |
| {{s04_arm6_name}} | {{s04_arm6_probe}} | {{s04_arm6_rate}} | {{s04_arm6_sweeps}} | {{s04_arm6_solo}} | {{s04_arm6_samples}} | {{s04_arm6_bytes}} | {{s04_arm6_guard_khz}} | yes | {{s04_arm6_imported}} | {{s04_arm6_scored}} |
| {{s04_arm7_name}} | {{s04_arm7_probe}} | {{s04_arm7_rate}} | {{s04_arm7_sweeps}} | {{s04_arm7_solo}} | {{s04_arm7_samples}} | {{s04_arm7_bytes}} | {{s04_arm7_guard_khz}} | yes | {{s04_arm7_imported}} | {{s04_arm7_scored}} |
| {{s04_arm8_name}} | {{s04_arm8_probe}} | {{s04_arm8_rate}} | {{s04_arm8_sweeps}} | {{s04_arm8_solo}} | {{s04_arm8_samples}} | {{s04_arm8_bytes}} | {{s04_arm8_guard_khz}} | NO | {{s04_arm8_imported}} | {{s04_arm8_scored}} |
| {{s04_arm9_name}} | {{s04_arm9_probe}} | {{s04_arm9_rate}} | {{s04_arm9_sweeps}} | {{s04_arm9_solo}} | {{s04_arm9_samples}} | {{s04_arm9_bytes}} | {{s04_arm9_guard_khz}} | yes | {{s04_arm9_imported}} | {{s04_arm9_scored}} |
| {{s04_arm10_name}} | {{s04_arm10_probe}} | {{s04_arm10_rate}} | {{s04_arm10_sweeps}} | {{s04_arm10_solo}} | {{s04_arm10_samples}} | {{s04_arm10_bytes}} | {{s04_arm10_guard_khz}} | yes | {{s04_arm10_imported}} | {{s04_arm10_scored}} |
| {{s04_arm11_name}} | {{s04_arm11_probe}} | {{s04_arm11_rate}} | {{s04_arm11_sweeps}} | {{s04_arm11_solo}} | {{s04_arm11_samples}} | {{s04_arm11_bytes}} | {{s04_arm11_guard_khz}} | yes | {{s04_arm11_imported}} | {{s04_arm11_scored}} |

One column is handicapped, and it is handicapped by physics rather than by
sampling. A {{s04_rate_125}} MS/s capture is only {{s04_rate_125}} MHz wide, so the {{s04_pilot_band_mhz}} MHz band the
detectors correlate against cannot fit inside it at any probe length: the pilot
guard is {{s04_guard_125_khz}} and `pilot_band_fits` is false at all three probe lengths.
That column was drawn as often as any other — {{s04_arm0_sweeps}}, {{s04_arm4_sweeps}} and {{s04_arm8_sweeps}} sweeps. The
authoritative full-corpus run puts {{s04_rate_125}} MS/s at {{s04_probe_80}} ms at the bottom of the arm
axis, f = {{s04_axis_low_f}} on {{s04_axis_low_cells}} cells with only {{s04_axis_low_solved}} of {{s04_axis_high_solved}} algorithms solvable, against
f = {{s04_axis_high_f}} for {{s04_rate_1000}} MS/s at {{s04_probe_80}} ms, over an arm axis spanning f {{s04_axis_f_low}}–{{s04_axis_f_high}} on {{s04_axis_cells}}
cells.

![The twelve arms as a grid, with sweep counts, sample budgets and the pilot-band verdict](figures/arm-matrix.png)

***Figure 4 — the draw was flat; one column is beaten by arithmetic.*** *Each
cell carries the sweeps captured with both radios on that arm, the solo captures
beside them, the sample and byte budget per radio, and the pilot-band verdict.
Colour encodes the sweep count only, on a scale spanning the uniform expectation
{{s04_expected_per_arm}} ± {{s04_colour_sigma}} sigma. The three hatched cells are the {{s04_rate_125}} MS/s column: guard =
rate/2 − {{s04_pilot_half_khz}} kHz, negative at that rate at every probe length, and the
collector's own `pilot_band_fits` flag agrees with the recomputed guard at all
four rates. Values in [`figures/arm-matrix.json`](figures/arm-matrix.json).*

**Two geometries came free.** Each radio draws its edge order (`L` or `U`)
independently every sweep, so the pair lands in one of two geometries by chance.
**Same-edge** puts both radios on one tuning at every instant: replication — do
two chains agree about this tuning? **Opposite-edge** splits them across one
channel's two edges at every instant: simultaneity across a channel — was the
whole channel live at this instant? Neither question can be asked of the other's
sweeps, and opposite-edge cannot replicate, because the chains never share a
tuning, so a disagreement there is not evidence that either chain is wrong.

| Population | n | same-edge | opposite-edge | same-edge share |
|---|---:|---:|---:|---:|
| Sweeps captured by the collector | {{s04_captured_n}} | {{s04_captured_same}} | {{s04_captured_opposite}} | {{s04_captured_same_pct}}% |
| Pairs imported to the corpus | {{s04_imported_n}} | {{s04_imported_same}} | {{s04_imported_opposite}} | {{s04_imported_same_pct}}% |
| Pairs scored — the analysable set | {{s04_scored_n}} | {{s04_scored_same}} | {{s04_scored_opposite}} | {{s04_scored_same_pct}}% |
| Cross-radio cells, scored | {{s04_cells_total}} | {{s04_cells_same}} | {{s04_cells_opposite}} | |

The draw really is a coin flip: {{s04_captured_same_pct}}% same-edge over {{s04_captured_n}} sweeps, two-sided
Monte-Carlo p = **{{s04_edge_p_mc}}** against a fair {{s04_edge_expected_pct}}% over {{s04_edge_mc_draws}} draws. Per-radio
edge-order counts are {{s04_radio_a_lower}} / {{s04_radio_a_upper}} and {{s04_radio_b_lower}} / {{s04_radio_b_upper}}. Geometry derived from the
recorded sample orders agrees with the declared `edge_order` letter on **{{s04_geometry_agreeing}}
of {{s04_geometry_pairs_checked}}** pairs, with exactly two distinct sample orders present in the corpus,
{{s04_instants_per_sweep}} instants per sweep and {{s04_live_receiver_pairs}} live receiver pairs per pair.

![Schematics of the two geometries and the counts that land in each](figures/geometry.png)

***Figure 5 — half the sweeps replicate; the other half ask a different
question.*** *Left panels are schematics of the two sample orders that actually
occur in the corpus (verified: there are exactly two). Right panels are the
counts at each stage — captured, imported, scored — with the geometry mixture
unmoved at every stage, and a verification against the authoritative run's
{{s04_auth_pairs}} / {{s04_auth_same}} / {{s04_auth_opposite}} line: same filter, {{s04_auth_pairs_added}} more pairs, same-edge share {{s04_auth_same_pct_then}}% then
and {{s04_auth_same_pct_now}}% now. Values in [`figures/geometry.json`](figures/geometry.json).*

**Takeaway.** The dataset is balanced across arms and geometries by
construction rather than by luck, and the two geometries are the only lever this
corpus has on simultaneity — the recorded skew, from
[section 3](#3-the-apparatus-two-radios-one-instant), is not one.

---
