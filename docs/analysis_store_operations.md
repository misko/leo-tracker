# Analysis store operations

The analysis store is a shadow projection until the parity and soak gates in
[`duckdb_analysis_store_plan.md`](duckdb_analysis_store_plan.md) pass. Enabling
it does not authorize removal of reports, dashboard shards, probe partitions,
or evidence.

## Host roles

- Kalman owns `/var/lib/leo-tracker-analysis-store/live/analysis.duckdb` through
  exactly one `leo-tracker-analysis-store.service` process.
- The sixteen analysis workers write only authenticated input manifests below
  the store inbox.
- QNAP holds immutable verified snapshots and the atomic `database/current.json`
  pointer.
- SATPI01 copies the selected snapshot to
  `/var/cache/leo-tracker-analysis-store` and opens that copy read-only.

Do not put the live database on QNAP and do not point notebooks or the dashboard
at it. Operator queries use a published snapshot.

## Preflight on Kalman

From Kalman's checkout and existing virtual environment:

```bash
uv run --active --no-sync python -c 'import duckdb; print(duckdb.__version__)'
uv run --active --no-sync leo-radio starlink-analysis-store init \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo
uv run --active --no-sync leo-radio starlink-analysis-store status \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo
```

`init` creates only the Kalman-local database. It does not scan reports or
publish a snapshot.

Once the daemon is running, read its atomic health document without opening
the live database:

```bash
uv run --active --no-sync leo-radio starlink-analysis-store status \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --runtime-output /mnt/qnap01/mouse9911/leo/reports/runtime/analysis-store.json
```

Without `--runtime-output`, `status` is an offline inspection and takes the
owner lock; it will refuse to race the daemon.

## Bounded shadow backfill

Preview first:

```bash
uv run --active --no-sync leo-radio starlink-analysis-store backfill \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo --limit 10 --dry-run
```

Enqueue and ingest the same bounded batch:

```bash
uv run --active --no-sync leo-radio starlink-analysis-store backfill \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo --limit 10
uv run --active --no-sync leo-radio starlink-analysis-store drain \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo --limit 10
```

The manual `backfill`/`drain` pair reads the live run-ID set, so stop the store
service first; the owner lock on `drain` will refuse an unsafe overlap. For an
online bounded repair, let the daemon's persistent reconciliation cursor find
the gap, or run a one-shot owner while the daemon is stopped:

```bash
uv run --active --no-sync leo-radio starlink-analysis-store service \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --reconciliation-limit 1000 --once
```

Backfill reads deterministic `reports/runs/<pipeline>/<recording>/completion.json`
receipts. For each recording it resolves the finite output contract paths and
hashes their contents. It does not infer freshness from file counts.

Failed input is moved to `failed/` with a structured error. Fix or investigate
the named source before re-enqueueing; do not edit the failed manifest to force
acceptance.

## Publish and query

The daemon publishes automatically when committed data has advanced. Manual
publication is a maintenance operation and requires the daemon to be stopped;
the owner lock refuses to race it:

```bash
uv run --active --no-sync leo-radio starlink-analysis-store publish \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --publication-root /mnt/qnap01/mouse9911/leo/database
```

The command checkpoints and closes the live connection, creates and verifies a
local candidate, copies it privately to QNAP, verifies the read-back, publishes
an immutable generation, then atomically updates `current.json`.

Query an immutable snapshot by passing it explicitly as `--database`:

```bash
snapshot=/mnt/qnap01/mouse9911/leo/database/snapshots/analysis-GENERATION.duckdb
uv run --active --no-sync leo-radio starlink-analysis-store query \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo --database "$snapshot" \
  --sql 'SELECT lnb, count(*) FROM probes GROUP BY lnb ORDER BY lnb'
```

Stable initial views are `current_runs`, `current_dashboard_records`, and
`probes`.

## Deployment sequence

1. Install and start `deploy/leo-tracker-analysis-store.service` on Kalman.
2. Confirm `reports/runtime/analysis-store.json` advances and reports no error.
3. Add `LEO_ANALYSIS_STORE_ROOT=/var/lib/leo-tracker-analysis-store` to the
   Kalman analysis service and restart it. This enables non-fatal shadow
   enqueueing only.
4. Backfill in bounded batches and compare the `probes` view with the current
   Parquet projection using bidirectional `EXCEPT` queries.
5. Confirm snapshot publication and copy/open a generation from SATPI01.
6. Deploy the dashboard flags only after snapshot verification succeeds. The
   dashboard retains its JSON fallback and last verified local generation.
7. Run the seven-day parity window and 72-hour fault/soak workload before any
   consumer or report-retention change.

The repository unit files contain the final paths, but copying them into systemd
and starting them changes external service state and is a separate operator
action.

## Health and rollback

Important health fields are queue age, failed count, commit sequence, last run,
snapshot generation, snapshot commit sequence, and last error stage. A growing
ready queue with advancing analysis completions means the database owner needs
attention; it does not mean analysis data was lost.

Rollback in shadow mode:

1. remove or empty `LEO_ANALYSIS_STORE_ROOT` from the analysis service;
2. stop the store service;
3. leave JSON and the existing dashboard configuration in place; and
4. retain the local database and published generations for diagnosis, or move
   them aside and rebuild later.

After dashboard opt-in, remove the two `--analysis-store-*` flags and restart
the dashboard to return immediately to JSON. A bad shared pointer does not
replace the last verified local cached generation.

No command in the initial implementation deletes scientific source reports.
