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

The initial deployment uses `shadow` plus `disabled`.

## Install on Kalman

From `/home/mouse9911/gits/leo-tracker`:

```bash
git pull --ff-only origin main
uv sync --active
sudo systemctl link "$PWD/deploy/leo-tracker-analysis-server.service"
sudo systemctl daemon-reload
sudo systemctl enable --now leo-tracker-analysis-server.service
```

The existing `.venv` is required and every invocation uses `uv --active`.

Graceful updates:

```bash
./scripts/starlink-analysis-server.sh --drain /mnt/qnap01/mouse9911/leo
sudo systemctl stop leo-tracker-analysis-server.service
git pull --ff-only origin main
sudo systemctl start leo-tracker-analysis-server.service
```

A normal `systemctl stop` sends `SIGTERM`; the server converts it into a drain
request and gives claimed jobs up to 30 minutes to finish.

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

## Promotion gates

Promote `shadow` to `required` only after all of these hold:

1. at least 500 recordings and every capture mode have completion receipts;
2. queue age remains bounded for seven days;
3. source-to-crop verification has zero unexplained failures;
4. replayed crops recover all reviewed high-value events;
5. restart, abandoned-claim, NFS interruption, and 16-worker tests pass.

Only after another explicit review may retention move from `disabled` to
`verified`. Local NVMe cleanup is a separate decision.
