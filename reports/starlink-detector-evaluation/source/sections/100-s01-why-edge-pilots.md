## 1. Why edge pilots, and why nothing here is visible

Each Starlink downlink channel is {{s01_channel_bandwidth_mhz}} MHz wide and carries eight known pilot
subcarriers just inside each band edge. They sit at fixed, published offsets, so
a receiver that can decide whether they are present can decide whether that
channel is lit — and at what frequency offset — without decoding anything. That
is the whole motivation: a cheap, known-code channel-occupancy sensor built out of
one known waveform feature.

**The pilots are not visible in any spectrum in this corpus.** Ranked over all
{{s01_target_observations}} target-arm observations in the {{s01_probe_ms}} ms / {{s01_sample_rate_msps}} MS/s arm — the longest probe at
the widest band, ranked by the deployed survey bank's own peak-to-median — the
top-scoring capture puts the {{s01_pilot_band_mhz}} MHz pilot band **{{s01_band_lift_measured}} dB** above its own
shoulder, 1 sigma {{s01_band_lift_sigma}} dB over {{s01_band_bins}} bins. The repository's own noiseless pilot
frame, measured the same way on the same axis, gives **{{s01_band_lift_reference_f2}} dB**. The sky is
about {{s01_reference_minus_measured}} dB below what a spectrum could show. And the next shoulder out sits at
{{s01_next_shoulder_f2}} dB, so {{s01_band_lift_measured}} dB is inside this receiver's own passband curvature, not
above it.

There is also almost nothing to resolve even in principle — though not for the
reason first given. The OFDM *useful* interval is the {{s01_symbol_us}} us symbol minus its
{{s01_cyclic_prefix_us}} us cyclic prefix, {{s01_useful_interval_us}} us, whose reciprocal is {{s01_subcarrier_spacing_khz}} kHz: exactly
the subcarrier spacing, by construction rather than coincidence. Adjacent pilots
are critically spaced, and the empirical consequence stands even though the
arithmetic first offered for it did not. With the noise removed entirely the on-pilot
minus between-pilot contrast in {{s01_comb_window_khz}} kHz windows is **{{s01_comb_reference_f2}} dB** — a comb
exists, but a {{s01_comb_reference_abs_f2}} dB one; on the real capture it is {{s01_comb_measured_f2}} dB. Treat the eight
edge pilots as a {{s01_pilot_band_mhz}} MHz **block** rather than a resolvable comb: the comb is
real and is simply far too shallow to find a signal by, which is a statement
about this geometry and not about noise.

| Quantity | Value | Source |
|---|---:|---|
| Pilot band | {{s01_pilot_band_mhz}} MHz | `PILOT_BANDWIDTH_HZ` |
| Pilot subcarriers per edge | {{s01_pilots_per_edge}} | `STARLINK_EDGE_PILOT_SUBCARRIERS` |
| Subcarrier spacing | {{s01_subcarrier_spacing_khz}} kHz | `STARLINK_SUBCARRIER_SPACING_HZ` |
| OFDM symbol duration | {{s01_symbol_us}} us total; useful interval {{s01_useful_interval_us}} us after the {{s01_cyclic_prefix_us}} us cyclic prefix, whose reciprocal is {{s01_subcarrier_spacing_khz}} kHz — exactly the subcarrier spacing | `OFDM_SYMBOL_DURATION_S` |
| Band lift, best of {{s01_target_observations}} real captures | **{{s01_band_lift_measured}} dB** (1 sigma {{s01_band_lift_sigma}}, n = {{s01_band_bins}} bins) | `edge-pilots.json` |
| Band lift, noiseless reference | **{{s01_band_lift_reference_f3}} dB** | `edge-pilots.json` |
| Next shoulder out, same capture | {{s01_next_shoulder_f3}} dB | `edge-pilots.json` |
| Comb contrast, noiseless reference | {{s01_comb_reference_f3}} dB | `edge-pilots.json` |
| Comb contrast, best real capture | {{s01_comb_measured_f3}} dB | `edge-pilots.json` |

![Best real capture and the noiseless reference frame on one axis, showing a flat band where the pilots are](figures/edge-pilots.png)

***Figure 1 — there is no picture of the thing being detected.*** *Panel (a):
capture `{{s01_capture_id}}`, tuning slot {{s01_iq_index}}, `{{s01_receiver_label}}`, channel {{s01_channel}}
{{s01_edge}} edge, {{s01_probe_ms}} ms at {{s01_sample_rate_msps_f2}} MS/s — {{s01_samples}} samples, {{s01_nfft}}-point transforms
averaged over {{s01_segments}} segments at {{s01_resolution_hz}} Hz per bin. It is rank {{s01_chosen_rank}} of {{s01_target_observations}} target
observations in that arm by the survey bank's own coarse statistic ({{s01_chosen_value}},
against that arm's own {{s01_false_alarm_pct}}% null bar of {{s01_null_threshold}} over n = {{s01_null_n}} null windows). The
shaded {{s01_pilot_band_mhz}} MHz pilot span is flat. Panel (b): the same band with
`leo_tracker`'s own noiseless `pilots.edge_pilot_frame` overlaid — a {{s01_band_lift_reference_f2}} dB
block with no internal comb, against the sky's {{s01_band_lift_measured_f2}} dB. Dotted verticals are
the eight published lower-edge pilot subcarriers ({{s01_pilot_first}}–{{s01_pilot_last}}). `lnb-a` excluded
from target and null. Every plotted value is in
[`figures/edge-pilots.json`](figures/edge-pilots.json).*

**Takeaway.** Every detection in this report is statistical inference beneath
the noise floor. You cannot look at a capture and see whether a detector was
right, which is exactly why ground truth has to be manufactured — the subject of
the next two sections.

---
