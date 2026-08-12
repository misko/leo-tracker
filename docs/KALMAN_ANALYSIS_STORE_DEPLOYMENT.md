# Deploying the analysis store on Kalman

This runbook deploys the DuckDB analysis store as a rebuildable shadow
projection. It does not delete or replace reports, evidence, probe-index
Parquet, completion receipts, or capture data.

The safe rollout has three independent gates:

1. start and backfill the Kalman-local database owner;
2. restart Kalman's analysis service once so new completions enqueue manifests;
3. switch the SATPI01 dashboard only after reconciliation and parity pass.

Stop after either of the first two gates if anything is unhealthy. Existing
JSON and Parquet consumers remain authoritative throughout this rollout.

## Restart impact

No SATPI01 radio, capture, or export worker needs to restart for the Kalman
database rollout. Acquisition continues while Kalman is updated.

`leo-tracker-analysis-server.service` on Kalman needs one restart at gate 2 so
its sixteen workers inherit `LEO_ANALYSIS_STORE_ROOT`. A normal systemd stop or
restart sends `SIGTERM`; the server stops claiming new work, lets claimed jobs
finish, and then exits. Its unit allows up to 30 minutes. Unclaimed QNAP jobs
remain queued.

The new `leo-tracker-analysis-store.service` is independent. Its failure does
not fail scientific analysis in shadow mode: workers leave the authenticated
completion receipt in place and bounded reconciliation can recover a missed
enqueue later.

## 1. Update and verify the Kalman checkout

Run on Kalman as `mouse9911`:

```bash
cd /home/mouse9911/gits/leo-tracker
git pull --ff-only origin main
git status --short
```

The working tree should be clean. Confirm the two storage mounts resolve to the
intended filesystems:

```bash
findmnt -T /var/lib
findmnt -T /mnt/qnap01/mouse9911/leo
test -d /mnt/qnap01/mouse9911/leo/reports/runs
```

The live database belongs below local `/var/lib`; never move it onto QNAP, NFS,
or CIFS. QNAP holds only immutable published snapshots.

DuckDB is an optional analysis dependency. Install it manually into the
existing Kalman environment; production units continue to use `--no-sync` and
will never modify the environment at startup:

```bash
uv pip install --python /home/mouse9911/gits/leo-tracker/.venv 'duckdb>=1.0'
/home/mouse9911/gits/leo-tracker/.venv/bin/python -c \
  'import duckdb; print("DuckDB", duckdb.__version__)'
```

Optionally run the focused preflight tests before touching systemd:

```bash
UV_CACHE_DIR=/home/mouse9911/gits/leo-tracker/.uv-cache \
  uv run --active --no-sync pytest -q \
  tests/test_analysis_store.py \
  tests/test_analysis_offload_protocol.py \
  tests/test_radio_cli.py
```

## 2. Gate 1: install the database owner only

Do not replace or restart the analysis-server unit yet.

```bash
cd /home/mouse9911/gits/leo-tracker
sudo install -m 0644 deploy/leo-tracker-analysis-store.service \
  /etc/systemd/system/leo-tracker-analysis-store.service
sudo systemctl daemon-reload
sudo systemctl enable --now leo-tracker-analysis-store.service
```

Systemd creates `/var/lib/leo-tracker-analysis-store` for `mouse9911`. The
owner then creates the local live database and these queue directories:

```text
/var/lib/leo-tracker-analysis-store/live/analysis.duckdb
/var/lib/leo-tracker-analysis-store/inbox/
/var/lib/leo-tracker-analysis-store/running/
/var/lib/leo-tracker-analysis-store/failed/
```

It publishes verified immutable generations and health under:

```text
/mnt/qnap01/mouse9911/leo/database/
/mnt/qnap01/mouse9911/leo/reports/runtime/analysis-store.json
```

Check the service and its atomic health document:

```bash
systemctl status leo-tracker-analysis-store.service --no-pager -l
journalctl -u leo-tracker-analysis-store.service -n 100 --no-pager

uv run --active --no-sync leo-radio starlink-analysis-store status \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --runtime-output \
  /mnt/qnap01/mouse9911/leo/reports/runtime/analysis-store.json
```

Proceed only when the service is active, `last_error` is null, the failed queue
count is zero, and the live database path is under `/var/lib`.

## 3. Backfill in bounded batches

The daemon automatically reconciles 100 completion receipts every ten minutes.
For migration, stop it and run larger one-shot batches under the same exclusive
owner lock:

```bash
sudo systemctl stop leo-tracker-analysis-store.service

sudo -u mouse9911 \
  /home/mouse9911/.local/bin/uv run --active --no-sync \
  leo-radio starlink-analysis-store service \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --reconciliation-limit 1000 --once
```

Each invocation advances the persistent lexicographic cursor, enqueues missing
authenticated runs, ingests that batch, and exits. Repeat while watching:

- `errors` remains empty;
- `store.run_count` increases or the batch reports every scanned run present;
- `queue.failed` remains zero; and
- `reconciliation.json` advances and eventually wraps after a complete corpus
  pass.

Inspect failures before continuing:

```bash
find /var/lib/leo-tracker-analysis-store/failed -maxdepth 1 \
  -type f -name '*.error.json' -print
```

A failed manifest is quarantined; it does not leave partial database rows or
block later good runs. Do not edit failed manifests to force acceptance.

Restart the owner after the bounded migration session:

```bash
sudo systemctl start leo-tracker-analysis-store.service
systemctl status leo-tracker-analysis-store.service --no-pager -l
```

It will publish the newly committed database generation automatically.

## 4. Verify a published generation

Never query the live database while the owner is running. Resolve the immutable
snapshot named by the QNAP pointer:

```bash
pointer=/mnt/qnap01/mouse9911/leo/database/current.json
relative_snapshot="$(jq -r .snapshot "$pointer")"
snapshot="/mnt/qnap01/mouse9911/leo/database/$relative_snapshot"

jq . "$pointer"
test -r "$snapshot"
```

Run smoke queries against that explicit snapshot:

```bash
uv run --active --no-sync leo-radio starlink-analysis-store query \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --database "$snapshot" \
  --sql 'SELECT count(*) AS current_runs FROM current_runs'

uv run --active --no-sync leo-radio starlink-analysis-store query \
  /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --database "$snapshot" \
  --sql 'SELECT lnb, count(*) AS probes FROM probes GROUP BY lnb ORDER BY lnb'
```

Counts are smoke tests, not migration proof. Before dashboard cutover, compare
the DuckDB `probes` view with the existing Parquet projection using
bidirectional `EXCEPT`, and require a complete reconciliation pass with no
missing, failed, or changed inputs.

## 5. Gate 2: connect new Kalman completions

The committed analysis-server unit contains:

```text
Environment=LEO_ANALYSIS_STORE_ROOT=/var/lib/leo-tracker-analysis-store
```

Install it only after gate 1 is healthy. Stopping first makes the graceful drain
visible and avoids changing the unit beneath a running process:

```bash
cd /home/mouse9911/gits/leo-tracker
sudo systemctl stop leo-tracker-analysis-server.service
sudo install -m 0644 deploy/leo-tracker-analysis-server.service \
  /etc/systemd/system/leo-tracker-analysis-server.service
sudo systemctl daemon-reload
sudo systemctl start leo-tracker-analysis-server.service
```

Verify both owners and the existing analysis contract:

```bash
systemctl status \
  leo-tracker-analysis-store.service \
  leo-tracker-analysis-server.service --no-pager -l

jq . /mnt/qnap01/mouse9911/leo/reports/runtime/analysis-server.json
jq . /mnt/qnap01/mouse9911/leo/reports/runtime/analysis-store.json

journalctl -u leo-tracker-analysis-server.service \
  --since '15 minutes ago' --no-pager | grep analysis_store
```

New successful jobs should log `analysis_store_enqueued`. An occasional
`analysis_store_enqueue_failed` is recoverable in shadow mode, but a continuing
stream of failures or growing queue age is a stop condition.

Run shadow mode until at least one complete reconciliation pass and the planned
parity/soak gates have passed. Keep the probe-index timer, JSON reports, and all
retention behavior unchanged during this period.

## 6. Gate 3: dashboard cutover on SATPI01

This is a separate deployment and is not required for Kalman ingest. Do it only
after snapshot verification, reconciliation, and dashboard-response parity.
SATPI01 needs the optional DuckDB dependency, then the modified
`deploy/systemd/leo-tracker-dashboard.service` and one dashboard restart.

No radio or exporter service needs to restart. The dashboard copies the QNAP
generation into `/var/cache/leo-tracker-analysis-store` and opens that local
copy read-only. If a pointer or generation is bad, it retains its last verified
copy and can fall back to JSON.

## Rollback

Shadow rollback does not require deleting any data.

To disconnect new analysis completions, restore an analysis unit without
`LEO_ANALYSIS_STORE_ROOT` (or set it empty) and gracefully restart only
`leo-tracker-analysis-server.service`. Then stop the projection owner:

```bash
sudo systemctl stop leo-tracker-analysis-server.service
sudoedit /etc/systemd/system/leo-tracker-analysis-server.service
# Remove Environment=LEO_ANALYSIS_STORE_ROOT=...
sudo systemctl daemon-reload
sudo systemctl start leo-tracker-analysis-server.service
sudo systemctl disable --now leo-tracker-analysis-store.service
```

Leave `/var/lib/leo-tracker-analysis-store` and published generations in place
for diagnosis. JSON, Parquet, completion receipts, evidence, and queued analysis
jobs remain valid. No initial deployment or rollback command authorizes source
retirement.

For design rationale and lower-level operations, see
[`duckdb_analysis_store_plan.md`](duckdb_analysis_store_plan.md) and
[`analysis_store_operations.md`](analysis_store_operations.md).
