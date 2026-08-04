# Live Ku signal population — 2026-08-03

## Current conclusion

The live dual-LNBF campaign contains one fully bounded, highly coherent narrow
swept feature with a LEO-scale frequency rate. It is a qualified **coherent
moving-RF event**, and its rate is compatible with a LEO pass. It is not yet an
orbital-shape detection: over its short 25-second arc, a straight frequency
drift fits better than every sampled SGP4 path. Its frequency, duration, and
morphology are compatible with published Starlink observations, but the present
evidence does not identify the protocol or a particular spacecraft. A second
otherwise strong sweep is capture-boundary-censored rather than qualified.

The machine-readable source is
`artifacts/starlink_measurement_watch_radio2_v3_20260802/wide/population-summary.json`.
Reproduce it with:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio \
  starlink-wide-feature-summary \
  artifacts/starlink_measurement_watch_radio2_v3_20260802/wide/population-summary.json \
  artifacts/starlink_measurement_watch_radio2_v3_20260802/wide
```

## Qualified population

| Capture | Family | Polarity | Duration | Instantaneous width | Swept box | Mean drift | RX path correlation | Global LO control | TLEs within one FFT bin |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| chunk 64, ch3 | broad state | positive | 27.9 s | 153.8 kHz | 187.5 kHz | -1.62 kHz/s | 0.628 | unavailable | 73 |
| chunk 85, ch3 | broad state | negative | 57.0 s | 371.2 kHz | 465.0 kHz | -1.41 kHz/s | 0.719 | pass | 60 |
| chunk 87, ch4 | narrow sweep | positive | 25.0 s | 15.0 kHz | 112.5 kHz | -3.42 kHz/s | 0.986 | pass | 39 |

As of the 90-report snapshot used for this analysis, the campaign provides
10,530.0 s (2.93 h) of usable wall-clock exposure: 7,710.3 s on channel 3 and
2,819.8 s on channel 4. The single bounded narrow-feature rate is therefore
0.34 per combined
observed hour, with very large one-event counting uncertainty. The three
qualified starts span 4,953.5 s; an exact conditional placement test gives
p = 0.141. The earlier apparently significant cluster was driven by treating
chunk 84 as a complete event and is no longer a valid result.

Chunk 84 begins 1.04 s after the first analyzed row and has no observed onset.
It remains an interesting RX-coherent swept signal, but its duration and finite
arc are left-censored and it is no longer qualified. The live watcher discards
eight buffers after every retune and records both Raspberry Pi and AD9361
temperature in each capture so settling and thermal confounds can be tested
prospectively.

At the original Ku carriers, the narrow slopes correspond to radial
accelerations of approximately 84.4 m/s² for the censored chunk 84 and 88.4
m/s² for qualified chunk 87. Frequency displacement in
hertz is unchanged by fixed LNB mixing; only the conversion from frequency to
radial motion uses the original 11.325 or 11.575 GHz carrier.

### Orbital-shape control

Chunk 87's best sampled SGP4 comparison is STARLINK-36414 (NORAD 67468), but
this is not an identification. Its RMS residual is 4.766 kHz, while the
non-orbital affine-drift control is better at 4.434 kHz. The nominal best TLE
therefore loses to the straight-line control by 0.333 kHz, rather than beating
it by the required one 7.5 kHz FFT bin. Thirty-nine TLE paths are within one
bin of the best and the margin to the second path is only about 46 Hz.
More importantly, that TLE predicts only 0.909 kHz RMS of non-linear curvature
during the 25-second event, or 0.121 of the current 7.5 kHz FFT bin. The arc is
therefore not long or curved enough for our conservative orbital-shape test to
succeed even for a noiseless copy of that nominal TLE.
A counterfactual expansion around the same event midpoint using that sampled
TLE reaches one legacy 7.5 kHz bin of curvature at roughly 82 seconds. This is not evidence
that the signal follows that TLE; it is an acquisition-design estimate showing
that a coherent track must persist about three times longer, or be measured at
finer frequency resolution, before orbital curvature becomes discriminating.

## Higher-resolution live acquisition

Beginning with chunk 135, the watcher uses an 8192-point FFT and stores 8192
bins across the same 30.72 MHz sampled bandwidth: **3.75 kHz/bin**, twice the
previous frequency resolution. PSD is losslessly useful for this analysis after
0.01 dB int16 quantization. A live 900-snapshot artifact is 21.2 MB versus the
older median of 24.9 MB, and its 118.8-second wall span and 0.132-second median
cadence are unchanged. The raw sample rate remains 30.72 MS/s.

At 3.75 kHz resolution, the same chunk-87 TLE geometry would reach the
conservative one-bin curvature threshold at about 55 seconds rather than 82
seconds. New baselines are keyed by channel and resolution and bootstrap from
four captures spanning both tuning-dither centers. During that warm-up, generic
event analysis continues, staged IQ is explicitly discarded, and incompatible
4096-bin baselines or dither partners cannot be selected.

Chunk 84 is also explained slightly better by a straight drift over the visible
part: affine RMS is 1.785 kHz versus 1.996 kHz for its best fully covering TLE,
in addition to the missing onset. That TLE provides only 0.082 bin of predicted
curvature. Chunk 85's longer 57-second arc comes closest to resolving
curvature: its best fully covering TLE predicts 3.409 kHz RMS, or 0.455 bin,
but still falls below the conservative one-bin observability threshold and
improves on the straight-drift fit by only 2.074 kHz. Chunk 64 provides only
0.128 bin of predicted curvature. Thus the current population has three coherent moving-RF
features (one narrow and two broad), three coarse LEO-rate-compatible features,
zero curvature-observable features, and **zero orbital-shape-qualified
features**. These levels are now displayed separately in the dashboard and
machine-readable reports.

## What the colors mean

The wide plots show novelty relative to a persistent absolute-RF baseline, not
absolute received power. Red/positive means more PSD than the baseline at that
time and RF frequency. Blue/negative means less PSD. A blue block can therefore
be a transmitter or channel turning off, a moving spectral gap, or an imperfect
baseline/system-state subtraction; it is not itself a negative-power signal.
Positive narrow sweeps are currently the cleanest signal evidence.

Broad simultaneous red or blue texture across much of the band is rejected as
a receiver/system state change where possible. The global-frequency control
registers persistent full-band texture independently for RX0 and RX1 and checks
whether a candidate's drift survives. Tuning-dither pairs separately test that
features remain fixed in absolute sky frequency instead of Pluto baseband.

## Comparison with published Starlink morphology

The reviewed Starlink experiments used a 2.5 MHz slice near 11.325 GHz and
reported:

- Doppler rates of thousands of hertz per second;
- nine central tones spaced about 43.9 kHz;
- an approximately 1.333 ms reference-signal period;
- abrupt beam changes with roughly 30-second dwell in one studied pass; and
- Doppler swings of hundreds of kilohertz over a pass.

The narrow features agree with the rate and dwell scales. They do not yet show
a resolved nine-tone, 43.9 kHz comb: a 15 kHz instantaneous feature is only two
of the current 7.5 kHz FFT bins. Chunk 64 has three shared internal peaks, but
their measured spacing is not a reliable uniform 43.9 kHz comb. This missing
waveform-specific evidence is why `Starlink-compatible` is stronger than the
data currently justify as a label.

## Main limitations and next discriminators

The bare LNBF feeds have no reflector or horn, so sensitivity and angular
selectivity are poor and uncontrolled. Both receivers seeing the same path is
strong evidence against a one-receiver artifact, but does not exclude shared
power, clock, electromagnetic, or terrestrial causes.

The complete short-window TLE catalog has 39 paths within one 7.5 kHz bin of
the best fit for chunk 87. A short monotonic arc cannot uniquely associate a
Starlink in that geometry. The next useful discriminators are:

1. retain raw IQ around a narrow trigger, then test 1.333 ms cyclostationarity
   and the 43.9495 kHz tone structure at finer frequency resolution;
2. join repeated arcs only when waveform fingerprints agree, never merely
   because approximate center frequency agrees;
3. quantify false alarms from channel-adjacent and vacant-frequency controls;
4. add an antenna aperture or horn for link margin and angular discrimination;
5. fit multiple separated arcs jointly before attempting a spacecraft ID or
   position solution.

## Raw-IQ discriminator deployment

The first discriminator above is now deployed in the continuous watcher. Each
capture ranks dual-receiver narrow positive novelty online while retaining the
best 262,144-sample block from each roughly 6.5-second stratum (normally 18
blocks) in RAM. A staged artifact is kept only if
the independent integrated-waterfall analysis later qualifies a moving feature;
otherwise it is removed. Transient staging is about 75 MB in `/dev/shm`, not
the SD card; only time-frequency-matched blocks reach persistent storage.

Retention requires two independent gates: the selected raw block must fall
inside the qualified event's UTC interval and its independently selected RX0
and RX1 spectral peaks must fall on the corresponding fitted RF paths. This
prevents an unrelated strong transient elsewhere in a qualifying chunk from
being misreported as waveform evidence for the tracked feature.

Qualified IQ is automatically analyzed by
`leo-radio starlink-waveform-iq-analyze`. The report searches both receivers
around the expected 750 Hz / 1.333 ms repetition and around 43.9495 kHz spectral
spacing, and reports adjacent-lag controls. These remain feature tests, not a
Starlink decoder or an identity declaration.

The first fully instrumented time-frequency-gated capture, chunk 121, staged 18
stratified blocks but had no qualified interval. All were rejected and the
75 MB staging file was removed before the scanner continued. No persistent IQ
or waveform report has yet passed both gates. Chunk 104's earlier 5.72 dB
trigger was investigated as a control: its instantaneous RX spectral
correlation was only 0.14 and its fitted paths disagreed, so it is a strong
transient rather than a coherent dual-receiver sky track.
