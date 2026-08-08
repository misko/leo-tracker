# Full Kalman analysis migration

Kalman is the authoritative analysis host; SATPI01 remains the acquisition and
immutable-source host during migration. The storage safety rules in
[`STORAGE.md`](STORAGE.md) remain mandatory.

## Pipeline contract

Every completed capture is copied atomically to
`/mnt/qnap01/mouse9911/leo/captures`, paired with an immutable context bundle,
and queued exactly once. Sixteen Kalman workers claim jobs atomically. Each
worker runs full-duration coarse beacon analysis and Doppler-window discovery,
fine follow-up over every permissive check, decoding wherever checks exist,
continuous tracking for every fixed-band recording, TLE association, and a
cropped evidence transaction.

Completion receipts are written both to the legacy receipt directory and to:

```text
reports/runs/<pipeline-id>/<recording-id>/completion.json
```

Receipts contain hashes for every required output. Different pipeline
identities therefore coexist without silently replacing scientific history.

## Safety modes

`LEO_ANALYSIS_ARCHIVE_MODE` supports:

- `off`: analysis only;
- `shadow`: attempt evidence archival but do not fail analysis if QNAP archive
  publication is unavailable;
- `required`: a source-verified archive receipt is required for job success.

`LEO_ANALYSIS_RETENTION_MODE` defaults to `disabled`. `verified` permits the
existing QNAP working-copy retention pass only after the recording has an
archive receipt. Neither setting authorizes deletion of SATPI01 NVMe sources.

The deployed service uses `shadow` plus `disabled`. Cropped publication is
working, but the historical archive is not yet complete. Do not promote either
mode based only on the live queue reaching zero: queue completion and archive
coverage are separate properties.

## Install on Kalman

From `/home/mouse9911/gits/leo-tracker`:

```bash
git pull --ff-only origin main
sudo systemctl link "$PWD/deploy/leo-tracker-analysis-server.service"
sudo systemctl daemon-reload
sudo systemctl enable --now leo-tracker-analysis-server.service
```

The existing `.venv` is required and every invocation uses `uv --active
--no-sync`; the production service must not create or silently replace an
environment at startup.
The service uses `/home/mouse9911/.local/bin/uv` explicitly because systemd
does not inherit the interactive shell's user-local `PATH`.

Graceful updates:

```bash
sudo systemctl stop leo-tracker-analysis-server.service
git pull --ff-only origin main
sudo systemctl start leo-tracker-analysis-server.service
systemctl status leo-tracker-analysis-server.service --no-pager -l
```

A normal `systemctl stop` sends `SIGTERM`; the server converts it into a drain
request and gives claimed jobs up to 30 minutes to finish.
`starlink-analysis-server.sh --drain` is useful for a manually launched server,
but the systemd unit uses `Restart=on-failure`: a clean drain exits successfully
and therefore remains stopped until explicitly started. On startup, the server
audits all completion records before emitting `server_start` or launching
workers. On the present history this can take several minutes; the held
`staging/analysis-queue/server.lock` distinguishes an audit in progress from a
dead service.

## Wide acquisition span

`LEO_ANALYSIS_WIDE_ACQUISITION_SPAN_HZ` is bounded by the recording, not by the
LNB uncertainty one would like to search. Wide captures are taken at 10 MS/s and
analyzed in 2.5 MHz subbands, so digital tuning cannot exceed +-3.75 MHz without
leaving the sampled band; the deployed value is 3.5 MHz, matching the
acquisition watcher. An over-wide request is now clamped to the usable span and
recorded as `requested_span_hz` plus `span_clamped_to_sampled_bandwidth` in the
acquisition block, rather than failing the `acquire` stage. Raising the capture
sample rate is the only way to genuinely widen this search.

The continuous tracker treats a valid frame artifact with no compatible pair
of dual-receiver navigation epochs as a successful negative result. Its track
artifact has `track_count: 0` and
`no_track_reason: no_grouped_dual_rx_observations`; TLE association and archive
publication still run. Malformed inputs, checksum failures, and missing
required artifacts remain job failures.

## Historical backfill

Queue complete IQ captures already present on QNAP, oldest first:

```bash
LEO_ANALYSIS_PIPELINE_ID=kalman-full-v1 \
  ./scripts/starlink-analysis-backfill.sh --dry-run
LEO_ANALYSIS_PIPELINE_ID=kalman-full-v1 \
  ./scripts/starlink-analysis-backfill.sh --limit 100
```

The command skips active jobs and recordings that already have a completion
receipt for the selected pipeline. Publication of each job marker is atomic,
and rerunning the command is safe. Live jobs and backfill use the same queue;
enqueue backfill in bounded batches so new observations retain priority.

After Kalman shadow processing is healthy, SATPI01 can queue preserved captures
that are no longer present in the QNAP working set:

```bash
./scripts/starlink-export-backfill.sh --dry-run --limit 10
./scripts/starlink-export-backfill.sh --limit 10
```

The normal copy-only exporter performs the transfer and leaves the NVMe source
untouched. Use small batches until queue age and QNAP capacity demonstrate that
live capture is unaffected.

## Archive completeness

Three counts must not be conflated:

- a Kalman completion receipt proves the configured analysis outputs exist;
- an evidence bundle is only a published directory;
- a cropped archive receipt with `status: verified` and
  `source_verified: true` proves the destination bytes match source slices.

The archive is complete only when every source recording intended for
preservation has a verified cropped receipt and the archive audit reports no
invalid or partial bundle. Compare recording identifiers across both current
source stores because the QNAP working set is not the entire NVMe history:

```bash
find /mnt/leo-nvme/leo-tracker/captures \
     /mnt/leo-nvme/leo-tracker/hop-sessions \
     /mnt/leo-nvme/leo-tracker/quarantine \
     -type f -name manifest.json -printf '%h\n' |
  xargs -r -n1 basename | sort -u > /tmp/leo-nvme-recordings
find /mnt/qnap01/mouse9911/leo/captures -mindepth 1 -maxdepth 1 -type d \
  -printf '%f\n' | sort -u > /tmp/leo-qnap-recordings
sort -u /tmp/leo-nvme-recordings /tmp/leo-qnap-recordings \
  > /tmp/leo-present-recordings
find /mnt/qnap01/mouse9911/leo-cropped/catalog/receipts -maxdepth 1 \
  -type f -name '*.json' -printf '%f\n' | sed 's/\.json$//' | sort -u \
  > /tmp/leo-cropped-receipts
comm -23 /tmp/leo-present-recordings /tmp/leo-cropped-receipts
```

Any output from the final `comm` is a recording still needing analysis/export
or cropped publication. Queue QNAP-resident work with
`starlink-analysis-backfill.sh` and NVMe-only work with
`starlink-export-backfill.sh`, using bounded batches as described above. Then
run the evidence audit in [`STORAGE.md`](STORAGE.md). Never delete a source just
because it disappeared from this comparison; first determine whether it moved,
was quarantined, or is represented by a receipt whose source is offline.

## Promotion gates

Promote `shadow` to `required` only after all of these hold:

1. at least 500 recordings and every capture mode have completion receipts;
2. queue age remains bounded for seven days;
3. source-to-crop verification has zero unexplained failures;
4. replayed crops recover all reviewed high-value events;
5. restart, abandoned-claim, NFS interruption, and 16-worker tests pass.

Only after another explicit review may retention move from `disabled` to
`verified`. Local NVMe cleanup is a separate decision.
