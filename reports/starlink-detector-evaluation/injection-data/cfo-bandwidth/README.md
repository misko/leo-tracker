# CFO x sample rate x RX bandwidth — the factorial that separates two limits

Records behind the question "is the 350–400 kHz sky detection cliff a bandwidth
effect?". Taken 2026-08-14/15 on radio `.165` over a closed cable, with the
carrier offset **imposed on the transmitted waveform** so the offset axis is
known by construction and no estimator sits between it and the answer.

## Why this experiment exists

`fast_scan.py:1077` writes `rf_bandwidth = sampling_frequency` on every sky arm.
So across the entire sky corpus the digital Nyquist window and the AD9361 analog
baseband filter are the same number, `min(Fs, B_RX)` is always `Fs`, and no sky
measurement can tell the two apart. Here they are set independently:

| limit | clips when | moves with |
|---|---|---|
| digital | `\|CFO\| > (window − 1,875,000)/2` | sample rate |
| analog | `\|CFO\| > (B_RX − 1,875,000)/2` | RX bandwidth |
| search | `\|CFO\| >` the coarse bank's outermost offset | **neither** |

The occupied edge-pilot band is 1,875,000 Hz wide (outermost pilot *centres*
±820,312.5 Hz), verified from `leo_tracker.radio.beacon.pilots`.

## Rig

One ADALM-Pluto Rev.C (Z7010-AD9361), `hw_serial`
`1040007c4a94000211000b009186843ef2`, at `ip:192.168.1.165`, firmware
`v0.38-plutoplus-spf-libiio-metadata-v5`, kernel `5.15.0-gd798b0d821b8`.

```
TX2 -> SMA splitter -> 2x 30 dB attenuator -> RX1 and RX2
```

A closed path. No antenna, no LNB, no sky. TX and RX share one oscillator, so
the rig's own carrier offset is ~0 and the offset the detectors face **is** the
offset imposed on the waveform.

| | |
|---|---|
| waveform | `leo_tracker.radio.beacon.pilots.edge_pilot_frame`, the repository's own pilot frame — not a tone |
| TX buffer | 3 whole frames, cyclic — 10,000 / 20,000 / 40,000 samples at 2.5 / 5 / 10 MS/s, exact at every rate, so it wraps at the true 750 Hz frame rate |
| LO | 1,190,312,500 Hz on TX and RX |
| TX port | TX2, `tx_hardwaregain_chan1`; TX1 and both ports at rest are held at −89.75 dB |
| TX `rf_bandwidth` | 6 MHz, fixed for the whole sweep, so the transmitter cannot become a second moving explanation |
| digital drive | peak-normalised to 8,192 counts |
| **RX gain** | **`manual`, 40.0 dB, fixed and read back on every cell** — an AGC would confound every power reading |
| probe | 40 ms (100,000 / 200,000 / 400,000 samples) |
| probes per cell | 20 captures x 2 receivers = 40 scored probes |
| null arm | 40 captures x 2 receivers per (rate, bandwidth), TX at −89.75 dB — 320 probes a rate, which is what makes the 1% threshold `supported` for the coarse statistic as well as the point ones |
| ADC full scale | 2,048 counts; the sweep aborts above a peak of 1,500. Observed maximum across the whole sweep: 89 counts |

## Detectors

Scored with the repository's own comparison family, through
`survey_scoring.search_observation` / `distinct_points` / `confirm_points` —
the same call sequence `radio-165/t3_cliff.py` uses:

* point statistics `full-frame-full` (headline), `full-frame-acquire`,
  `full-frame-verify`, `anchor-8`, `differential-16`, `differential-32`,
  `glrt-32`, `glrt-64`
* coarse front ends `coarse-A` (deployed, `(3, 8)`, ±300 kHz) and `coarse-E`
  (`(13, 8)`, ±700 kHz)

Thresholds are drawn by `survey_comparison.threshold_from` at 1% per candidate
point on this radio's own TX-off null, and turned into one verdict per probe by
`cross_radio.observation_fires` — the report's own rules, not a fresh one
invented here. Every threshold in this sweep comes back `supported: true`.

## The answer

The cliff does not move. Across all twelve (Fs, B_RX) combinations — sample
rate varying 4x, RX bandwidth varying 8x — detection collapses at the **same
offset**, to within the 30 kHz step the sweep resolves it at. That is the
headline arm, at the report's own −20 dB drive:

| detector | search span | cliff, every one of the 12 combinations |
|---|---:|---:|
| `coarse-A` | ±300 kHz | 400 → 450 kHz |
| `coarse-E` | ±700 kHz | 780–810 → 800–840 kHz |
| `full-frame-full` (conditioned at coarse-E) | inherits E | 810 → 840 kHz |

Each bank fails at **its own span plus ~115 kHz**, and the third detector fails
where the bank it is conditioned on fails. What predicts the cliff is the
`offset_span_hz` of the coarse bank; what does not predict it is either
bandwidth. Over the same matrix the nominal digital edge ranges 312 → 4,062 kHz
and the nominal analog edge −312 → 4,062 kHz.

The power column separates the two mechanisms directly. At `B_RX` = 1.25 MHz the
received power rolls off with offset — −37.9 dBFS at zero to −41.0 dBFS at 1 MHz
at 10 MS/s — so the analog filter is measurably attenuating the block; at
`B_RX` = 10 MHz it is flat within 0.7 dB. **Both rows die at the same offset.**
Pd falls while power is flat, and power falls without Pd following it: it is not
the analog filter.

The one thing bandwidth does buy is small and honest: `coarse-E`'s edge sits at
780–800 kHz on the 1.25 MHz arms against 810–840 kHz on the wider ones. An
eight-fold bandwidth change moves the cliff about 35 kHz.

The mechanism is visible in the coarse columns. `coarse-E` tracks the imposed
offset on its 116.7 kHz grid out to 700 kHz (residual ≤ 50 kHz), then pins to
its outermost hypothesis while the residual grows — 750/780/800/810 kHz give
residuals of 50/80/100/110 kHz and a peak-to-median decaying 6.30 → 3.48 — and
at 840 kHz it lets go entirely, recovering 583 kHz for a signal at 840 and
dropping to 2.14. `full-frame-full` goes with it, 0.949 to 0.056 in one step.
That 110 kHz of tolerable residual is
`survey_scoring`'s own ±113.6 kHz relative-phase uniqueness window, quoted in
the `CANDIDATE_COARSE` comment.

`coarse-A` is the deployed 3-hypothesis bank, and its edge — 400 → 450 kHz,
identical in all twelve cells — lands on the sky corpus's own 350–400 kHz
collapse.

## What the cliff does move with: drive

Three drives were run over the same matrix. What changes the offset a detection
survives is the SNR, and it changes it by nearly an order of magnitude — while
staying bandwidth-independent at every drive:

| TX drive | SNR at zero offset | `full-frame-full` at zero offset | offsets surviving | varies with Fs or B_RX? |
|---|---:|---:|---|---|
| −20 dB | +24 to +27 dB | 0.75–0.98 | to 810 kHz, dead at 840 | no — 12/12 identical |
| −55 dB | −6 to −11 dB | 0.26–0.37 | to 700 kHz, Pd 1.00 throughout | no — 12/12 identical |
| −64 dB | −19 to −20 dB | 0.10–0.14 | 0 only; every offset ≥ 200 kHz at the noise floor | no — 12/12 identical |

So the sky's 350–400 kHz collapse sits *between* these arms in drive, not
between them in bandwidth. Nothing in the matrix reproduces it as a bandwidth
effect at any SNR, and both an eight-fold bandwidth change and a four-fold rate
change leave it where it is.

The −64 dB arm shows the mechanism in its rawest form. There `coarse-E`'s
peak-to-median is 1.21–1.25 at *every* offset including zero — the coarse stage
has stopped working altogether — and the only offsets that still produce a score
are the ones that land exactly on its 116.7 kHz frequency grid, where the
residual is zero by luck rather than by search. At 10 MS/s and 10 MHz the
surviving offsets are 0 kHz (0.099) and 350 kHz (0.092) against 0.011–0.014
everywhere else; 350 kHz is 3 x 116.7 kHz. What is left of the pipeline's offset
tolerance at low SNR is the coarse grid, not the passband.

## Contents

| File | What it is |
|---|---|
| `raw/cfo_bandwidth-165.jsonl.gz` | the factorial at −20 dB: 228 cells (3 rates x 4 bandwidths x 19 offsets), one record per probe, a `cell_header` per cell carrying the readbacks, and a `null` arm per (rate, bandwidth) |
| `raw/cfo_bandwidth_mid-165.jsonl.gz` | 96 cells at −55 dB, 8 offsets |
| `raw/cfo_bandwidth_threshold-165.jsonl.gz` | 108 cells at −64 dB, 9 offsets, plus the 27 `cell_error` records from the first attempt (see below) |
| `raw/*.summary.json` | one object per cell, per arm, from `analyse_cfobw.py` |
| `raw/filter_shape-165.jsonl.gz` | the RX filter's measured power response, off the noise floor, per (rate, bandwidth) |
| `raw/drive_ladder-165.jsonl.gz` | `full-frame-full` against TX drive, which is how the three drives were chosen |
| `raw/bringup.json` | identity, `rf_bandwidth_available`, and the requested-vs-readback grid before anything radiated |
| `raw/sweep_*.log` | the run logs, including the per-cell pacing |
| `*.py` | the harnesses that produced the records and the analysis helpers |

`.jsonl.gz` files are one JSON record per line — gunzip and read directly.
`combine_cfobw.py` merges the three arms' summaries into a single object with
one entry per cell across all 432.

Totals: **432 cells, 17,280 scored probes** — 40 per cell, every cell, from 20
captures x 2 receivers — plus 3,120 TX-off null probes. 27 cells failed, all in
the −64 dB arm's first attempt, all re-run.

## The readback, and why it is not enough on its own

The brief asks for `rf_bandwidth` and `sampling_frequency` to be read back off
the phy channel after every write, because the AD9361 quantises `rf_bandwidth`
to achievable filter corners. That is recorded on every cell, through raw libiio
rather than the pyadi properties, and **it comes back clean everywhere**: all
twelve (rate, bandwidth) combinations return exactly the value written, and so
does `sampling_frequency`.

That is a weaker result than it looks, and it is the reason `filter_shape.py`
exists. The AD9361 driver stores the *requested* baseband bandwidth and reports
it back; the analog corner underneath is a coarse RC-tuning word. An unchanged
readback proves the driver accepted the request and nothing about the filter.

So the corner is **measured** instead: TX off, the input is the receiver's own
thermal noise, and thermal noise is the ideal probe because it is white by
construction — the received power spectrum *is* the filter's power response,
with no waveform, no detector and no offset estimate in between. Measured −3 dB
half-corners, and the resulting geometry:

| Fs | B_RX requested | readback | measured width | measured/requested | effective half-window | block clips beyond |
|---:|---:|---:|---:|---:|---:|---:|
| 2.5 | 1.25 | 1.25 | 1.392 | 1.11 | 696 kHz | already clipped |
| 2.5 | 2.50 | 2.50 | 2.349 | 0.94 | 1,174 kHz | 237 kHz |
| 2.5 | 5.00 | 5.00 | 2.407 | 0.48 | 1,192 kHz | 255 kHz |
| 2.5 | 10.0 | 10.0 | 2.385 | 0.24 | 1,192 kHz | 255 kHz |
| 5.0 | 1.25 | 1.25 | 1.519 | 1.22 | 760 kHz | already clipped |
| 5.0 | 2.50 | 2.50 | 3.056 | 1.22 | 1,528 kHz | 591 kHz |
| 5.0 | 5.00 | 5.00 | 4.735 | 0.95 | 2,368 kHz | 1,430 kHz |
| 5.0 | 10.0 | 10.0 | 4.815 | 0.48 | 2,408 kHz | 1,470 kHz |
| 10.0 | 1.25 | 1.25 | 1.588 | 1.27 | 794 kHz | already clipped |
| 10.0 | 2.50 | 2.50 | 3.144 | 1.26 | 1,572 kHz | 634 kHz |
| 10.0 | 5.00 | 5.00 | 6.099 | 1.22 | 3,049 kHz | 2,112 kHz |
| 10.0 | 10.0 | 10.0 | 9.505 | 0.95 | 4,753 kHz | 3,815 kHz |

Two things fall out of that table that the nominal formulae do not give you.
The analog filter sits about **1.2x wider** than requested — a 2.5 MHz request
measures 3.1 MHz — so `(B_RX − 1,875,000)/2` understates every analog edge. And
the widest arm at each rate is limited not by Fs/2 but by the digital
decimating FIR: `filter_fir_en` reads **1** on both RX channels at every rate,
decimating 4:1 (`RF:40000000 … RXSAMP:10000000`), and it lands the window at
~0.475·Fs rather than 0.5·Fs.

Between them the predicted clipping offset ranges from 237 kHz to 3,815 kHz
across this matrix — a factor of sixteen. A cliff that does not move across that
span is not a bandwidth effect.

## A failure worth keeping

The −64 dB arm's first attempt lost all 27 of its (rate, offset) TX loads and
recorded 27 `cell_error` records instead of measurements. The cause was in the
harness, not the radio: `Rig.load` proves the cyclic TX buffer actually started
by requiring the received rms to rise, and it was taking that proof at the
cell's own drive. At −64 dB the signal is under the noise floor, so "the port is
silent" and "the signal is below the floor" are the same reading and no working
transmitter could have passed. The check now runs at a fixed −20 dB and the gain
is dropped afterwards; `verify_gain_db` is recorded beside `tx_gain_db` on every
reference record so the two can never be confused again, and the analysis
refuses to price a bandwidth loss against a reference taken at a different
drive.

The failed records are left in `raw/cfo_bandwidth_threshold-165.jsonl.gz`
alongside the retry rather than deleted. The harness refusing to report a cell
it could not verify is the behaviour that makes the rest of this file
trustworthy, and it is worth being able to see it work.

## What these cannot tell you

No LNB, no antenna, no sky. Transmit and receive share one oscillator, so
nothing here speaks to LO drift. The thresholds are drawn on this radio's own
TX-off null at this rate, bandwidth and probe length, and are not the sky
corpus's thresholds.
