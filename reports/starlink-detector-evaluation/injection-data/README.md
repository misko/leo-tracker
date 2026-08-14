# Injection records

Raw records behind sections 11, 13 and 14 — the only measurements in this
report taken against a **known** input. Everything else rests on the sky corpus,
where the truth is unknown by construction.

## Rig

Two ADALM-Pluto Rev.C, firmware `v0.38-plutoplus-spf-libiio-metadata-v5`, each
wired identically and independently:

```
TX2 -> SMA splitter -> 2x 30 dB attenuator -> RX1 and RX2
```

A closed path. No antenna, no LNB, no RF connection between the two radios.

| | serial | address |
|---|---|---|
| one-radio, two-radio A | `104000bac4950008230026001b440a003a` | `ip:192.168.1.183` |
| radio-165, two-radio B | `1040007c4a94000211000b009186843ef2` | `ip:192.168.1.165` |

Channel map, verified on both: `ad9361-phy` TX1=`voltage0` TX2=`voltage1`
RX1=`voltage0` RX2=`voltage1`; TX DMA `cf-ad9361-dds-core-lpc` TX1=`voltage0/1`
TX2=`voltage2/3`; RX DMA `cf-ad9361-lpc` RX1=`voltage0/1` RX2=`voltage2/3`.
Driving TX1's DMA while the cable is on TX2 returns the noise floor with no
error — that trap cost three attempts across this work.

## Conditions

| | |
|---|---|
| waveform | `leo_tracker.radio.beacon.pilots.edge_pilot_frame`, the repository's own pilot frame — not a tone |
| LO | 1,190,312,500 Hz on both TX and RX |
| sample rate | 5 MS/s |
| probe | 20 ms |
| RX gain | `manual`, 40 dB |
| digital drive | 0.3048 full scale on `.183` (0.9 FS came out 9.4 dB hot; this figure is not recoverable from the analog settings) |
| `.165` | 8.2 dB more cable loss than `.183` at identical settings |
| thresholds | 1% per candidate point, drawn on genuinely empty input (TX at −89.75 dB, verified indistinguishable from a dark DAC) |
| carrier offset | ~0.07 Hz natural — TX and RX share one reference. Offsets in E4/T3 are **imposed** on the waveform |

## Contents

| Directory | Records |
|---|---|
| `one-radio/` | `e2_roc` SNR ladder, `e3_off` / `e3_dark` / `e3b_nullarm` false alarm, `e4_offset` imposed-offset sweep, `e5_occupancy` known-`f` runs |
| `radio-165/` | `t2_scores` false alarm, `t3_cliff` imposed-offset sweep, topology probe |
| `two-radio/` | `runs.tar.gz` — per-instant records for the synchronised dual-rig occupancy schedule |

`.jsonl.gz` files are one JSON record per line, gunzip and read directly. The
`.py` files beside them are the harnesses that produced the records and the
analysis helpers the figure scripts import.

## What these cannot tell you

No LNB, no antenna, no sky. They test the detectors and the digital pipeline,
which is what was in question. They say nothing about the LNB chain, the water
on the `lnb-c` / `lnb-d` bias tees, real interference, or the −150 kHz
common-mode offset in section 12 — and because transmit and receive share one
oscillator, nothing about LO drift either.
