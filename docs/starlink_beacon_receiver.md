# Starlink beacon receiver

## Objective and claim ladder

This receiver is designed to produce repeatable evidence, not to label every
sloped waterfall feature as Starlink. A result advances through these levels:

1. `structural`: excess complex autocorrelation at the published 750 Hz frame rate.
2. `pss`: a repeatable frame epoch from the exact published PSS, above folded-time controls.
3. `pilot`: the exact 2026 edge-pilot code beats a 17-symbol-shifted negative-control code.
4. `dual_rx`: RX0 and RX1 agree on frame epoch and carrier-frequency offset.
5. `doppler_lock`: carrier offset evolves continuously with physically plausible rate and curvature.
6. `identified`: the blind RF track is compatible with a retrospectively archived TLE trajectory.

Only levels 3 and above are called Starlink beacon candidates. TLEs never seed
the RF detector; they are used after detection to avoid circular identification.

## Frequencies and capture geometry

The published channel center is
`10.7 GHz + 117.1875 kHz + 250 MHz * (channel - 0.5)`. A universal LNB with a
9.75 GHz low-band LO maps channel 3 to 1.5751171875 GHz IF and channel 4 to
1.8251171875 GHz IF. Those channel centers contain a four-subcarrier gutter,
so a narrow receiver centered there is looking at the wrong structure.

Qin et al. publish two eight-subcarrier pilot bands per active 240 MHz channel.
Their centers are -115.4296875 MHz and +115.1953125 MHz relative to the channel
center. The four supported IFs are therefore:

| Channel | Region | Pluto IF | Ku RF | Captured pilot width |
|---:|---|---:|---:|---:|
| 3 | lower edge | 1.459687500 GHz | 11.209687500 GHz | 1.875 MHz |
| 3 | upper edge | 1.690312500 GHz | 11.440312500 GHz | 1.875 MHz |
| 4 | lower edge | 1.709687500 GHz | 11.459687500 GHz | 1.875 MHz |
| 4 | upper edge | 1.940312500 GHz | 11.690312500 GHz | 1.875 MHz |

The lock mode synchronously records both Pluto+ receivers as little-endian CI16
at 2.5 MS/s and 2.3 MHz RF bandwidth. This is 20 MB/s, or 2.4 GB per two-minute
dwell. The Nyquist interval leaves 312.5 kHz per side around the 1.875 MHz pilot,
but the configured analog RF filter leaves only about 212.5 kHz per side. That
can attenuate the largest LEO Doppler plus the unknown, independent frequency
errors of two inexpensive LNBs.

Production currently focuses on the channel-4 lower edge because that tuning
produced the strongest legacy candidates. Those events remain hypotheses: the
legacy carrier grid has since been shown to have large off-grid blind spots and
its confirmations must survive the newer detector before being called beacons.
The target list is
configurable with `LEO_BEACON_TARGETS`; the other three edge bands remain
available for controlled comparison. Every tenth cycle, production inserts a
15-second 5 MS/s, 3 MHz RF-bandwidth oversampled dwell. It demodulates natively
and also digitally downsamples the same IQ to 2.5 MS/s, yielding a controlled
same-signal comparison without confusing sky variability for a rate benefit.
The cadence is configurable with `LEO_BEACON_OVERSAMPLE_EVERY_CYCLES`.
Every fifteenth narrow lock cycle (roughly
30 minutes) is followed by a wide acquisition on the same target. The cadence is
configurable with `LEO_BEACON_WIDE_EVERY_CYCLES`. Each wide dwell captures ten seconds
at 10 MS/s and 9 MHz bandwidth,
then searches digital 2.5 MHz subbands from -3.5 through +3.5 MHz in 500 kHz
steps. Every bank receives a joint frame-epoch/CFO matched search against both
the exact waveform and a 17-symbol-rolled control. The winning exact-minus-
control bank seeds detailed symbolwise tracking. PSS remains independent
supporting evidence rather than an authoritative timing seed. Once a stable LNB
offset is acquired, the continuous narrow mode supplies the denser Doppler record.

The `coherent_grid_v1` detector joint-matches narrow captures every second using
10 ms windows. Its exact and rolled-control delay/CFO searches share a vectorized
FFT bank. Controlled tests found that its 25 kHz carrier grid can miss a perfect
pilot only 2.5--12.5 kHz off a grid point, so v1 is retained for retrospective
comparison rather than treated as the final detector.

`pss_symbolwise_v2` searches a coarse CFO bank with the published PSS, retains
multiple separated timing hypotheses, tracks the exact pilot code
noncoherently symbol by symbol, and then refines CFO on a 100 Hz grid. The
17-symbol-rolled control is evaluated at the *same* selected epoch and CFO; it
is not allowed to choose an easier unrelated hypothesis. Silent-window energy
masking prevents FFT roundoff from manufacturing PSS peaks. Detector generation
is written to every report and follow-up so their score populations cannot be
silently combined.
Dual-RX agreement remains the promotion gate, while strong single-RX evidence
is explicitly labeled and retained for dense replay because
the two independent LNBs need not have equal sensitivity.

`pilot_symbolwise_v3` is the production detector. At 2.5 MS/s the narrow PSS
contains only about eleven samples per frame, which made weak-signal timing
acquisition fragile even though v2 removed the carrier-grid blind spots. V3
instead searches every possible frame epoch by noncoherently combining 24
spread pilot-symbol anchors across the capture window and a coarse CFO bank.
It then applies the same conditioned exact-code versus rolled-control test and
fine CFO refinement as v2. The all-epoch search costs more CPU, so production
checks narrow captures every three seconds and wide captures every ten seconds;
a hit still launches the 100 ms dense follow-up.

Every elevated single-RX margin, plus any near-dual event with a common frame
epoch, triggers an automatic ±0.5 s replay at 100 ms spacing. Temporal
confirmation requires consecutive exact/control candidates with frame epochs
within 20 samples and a CFO change compatible with at most 15 kHz/s plus a
25 kHz tolerance. A separate cross-receiver rule accepts candidates that switch
between LNBs within 400 ms only when both RF chains select the same frame epoch
at each observation and the candidate CFO remains continuous. Because production
stays on one pilot band, the next dwell is already a repeat without a retune to
another edge. Isolated excursions remain follow-ups; they are not promoted.

Confirmed follow-ups are retrospectively joined to the archived pass catalog.
The report records every Starlink pass overlapping the RF event, its NORAD ID,
nearest predicted elevation/Doppler, and culmination elevation. With many
simultaneous visible spacecraft this is compatibility evidence, not unique
identification; unique TLE matching still requires a longer Doppler trajectory.

After every analyzed capture, `starlink-beacon-calibrate` rebuilds an
empirical null report from all non-confirmed observations. Acquisition methods
and narrow/wide modes are kept separate because detector and frequency-bank
maximization change the null. The
dashboard publishes receiver/check counts, match-margin percentiles, single-RX
exceedances, and complete dual-gate exceedances alongside the fixed gates.
`starlink-beacon-null-replay` creates a resumable, thermally guarded null for a
new detector by replaying stratified windows from retained strict negatives.

On 2026-08-05 v3 recovered two independent channel-4 lower-edge events. The
first produced consecutive dual-receiver links near capture time 76 s. The
second produced 21/21 dual-receiver candidate points over a dense two-second
replay: both receivers selected the same frame epoch to within one sample and
tracked parallel CFO curves from approximately -96 to -104 kHz. After those
captures were preserved and excluded, the initial v3 field null contained 48
checks with zero single- or dual-receiver gate crossings. These establish a
repeatable Starlink-specific code detection, but not a unique spacecraft ID;
many visible Starlinks overlap each event and the inexpensive LNB offsets hide
absolute Doppler. After deployment, the first live v3 observation found a third
event without retrospective targeting. Its dense replay again showed identical
dual-receiver timing and parallel CFO slopes near -4 kHz/s, demonstrating that
the production path can repeat the detection autonomously.

## Narrowband waveform decoding

Every confirmed narrow capture is now demodulated rather than stopping at a
correlation score. The decoder removes the measured carrier offset, solves the
eight edge-subcarrier coefficients for each OFDM symbol, aligns repeated 750 Hz
frames, and stacks them. It decodes the 300 published pilot symbols per frame
and the eight visible SSS subcarriers. Pilot channel estimates are cross-fitted:
each held-out symbol parity is equalized using the opposite parity, so a symbol
is not credited for fitting its own channel. The pilot and SSS sequences are
known synchronization structure; they are not decoded user payload.

Decoder revision 2 estimates and removes residual within-frame carrier slope,
weights repeated frames by coherent quality, and publishes normalized QPSK
state probabilities for every symbol. Independently equalized RX0/RX1 symbols
are inverse-noise combined, preserving per-receiver results alongside the soft
dual-RX result. On preserved event `20260805T203100Z`, this changed the separate
receiver decisions from 87.5%/86.5% to 88.0%/86.9%, while dual-RX combining
reached 95.3% pilot accuracy and 68.8% on the narrow SSS slice. A previously
weak receiver in event `20260805T200557Z` improved from 32.7% to 82.5% after a
-584 Hz residual-CFO correction; the combined pilot result reached 92.1%.

The preserved `20260805T171900Z` field event contains seven complete frames in
the selected 10 ms replay. Its held-out pilot decisions are 73.8% correct on
RX0 and 71.9% on RX1, compared with 25% random chance. The narrow SSS slice is
weaker at 44.6% and 37.5%, so it is supporting rather than conclusive evidence.
At 2.5 MS/s the receiver fully contains the 1.875 MHz pilot band, but it cannot
capture or decode the full 240 MHz Starlink channel, header, or user payload.

The 10 MS/s path is intentionally described as periodic, not continuous. A
hardware trial needed 121.4 seconds of wall time to return 30 seconds of dual-RX
IQ, while the 2.5 MS/s path runs approximately in real time. The NVMe is not the
bottleneck; this is the observed Pluto transport ceiling.

Before each dwell, the service applies a host thermal guard: capture pauses at
75 °C and resumes below 70 °C. This protects continuity and artifact integrity
without conflating thermal throttling with RF absence. Both host and Pluto
temperatures remain recorded in each capture manifest.

A bounded reader thread continuously refills the Pluto while a consumer thread
converts, hashes, and writes CI16 chunks. Manifests include host-side read
duration, positive inter-read gaps, and host-read duty. These diagnose writer
stalls but are explicitly not treated as RF hardware timestamps; the current
Pluto/IIO path cannot independently prove an overrun that firmware does not
report.

## Operations

The production service is `leo-tracker-beacon-watch.service`. By default it
captures consecutive two-minute channel-4 lower-edge dwells and adds a periodic
wide LNB-offset acquisition on that same edge. A bounded one-worker pipeline
analyzes capture N while the radio records capture N+1. There can be at most one
completed unprocessed capture, so analysis cannot build an unbounded NVMe
backlog. If processing falls behind, capture waits at that boundary. This raises
the narrow observation duty cycle from the former sequential capture/analysis
loop while preserving every capture for exact replay. All candidate captures
are preserved.
Only the newest twelve negative captures are kept. Interrupted captures are
moved to `quarantine` rather than deleted.
At startup, the service idempotently analyzes every complete artifact lacking a
report before beginning new captures. Thus a reboot between atomic capture
completion and analysis cannot strand or silently discard an observation.

Data live under `/mnt/leo-nvme/leo-tracker`:

- `captures/<session>/manifest.json`: versioned parameters, timing, chunk hashes, and state.
- `captures/<session>/chunk-*.ci16`: atomic five-second, dual-RX IQ chunks.
- `reports/<session>.json`: structural, PSS, exact-pilot, control, CFO, and confidence evidence.
- `reports/plots/<session>.png`: PSS, pilot/control margin, and CFO evidence.
- `reports/followups/<session>.json`: dense replay and temporal-confirmation evidence.
- `reports/decoded/<session>.json`: decoder parameters and pilot/SSS confidence metrics.
- `reports/decoded/<session>.npz`: equalized symbols, decisions, and channel estimates.
- `reports/decoded/<session>.png`: dual-RX constellations and held-out decision maps.
- `reports/calibration/calibration.json`: cumulative empirical null distributions.
- `quarantine/`: recoverable interrupted sessions.

Useful commands:

```bash
env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-capture /mnt/leo-nvme/leo-tracker/captures/test \
  --duration-s 120 --channel-number 3 --region lower-edge

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-analyze /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/test.json \
  --exact-acquisition-method pilot_symbolwise_v3

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-null-replay /mnt/leo-nvme/leo-tracker \
  /mnt/leo-nvme/leo-tracker/reports/calibration/pilot_symbolwise_v3-null

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-decode /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/followups/test.json \
  /mnt/leo-nvme/leo-tracker/reports/decoded/test.json \
  --plot /mnt/leo-nvme/leo-tracker/reports/decoded/test.png \
  --symbols /mnt/leo-nvme/leo-tracker/reports/decoded/test.npz
```

## Verification strategy

Unit tests pin published channel centers, pilot offsets, the PSS hexadecimal
sequence, its inversion/repetition pattern, all 16 pilot-code lengths, and the
4QAM code mapping. Synthetic tests exercise frame-period recovery, exact-PSS
epoch folding, pilot timing, off-grid carrier offsets from 2.5 through 337.5 kHz,
conditioned exact/control scoring, carrier offset refinement, dual-channel artifact
round trips, noise rejection, and the time-shifted pilot control. CLI E2E tests
run capture through analysis using the fake paired radio. A finite fake-radio
watcher test also exercises capture, bounded asynchronous analysis, dense
follow-up, retention, calibration, and clean pipeline drain through the same
bash entry point used by the service. Storage tests cover
chunk rollover, SHA-256 verification, interrupted manifests, discontinuity
rejection, AGC/manual-gain semantics, and evidence-aware retention.
Decoder tests pin the published SSS base-4 sequence and edge slices, recover
held-out pilots and SSS from a synthetic channel with CFO/frame phase/noise,
reject noise at chance-level accuracy, reject sample rates below the complete
1.875 MHz pilot span, recover a deliberately omitted 600 Hz carrier correction,
validate normalized soft probabilities, compare native 5 MS/s decoding against
the same IQ downsampled to 2.5 MS/s, and run JSON/NPZ/PNG generation through the public CLI.
Dashboard E2E coverage verifies both decoder artifact routes.

Hardware acceptance requires a complete capture with contiguous sample indexes,
monotonic timestamps, valid checksums, both receivers present, and a report.
A field detection additionally requires both receivers to pass the exact
pilot/control gates with consistent frame epoch and physically continuous CFO;
PSS is independent supporting evidence. Longer-term false-alarm thresholds are
calibrated from retained negative sky captures rather than chosen from a desired
result.

## Primary references

- Humphreys, Iannucci, Komodromos, and Graff, [Signal Structure of the Starlink
  Ku-Band Downlink](https://arxiv.org/abs/2210.11578), IEEE TAES 2023.
- Qin, Psiaki, Bowman, and Humphreys, [Pilots and Other Predictable Elements of
  the Starlink Ku-Band Downlink](https://arxiv.org/abs/2602.02627), 2026.
- Kozhaya, Saroufim, and Kassas, [Unveiling Starlink for PNT](https://navi.ion.org/content/72/1/navi.685),
  NAVIGATION 2025.
