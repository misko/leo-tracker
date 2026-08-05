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
dwell. Its approximately 312.5 kHz filter margin is sufficient for Doppler but
not for the unknown, independent frequency errors of two inexpensive LNBs.

Production currently focuses on the channel-4 lower edge because that tuning
produced the first temporally confirmed exact-code event. The target list is
configurable with `LEO_BEACON_TARGETS`; the other three edge bands remain
available for controlled comparison. Every fifteenth narrow lock cycle (roughly
30 minutes) is followed by a wide acquisition on the same target. The cadence is
configurable with `LEO_BEACON_WIDE_EVERY_CYCLES`. Each wide dwell captures ten seconds
at 10 MS/s and 9 MHz bandwidth,
then searches digital 2.5 MHz subbands from -3.5 through +3.5 MHz in 500 kHz
steps. Every bank receives a joint frame-epoch/CFO matched search against both
the exact waveform and a 17-symbol-rolled control. The winning exact-minus-
control bank seeds detailed symbolwise tracking. PSS remains independent
supporting evidence rather than an authoritative timing seed. Once a stable LNB
offset is acquired, the continuous narrow mode supplies the denser Doppler record.

Narrow captures are joint-matched every two seconds using 10 ms windows. The
expensive symbolwise tracker runs only when the joint exact-minus-control margin
exceeds 0.008. This provides roughly ten times the earlier temporal coverage at
similar compute cost. Dual-RX agreement remains the promotion gate, while strong
single-RX evidence is explicitly labeled and retained for dense replay because
the two independent LNBs need not have equal sensitivity.

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
empirical null report from all non-confirmed observations. Narrow and wide modes
are kept separate because frequency-bank maximization changes the null. The
dashboard publishes receiver/check counts, match-margin percentiles, single-RX
exceedances, and complete dual-gate exceedances alongside the fixed gates.

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
- `reports/calibration/calibration.json`: cumulative empirical null distributions.
- `quarantine/`: recoverable interrupted sessions.

Useful commands:

```bash
env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-capture /mnt/leo-nvme/leo-tracker/captures/test \
  --duration-s 120 --channel-number 3 --region lower-edge

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-analyze /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/test.json
```

## Verification strategy

Unit tests pin published channel centers, pilot offsets, the PSS hexadecimal
sequence, its inversion/repetition pattern, all 16 pilot-code lengths, and the
4QAM code mapping. Synthetic tests exercise frame-period recovery, exact-PSS
epoch folding, pilot timing, carrier offset refinement, dual-channel artifact
round trips, noise rejection, and the time-shifted pilot control. CLI E2E tests
run capture through analysis using the fake paired radio. A finite fake-radio
watcher test also exercises capture, bounded asynchronous analysis, dense
follow-up, retention, calibration, and clean pipeline drain through the same
bash entry point used by the service. Storage tests cover
chunk rollover, SHA-256 verification, interrupted manifests, discontinuity
rejection, AGC/manual-gain semantics, and evidence-aware retention.

Hardware acceptance requires a complete capture with contiguous sample indexes,
monotonic timestamps, valid checksums, both receivers present, and a report.
A field detection additionally requires both receivers to pass the exact PSS
and pilot/control gates with consistent epoch and CFO. Longer-term false-alarm
thresholds will be calibrated from retained negative sky captures rather than
chosen from a desired result.

## Primary references

- Humphreys, Iannucci, Komodromos, and Graff, [Signal Structure of the Starlink
  Ku-Band Downlink](https://arxiv.org/abs/2210.11578), IEEE TAES 2023.
- Qin, Psiaki, Bowman, and Humphreys, [Pilots and Other Predictable Elements of
  the Starlink Ku-Band Downlink](https://arxiv.org/abs/2602.02627), 2026.
- Kozhaya, Saroufim, and Kassas, [Unveiling Starlink for PNT](https://navi.ion.org/content/72/1/navi.685),
  NAVIGATION 2025.
