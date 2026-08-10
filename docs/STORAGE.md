# LEO Tracker storage and preservation

This document is the authoritative storage contract for the Starlink capture
experiment. Paths embedded in historical artifacts are provenance, not an
instruction to delete or relocate their source.

## Safety invariants

1. `/mnt/leo-nvme/leo-tracker` is the immutable full-IQ source during evidence
   archive development.
2. Evidence tooling may read the source but must never rename, truncate,
   overwrite, or delete below it.
3. QNAP publication is atomic: data is written below a stable `.partial`
   directory, read back, checksummed, and then renamed into place.
4. A source is not replaceable merely because analysis completed. A valid
   evidence receipt proves exact clips were archived; deletion remains a
   separate future policy decision.
5. Both receivers, original ci16 samples, sample rate, absolute sample indexes,
   radio configuration, UTC mapping and detector provenance are preserved.
6. When preservation mode reaches its storage floor, acquisition waits. It
   does not reclaim raw IQ.

## Hosts and paths

| Path | Writer | Purpose | Durability |
|---|---|---|---|
| `/mnt/leo-nvme/leo-tracker/captures` | SATPI01 | Complete dual-RX raw IQ | Immutable source |
| `/mnt/leo-nvme/leo-tracker/hop-sessions` | SATPI01 | Retuned channel surveys | Immutable source |
| `/mnt/leo-nvme/leo-tracker/quarantine` | SATPI01 | Interrupted/irregular IQ | Preserve for review |
| `/mnt/leo-nvme/leo-tracker/reports` | SATPI01 | Detector, decode, track and association outputs | Regenerable but retained |
| `/home/satpi01/leo-tracker/artifacts` | SATPI01 | Legacy surveys and waterfall experiments | Historical source |
| `/mnt/qnap01/mouse9911/leo` | SATPI01/Kalman | Existing full-IQ analysis exchange | Operational; do not bulk-delete |
| `/mnt/qnap01/mouse9911/leo-cropped` | Kalman evidence archiver | Cropped archive described below | Verified primary archive; not a second backup |

The QNAP NFS client is mounted with `soft` semantics. A successful copy command
is therefore insufficient evidence of durability. The archive verifier opens
and hashes the destination again.

## Cropped QNAP archive

```text
leo-cropped/
├── README.md
├── evidence-v2/<recording-id>/
│   ├── manifest.json             clip boundaries, radio parameters and hashes
│   ├── source-manifest.json      byte-exact original capture manifest
│   ├── clip-000.ci16             original dual-RX ci16 sample layout
│   ├── ...
│   └── verification.json         destination and source-equality checks
├── catalog/v2/plans/<id>.json       why every interval was selected
├── catalog/v2/references/<id>.json  conservative replay reference
├── catalog/v2/comparisons/<id>.json required-event coverage result
├── catalog/v2/receipts/<id>.json    final archive transaction receipt
└── staging/                      locks and resumable unpublished work
```

An evidence plan includes all known exact, decoded, continuous-track and
broadband/window candidates plus interesting dense-followup checks. Signal
intervals receive tier-sized guards and merge when they overlap. Deterministic
controls preserve a bounded noise/calibration sample even for strict negatives.

The first archive revision performs lossless time cropping only. It does not
filter, downconvert, resample or compress IQ.

Cropping reduces storage only when selected intervals plus guards cover less
than the source. A short two- or five-second hop child can legitimately retain
100% of its IQ because the default ten-second evidence guard covers the whole
recording. This is not an archive failure. Longer negative and sparse-event
captures provide most of the measured reduction.

The live Kalman service uses `LEO_ANALYSIS_ARCHIVE_MODE=shadow` and
`LEO_ANALYSIS_RETENTION_MODE=disabled`. It attempts one archive transaction per
job, but an archive failure does not delete QNAP raw IQ. Acquisition-host
duplicates are governed separately by the verified local reclaimer below.

## Preservation mode

The acquisition service uses this systemd drop-in:

```ini
[Service]
Environment=LEO_BEACON_PRESERVE_RAW=1
Environment=LEO_BEACON_MINIMUM_FREE_GB=150
Environment=LEO_BEACON_ANALYSIS_MODE=offload
```

In this mode the local retention command is skipped. Capture waits when free
NVMe space is below 150 GB. The analysis exporter must use:

```bash
LEO_OFFLOAD_SOURCE_POLICY=retain
```

`retain` is required: it verifies and queues the QNAP work copy but leaves the
NVMe directory unchanged until Kalman has completed. `delete` is the legacy
move behavior and must not be used because it runs before analysis succeeds.
In `offload` mode the exporter is the only local analysis-queue consumer;
SATPI01 must not run the legacy local DSP workers alongside it.

## Verified local reclamation

`leo-tracker-local-reclaimer.service` removes only the redundant SATPI01 copy.
It does not remove the complete QNAP recording or any cropped evidence. A local
recording is eligible only when all of the following are true:

1. its manifest is complete and at least five minutes old;
2. no local/export/Kalman job or partial transfer owns the recording;
3. the QNAP manifest is byte-identical to the local manifest;
4. every QNAP chunk has the manifest-declared size (optional full SHA-256 mode
   additionally re-reads every shared chunk);
5. the successful analysis receipt names the same recording and its analysis
   output still exists with the recorded size; and
6. the local path resolves to exactly one ordinary capture or hop child below
   the configured NVMe root and is not a symlink.

Before deletion the reclaimer atomically writes a `prepared` receipt beneath
`reports/reclamation/local/`; after deletion it replaces that receipt with
`status: removed`, the exact manifest hash and reclaimed byte count. Repeated
runs are idempotent. Incomplete, missing, corrupt, active and ambiguous sources
are deferred rather than repaired or removed.

Dry-run and bounded application:

```bash
uv run --active --no-sync leo-radio starlink-storage-reconcile \
  /mnt/leo-nvme/leo-tracker /mnt/qnap01/mouse9911/leo \
  --output /tmp/local-reclamation-plan.json

uv run --active --no-sync leo-radio starlink-storage-reconcile \
  /mnt/leo-nvme/leo-tracker /mnt/qnap01/mouse9911/leo \
  --apply --limit 10
```

The watcher remains in `LEO_BEACON_PRESERVE_RAW=1` so its older ring-retention
logic stays disabled. The separate reclaimer is the sole authority for local
deletion and capture still stops at the configured free-space floor if shared
verification falls behind.

## Automatic QNAP raw-IQ lifecycle

`leo-radio starlink-qnap-lifecycle` inventories raw captures and removes raw IQ
older than six hours only after a production tiered-v2 bundle is byte-verified
against its source and passes the required-event replay gate. Raw is a bounded
working set; the v2 clips and structured reports are the durable record.

| Tier | Name | Evidence | Default deletion status |
|---:|---|---|---|
| 0 | `strict_negative` | No exact/single-RX candidate, trigger, confirmed beacon, or track | Raw eligible after verified v2 |
| 1 | `weak_candidate` | Candidate or trigger without confirmed temporal evidence | Raw eligible after verified v2 |
| 2 | `tracked_signal` | Qualified detector track, ≥5-second track, or ≥20 dual-RX epochs | Raw eligible after verified v2 |
| 3 | `confirmed_beacon` | Temporally confirmed beacon | Raw eligible after verified v2 |
| 4 | `qualified_identity` | Qualified held-out TLE association | Raw eligible after verified v2 |
| 5 | `manual_pin` | Operator pin | Never deletable by this policy |

Tier is evaluated from the current authoritative reports every time a plan is
built and immediately before deletion. Creating
`reports/retention/pins/<recording-id>.json` promotes any recording to tier 5.
Pinning is fail-safe and does not require a special schema; the JSON should
record the operator, timestamp and reason for auditability.

Raw deletion eligibility additionally requires:

1. a complete/interrupted manifest with checksummed chunks;
2. a successful Kalman completion receipt;
3. a production v2 receipt with `status: verified`, `source_verified: true`,
   `policy: tiered-v2`, and `required_event_replay_valid: true`;
4. exact agreement between the current QNAP manifest hash and the archive's
   source-manifest hash;
5. a valid, hashed evidence-bundle manifest;
6. the configured minimum raw age; and
7. an exact, non-symlink capture directory below the QNAP capture root.

The equivalent dry-run plan is:

```bash
uv run --active --no-sync leo-radio starlink-qnap-lifecycle \
  /mnt/qnap01/mouse9911/leo /mnt/qnap01/mouse9911/leo-cropped \
  --minimum-age-hours 6 --maximum-tier 4 \
  --output /mnt/qnap01/mouse9911/leo/reports/retention/qnap-lifecycle.latest.json
```

The production service uses `--ignore-pressure`: age-eligible verified-v2 raw
leaves even when QNAP has free space, so the working set cannot silently grow.
Application still requires the literal confirmation token:

```bash
uv run --active --no-sync leo-radio starlink-qnap-lifecycle \
  /mnt/qnap01/mouse9911/leo /mnt/qnap01/mouse9911/leo-cropped \
  --minimum-age-hours 6 --maximum-tier 4 --ignore-pressure \
  --apply --confirm DELETE-QNAP-RAW-IQ
```

Application deletes only `captures/<recording-id>` raw IQ. Evidence clips,
analysis reports, plots, decoded symbols, TLE context, completion receipts and
fingerprints remain. A prepared/removed transaction receipt is written beneath
`reports/reclamation/qnap/`. The global QNAP reclaimer lock prevents concurrent
applications.

[`leo-tracker-qnap-lifecycle.service`](../deploy/leo-tracker-qnap-lifecycle.service)
is checked in with a six-hour minimum age, tier 4 maximum and a one-minute
post-pass delay. Tier 5 manual pins and unclassified, active, incomplete, stale,
or unverified recordings remain fail-safe protected.

## Production evidence archive v2

Kalman now publishes only production tiered-v2 bundles for new recordings. It
does not create a v1 bundle or a same-volume `derived/` copy of reports. The v2
policy differs from v1 in two important ways:

- non-triggering checks from a dense confirmed follow-up are not treated as
  signal spans; and
- guards and deterministic controls are sized by evidence tier, from one
  100 ms control for a strict negative to two 500 ms controls plus two-second
  guards for tracked, confirmed, identified or pinned evidence.

Every plan records detector-required event intervals. A v2 bundle publishes only if
it fully contains every event in a freshly regenerated conservative reference
plan and every extracted byte matches the corresponding source sample. Kalman
writes production artifacts to:

```text
/mnt/qnap01/mouse9911/leo-cropped/catalog/v2/references/
/mnt/qnap01/mouse9911/leo-cropped/catalog/v2/plans/
/mnt/qnap01/mouse9911/leo-cropped/catalog/v2/comparisons/
/mnt/qnap01/mouse9911/leo-cropped/catalog/v2/receipts/
/mnt/qnap01/mouse9911/leo-cropped/evidence-v2/
```

Historical convergence is transactional and bounded. Each successful recording
gets v2 evidence, a final source comparison and a prepared receipt; its obsolete
v1 bundle and verified report duplicates are retired before raw is removed last:

```bash
uv run --active --no-sync leo-radio starlink-storage-regime-v2 \
  /mnt/qnap01/mouse9911/leo /mnt/qnap01/mouse9911/leo-cropped \
  --minimum-age-hours 6 --limit 2 \
  --apply --confirm MIGRATE-TO-EVIDENCE-V2
```

[`leo-tracker-storage-regime-v2.service`](../deploy/leo-tracker-storage-regime-v2.service)
runs this on Kalman in batches of two with the repository's existing uv virtual
environment. A failure preserves raw and v1 and is retried; manual pins are
recropped to v2 but their raw remains. Archive-only history whose raw was
already reclaimed is recropped transitively from source-verified v1 clips; any
gap covering a required v2 interval fails closed. Completed transaction
receipts live beneath `reports/reclamation/storage-regime-v2/`.

SATPI01 also runs
[`leo-tracker-storage-regime-v2-fallback.service`](../deploy/systemd/leo-tracker-storage-regime-v2-fallback.service)
as a persistent low-priority fallback. It processes four transactions with two
independent deletion-last workers per bounded plan, followed by a ten-second
idle interval. Kalman migration, the Pi fallback, and the six-hour QNAP raw
lifecycle acquire one global QNAP storage lock before inventory as well as
mutation. A competing policy defers before walking the NFS archive and retries
on its normal interval. This survives Pi reboots without allowing two policies
to inventory or mutate the archive concurrently. Current workers also honor
the former `qnap.lock` and `storage-regime-v2.lock` names, preserving exclusion
while Kalman or the Pi rolls forward from an older commit.
Its automatic scope scans only raw while an eligible raw backlog exists, then
switches to archive-only v1 compaction. This avoids walking the multi-terabyte
legacy archive before every urgent raw transaction. Each operational plan also
stops after a small batch of eligible records (32 on the Pi, 64 on Kalman), so
one transaction does not require a complete multi-thousand-record inventory.
Unbounded CLI dry runs remain available for authoritative capacity audits.
Kalman remains the high-throughput worker.

Analysis completion records no longer copy immutable outputs beneath
`reports/runs/.../outputs/`; they store hashes and authoritative references to
the ordinary report tree. Historical `reports/runs/.../outputs/` and
`leo-cropped/derived` files are normalized during migration: a hash-verified
artifact is promoted into the current report tree when that authoritative path
is missing, while an obsolete archive copy is removed when a current live
artifact exists. Hash-invalid legacy files fail safe and are listed in the
migration receipt. Interrupted v1 `<recording>.partial` bundles are also retired
only after that recording's production-v2 source and replay gates pass.

Inspect the live settings:

```bash
systemctl show leo-tracker-beacon-watch.service -p Environment
systemctl show leo-tracker-analysis-export.service -p Environment
```

The exporter is installed as the persistent
`leo-tracker-analysis-export.service`. It starts after the NVMe mount and
network are available, resumes stable `.partial` transfers, and restarts after
host reboots. Do not replace it with a transient `systemd-run` unit: transient
units are not enabled across boots.
It also reconciles complete/interrupted NVMe recordings against QNAP every ten
minutes, so a capture committed immediately before a crash cannot remain
stranded merely because its queue marker was never published.

## Evidence CLI

Every command uses the repository's existing `.venv` through `uv`.

```bash
# Review the proposed intervals before copying IQ.
uv run --active --no-sync leo-radio starlink-evidence-plan \
  /mnt/leo-nvme/leo-tracker/captures/RECORDING \
  /mnt/leo-nvme/leo-tracker/reports /tmp/RECORDING.plan.json

# Extract exact samples and atomically publish a bundle.
uv run --active --no-sync leo-radio starlink-evidence-extract \
  /mnt/leo-nvme/leo-tracker/captures/RECORDING \
  /tmp/RECORDING.plan.json \
  /mnt/qnap01/mouse9911/leo-cropped/evidence-v2/RECORDING

# Prove QNAP bytes equal the original source slice.
uv run --active --no-sync leo-radio starlink-evidence-verify \
  /mnt/qnap01/mouse9911/leo-cropped/evidence-v2/RECORDING \
  --source /mnt/leo-nvme/leo-tracker/captures/RECORDING

# Materialize one clip as an ordinary BeaconCapture for existing analyzers.
uv run --active --no-sync leo-radio starlink-evidence-materialize \
  /mnt/qnap01/mouse9911/leo-cropped/evidence-v2/RECORDING clip-000 \
  /mnt/qnap01/mouse9911/leo-cropped/staging/replay-RECORDING

# Plan, extract, verify, copy derivatives and write a receipt in one operation.
scripts/starlink-evidence-archive.sh --recording RECORDING \
  /mnt/leo-nvme/leo-tracker /mnt/qnap01/mouse9911/leo-cropped

# Audit production-v2 bundles against sources that are still present.
uv run --active --no-sync leo-radio starlink-evidence-audit \
  /mnt/leo-nvme/leo-tracker \
  /mnt/qnap01/mouse9911/leo-cropped/evidence-v2 \
  --output /mnt/qnap01/mouse9911/leo-cropped/catalog/v2/audit.json
```

The archive wrapper defaults to one finite scan. `--watch` enables periodic
processing, `--limit N` bounds a development run, and `--recording ID` selects
one reviewed recording. It skips active captures, recordings without both
analysis and follow-up artifacts, and captures younger than ten minutes unless
explicitly selected.

## Completeness audit

Archive completeness is defined by verified receipts, not by directory count,
analysis queue depth, or aggregate bytes. For every recording in scope:

1. `catalog/v2/receipts/<id>.json` exists;
2. it has `status: verified`, `source_verified: true`, and
   `required_event_replay_valid: true`;
3. `evidence-v2/<id>/verification.json` has `valid: true`;
4. no `<id>.partial` directory remains;
5. all source identifiers from both NVMe and the QNAP working set have been
   reconciled, including quarantine and intentionally excluded records.

The storage-layout convergence gate additionally requires that no age-eligible
raw (except a current manual pin), v1 evidence/catalog, shadow catalog,
same-volume `derived/` duplicate, or physical `runs/.../outputs/` artifact
remains. Run it only when no mutator owns the shared QNAP lock; the CLI acquires
that lock for a consistent inventory and exits nonzero until every category is
zero:

```bash
uv run --active --no-sync leo-radio starlink-storage-audit-v2 \
  /mnt/qnap01/mouse9911/leo /mnt/qnap01/mouse9911/leo-cropped \
  --minimum-age-hours 6 \
  --output /mnt/qnap01/mouse9911/leo/reports/retention/storage-v2-audit.json
```

Three populations are permanently outside "archivable" and must be counted
separately rather than left as an open gap:

- recordings whose raw is gone from both stores with no receipt. The bounded
  raw-IQ ring discarded them before the cropped archive existed; their derived
  analysis, follow-up, decode and track artifacts survive, but replay against
  raw is impossible. As of 2026-08-08 this is 3,402 recordings, 518 of them
  confirmed detections;
- quarantined captures that wrote no chunks at all;
- anything a review explicitly excludes, with the reason recorded.

Quarantined captures that *did* write chunks are not in this list. They stop
early but hold a checksummed contiguous prefix, so they are ordinary
observations and flow through the normal pipeline.

Use the cross-store identifier comparison in
[`KALMAN_MIGRATION.md`](KALMAN_MIGRATION.md), then validate published bytes:

```bash
uv run --active --no-sync leo-radio starlink-evidence-audit \
  /mnt/leo-nvme/leo-tracker \
  /mnt/qnap01/mouse9911/leo-cropped/evidence-v2 \
  --output /mnt/qnap01/mouse9911/leo-cropped/catalog/v2/audit.json
```

That command can verify only sources visible below the supplied source root.
Run an equivalent audit from the QNAP analysis root for recordings whose
authoritative source is there, or restore/materialize the source before calling
the archive globally complete.

## Artifact authority

| Artifact | Meaning |
|---|---|
| Source `manifest.json` and chunk hashes | Authority for complete raw IQ |
| Evidence plan | Selection policy and detector provenance |
| Evidence bundle manifest | Exact source sample ranges and clip hashes |
| Verification report | Destination read-back and source byte equality |
| Archive receipt | Completed bundle plus copied derived artifact hashes |
| Analysis JSON/plots | Scientific interpretation; regenerable from suitable IQ |
| TLE context bundle | Orbit input needed for retrospective association |

Only a receipt with `status: verified` and `source_verified: true` represents a
completed evidence transaction. It still does not authorize source deletion.

## Replay equivalence and the deletion rule

Measured 2026-08-08 against `ch4-lower-edge-narrow-20260807T011325Z`, by
materializing `clip-000` and rerunning the deployed analysis:

| | |
|---|---|
| checks recovered | 46 of 46 |
| max abs epoch difference | 0 samples |
| max abs match-margin difference | 7.8e-08 |
| max abs CFO difference | 8.1e-04 Hz |
| candidate checks | 18 original, 18 replayed |

Cropping is lossless time selection over original ci16 bytes, so **fidelity
inside a retained clip is exact** and is not the risk. **Coverage is.** A plan
selects intervals using the detector available when it ran, and a later, better
detector reports events in intervals that plan already discarded. Across all
confirmed recordings, 7,382 of 7,486 high-value events fell inside a retained
clip; the 104 that did not belong to 5 recordings whose analysis was newer than
their crop plan.

A source is therefore safe to delete only when, in addition to a verified
receipt, every candidate or qualified check in the *current* follow-up lies
inside a retained clip. Comparing against the plan's own view is circular: it
cannot see events found after it ran. Re-archiving a recording whose detector
has since improved restores coverage; deleting it first makes the loss
permanent.

## Failure and recovery

- An interrupted extraction leaves `<recording-id>.partial`; rerunning the same
  plan validates completed clips and resumes it.
- A published bundle with a changed or corrupt clip fails verification and is
  never silently overwritten.
- An existing derived artifact is reused only when its SHA-256 matches.
- A same-name artifact with different contents stops publication as a
  collision.
- If QNAP is unavailable, local capture remains authoritative.
- If NVMe reaches the preservation floor, capture waits while the verified
  local reclaimer catches up; unresolved sources still require operator review.

No recovery procedure deletes QNAP raw IQ. Local deletion is performed only by
the explicitly deployed, receipt-driven reclaimer.

## Capacity and future decisions

Raw dual-RX IQ is produced faster than the current NVMe and QNAP can retain
indefinitely. During this evaluation we preserve local data and use a hard
free-space floor. Before enabling any cleanup we must review measured clip
coverage, replay equivalence, storage reduction, restored-recording tests and
the set of permanently pinned scientific sources.

QNAP is shared primary storage, not an independent backup. Permanent evidence
should ultimately have a snapshot or second replicated copy.
