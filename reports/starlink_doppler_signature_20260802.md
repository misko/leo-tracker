# Starlink channel-4 Doppler signature — 2026-08-02

> **2026-08-03 methodological correction:** The original `PASS` below is
> retained as experiment history but is superseded. The repeat contains
> statistically nonstationary moving Ku structure, yet RX0 and RX1 differ by a
> median 90 kHz, occupy the same FFT bin only 1.8% of the time, and have fitted
> rates differing by 1.38 kHz/s. The best TLE is also different for each
> receiver, and nearby time-shift controls retain material improvement. With no
> Starlink-specific 1.333 ms or 43.9495 kHz waveform evidence, these artifacts
> do **not** establish a Starlink constellation signature under the current
> controls. Treat the result as an externally moving Ku candidate. The current
> population assessment is in `reports/live_signal_population_20260803.md`.

## Result

**PASS for a Starlink constellation-level Doppler signature.**  The experiment
does not uniquely identify one spacecraft because the bare LNBFs have broad
fields of view and adjacent Starlinks in a plane produce similar curves a few
minutes apart.

## Configuration

- Site: 37.849165355010086, -122.48567658142287
- Radio: Pluto Plus, synchronous RX0 and RX1
- Antennas: two bare universal Ku LNBFs, no reflector
- Starlink channel: 4, nominal IF 1,825,117,187.5 Hz with 9.75 GHz LNB LO
- Tuner: nominal IF +5 MHz center-shift control
- Sample rate: 30.72 MS/s configured; sparse 262,144-sample IIO snapshots
- Compact waterfall: 4,096 bins over 30.72 MHz, one-second integration
- Detector: blind stationary-median-subtracted moving depression tracker
- Routine raw IQ: not retained

## Evidence

1. **Absolute-RF validation.**  The channel-center feature was first measured
   near -0.23 MHz baseband.  Moving the tuner +5 MHz moved it to approximately
   -5.24 MHz, preserving an absolute IF near 1.82488 GHz.  It therefore follows
   RF rather than tuner DC.
2. **Channel specificity.**  Channel 4 produced 0.41--0.46 dB median moving
   depressions.  Identically configured channel 1 and channel 3 controls were
   about 0.35--0.39 dB and generally failed permutation significance.
3. **Independent repeat.**  The radio was retuned through channels 1 and 3
   before returning to channel 4.  The second channel-4 capture again produced
   a shared moving feature.
4. **Two-receiver agreement.**  Repeat-capture paths have correlation 0.854
   and median separation 90 kHz.  Both move downward in frequency.
5. **LEO-scale motion.**  RX0 spans 690 kHz at a fitted -3.50 kHz/s; RX1 spans
   585 kHz at -2.13 kHz/s over 217 one-second points.  The visible signal-rich
   portion lasts roughly 75 seconds and reaches 0.9--1.6 dB depression.
6. **Blind significance.**  Neither receiver's observed path was equaled by
   128 independently time-scrambled versions of its own waterfall, giving
   empirical p <= 1/129 = 0.00775 for each receiver.
7. **Orbital shape.**  Over the complete repeat, the best correct-time TLE
   improves on constant-plus-linear drift by 32.4% on RX0 and 22.4% on RX1.
   The matching geometries culminate above 84 degrees.  Ten- and thirty-minute
   global shifts collapse to the linear baseline.  Five-minute ambiguity
   remains because another satellite in the dense constellation can supply a
   similar curve.

## Controls

| Capture | RX0 permutation p | RX1 permutation p | Interpretation |
|---|---:|---:|---|
| Channel 4, first | <=0.00775 | <=0.00775 | significant, receivers select different paths |
| Channel 4, repeat | <=0.00775 | <=0.00775 | significant and receiver-coherent |
| Channel 3 | 0.485 | 0.152 | null-like |
| Channel 1 (reported vacant) | 0.152 | 0.030 | one marginal control path |

## Authoritative artifacts

- `artifacts/starlink_ch4_waterfall_repeat_20260802.npz`
- `artifacts/starlink_ch4_wide_repeat_sig128_20260802.json`
- `artifacts/starlink_ch4_doppler_signature_20260802.png`
- `artifacts/starlink_ch4_repeat_global_tle_controls_20260802.json`
- `artifacts/starlink_ch4_event_tle_controls_20260802.json`
- `artifacts/starlink_ch1_waterfall_control_20260802.npz`
- `artifacts/starlink_ch1_wide_sig32_20260802.json`
- `artifacts/starlink_ch3_waterfall_20260802.npz`
- `artifacts/starlink_ch3_wide_sig32_20260802.json`

## Scope of the claim

The evidence supports a moving, channel-4-specific Ku feature with Starlink
channel-center width, LEO-scale dynamics, strong time-scramble rejection, and
agreement with high-elevation Starlink geometry.  It is appropriately called
a **Starlink constellation Doppler signature**.  It is not yet a unique
satellite association or a navigation solution.  Unique association will
require narrower antenna coverage, a longer continuously visible waveform,
or joint fitting of multiple independently resolved features.
