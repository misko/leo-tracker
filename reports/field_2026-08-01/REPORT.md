# Pluto+ Starlink Doppler field report — 650 Main St, Sausalito

Date: 2026-08-01 UTC
Status: **end-to-end field system operational; no defensible Starlink Doppler detection yet**

## Executive summary

The separate orbit and radio modules were exercised end to end on real hardware.
The orbit module froze a 10,769-object Starlink TLE catalog, propagated it with
SGP4, and generated pass geometry and expected Ku-band Doppler. The radio module
identified the LNB-connected Pluto+ input, recorded powered RX0 IQ, generated
memory-bounded waterfalls, and extracted moving features without using TLEs.
Only afterward were blind measurements compared with every visible satellite
and with negative controls.

The most suggestive powered trial was a five-minute recording at 1.325 GHz IF,
corresponding to the documented 11.075 GHz beacon channel under a 9.750 GHz LNB
LO. A blind nine-tone tracker produced a smooth curve that can be fitted to an
SGP4 curve:

![Powered RX0 waterfall with best SGP4 overlay](../../artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_report_65831/waterfall_expected_overlay.png)

That resemblance is **not a detection**. It fails three independent controls:

- Only 4 of 9 expected beacon teeth are positive at the median epoch.
- Wrong comb spacings of 37 and 50 kHz score slightly higher than the documented
  43.9495 kHz spacing.
- The electrically quiet, no-LNB RX1 control produces a still higher comb score
  and an equally smooth path.
- Across 256 visible-satellite hypotheses, a shifted-time control fits better
  than every correct-time hypothesis.

The requested measurement and expected-Doppler plots exist, but the evidence
does not support claiming that the plotted measurement is Starlink. The raw IQ,
checksums, null results, and controls are retained so future improvements can be
evaluated without moving the goalposts.

## Site and timing

The USB receiver is a u-blox 7 on `/dev/ttyACM1`; the Pluto+ is on
`/dev/ttyACM0`. After the antenna was relocated, the GPS produced this valid
NMEA fix:

| Field | GPS result |
|---|---:|
| UTC | 2026-08-01T14:09:28Z |
| Latitude | 37.849099833° N |
| Longitude | 122.485632167° W |
| Receiver-reported altitude | 77.6 m |
| Fix quality | 2 (differential) |
| Satellites used | 9 |
| HDOP | 1.46 |

The independently read position supplied during the experiment was
37.849165355°, −122.485676581°, within approximately 10 m of the serial fixes.
Receiver altitude varied from 48.6 to 102.7 m during monitoring, so orbit
work retains the independent 73 m terrain-model altitude rather than treating
the GNSS vertical component as surveyed. These differences are negligible
relative to the geometry discrimination in this experiment. The latest
machine-readable receiver fix is [fix.json](../../artifacts/site_650_main/gps/fix.json).

The host clock was NTP synchronized. New radio artifacts record first-buffer
host UTC in nanoseconds. GPS supplied position and an independent UTC check; it
was not wired as a Pluto sample-clock or PPS discipline source.

The site view was reported substantially obstructed. Orbit geometry uses SGP4
TEME converted to `ITRF_APPROX`, with UTC substituted for UT1 and no polar-motion
correction.

## Hardware identification

- Receiver: PlutoSDR/Pluto+, USB `0456:b673`
- Serial: `104000b299050013f4ff0700255e35222f`
- IIO endpoint: `ip:192.168.2.1`
- Capture implementation: `pyadi.ad9361`
- RX0: LNB-connected input throughout the experiment
- RX1: quiet input without an LNB during the original control trials; a second,
  different active LNB was connected at approximately 17:04 UTC
- Manual gain: 40 dB
- Clipping in powered scans: none

Identical sweeps established RX0 as the LNB path: RX1 minus RX0 median power was
−28.32 dB, with separation as large as −33.98 dB. The LNB power supply was
confirmed restored before the powered trials below. A post-power RX0 scan near
1.575 GHz measured approximately −34 dBFS with zero clipping.

![Powered LNB IF scan](../../artifacts/site_650_main/powered_lnb_scan_1575_rx0/scan.png)

Earlier overlapping scans found fixed front-end lines near 1,563.556 and
1,567.959 MHz. Their invariant absolute frequency identifies them as stationary
front-end/receiver features, not LEO Doppler.

### Dual-LNB hardware change

At approximately 17:04 UTC a different active LNB was connected to RX1. Results
before that time retain their intended interpretation because RX1 was then a
quiet no-LNB control. Results after the change must treat RX0 and RX1 as two
independent sensors with potentially different LO frequency, gain, bandpass,
polarization, and drift; RX1 is no longer a noise control.

The radio infrastructure was extended so `scan` and `capture` accept
`--channels 0`, `--channels 1`, or `--channels 0,1`. Dual mode uses one Pluto
connection, one shared retune, and one hardware refill returning both channels.
The full deterministic suite passed 60 tests. A live simultaneous 1.0–2.2 GHz
dual sweep then completed 241 shared centers per channel with zero clipping.
RX0/RX1 median powers were −35.37 and −42.35 dBFS respectively, while their
bandpass-power correlation was 0.691. The distinct power and line profiles
confirm two active but materially different receive chains. A subsequent dual
IQ smoke capture recorded exactly 1,000,000 samples per channel with one shared
first-buffer timestamp, and both SHA-256 artifact verifications passed. An
RX1-only capture through the same unified command also passed verification.

- [Simultaneous dual-LNB broad sweep](../../artifacts/site_650_main/scan_20260801T1704Z_dual_lnb_simultaneous_1p0_2p2GHz_gain40/scan.png)

### Dual-LNB LEO sweep and local capture

A fresh six-hour schedule beginning 17:10 UTC precisely solved 512 of the
strongest catalog candidates and contained repeated 80–89° Starlink geometry.
During the opening cluster, a simultaneous dual-LNB sweep sampled 241 shared
centers from 1.0 through 2.2 GHz. Relative to the 17:04 UTC baseline, RX0
spectral excess increased by 8.23 dB near 1.8800495 GHz and RX1 increased by
11.96 dB near 2.1050343 GHz. Neither change survived the fixed promotion rule:
the RX1 candidate produced one isolated center-shift match and RX0 produced two,
but both had zero features surviving the immediate confirmation pair.

- [Pass-time simultaneous dual-LNB sweep](../../artifacts/site_650_main/scan_20260801T1711Z_dual_lnb_simultaneous_1p0_2p2GHz_gain40_leo/scan.png)
- [RX1 2.105 GHz candidate rejection](../../artifacts/site_650_main/validated_scan_20260801T1715Z_rx1_2100_2110MHz_candidate/validated_scan.png)
- [RX0 1.880 GHz candidate rejection](../../artifacts/site_650_main/validated_scan_20260801T1717Z_rx0_1875_1885MHz_candidate/validated_scan.png)

A 90-second simultaneous local capture at the documented 1.825 GHz beacon IF
then recorded 360,000,000 samples per LNB through the 81–84° pass group. Blind
stationary-suppressed ridge scores were 12.20/11.31 for RX0/RX1, with respective
9.13/9.14 dB median excess. The selected paths were too broad and inconsistent
to identify: spans were 816/475 kHz and fitted slopes were −9.9/−3.4 kHz/s.
The documented 43.9495 kHz comb achieved scores of 2.799/2.904 but only 3/9
median tone support on both LNBs. RX0 lost to both predeclared wrong spacings;
RX1 beat its strongest wrong spacing by only 0.075 z, far below the fixed +1.0
margin. After median removal the two comb paths correlated at 0.67, but still
disagreed by 23.1 kHz RMS. The observation is retained as a radio-gate null,
not a Starlink detection.

Across the broad sweep, 17 strong paired lines established a repeatable
RX1−RX0 IF offset of +21,423 Hz (488 Hz MAD), with a 3.16 ppm frequency-
dependent slope and 1.10 kHz residual RMS. This is a useful empirical relative
LO calibration for future dual-LNB sky-frequency association, though the
calibration lines themselves must not be treated as LEO signals.

The 5.76 GB IQ pair was analyzed entirely on local storage before transfer.
Both NFS payloads were then independently re-read over Wi-Fi and matched their
manifests: RX0
`74057535ae77d108b640f7e94890bb508c96b63a1998c0c9757825b3954de604`
and RX1
`e3e9a22d144fd6941fe4ce23fc009576997a906789532614f2aee7feffaf987d`.
Only afterward were the two local duplicates replaced by exact NFS symlinks,
recovering 5.76 GB of acquisition space.

## Orbit provenance

- Frozen catalog retrieval: 2026-08-01T04:02:39.871852Z
- Objects: 10,769
- Catalog SHA-256:
  `59fb60183a4570f993a5ac57ff5ac99abb285be9fa95d4805ed105da687718e3`
- Frozen catalog: [starlink_catalog.json](../../artifacts/site_650_main/starlink_catalog.json)
- GPS-site schedule: [schedule_next_6h_gps.json](../../artifacts/site_650_main/schedule_next_6h_gps.json)

The primary CelesTrak endpoint was unreachable from this host. The frozen daily
mirror identifies CelesTrak as its source and is retained with retrieval time
and content hash. Candidate searches used the full frozen catalog rather than
selecting the satellite that looked best in a plot.

Expected one-way Doppler uses
`f_d = -(range_rate / c) * carrier`, so approaching objects are positive. At
11.325 GHz a Starlink pass can span approximately ±250 kHz.

## Captures and results

| Trial | Power/channel | IF | Rate / duration | Blind result | Controls | Verdict |
|---|---|---:|---:|---|---|---|
| A | power uncertain, RX0 | 1.575 GHz | 1 MS/s / 120 s | strongest ridge, 9.39 dB median SNR | geometry RMS 15.31 kHz; shifted 16.41 kHz | fail |
| B | power uncertain, RX0 | 0.575 GHz | 1 MS/s / 120 s | stationary spur, 12.26 dB | geometry RMS 18.25 kHz; shifted 15.44 kHz | fail |
| C | powered RX0 | 1.575 GHz | 4 MS/s / 240 s | comb 2.286 z, 3/9 teeth | 37 kHz: 2.314; 50 kHz: 2.300 | fail |
| D | powered RX0 | 1.325 GHz | 1.2 MS/s / 300 s | comb 2.395 z, 4/9 teeth | 37 kHz: 2.401; 50 kHz: 2.410 | fail |
| E | no-LNB RX1 | 1.325 GHz | 1.2 MS/s / 120 s | comb 2.516 z, 4/9 teeth | higher than powered RX0 | null control |

### Powered 1.575 GHz trial

- Start: 2026-08-01T04:40:05.301953Z
- Samples: 960,000,000 complex64 (7.68 GB)
- Dropped samples reported: 0
- IQ SHA-256:
  `f298672e79b648c0f02314048deca19ef63a029307a9ee3f57f2396bd104d5e7`

![Absolute powered RX0 waterfall](../../artifacts/site_650_main/powered_pass_20260801T0442Z_1575MHz_analysis/waterfall.png)

![Blind nine-tone track](../../artifacts/site_650_main/powered_pass_20260801T0442Z_1575MHz_analysis/moving_comb.png)

All 230 satellites crossing 20° were replayed at the exact measurement epochs.
The best correct-to-shifted-control improvement was only 1.174×, below the 2×
gate. Detailed machine-readable outputs:

- [orbit candidates](../../artifacts/site_650_main/powered_pass_20260801T0442Z_1575MHz_analysis/orbit_candidates.json)
- [expected Doppler tracks](../../artifacts/site_650_main/powered_pass_20260801T0442Z_1575MHz_analysis/expected_doppler_tracks.json)
- [candidate ranking](../../artifacts/site_650_main/powered_pass_20260801T0442Z_1575MHz_analysis/orbit_candidate_ranking.json)

### Powered 1.325 GHz trial

- Start: 2026-08-01T04:58:27.830510Z
- Samples: 360,000,000 complex64 (2.88 GB)
- Dropped samples reported: 0
- IQ SHA-256:
  `1364438a5d327131bc31c242e003d43ab62c99556220d4df931feefc95de0605`
- Correct comb score: 2.395 z; median positive teeth: 4/9
- Wrong-spacing controls: 2.401 z at 37 kHz; 2.410 z at 50 kHz

![Blind comb diagnostics](../../artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_analysis/moving_comb.png)

The all-catalog replay found 256 satellites above 20°. The linear-only null RMS
was 19.720 kHz. The best correct-time fit was 14.379 kHz, but the best of 512
±60-second shifted controls was better at 14.184 kHz. The top conservative
candidate, STARLINK-35337 / NORAD 65831, improved over both null families by
only 1.327×.

- [orbit candidates](../../artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_analysis/orbit_candidates.json)
- [expected Doppler tracks](../../artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_analysis/expected_doppler_tracks.json)
- [candidate ranking](../../artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_analysis/orbit_candidate_ranking.json)
- [candidate-specific report](../../artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_report_65831/REPORT.md)

### RX1 noise control

The no-LNB RX1 capture used the same 1.325 GHz tuning, sample rate, bandwidth,
gain, and detector. It produced a smooth 123.5 kHz path with score 2.516 z and
4/9 median tooth support—stronger than powered RX0. This demonstrates that
smooth Viterbi paths can be manufactured from receiver noise/transients and
must never be accepted without radio-structure and orbit controls.

![No-LNB RX1 false track](../../artifacts/site_650_main/control_rx1_20260801T0510Z_1325MHz_analysis/moving_comb.png)

### Continued three-IF monitoring cycle

At 05:18 UTC, six additional 30-second blocks interleaved RX0 and RX1 at all
three documented low-LO beacon IFs. Each block contains 36,000,000 complex
samples at 1.2 MS/s. The true-spacing score was compared with both its paired
hardware control and the two wrong-spacing controls before any orbit search:

| IF | RX0 true | RX1 true | RX0−RX1 | Best RX0 wrong spacing | Median tooth support | Radio-qualified? |
|---:|---:|---:|---:|---:|---:|---|
| 1.325 GHz | 2.731 z | 2.688 z | +0.043 z | 2.787 z | 4/9 | no |
| 1.575 GHz | 2.545 z | 2.666 z | −0.121 z | 2.694 z | 4/9 | no |
| 1.825 GHz | 2.560 z | 2.625 z | −0.065 z | 2.692 z | 4/9 | no |

The 1.325 GHz RX0 score is marginally above RX1, but its 37 kHz wrong-spacing
score is higher than the documented Starlink spacing. The other two IFs score
below their no-LNB controls. Because no block passed the radio-only gate, no
TLE was fitted to this cycle. This ordering prevents an orbit bank from turning
an ordinary noise path into a post-hoc satellite candidate.

- [1.325 GHz RX0 diagnostics](../../artifacts/site_650_main/monitor_20260801T0518Z_1325MHz_rx0_analysis/comb_43949.png)
- [1.325 GHz RX1 diagnostics](../../artifacts/site_650_main/monitor_20260801T0518Z_1325MHz_rx1_analysis/comb_43949.png)
- [1.575 GHz RX0 diagnostics](../../artifacts/site_650_main/monitor_20260801T0518Z_1575MHz_rx0_analysis/comb_43949.png)
- [1.825 GHz RX0 diagnostics](../../artifacts/site_650_main/monitor_20260801T0518Z_1825MHz_rx0_analysis/comb_43949.png)

### Simultaneous two-receiver monitoring

The next implementation enabled both AD9361 receive channels in one hardware
refill and jointly published two artifacts with a shared first-buffer UTC and
pair session ID. This removes the 30-second offset in the interleaved controls.

| IF / duration | RX0 true | simultaneous RX1 | Best RX0 wrong spacing | Tooth support | Radio-qualified? |
|---:|---:|---:|---:|---:|---|
| 1.325 GHz / 90 s | 2.522 z | 2.448 z | 2.494 z | 4/9 | no |
| 1.575 GHz / 240 s | 2.433 z | 2.435 z | 2.473 z | 4/9 | no |
| 1.825 GHz / 180 s | 2.433 z | 2.430 z | 2.470 z | 4/9 | no |
| 1.325 GHz repeat / 180 s | 2.393 z | 2.455 z | 2.436 z | 4/9 | no |
| 1.575 GHz repeat / 240 s | 2.425 z | 2.430 z | 2.459 z | 4/9 | no |

The first 1.325 GHz block is the nearest radio-only result: its documented spacing
beats both wrong spacings and simultaneous RX1. It still fails the predeclared
70% tooth-support gate, and its tracked center oscillates within a narrow band
rather than presenting an unambiguous pass-scale curve. More importantly, its
predeclared repeat reverses the result: true-spacing RX0 falls below both RX1
and a wrong-spacing control. The other two IFs also fail the hardware or
wrong-spacing control directly. None proceeds to TLE matching.

- [paired 1.325 GHz RX0](../../artifacts/site_650_main/paired_20260801T0526Z_1325MHz_analysis/rx0/comb_43949.png)
- [paired 1.325 GHz RX1](../../artifacts/site_650_main/paired_20260801T0526Z_1325MHz_analysis/rx1/comb_43949.png)
- [paired 1.575 GHz RX0](../../artifacts/site_650_main/paired_20260801T0529Z_1575MHz_analysis/rx0/comb_43949.png)
- [paired 1.825 GHz RX0](../../artifacts/site_650_main/paired_20260801T0534Z_1825MHz_analysis/rx0/comb_43949.png)
- [paired 1.325 GHz repeat RX0](../../artifacts/site_650_main/paired_20260801T0543Z_1325MHz_repeat_analysis/rx0/comb_43949.png)
- [paired 1.575 GHz repeat RX0](../../artifacts/site_650_main/paired_20260801T0549Z_1575MHz_repeat_analysis/rx0/comb_43949.png)

Three later 30-second paired snapshots continued the same monitoring cycle:

| IF | RX0 true | simultaneous RX1 | Best RX0 wrong spacing | Tooth support | Result |
|---:|---:|---:|---:|---:|---|
| 1.325 GHz | 2.713 z | 2.543 z | 2.716 z | 4/9 | wrong spacing wins |
| 1.575 GHz | 2.607 z | 2.525 z | 2.701 z | 4/9 | wrong spacing wins |
| 1.825 GHz | 2.556 z | 2.619 z | 2.722 z | 4/9 | RX1 and wrong spacing win |

None qualified for orbit matching. Across long and short blocks, the persistent
4/9 support and comparable wrong-spacing/RX1 scores form a stable null pattern.

At 06:11 UTC, another three-IF cycle overlapped a dense high-elevation window.
The 1.575 GHz precursor was the strongest short radio-only near-event so far:
RX0 true spacing scored 2.785 z versus 2.723 z for its best wrong spacing and
2.672 z on simultaneous RX1. It still supported only 4/9 teeth. A predeclared
180-second follow-up was started immediately across the 06:14 culmination. The
advantage did not reproduce: RX0 true spacing fell to 2.435 z while its 50 kHz
control reached 2.456 z; support remained 4/9. No orbit search was performed.

- [1.575 GHz precursor RX0](../../artifacts/site_650_main/paired_20260801T0611Z_1575MHz_cycle_analysis/rx0/comb_43949.png)
- [1.575 GHz follow-up RX0](../../artifacts/site_650_main/paired_20260801T0614Z_1575MHz_followup_analysis/rx0/comb_43949.png)

Monitoring continued through the 06:20–06:28 high-elevation cluster with short
synchronized snapshots at all three IFs. Every block was rejected before orbit
matching. Representative results include 1.325 GHz true/RX1/wrong of
2.557/2.647/2.622 z, 1.575 GHz true/best-wrong of 2.487/2.737 z, and 1.825 GHz
true/best-wrong of 2.620/2.690 z. Tooth support remained at or below 4/9.

At 06:41–06:43 UTC, jointly atomic 30-second paired captures covered another
dense cluster of predicted high-elevation passes. The persisted TLE-blind
qualifier rejected all three IFs:

| IF | RX0 true | RX0 true − best wrong | RX0 true − RX1 true | Tooth support | Result |
|---:|---:|---:|---:|---:|---|
| 1.325 GHz | 2.807 z | +0.059 z | −0.020 z | 4/9 | reject |
| 1.575 GHz | 2.948 z | +0.121 z | +0.126 z | 4/9 | reject |
| 1.825 GHz | 2.800 z | −0.020 z | −0.142 z | 4/9 | reject |

The required margins are +1.0 z over every wrong spacing and +1.0 z over RX1,
with at least 70% tone support. Thus even the strongest-looking 1.575 GHz block
missed both score margins by nearly an order of magnitude and again reproduced
the stable 4/9 null pattern. No TLE identity search or position fit was run.

A 120-second paired 1.575 GHz follow-up then integrated across the 06:47:48 UTC
near-overhead culmination. Its true-spacing score was 2.638 z, only +0.005 z
above the 37 kHz control, +0.031 z above the 50 kHz control, and +0.040 z above
simultaneous RX1. Support remained 4/9. The longer observation therefore
eliminated rather than strengthened the preceding short-block hint. Median
sample power was 29.38 dB in powered LNB RX0 versus 0.16 dB in no-LNB RX1, a
29.22 dB separation confirming that the antenna/LNB path remained powered and
distinct during the radio-null result.

The matched 120-second sweep was then completed at the other two IFs across
additional high-elevation culminations:

| IF | RX0 true | True − 37 kHz | True − 50 kHz | True − RX1 | Support | RX0/RX1 power separation |
|---:|---:|---:|---:|---:|---:|---:|
| 1.325 GHz | 2.711 z | +0.076 z | +0.083 z | +0.064 z | 4/9 | 30.80 dB |
| 1.575 GHz | 2.638 z | +0.005 z | +0.031 z | +0.040 z | 4/9 | 29.22 dB |
| 1.825 GHz | 2.636 z | −0.021 z | +0.008 z | −0.028 z | 4/9 | 27.98 dB |

All three powered antenna observations are statistically indistinguishable
from their wrong-spacing and no-LNB controls at the beacon-comb level. The
large broadband RX0/RX1 power separation in every IF confirms that this null
cannot be attributed to the LNB supply being off.

At 07:10 UTC, a 180-second paired 1.575 GHz observation spanned multiple
predicted 88–90° culminations. The true 43.9495 kHz spacing scored 2.748 z, but
beat the two wrong spacings by only +0.005 and +0.007 z and RX1 by only +0.098 z;
support remained 4/9. An independent single-ridge search found moving scores of
15.38 on RX0 and 14.91 on RX1, with implausibly broad 580 and 498 kHz path spans
and similar 10.45/10.13 dB median excess. Thus the blind ridge was also a
control-matched chain of residual peaks, not an antenna-only Doppler detection.
RX0 broadband power remained 29.25 dB above RX1 throughout the null.

The next overhead cluster included a catalog entry marked DTC and several
predicted 86–90° culminations. Paired 60-second snapshots at 1.325 and
1.825 GHz still failed all radio gates:

| IF | RX0 true | True − 37 kHz | True − 50 kHz | True − RX1 | Support | RX0/RX1 power separation |
|---:|---:|---:|---:|---:|---:|---:|
| 1.325 GHz | 2.750 z | +0.129 z | +0.021 z | +0.061 z | 4/9 | 30.84 dB |
| 1.825 GHz | 2.854 z | +0.128 z | +0.070 z | +0.132 z | 4/9 | 28.03 dB |

Neither result approaches the fixed +1.0 z control margins or 70% tone-support
threshold, so the presence of favorable geometry and a DTC-capable catalog
object does not change the radio-null conclusion.

## Broad IF survey and real-carrier control

A new 1.0–2.2 GHz receive-only survey used 5 MHz tuning steps on RX0 followed
immediately by RX1. Powered RX0 exceeded RX1 by a median 30.73 dB. The strongest
RX0-only spectral excess occurred in the 1.755 GHz tuning. A paired 60-second
capture and a +200 kHz center-frequency shift proved that a narrow line was
fixed in absolute RF near 1,755,186 kHz: it moved from approximately +185.6 kHz
to −14.2 kHz baseband when the tuner moved by +200 kHz. Thus it is real
antenna/LNB-path RF, not a Pluto tuning-locked spur.

- [RX0 broad IF survey](../../artifacts/site_650_main/scan_20260801T0730Z_rx0_1p0_2p2GHz/scan.png)
- [RX1 broad IF control survey](../../artifacts/site_650_main/scan_20260801T0731Z_rx1_1p0_2p2GHz/scan.png)

A tested, memory-bounded narrow-carrier tracker then measured a retuned paired
180-second observation across multiple high-elevation passes. RX0 yielded all
180 points with 8.66 dB median prominence, 3.06 kHz total span,
+16.121 Hz/s linear drift, and 264 Hz linear-fit residual RMS. RX1 was
noise-like: 3.10 dB median prominence, 19.79 kHz span, and 5.82 kHz residual
RMS. The line is therefore real and antenna-only. It is not the documented
Starlink beacon comb: correct 43.9495 kHz spacing lost to both wrong-spacing
controls, exceeded RX1 by only 0.033 z, and again supported only 4/9 teeth.

- [RX0 carrier track](../../artifacts/site_650_main/paired_20260801T0736Z_1755p2857MHz_carrier_180s/rx0/carrier.png)
- [Exploratory orbit-control report](../../artifacts/site_650_main/paired_20260801T0736Z_1755p2857MHz_carrier_180s/rx0/ORBIT_EXPLORATORY_REPORT.md)

For completeness, the orbit module screened all 10,769 frozen catalog TLEs;
172 objects crossed above 20° during the capture. With only a constant carrier
bias allowed, the measured constant-null RMS was 878 Hz while the best
correct-time Starlink candidate fit at 5.612 kHz RMS—more than six times worse
than the null. A shifted-time control fit at 2.432 kHz and beat every
correct-time candidate. Even a diagnostic model with an extra drift nuisance
fit at 1.511 kHz versus the 264 Hz linear null, required +105 Hz/s to cancel the
predicted orbital slope, and lost to its shifted control. The carrier is thus
incompatible with defensible Starlink LEO geometry; it is retained as a useful
stationary/GEO/local-RF calibration signal, not a navigation observable.

Two independent fast broad scans and a third scan with retune settling increased
from 20 to 250 ms reproduced the same strongest apparent peaks. Centered paired
follow-ups revealed that reproducibility at one tuner setting was insufficient:

| Survey peak | Centered paired follow-up | Classification |
|---:|---|---|
| 1.786877 GHz | RX0/RX1 prominence 3.13/3.12 dB; ~20 kHz random spans | tuning/filter edge artifact |
| 2.005056 GHz | RX0/RX1 residual 5.74/5.65 kHz; full-window random spans | tuning artifact |
| 1.255034 GHz | RX0/RX1 residual 5.57/5.86 kHz; full-window random spans | tuning artifact |

All three paired beacon-comb checks also failed every gate with 4/9 support.
This motivates a stricter validated-scan method: acquire each candidate at two
offset tuner centers and require an excess to remain at the same absolute RF
frequency in their overlap. A peak that only repeats at one baseband offset or
filter edge is not promoted to a time capture.

That center-shift validated scanner was implemented with synthetic fixed-RF,
baseband-spur, noise, JSON, and plot tests, then run across 1.0–2.2 GHz on RX0.
It reduced the old survey's many apparent peaks to exactly two validated lines:
1,000,029,077 and 1,000,035,913 Hz. Both agreed across +200 kHz tuner shifts to
within 49 Hz, with respective shifted/primary prominences of 19.0/20.3 dB and
13.9/13.1 dB.

- [Center-shift validated IF scan](../../artifacts/site_650_main/validated_scan_20260801T0807Z_rx0_1p0_2p2GHz/validated_scan.png)

A paired 120-second capture placed both lines about −100 kHz from DC during an
89.9° predicted pass. They were strongly present only on RX0 but essentially
stationary:

| Absolute IF | RX0 median prominence | Span | Drift | Fit residual | RX1 behavior |
|---:|---:|---:|---:|---:|---|
| 1,000,029,038 Hz | 29.81 dB | 25.8 Hz | +0.082 Hz/s | 5.0 Hz | 2.72 dB, 4.90 kHz random span |
| 1,000,035,948 Hz | 22.02 dB | 36.0 Hz | −0.259 Hz/s | 3.0 Hz | 2.61 dB, 2.95 kHz random span |

The approximately 6.914 kHz-separated pair is useful as a stable
antenna/LNB-path calibration reference, but its near-zero motion during the
overhead LEO interval excludes it as a Doppler navigation observable. Its
43.9495 kHz beacon-comb qualification also failed all gates with 4/9 support.

A denser validated scan then covered 1.2–1.9 GHz continuously at 3 MHz steps
and an 8 dB prominence threshold during 80–85° predicted passes. RX0 produced
one strong transient at 1,784,677,515 Hz (28.0/14.2 dB primary/shifted
prominence and 537 Hz retune agreement); RX1's only match was an unrelated weak
low-boundary feature. A paired 120-second follow-up minutes later did not retain
the narrow line, so its full ±500 kHz band was searched blindly for motion.

RX0's blind moving score was 15.015 versus 12.906 on RX1, with a nominal
+4.439 kHz/s slope and 527 kHz robust span. Formal controls showed that this was
peak stitching rather than orbital motion: RX0 had 5.127/11.279 kHz
median/95th-percentile frame steps at 0.218 s cadence, closely matching RX1's
4.688/11.426 kHz; their median excesses were also identical at 10.021/10.056 dB.
Across 10,769 TLEs and 141 objects above 20°, the best correct-time orbit fit
was 178.343 kHz RMS and anticorrelated with the data at −0.758. It lost even to
the 174.562 kHz constant null, while linear/quadratic/cubic nulls reached
82.524/45.214/39.770 kHz. The best shifted-time control also beat every
correct-time orbit. Evidence was 0.254× versus the required 2×.

- [Dense RX0 validated scan](../../artifacts/site_650_main/validated_scan_20260801T0825Z_rx0_1p2_1p9GHz_dense/validated_scan.png)
- [Dense RX1 control scan](../../artifacts/site_650_main/validated_scan_20260801T0827Z_rx1_1p2_1p9GHz_dense/validated_scan.png)
- [Moving-ridge orbit-control report](../../artifacts/site_650_main/paired_20260801T0829Z_1784p7775MHz_validated_120s/rx0/ORBIT_EXPLORATORY_REPORT.md)

Validated-scan schema v2 now records primary and shifted acquisition UTC,
their midpoint and separation, and overall scan start/end UTC. Future transient
features can therefore be orbit-scored at their actual scan time rather than
only after a delayed paired follow-up.

### Timestamped transient follow-ups and retune-settling control

A timestamped 1.525–1.625 GHz center-shift survey produced a narrow candidate
near 1,524,477,075 Hz. An immediate 120-second paired follow-up confirmed a
real antenna-path carrier on RX0: median prominence was 12.14 dB versus
3.12 dB on RX1, total span was 1.78 kHz, linear drift was +14.02 Hz/s, and the
linear-fit residual was 157 Hz. The blind moving-ridge result did not survive
the RX1 control, and the beacon-comb qualifier rejected the documented spacing,
RX1-margin, and tone-support gates.

- [Timestamped 1.525–1.625 GHz survey](../../artifacts/site_650_main/validated_scan_20260801T0840Z_rx0_1525_1625MHz/validated_scan.png)
- [RX0 narrow-carrier track](../../artifacts/site_650_main/paired_20260801T0840Z_1524p577MHz_candidate_120s/rx0/carrier.png)
- [All-catalog orbit-control report](../../artifacts/site_650_main/paired_20260801T0840Z_1524p577MHz_candidate_120s/rx0/ORBIT_EXPLORATORY_REPORT.md)

At the scan-to-capture boundary the fitted carrier changed by only +181 Hz.
The all-catalog replay screened 10,769 TLEs and 138 objects above 20°. The best
correct-time candidate, STARLINK-36293 / NORAD 68515, predicted a 17.3 kHz
span and missed the measurement by 5.35 kHz RMS, while a simple linear null
reached 157 Hz. A predeclared shifted-time control fit at 2.30 kHz and beat all
correct-time candidates. The evidence ratio was 0.029× versus the required
2×. This is real RF but a formal Starlink/orbit non-detection; LNB/receiver LO
drift also remains confounded with its small apparent slope.

A subsequent 1.775–1.875 GHz survey appeared to validate a feature near
1,784,685,449 Hz, but its two acquisitions were separated by only 155 ms and
the matcher paired different peaks. The strong primary line vanished in the
shifted acquisition and was absent from an immediate 120-second repeat. In that
repeat, RX0 and RX1 both showed noise-like approximately 19.4 kHz carrier spans;
RX1 also beat RX0 in the blind moving-ridge score. The persisted paired
qualifier rejected wrong-spacing, RX1, and tone-support gates.

An initial control repeated one tuning with two settle intervals. At 50 ms,
another apparent validated feature scored 11.93; with an otherwise identical
1.0-second settle, no feature survived:

- [50 ms diagnostic-only scan](../../artifacts/site_650_main/validated_scan_20260801T0847Z_rx0_1784_shortsettle/validated_scan.png)
- [1.0 s promotion-grade control](../../artifacts/site_650_main/validated_scan_20260801T0847Z_rx0_1784_settle1s/validated_scan.png)

Subsequent full surveys established that one second was still insufficient. A
feature near 1,784,678 kHz recurred in two nominally promotion-grade surveys,
within 488 Hz across observations, but vanished from an immediate 120-second
paired capture. RX0 and RX1 carrier residuals were both noise-like at
approximately 11–12 kHz, RX1 beat RX0 in the blind moving score, and the comb
failed the wrong-spacing, RX1, and tone-support gates. Single-center controls
initially showed the feature absent after three-second settling on RX0 with
both +200 and −200 kHz validation offsets, and absent on RX1. A later full
three-second survey nevertheless reproduced it, proving that settling time
alone was not a sufficient control.

Thirty consecutive three-second pairs at a fixed 1.784 GHz center produced
zero matches. In contrast, repeating the preceding 1.781→1.784 GHz sweep
transition produced the 1.784678 GHz line on all 12 post-jump points while the
strong primary peak remained pinned to +677,490 Hz baseband. The same sequence
also produced analogous 1.78188 GHz lines. This deterministic dependence on
the prior tuner state identifies a receiver/LNB retune artifact, not sky
Doppler.

- [Fixed-center 30-pair null](../../artifacts/site_650_main/spot_monitor_20260801T0919Z_rx0_1784_settle3s_30x/validated_scan.png)
- [Artifact-generating transition sequence](../../artifacts/site_650_main/sequence_monitor_20260801T0922Z_rx0_1781_1784_settle3s_12x/validated_scan.png)

Accordingly, every validated scan in this report acquired with less than three
seconds of retune settling is classified **diagnostic-only**, including the
1.524 GHz carrier discovery and both 1.784678 GHz recurrences. The scanner
defaults to a 3.0-second settle and requires two consecutive center-shift pairs
at every center. A feature must survive both pairs at the same absolute RF.
Shorter or single-pair runs persist `promotion_grade: false` plus explicit
reasons. Only confirmed features from promotion-grade scans may trigger a
Starlink claim or position solution.

The first scans run at the intermediate one-second rule covered all three
documented IF regions and produced nulls at 1.275–1.375 and 1.525–1.625 GHz.
They are retained as settling experiments, but are no longer promotion-grade.

- [One-second diagnostic 1.525–1.625 GHz null](../../artifacts/site_650_main/validated_scan_20260801T0900Z_rx0_1525_1625MHz_settle1s/validated_scan.png)

Schema v3 persists both individual pair matches and separately confirmed
features. A direct transition control retained five individual artifact peaks
but zero confirmed features. Full 3-second, two-confirmation surveys then
covered all three documented IF regions: 1.275–1.375 and 1.525–1.625 GHz had
zero individual or confirmed matches; 1.775–1.875 GHz retained one isolated
pair but zero confirmed features. No v3 scan qualified for paired capture or
orbit fitting.

- [Consecutive-pair artifact rejection](../../artifacts/site_650_main/confirmation_control_20260801T0927Z_rx0_1781_1784_settle3s_3x/validated_scan.png)
- [Strict 1.275–1.375 GHz scan](../../artifacts/site_650_main/validated_scan_20260801T0929Z_rx0_1275_1375MHz_v3/validated_scan.png)
- [Strict 1.525–1.625 GHz scan](../../artifacts/site_650_main/validated_scan_20260801T0936Z_rx0_1525_1625MHz_v3/validated_scan.png)
- [Strict 1.775–1.875 GHz scan](../../artifacts/site_650_main/validated_scan_20260801T0943Z_rx0_1775_1875MHz_v3/validated_scan.png)

A second complete v3 cycle overlapped an exceptional sequence of independent
78–90° predicted culminations from 09:57 through 10:16 UTC. All three bands
were clean: each retained zero individual pair matches and zero confirmed
features across 68 center-shift points. These favorable-geometry nulls further
constrain RF visibility at the obstructed site; they do not justify raw capture
or an all-catalog orbit search.

- [Overhead 1.275–1.375 GHz null](../../artifacts/site_650_main/validated_scan_20260801T0957Z_rx0_1275_1375MHz_v3_overhead/validated_scan.png)
- [Overhead 1.525–1.625 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1005Z_rx0_1525_1625MHz_v3_overhead/validated_scan.png)
- [Overhead 1.775–1.875 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1012Z_rx0_1775_1875MHz_v3_overhead/validated_scan.png)

To complement the sparse per-frequency survey dwell, a continuous 180-second
paired capture at the documented 1.825 GHz beacon IF spanned the 82.4, 86.4,
and 84.3° culminations. It also failed every radio gate. The blind moving ridge
on RX0 scored 13.75 with a 250.0 kHz span, while the simultaneous no-LNB RX1
control scored higher at 14.73 with a 461.8 kHz span. Correct beacon spacing
lost the wrong-spacing, RX1-margin, and tone-support controls. This is a
continuous favorable-geometry radio null, so no TLE identity search was run.

An identically configured continuous 180-second pair at 1.575 GHz then covered
the 89.7, 76.1, 80.1, and 80.7° culminations. It reproduced the same null:
RX0 scored 13.73 with a 234.3 kHz stitched span, while RX1 scored higher at
14.45 with a 396.5 kHz span; the comb again failed wrong-spacing, RX1-margin,
and tone-support gates. The matched 1.575/1.825 GHz observations show that the
null persists under continuous multi-pass sampling, not only sparse surveys.

The matched 1.325 GHz capture covered the 89.4, 75.1, and 86.3° culminations.
Its RX0 ridge scored 13.90 versus 13.75 on RX1, but their 304.4/277.5 kHz
stitched spans and 10.02/10.06 dB median excess were control-matched. The comb
again failed all three gates. Thus the complete continuous three-IF set is a
radio-only null; none proceeds to orbit matching.

Monitoring continued under the extended six-hour schedule with another full
schema-v3 cycle from 10:56 through 11:18 UTC. It covered repeated 80–89°
culminations, including an 87.4° DTC-labelled object and an 89.6° pass. All
three IF regions again retained zero individual pair matches and zero confirmed
features across 68 points per band. The result is independently null under the
same fixed promotion rule.

- [10:56 UTC strict 1.775–1.875 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1056Z_rx0_1775_1875MHz_v3_cluster/validated_scan.png)
- [11:03 UTC strict 1.525–1.625 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1103Z_rx0_1525_1625MHz_v3_cluster/validated_scan.png)
- [11:11 UTC strict 1.275–1.375 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1111Z_rx0_1275_1375MHz_v3_cluster/validated_scan.png)

The next strict cycle from 11:20 through 11:42 UTC covered repeated 78–86°
passes, an 81.4° DTC-labelled object, and an 88.6° culmination. The
1.775–1.875 GHz scan retained one isolated pair at 1,784,676,782 Hz—again the
known transition-dependent line—but it failed the immediate confirmation pair.
The other two bands had zero individual matches. All three confirmed-feature
lists were empty, so no raw capture or orbit search was triggered.

- [11:20 UTC isolated-peak rejection](../../artifacts/site_650_main/validated_scan_20260801T1120Z_rx0_1775_1875MHz_v3_cluster2/validated_scan.png)
- [11:27 UTC strict 1.525–1.625 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1127Z_rx0_1525_1625MHz_v3_cluster2/validated_scan.png)
- [11:34 UTC strict 1.275–1.375 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1134Z_rx0_1275_1375MHz_v3_cluster2/validated_scan.png)

Another complete strict cycle from 11:43 through 12:05 UTC covered repeated
81–89° passes and an 80.8° DTC-labelled event. The lower two IF bands had zero
individual matches. The upper band retained one weak isolated pair at
1,784,676,416 Hz, consistent with the known transition line, but it failed the
immediate confirmation pair. All confirmed-feature lists remained empty.

- [11:43 UTC strict 1.275–1.375 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1143Z_rx0_1275_1375MHz_v3_cluster3/validated_scan.png)
- [11:50 UTC strict 1.525–1.625 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1150Z_rx0_1525_1625MHz_v3_cluster3/validated_scan.png)
- [11:58 UTC isolated-peak rejection](../../artifacts/site_650_main/validated_scan_20260801T1158Z_rx0_1775_1875MHz_v3_cluster3/validated_scan.png)

For denser temporal coverage, the next monitoring block narrowed each strict
survey to ±10 MHz around the documented 1.325, 1.575, and 1.825 GHz beacon IFs
while retaining the same three-second settling and two-pair confirmation rule.
Three complete rotations from 12:07 through 12:21 UTC produced nine independent
16-point observations during 82–89° passes. Every observation had zero
individual matches and zero confirmed features. The focused protocol revisited
each beacon neighborhood about every 4.5 minutes without weakening the false-
promotion controls.

- [Focused 1.325 GHz observation](../../artifacts/site_650_main/validated_scan_20260801T1207Z_rx0_1315_1335MHz_v3_narrow/validated_scan.png)
- [Focused 1.575 GHz observation](../../artifacts/site_650_main/validated_scan_20260801T1208Z_rx0_1565_1585MHz_v3_narrow/validated_scan.png)
- [Focused 1.825 GHz observation](../../artifacts/site_650_main/validated_scan_20260801T1210Z_rx0_1815_1835MHz_v3_narrow/validated_scan.png)

A second three-rotation focused block from 12:25 through 12:42 UTC covered a
dense sequence of 82–90° passes, including 89.4, 89.9, and 89.3° culminations.
All nine 16-point observations again had zero individual matches and zero
confirmed features. Repeated strict nulls at the exact beacon neighborhoods
during three near-overhead passes strengthen the conclusion that the present
obstructed RF setup is not exposing a usable Starlink beacon observable.

- [12:25 UTC focused 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1225Z_rx0_1315_1335MHz_v3_narrow/validated_scan.png)
- [12:32 UTC focused 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1232Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [12:34 UTC focused 1.825 GHz near-overhead null](../../artifacts/site_650_main/validated_scan_20260801T1234Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)

The 12:44–12:52 UTC focused block aligned the three IFs with 89.5, 89.6, and
89.0° passes. All three primary visits had zero individual matches and zero
confirmed features. A second independent 1.825 GHz visit directly into the
89.0° culmination was also completely null. The repeated near-overhead nulls
are retained as evidence against insufficient survey timing at these IFs.

- [89.5° 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1244Z_rx0_1315_1335MHz_v3_narrow/validated_scan.png)
- [89.6° 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1246Z_rx0_1565_1585MHz_v3_narrow/validated_scan.png)
- [89.0° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1250Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)

Three further focused rotations from 12:53 through 13:09 UTC produced another
nine clean nulls. The block covered 82–84° passes, paired 87.5/87.2°
culminations, and an 88.2° event. Every 16-point observation had zero
individual matches and zero confirmed features. High-cadence revisits therefore
continue to reject a timing-gap explanation for the beacon-band nulls.

- [12:53 UTC focused 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1253Z_rx0_1315_1335MHz_v3_narrow/validated_scan.png)
- [13:02 UTC paired-87° 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1302Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [13:05 UTC 88.2° 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1305Z_rx0_1565_1585MHz_v3_narrow_repeat2/validated_scan.png)

Two additional pass-aligned blocks from 13:16 through 13:35 UTC contributed
eight promotion-grade observations, including immediate repeats at 1.825 GHz.
The first block covered 83–90° culminations; the second aligned the three IFs
with an 81.7/84.9° pair, an 86.9° pass, and a dense multi-azimuth group reaching
89.2/89.6°. All 128 repeated tuning centers had zero individual matches and
zero confirmed features. The products were copied to the Wi-Fi NFS archive and
all 16 JSON/plot files were re-read there with matching SHA-256 digests.

- [13:16 UTC 83–90° 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1316Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [13:22 UTC 89.9° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1322Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)
- [13:26 UTC paired-high-pass 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1326Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [13:28 UTC 86.9° 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1328Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [13:30 UTC 89.2/89.6° 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1330Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [13:33 UTC 83.1/87.1° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1333Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)

The following 13:37–13:46 UTC rotation was likewise null at all three IFs. It
covered 82–87.4° geometry, and the 1.825 GHz visits sampled multiple azimuth
sectors before an immediate repeat into an 87.0° culmination. All 64 repeated
tuning centers had zero individual matches and zero confirmed features. The
eight JSON/plot files were copied to NFS and independently hash-verified.

- [13:37 UTC 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1337Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [13:39 UTC 87.4° 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1339Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [13:41 UTC multi-azimuth 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1341Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [13:44 UTC 87.0° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1344Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)

The 13:47–13:55 UTC rotation targeted the strongest subsequent cluster. The
1.325 GHz scan spanned four 86.6–88.4° passes, the 1.575 GHz scan covered paired
89.3/89.5° culminations, and the two 1.825 GHz visits covered 87.5° and 86.5°
passes. Every one of the 64 repeated centers was null. All eight products were
then copied to NFS and hash-verified.

- [13:47 UTC four-pass 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1347Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [13:49 UTC paired-89° 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1349Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [13:51 UTC 87.5° 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1351Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [13:53 UTC 86.5° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1353Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)

The next 13:52–14:00 UTC rotation covered an 82.6–87.5° group at 1.325 GHz,
an 89.4° event at 1.575 GHz, and an 89.5° event followed by an 83.6–85.7°
multi-pass repeat at 1.825 GHz. All 64 repeated centers again had zero
individual matches and zero confirmed features. The eight products were copied
to NFS and re-read there with matching SHA-256 digests.

- [13:52 UTC 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1352Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [13:54 UTC 89.4° 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1354Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [13:56 UTC 89.5° 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1356Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [13:58 UTC multi-pass 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1358Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)

The 14:00–14:08 UTC rotation covered an 83.6–86.4° sequence at the lower IF,
then two high upper-IF passes culminating at 88.2° and 87.5°. All four strict
observations were null across 64 repeated tuning centers. Their eight products
were copied to NFS and independently hash-verified.

- [14:00 UTC 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1400Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [14:02 UTC 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1402Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [14:04 UTC 88.2° 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1404Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [14:06 UTC 87.5° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1406Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)

Two further rotations from 14:09 through 14:25 UTC targeted successively
stronger geometry. The first covered 89.3° at 1.325 GHz, a multi-azimuth
79–85.4° group at 1.575 GHz, and 85.8/86.0° upper-IF visits. The second covered
88.4/90.0/86.8° at 1.325 GHz, 87.6/89.9° at 1.575 GHz, and 88.1/89.6° at
1.825 GHz. All eight promotion-grade observations—128 repeated tuning
centers—had zero individual matches and zero confirmed features. All 16
products were copied to NFS and independently hash-verified.

- [14:09 UTC 89.3° 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1409Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [14:15 UTC 86.0° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1415Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)
- [14:17 UTC 90.0° cluster 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1417Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [14:19 UTC 89.9° 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1419Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [14:21 UTC 88.1° 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1421Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [14:23 UTC 89.6° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1423Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)

The 14:26–14:34 UTC rotation covered paired 83.9/81.9° lower-IF passes,
80.3° at the middle IF, and 87.0/86.5° upper-IF passes with an immediate
repeat. All 64 repeated tuning centers had zero individual matches and zero
confirmed features. The eight products were copied to NFS and hash-verified.

- [14:26 UTC paired-pass 1.325 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1426Z_rx0_1315_1335MHz_v3_narrow_repeat/validated_scan.png)
- [14:28 UTC 1.575 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1428Z_rx0_1565_1585MHz_v3_narrow_repeat/validated_scan.png)
- [14:30 UTC 87.0° 1.825 GHz null](../../artifacts/site_650_main/validated_scan_20260801T1430Z_rx0_1815_1835MHz_v3_narrow_repeat/validated_scan.png)
- [14:32 UTC 86.5° 1.825 GHz repeat null](../../artifacts/site_650_main/validated_scan_20260801T1432Z_rx0_1815_1835MHz_v3_narrow_repeat2/validated_scan.png)

## Detection gate

A Starlink Doppler claim requires all of the following, fixed before looking at
the TLE identity:

1. A visible moving feature in the stationary-suppressed waterfall.
2. At least 70% median support across the documented nine beacon teeth.
3. Correct 43.9495 kHz spacing must outperform wrong-spacing controls.
4. Correct-time SGP4 geometry must improve RMS by at least 2× over a linear null
   and every predeclared shifted-time/wrong-satellite control.
5. The result must exceed the no-LNB RX1 control and survive correction for all
   searched satellites.
6. A repeat pass must reproduce the result.

No trial passes these gates.

## Software architecture and tests

The modules remain separate Python packages and import one another directly:

- `leo-orbit`: frozen TLE retrieval, SGP4 passes, schedules, expected Doppler
- `leo-radio`: hardware preflight, capture, verification, waterfall, blind
  ridge, stationary suppression, and blind beacon-comb tracking
- `leo-gps`: strict checksummed NMEA parsing and quality-gated USB fix
- `leo-report`: measured-versus-expected overlays, controls, metrics, Markdown

Large IQ is memory mapped; waterfalls are preallocated and time-decimated, so
analysis is bounded on the 4 GB host. Atomic capture writes a hidden staging
directory, flushes and `fsync`s IQ, computes SHA-256, writes a read-only
manifest, and only then publishes the capture directory.

Current verification: **55 tests passed**. Tests include synthetic Doppler,
comb recovery amid a strong stationary spur, wrong/corrupt capture rejection,
memory-map behavior, GPS checksums/fix rejection, SGP4 geometry, pass boundaries,
CLI independence, deterministic fake-radio end-to-end operation, and jointly
atomic paired-channel publication with synchronized timestamps. The paired
qualifier is additionally tested on a weak drifting comb, pure noise, a
wrong-spacing comb, and persisted CLI rejection output.
The narrow-carrier tracker adds stationary/drifting-tone recovery, read-only
memory-map and bounded-output checks, CLI provenance, and diagnostic-plot tests.
The validated scanner adds fixed-absolute-RF acceptance and explicit rejection
of baseband-locked tuning spurs and noise, with JSON and plot checks.
Deterministic clock tests cover the per-tuning UTC provenance added after the
first transient validated candidate. Schema-v3 tests additionally cover
consecutive center confirmation and ordered repeat acquisition.

Only `/home/satpi01/.local/bin/uv` and the existing `.venv` were used. Native
`libiio` and `libxml2` live inside `.venv`; no system package installation or
venv recreation occurred.

## Reproduction

```bash
/home/satpi01/.local/bin/uv run --active --no-sync pytest -q

/home/satpi01/.local/bin/uv run --active --no-sync leo-radio verify \
  artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz

/home/satpi01/.local/bin/uv run --active --no-sync leo-radio comb \
  artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz \
  artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_analysis/moving_comb.json \
  --tone-spacing-hz 43949.5 --tone-count 9 --search-hz -400000 400000 \
  --max-drift-hz-s 6000 --plot \
  artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_analysis/moving_comb.png

/home/satpi01/.local/bin/uv run --active --no-sync leo-radio qualify-pair \
  artifacts/site_650_main/paired_20260801T0642Z_1575MHz_cycle3 \
  artifacts/site_650_main/paired_20260801T0642Z_1575MHz_cycle3/qualification.json \
  --skip-checksum --plot-dir \
  artifacts/site_650_main/paired_20260801T0642Z_1575MHz_cycle3/qualification_plots

/home/satpi01/.local/bin/uv run --active --no-sync leo-radio carrier \
  artifacts/site_650_main/paired_20260801T0736Z_1755p2857MHz_carrier_180s/rx0 \
  artifacts/site_650_main/paired_20260801T0736Z_1755p2857MHz_carrier_180s/rx0/carrier.json \
  --search-center-hz 1755185700 --search-span-hz 20000 \
  --integration-s 1 --fft-size 65536 --spectra-per-integration 16 \
  --skip-checksum --plot \
  artifacts/site_650_main/paired_20260801T0736Z_1755p2857MHz_carrier_180s/rx0/carrier.png

/home/satpi01/.local/bin/uv run --active --no-sync leo-radio validated-scan \
  artifacts/site_650_main/validated_scan_20260801T0807Z_rx0_1p0_2p2GHz \
  --start-hz 1000000000 --stop-hz 2200000000 --step-hz 5000000 \
  --validation-offset-hz 200000 --sample-rate-hz 4000000 \
  --bandwidth-hz 3000000 --samples-per-tuning 262144 --fft-size 16384 \
  --min-prominence-db 12 --frequency-tolerance-hz 2000 --channel 0 \
  --settle-seconds 3 --confirmations 2 \
  --uri pluto://ip:192.168.2.1

/home/satpi01/.local/bin/uv run --active --no-sync leo-report \
  --catalog artifacts/site_650_main/starlink_catalog.json --norad-id 65831 \
  --capture artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz \
  --ridge artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_analysis/moving_comb.json \
  --capture-start 2026-08-01T04:58:27.830510Z \
  --lat 37.849078 --lon -122.48562083333333 --alt-m 82 \
  --carrier-hz 11075000000 --detection-snr-db 3 \
  --minimum-tone-fraction 0.7 --fft-size 8192 --hop-size 131072 \
  --output-dir artifacts/site_650_main/powered_pass_20260801T0500Z_1325MHz_report_65831
```

Live IQ should remain on local storage to prevent Wi-Fi/NFS jitter. The mounted
`/mnt/qnap01/mouse9911/satellites` path is suitable for checksum-verified cold
archives and report copies. The 7.68 GB powered 1.575 GHz capture was copied to
that mount, re-hashed there, and matched its manifest SHA-256 exactly before the
local payload was replaced by a symlink to the verified archive. The 2.88 GB
powered 1.325 GHz capture was handled identically; its remote and manifest hash
both equal `1364438a5d327131bc31c242e003d43ab62c99556220d4df931feefc95de0605`.
The 4.61 GB 05:29 UTC paired capture was also copied and independently re-hashed
on the NFS mount. RX0 matched
`c1ac84bb0feb8ea39ad7a109a600bd0a51f2895569ce3f5760429d2ca31ac570`
and RX1 matched
`cfabf1315ca7b2b3652195660eea41acd3b01ad05cadefac49745f01a8f5f2b9`;
only then were the two local payloads replaced by archive symlinks.
The three 120-second paired captures at 1.325, 1.575, and 1.825 GHz were
subsequently archived the same way. All six remote IQ SHA-256 digests matched
their channel manifests before the 6.91 GB of local payloads were replaced by
exact NFS symlinks; manifests, qualification summaries, and plots remain local
as well as in the archive.
The 07:10 UTC 180-second pair, including both blind-ridge outputs, was also
copied and re-read on NFS. Its RX0 and RX1 digests matched their manifests at
`9c273a08724e825b022272b576bce90468d0e87ecb02c191b6ccc30647462e2a`
and `66c573ebc1bd36841e4875812c101a00bdd14f8bddcf95e891896ced3bcc2756`
before the 3.46 GB local payload was replaced by two archive symlinks.
The paired 07:21 and 07:23 UTC 60-second captures were likewise archived only
after all four NFS payload hashes matched their manifests. Their 2.30 GB of
local IQ was then replaced by exact archive symlinks.
The broad surveys and three 1.755 GHz carrier-investigation pairs were copied
with their radio and orbit analyses. All six remote IQ hashes matched their
manifests before 5.18 GB of local payload was replaced by archive symlinks.
The three rejected survey-retune pairs and the validated 1.00003 GHz reference
pair were archived identically; all eight channel payloads matched their
manifests before 6.91 GB of local IQ was replaced by exact NFS symlinks.
The dense scans and their 1.78468 GHz paired follow-up, including all-catalog
orbit controls, were also archived; both 1.15 GB IQ payloads matched their
manifests before replacement by exact NFS symlinks.
The later 1.52458 and 1.78478 GHz paired follow-ups, their timestamped scans,
radio controls, and orbit analysis were archived identically. All four 1.152 GB
NFS IQ payloads were re-read over the Wi-Fi mount and matched their manifest
SHA-256 digests exactly before the local duplicates were replaced by exact
archive symlinks. The operation recovered 4.61 GB of local acquisition space;
the verified payloads remain recoverable on NFS.
The 09:05 UTC 1.78478 GHz paired control and all subsequent settling,
fixed-center, transition-sequence, confirmation-control, and schema-v3 survey
artifacts were then archived. RX0 and RX1 payloads matched their manifests at
`1fb1c5396f68533dbfc95cf9df9bafe2b30b1572cc64f35d45ad2a3044d948f7`
and `213bc6efc4672508bac959d8273a7f4f6e1a02b6f00438cf5bf848bdf018c520`
before 2.30 GB of local IQ was replaced by exact NFS symlinks.
The overhead schema-v3 cycle and continuous 10:20 UTC 1.825 GHz pair were
archived next. The 180-second RX0 and RX1 payloads matched their manifests at
`24aa4233cb1e3c9138ae47124d28e64ffb2a76a544aa35eeb2e1cea8b4e2b1dd`
and `f0a16c6cb1d49aa14ba0f75bd04ca489a652eaf6fdae0cb06d8d0ce2cd249e43`
before 3.46 GB of local IQ was replaced by exact NFS symlinks.
The matched 10:32 UTC 1.575 GHz continuous pair was handled identically. RX0
and RX1 matched their manifests at
`97583e1127000aace0f90b1148a980f8249657395586e152e939ab356a8c7f7e`
and `849c4bf3efff5c948b7b2c3f0b6ad677245d1472ff6e4ae298d98f64b5066a31`
before another 3.46 GB of local IQ was replaced by exact NFS symlinks.
The final 10:43 UTC 1.325 GHz pair completed the matched set. RX0 and RX1
matched their manifests at
`257c64d04c7fab4db63f5e7e156db0473ff1bcb0a0e64c8ccf492b608679d3af`
and `9ae3f717fd181e0ec4b7df379d49d44ce29e41448219124c9aa7f36720de8c26`
before its 3.46 GB of local IQ was replaced by exact NFS symlinks.

## Next field step

The immediate limitation is RF visibility, not orbit software. For the next
session:

1. Move the LNB/feed to the clearest sky sector and record its azimuth,
   elevation, polarization, model, nominal LO, supply voltage, and whether a
   dish is present.
2. Keep the GPS antenna in its current fix-producing location.
3. Capture RX0 and RX1 simultaneously or in tightly interleaved blocks so the
   quiet channel is a contemporaneous false-alarm reference.
4. Survey 1.325, 1.575, and 1.825 GHz, then preselect the IF only if the true
   nine-tone spacing exceeds wrong spacings.
5. Require two repeat passes before attempting receiver-position estimation.

Until those gates pass, incorporating this feature into a position solver
would produce a precise-looking but unvalidated answer.
