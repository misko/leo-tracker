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
center. The four currently monitored IFs are therefore:

| Channel | Region | Pluto IF | Ku RF | Captured pilot width |
|---:|---|---:|---:|---:|
| 3 | lower edge | 1.459687500 GHz | 11.209687500 GHz | 1.875 MHz |
| 3 | upper edge | 1.690312500 GHz | 11.440312500 GHz | 1.875 MHz |
| 4 | lower edge | 1.709687500 GHz | 11.459687500 GHz | 1.875 MHz |
| 4 | upper edge | 1.940312500 GHz | 11.690312500 GHz | 1.875 MHz |

Each dwell synchronously records both Pluto+ receivers as little-endian CI16 at
2.5 MS/s and 2.3 MHz RF bandwidth. This is 20 MB/s, or 2.4 GB per two-minute
dwell. The 1.875 MHz pilot band leaves approximately 312.5 kHz of analog-filter
margin on either side for Doppler and modest LNB frequency error.

A bounded reader thread continuously refills the Pluto while a consumer thread
converts, hashes, and writes CI16 chunks. Manifests include host-side read
duration, positive inter-read gaps, and host-read duty. These diagnose writer
stalls but are explicitly not treated as RF hardware timestamps; the current
Pluto/IIO path cannot independently prove an overrun that firmware does not
report.

## Operations

The production service is `leo-tracker-beacon-watch.service`. It cycles through
the four pilot bands, captures two continuous minutes per tuning, analyzes the
capture, and then applies retention. All candidate captures are preserved.
Only the newest twelve negative captures are kept. Interrupted captures are
moved to `quarantine` rather than deleted.

Data live under `/mnt/leo-nvme/leo-tracker`:

- `captures/<session>/manifest.json`: versioned parameters, timing, chunk hashes, and state.
- `captures/<session>/chunk-*.ci16`: atomic five-second, dual-RX IQ chunks.
- `reports/<session>.json`: structural, PSS, exact-pilot, control, CFO, and confidence evidence.
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
run capture through analysis using the fake paired radio. Storage tests cover
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
