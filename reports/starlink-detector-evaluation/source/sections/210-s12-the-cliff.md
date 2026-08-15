## 12. What the cliff actually is

[Section 11c](#11c-the-350400-khz-cliff-is-not-in-the-detectors-the-banks-or-the-search)
established by injection that the {{s12_edge_350}}–{{s12_edge_400}} kHz collapse is not the detectors,
the banks, the search span or the analogue filter. That left the sky, the LNBs,
or the offset estimation. It is the third, and it is not what it looks like.

**The obvious suspicion was wrong.** Since the banks search *raw* offset about
each receiver's LO while the cliff is plotted on a *bias-corrected* axis, the
correction looked like a candidate for manufacturing the feature. It is the
opposite. `lnb-b` and `lnb-d` carry `receiver_centers_hz` of **exactly zero** —
verified elementwise, so for those two ports the raw and corrected axes are the
same array and the correction has no leverage at all. They show the cliff at
full depth regardless:

| Port | Recorded centre | Raw axis | Corrected axis |
|---|---:|---|---|
| `lnb-b` | {{s12_lnbb_recorded_centre}} Hz | {{s12_lnbb_raw_pre}}% → {{s12_lnbb_raw_post}}% (**{{s12_lnbb_raw_ratio}}×**) | identical by construction |
| `lnb-d` | {{s12_lnbd_recorded_centre}} Hz | {{s12_lnbd_raw_pre}}% → {{s12_lnbd_raw_post}}% (**{{s12_lnbd_raw_ratio}}×**) | identical by construction |
| `lnb-c` | {{s12_lnbc_recorded_centre}} Hz | {{s12_lnbc_raw_pre}}% → {{s12_lnbc_raw_post}}% — **no cliff**, rises to {{s12_lnbc_raw_peak}}% at {{s12_lnbc_raw_peak_lo}}–{{s12_lnbc_raw_peak_hi}} kHz | {{s12_lnbc_cor_pre}}% → {{s12_lnbc_cor_post}}% (**{{s12_lnbc_cor_ratio}}×**) |

The cliff sits at the same *corrected* offset for all three ports and at
different *raw* offsets. Correcting is what makes them agree, and `lnb-b` and
`lnb-d` — the two the correction cannot touch — carry {{s12_precliff_bd_share}}% of the pre-cliff bin.
Independent confirmation that the `pluto-19f2` correction is sound: re-deriving
it from the corpus with `lnb_calibration`'s own rx0−rx1 estimator gives
{{s12_pluto19f2_rederived}} Hz against the recorded {{s12_lnbc_recorded_centre}}, agreeing to **{{s12_pluto19f2_agreement}}**.

### The window is one-sided, and it is not centred on zero

Unfolding the `abs()` changes the question. On refined points — those no coarse
proposer claimed, so their offset is a continuous estimate rather than a grid
tooth — the live region is a band, not a symmetric tolerance:

| Port | Live window (≥10% fire), signed corrected offset |
|---|---|
| `lnb-b` | {{s12_lnbb_window_lo}} … {{s12_lnbb_window_hi}} kHz |
| `lnb-c` | {{s12_lnbc_window_lo}} … {{s12_lnbc_window_hi}} kHz |
| `lnb-d` | {{s12_lnbd_window_lo}} … {{s12_lnbd_window_hi}} kHz |

**No bin outside those windows is live at all.** So the "collapse between
{{s12_edge_350}} and {{s12_edge_400}} kHz" is the folded far edge of a **~{{s12_window_width_khz}} kHz window centred near
−150 kHz** — not a symmetric ±{{s12_bank_span_khz}} kHz tolerance, and not a Doppler population:
the centre is stable hour by hour through the whole corpus.

That −150 kHz is precisely the quantity the calibration cannot see.
`lnb_calibration` measures **rx0 − rx1 only**, and its own docstring states that
the absolute error "is not recoverable here and is not needed". A common-mode
offset shared by both radios is invisible to a differential estimator, so it is
never corrected — and what earlier sections read as a symmetric frequency
tolerance is a ±{{s12_window_halfwidth_khz}} kHz window about a centre that is not zero.

**It has since been identified, and it is not the tuning plan.** Comparing each
detection's measured carrier offset against TLE-predicted Doppler breaks the
circularity of an axis the pipeline estimates for itself. The sync corpus cannot
carry that test — every `sync-*` capture records `utc: null` and `rf_center_hz:
null` — so it was run on the {{s12_sweeps}} narrow sky sweeps that do carry a probe UTC and
a tuning carrier, {{s12_target_points}} target points.

TLE Doppler over catalogued satellites in view is **symmetric about zero** —
mean {{s12_doppler_mean_khz}} kHz, p5/p95 ±{{s12_doppler_p95_khz}} kHz above 40° elevation — so residual ≈ measured
everywhere, and no Doppler population can put a centre at −150 kHz.

The decisive numbers are what the receivers did across the LNB swap:

| Receiver | Before 2026-08-13 | After |
|---|---:|---:|
| `lnb-b` (5d4d rx1, untouched radio) | **{{s12_lnbb_before}}** kHz | **{{s12_lnbb_after}}** kHz |
| `lnb-d` (19f2 rx1, **replaced**) | **{{s12_lnbd_before}}** kHz | **{{s12_lnbd_after}}** kHz |
| `lnb-c` (19f2 rx0, **replaced**) | {{s12_lnbc_before}} kHz | {{s12_lnbc_after}} kHz |

**Two radios watching the same sky at the same instant sat {{s12_gap_before}} kHz apart before
the swap.** A tuning-plan or beacon-frequency error cannot do that — it would
move both alike. They only came to agree near −150 kHz *after* two LNBs were
physically replaced, with `lnb-d` moving {{s12_lnbd_move}} kHz across the boundary while
`lnb-b`, on the untouched radio, moved {{s12_lnbb_move}} kHz.

The frequency dependence closes it. The eight tunings give a {{s12_if_lever}}× lever on the
Pluto IF ({{s12_if_low_mhz}} → {{s12_if_high_mhz}} MHz) at a fixed {{s12_lnb_lo_ghz}} GHz LNB LO. Fitting each
receiver-epoch as constant + IF-slope + edge term leaves an IF-proportional term
of **{{s12_if_slope_min_ppm}} to {{s12_if_slope_max_ppm}} ppm — at most {{s12_if_term_max_khz}} kHz across the whole span**, where a tuner or
reference error large enough to *be* the −150 kHz would contribute {{s12_tuner_line_khz}} kHz. It
is not there. The constant term carries everything.

**So the −150 kHz is each LNB's own local-oscillator error, per receiver, plus a
small geometry term.** That is a different and less convenient
finding than a single tuning-plan mistake: there is no one number to correct.
Each receiver needs its own measured centre — which is exactly what
[section 14](#14-the-dead-port-and-the-stale-calibration-are-one-fault) had to do
for `lnb-a`, and what the differential calibration structurally cannot supply for
any of them.

![Measured carrier offset against TLE-predicted Doppler, per receiver and epoch](figures/injection/tle-residual.png)

*Residual between measured carrier offset and TLE-predicted Doppler, split by
receiver and by hardware epoch, over {{s12_sweeps}} narrow sky sweeps.*

![Detection against raw and corrected offset, split by receiver](figures/injection/raw-vs-corrected.png)

*Detection against raw and bias-corrected offset per receiver, with bank edges
marked. The two zero-centre ports show the cliff identically on both axes.*

### Two defects this exposed

**`lnb-a`'s calibration is stale by {{s12_lnba_stale_khz}} kHz.** The same rx0−rx1 estimator gives
`pluto-5d4d` = **{{s12_lnba_rederived}} Hz** against a recorded `receiver_centers_hz` of
{{s12_lnba_recorded_centre}} Hz. Its "corrected" axis is therefore effectively uncorrected, which is
why its live window sits at {{s12_lnba_window_lo}}…{{s12_lnba_window_hi}} kHz — the same window displaced by the
calibration error. `lnb-a` is otherwise plainly live, firing {{s12_lnba_window_rate}} inside its
own window, which independently confirms
[the exclusion was wrong](#the-corpus); but **its centre must be re-measured
before it can be pooled into any offset-binned figure.** The receiver-agreement
and occupancy figures in this report are fire-based and do not use this axis, so
they are unaffected.

**The cliff's height is inflated by grid teeth.** The {{s12_edge_300}}–{{s12_edge_350}} kHz bin contains
the coarse-A tooth at exactly {{s12_coarse_a_tooth_khz}} kHz and the {{s12_edge_350}}–{{s12_edge_400}} bin the coarse-E tooth
at {{s12_coarse_e_tooth_khz}}. Pooled over `lnb-b`, `lnb-c` and `lnb-d`, most of the pre-cliff bin is
tooth points, firing at a higher rate than the refined points in the same bin.
The cliff exists and is sharp; the plateau it appears to fall from is
mostly grid.

### What is still unexplained

The window centre is no longer among the open questions:
[section 16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs)
measures all four receivers absolutely, finds four independent constants between
{{s12_ppm_min}} and {{s12_ppm_max}} ppm, and shows that correcting each one separately moves the
pooled window onto **{{s12_after_centroid_khz}} kHz** in a genuinely out-of-sample test. What remains
open is the **~±{{s12_window_halfwidth_khz}} kHz half-width**, given that injection shows the detectors
themselves flat out to ±{{s12_flat_offset_khz}} kHz — so the width belongs to the acquisition stage,
not to the decision statistics.
