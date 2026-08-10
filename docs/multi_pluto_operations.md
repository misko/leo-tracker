# Two-Pluto capture operations

This repository can run one capture watcher per Pluto+ while retaining one
shared NVMe tree and one offload exporter. Each Pluto contributes its two
synchronous AD9361 receive channels, for four labeled LNB inputs in total.
The checked-in files are deployment inputs only; no instance is enabled by the
repository change.

## Current hardware and compatibility status

The identities below were read on 2026-08-09. USB discovery showed the radios
on separate USB 2 host-controller buses, which avoids putting their aggregate
40 MB/s narrow traffic through one 480 Mb/s bus.

| Instance | Serial | Explicit IIO URI | Firmware | RX interface status |
|---|---|---|---|---|
| `pluto-new` | `1040005e0b100007100010000bf33a5d4d` | `pluto://usb:` resolved by serial | `v0.38-plutoplus-spf-gain-rssi-fingerprint-v2-8-gf53d` | Four CI16 I/Q scan elements exposed; repository QSPI and RAM lifecycle evidence exists |
| `pluto-old` | `10400056f695001322002d0010ad1719f2` | `pluto://usb:` resolved by serial | `v0.32-1-g7bdc-dirty` | Four CI16 I/Q scan elements exposed; no equivalent repository lifecycle/throughput acceptance yet |

The watcher uses the ordinary libiio/pyadi paired-RX path, not the custom
direct-USB transport, so the old firmware's exposed two-RX buffer layout is
structurally compatible. That is not yet a production acceptance result: do a
bounded capture and repeated open/stream/close test before leaving `pluto-old`
running. Do not flash either radio merely to deploy these service files.
The newer `fingerprint-v3` image currently cached on SATPI01 is documented in
the firmware repository as a RAM-tested candidate, not an approved persistent
QSPI release. It must not be written permanently to the older radio merely to
make firmware versions match; standard dual-RX IIO capture already works.

USB device addresses can change after a disconnect. Each instance scans libiio
USB contexts and resolves its transient bus URI from the configured stable
serial before opening the radio. The preflight requires exactly one match, and
the capture source independently checks the opened serial; a missing, duplicate,
or wrong radio therefore fails instead of silently recording the other unit.

## Configuration invariants

`leo-tracker-beacon-watch@.service` loads
`/etc/leo-tracker/beacon-watch@<instance>.env`. Its preflight refuses an IP or
non-USB URI, a missing serial/radio ID, duplicate receiver labels, a
private storage root, local analysis, or non-preserving retention.

Both instances intentionally write to `/mnt/leo-nvme/leo-tracker`. Unique
`LEO_BEACON_RADIO_ID` values make capture directories, hop sessions, reports,
and queue markers disjoint. All four `LEO_BEACON_RECEIVER_LABELS` are unique so
downstream diagnostics can identify the physical input. There must still be
exactly one `leo-tracker-analysis-export.service`; it is the sole consumer of
the shared `staging/analysis-queue` and the sole producer of Kalman jobs.

The template conflicts with the legacy `leo-tracker-beacon-watch.service` to
prevent an unlabelled third watcher. Never start the template instances while
the legacy unit is active.

## Storage and rate budget

At 2.5 MS/s, one dual-RX native-CI16 watcher writes about 20 MB/s (72 GB/hour);
two write about 40 MB/s (144 GB/hour) before verified reclamation. A concurrent
pair of 10 MS/s wide dwells would peak near 160 MB/s. On 2026-08-09 the 916 GB
NVMe had 778 GB free and QNAP had 745 GB free, so preserved narrow IQ alone
could consume the then-free NVMe space in roughly 5.4 hours if export and
verified reclamation stopped.

The examples raise the acquisition floor from 150 to 200 GB. The old radio's
initial profile disables hop, oversample, and wide bursts to avoid coincident
high-rate writes while it is being accepted. This does not solve sustained
capacity: watch NVMe free space, shared queue depth, exporter receipts, and
QNAP capacity. Both watchers pause independently at the floor, so keep enough
headroom for two already-running dwells and use the verified reclaimer as the
only local deletion authority.

## Staged rollout (operator-run only)

1. Stop and disable the legacy single-radio watcher. Do not change the one
   exporter.
2. Copy the two example files into `/etc/leo-tracker/`, removing `.example`,
   with root ownership and mode `0640`. Re-run discovery and verify both serials.
3. Install the template in `/etc/systemd/system/`, run `systemctl daemon-reload`,
   and use `systemd-analyze verify` before starting anything.
4. Start `leo-tracker-beacon-watch@pluto-new.service` alone and confirm its log
   announces the expected ID, serial, URI, and `lnb-a`/`lnb-b` labels.
5. Stop that bounded check. Validate the old radio with a short dual-RX capture
   plus repeated lifecycle test; inspect the manifest identity and sample count.
6. Start both instances only after the old radio passes. Confirm two distinct
   radio IDs in capture names and manifests, four receiver labels, one exporter,
   queue progress, temperatures, USB errors, and free-space trend.

Rollback is to stop the two instances. Their complete and interrupted artifacts
remain in the shared immutable tree; do not manually delete them. Re-enable the
legacy service only after both instances are stopped, since it lacks a required
radio ID and explicit serial.
