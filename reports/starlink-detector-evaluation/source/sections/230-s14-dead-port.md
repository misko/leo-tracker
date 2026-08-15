## 14. The dead port and the stale calibration are one fault

Two problems with `lnb-a` appear separately in this report: it was excluded as a
dead port on a failure that postdates every observation
([the corpus](#the-corpus)), and its recorded centre is stale by {{s14_stale_by}} kHz
([section 12](#12-what-the-cliff-actually-is)). They are the same event.

**Its local oscillator moved.** Measured from the corpus at instants where both
ports of `pluto-5d4d` fired:

| Epoch | `rx0 − rx1` | n | `lnb-a` fire rate | Its median offset |
|---|---:|---:|---:|---:|
| before {{s14_epoch_before}} | **{{s14_diff_gen1}} Hz** | {{s14_n_dual_gen1}} pairs | {{s14_fire_before}} | {{s14_median_offset_before}} |
| after {{s14_epoch_after}} | **{{s14_diff_gen2}} Hz** ±{{s14_uncertainty_khz}} kHz | {{s14_n_points_gen2}} points / {{s14_n_sweeps_gen2}} sweeps | **{{s14_fire_after}}** | pinned at the grid edge |

`lnb-b`, on the same radio, is unchanged across that boundary ({{s14_partner_before}} → {{s14_partner_after}}).
A step in one port and not its sibling, at one instant, localises the change to
`lnb-a`. And the recorded `{{s14_recorded_centre}}` was **correct for the earlier epoch** —
before the boundary `lnb-a` sat near zero and fired indistinguishably from
`lnb-b`.

So the port was never dead. Its LO moved out of the search grid, its detections
collapsed to those pinned at the grid edge, and the dwell path read that as a
flat peak-to-median. One fault, seen twice.

**The boundary is the window `hardware/epochs.json` brackets for `pluto-19f2`'s
LNB swap** — where that file recorded `pluto-5d4d` as *"not touched by this
swap; calibration entry remains valid"*. It was touched. That entry is now
corrected, with `pluto-5d4d.lnb-a` gen1 and gen2 epochs recorded.

### Why the calibration could never notice

`acquisition.py` builds its search as
`arange(-350_000, 350_001, 25_000) + centre`, where `centre` is the **already
applied** `receiver_centers` value. A port anchored near zero is therefore blind
outside the span that grid covers and **cannot discover that it has moved**. In
the data the replacement LNB on `19f2` was findable only because its stale
{{s14_19f2_stale_centre}} kHz calibration had already displaced the grid into range.

This is a self-sealing failure. The daily calibration returned {{s14_daily_calibration}} Hz on
2026-08-14 and will keep returning near zero forever, because the offset it must
measure lies outside the window it searches. The value has to come from the
survey path, whose coarse bank spans ±700 kHz about raw zero.

### The fix, and its verification

Applying `measured_centers_hz: [{{s14_measured_centre_applied}}, 0.0]` to `pluto-5d4d`:

| Port | Signed live-window centroid |
|---|---:|
| `lnb-a`, corrected | **{{s14_centroid_lnba}} kHz** |
| `lnb-b` | {{s14_centroid_lnbb}} kHz |
| `lnb-c` | {{s14_centroid_lnbc}} kHz |
| `lnb-d` | {{s14_centroid_lnbd}} kHz |

Its window moves from {{s14_window_before_low}}…{{s14_window_before_high}} kHz onto {{s14_window_after_low}}…{{s14_window_after_high}} kHz, and the paired
per-instant residual after correction is **{{s14_residual_khz}} kHz, CI {{s14_residual_ci}} Hz**. The
`lnb-a`↔`lnb-b` landing is partly definitional, but the non-circular part is
that it also lands on `lnb-c` and `lnb-d` — on the other radio, corrected by an
independently recorded centre.

`lnb-a` can now enter offset-binned figures. The ±{{s14_uncertainty_khz}} kHz uncertainty is a twelfth
of a {{s14_bin_khz}} kHz bin.

![All four ports before and after correction](figures/injection/lnba-centre.png)

**And all four still sit at {{s14_centroid_shallowest}} to {{s14_centroid_deepest}} kHz, not zero.** That residual is the
offset of [section 12](#12-what-the-cliff-actually-is) — invisible to every
differential measurement, which is why it survived this correction.
[Section 16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs)
measures it away, one receiver at a time, and prices what it has been costing.

Note also that the differential step measured here, {{s14_diff_step}} Hz, and the move in
`lnb-a`'s absolute centre measured in section 16, {{s14_abs_move}} Hz, differ by {{s14_step_gap}} kHz.
Neither is wrong: they are marginal means over different detection sets, which
[section 16b](#16b-the-correction-survives-being-tested-out-of-sample) shows sets
a 20–40 kHz floor on any absolute per-receiver number.
