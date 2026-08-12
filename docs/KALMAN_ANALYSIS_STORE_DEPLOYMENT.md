# Deploying the analysis store on Kalman

This runbook deploys the DuckDB analysis store as a rebuildable shadow
projection. It does not delete or replace reports, evidence, probe-index
Parquet, completion receipts, or capture data.

The rollout has four gates. Stop at any gate that is unhealthy; everything
before it is safe to leave running, and nothing before it is load-bearing.

| Gate | What it turns on | Publishes snapshots? | Reversible by |
|------|------------------|----------------------|---------------|
| 1 | Kalman-local database owner | no | `systemctl mask` |
| 2 | Backfill of the historical corpus | no | delete the database |
| 3 | New Kalman completions enqueue | no | drop-in, no unit edit |
| 4 | SATPI01 dashboard reads DuckDB | **yes** | remove two flags |

Existing JSON and Parquet consumers stay authoritative throughout gates 1-3.

## Readiness register

Two items are true blockers, and both block only gate 4. Nothing blocks gates
0-3.

| Item | Blocks | Severity | Recommendation |
|---|---|---|---|
| No snapshot retention exists | 4 | **Blocker** | Keep the `no-publication` drop-in through gates 1-3; write a `prune` action before gate 4 |
| DuckDB version drift Kalman↔SATPI01 | 4 | **Blocker** | Pin an exact version in the `analysis` extra; assert it from the publication receipt before cutover |
| Publication interval unsized | 4 | High | Defer; derive from gate 2's measured size and `df`, targeting ≤1 generation/hour |
| Parity + soak evidence not gathered | 4 | High | Bidirectional `EXCEPT` vs Parquet plus a full cursor wrap, then 7-day parity and 72-hour soak |
| Preflight tests skip silently | 0 | Medium | Install the extra first; require 19 passed, never exit status alone |
| Backfill duration unknown | 2 | Medium | Time the first 2000-run round and extrapolate; run under tmux — idempotent and interruptible |
| Database size unknown | 2→4 | Medium | Read `store.database_bytes` at gate 2 exit; sole input to the interval decision |
| Ingest failure rate unknown | 2 | Medium | Agree a stop threshold before starting; inspect `failed/` after round 1 |
| Analysis-server restart cost | 3 | Low | Schedule in a low-capture window; 30-minute drain, acquisition unaffected |

Version parity is cheap to enforce because `verify_snapshot` already stamps
`duckdb_version` into every publication receipt — so gate 4 gets an assertion
rather than a convention. Read the receipt, not the pointer, which carries no
version:

```bash
jq -r .duckdb_version "$SHARED/database/publication/<generation>.json"
```

## Why gates 1-3 publish nothing

`publish_snapshot` copies the entire live database into
`$STORE/snapshots/` on Kalman *and* into `database/snapshots/` on QNAP, once
per generation (`analysis_store/snapshot.py:64-87`). SATPI01 then keeps a third
full copy per generation in `/var/cache/leo-tracker-analysis-store`
(`analysis_store/repository.py:33-42`). **Nothing in the codebase deletes any of
them.** The daemon publishes whenever `commit_sequence` advanced and
`--snapshot-interval-s` (default 300) has elapsed, so under normal completion
traffic that is up to 288 full-database copies per day, per location.

The database embeds full document payloads — `structured_documents.payload_json`
stores the parsed contents of every source document, and `dashboard_records`
stores two more JSON blobs per run (`analysis_store/mapping.py:408-419`). Across
~24.5k completion receipts drawn from a 35 GB reports tree, expect a
multi-gigabyte database; gate 2 measures the real figure. The exact size is not
needed to make the decision, because the arithmetic is ruinous at any plausible
value: at 1 GB per generation, 288 generations/day is 288 GB/day *per location*,
against 1.3 TB free on QNAP (80% used) and 1.5 TB on `/`.

Only the gate-4 dashboard consumes snapshots. So gates 1-3 run the owner with
publication switched off, and gate 4 is blocked on retention tooling that does
not exist yet. Deferring costs nothing: a snapshot is a pure function of
committed state, so one taken later contains everything an earlier one would
have.

## Restart impact

No SATPI01 radio, capture, or export worker restarts for this rollout.
Acquisition continues throughout.

`leo-tracker-analysis-server.service` needs exactly one restart, at gate 3, so
its sixteen workers inherit `LEO_ANALYSIS_STORE_ROOT`. Systemd sends `SIGTERM`;
the server traps it as a drain request (`scripts/starlink-analysis-server.sh:636`),
stops claiming new work, finishes claimed jobs, and exits. `TimeoutStopSec=1800`
allows 30 minutes. Unclaimed QNAP jobs stay queued.

The store owner's failure is not a scientific failure: workers leave the
authenticated completion receipt in place and reconciliation recovers a missed
enqueue later.

## 0. Preflight

Nothing here changes state. Establish a shell that every later section reuses:

```bash
cd /home/mouse9911/gits/leo-tracker

radio() { env VIRTUAL_ENV=/home/mouse9911/gits/leo-tracker/.venv \
  UV_CACHE_DIR=/home/mouse9911/gits/leo-tracker/.uv-cache \
  /home/mouse9911/.local/bin/uv run --active --no-sync leo-radio "$@"; }

STORE=/var/lib/leo-tracker-analysis-store
SHARED=/mnt/qnap01/mouse9911/leo
```

Defining `radio` this way makes every command below independent of cwd and of
whether a virtualenv is active, and matches how the analysis server invokes the
CLI.

Confirm the checkout, the mounts, and the headroom:

```bash
git pull --ff-only origin main
git status --short

findmnt -T /var/lib
findmnt -T "$SHARED"
test -d "$SHARED/reports/runs"

df -h /var/lib "$SHARED"
```

The working tree should be clean. The live database belongs below local
`/var/lib`; never move it onto QNAP, NFS, or CIFS — `require_local_database`
refuses at connect time (`analysis_store/ingest.py:41-46`). Record both `Avail`
figures; gate 2 is sized against the local one.

DuckDB is the optional `analysis` extra. `pyproject.toml` is the source of truth
for the version floor — read it, then install what it says, so the two cannot
drift silently:

```bash
grep -A 2 '^analysis = \[' pyproject.toml
```

Production units run `--no-sync` and never modify the environment at startup, so
install it once, by hand, into the existing venv:

```bash
uv pip install --python /home/mouse9911/gits/leo-tracker/.venv 'duckdb>=1.0'

/home/mouse9911/gits/leo-tracker/.venv/bin/python -c \
  'import duckdb; print("DuckDB", duckdb.__version__)'
```

Record the resolved version — gate 4 needs SATPI01 to match it exactly, and
DuckDB storage is not forward-compatible: an older DuckDB cannot open a file a
newer one wrote. Two hosts each resolving `>=1.0` at different times is a
silently broken dashboard.

Run the focused tests only *after* the install above. The whole analysis-store
suite sits behind `pytest.importorskip` (`tests/test_analysis_store.py:8`), so
without DuckDB the file reports `1 skipped` and exits 0 — green, having tested
nothing. Assert the count, not the exit status:

```bash
UV_CACHE_DIR=/home/mouse9911/gits/leo-tracker/.uv-cache \
  uv run --active --no-sync pytest -q \
  tests/test_analysis_store.py \
  tests/test_analysis_offload_protocol.py \
  tests/test_radio_cli.py
```

`tests/test_analysis_store.py` must contribute **19 passed** — 18 test functions,
one of which is parametrized two ways. If it reports skipped, the extra did not
land in the interpreter the tests run under.

Finally, record the size of the job you are about to run:

```bash
find "$SHARED/reports/runs" -mindepth 3 -maxdepth 3 \
  -name completion.json -printf . | wc -c
```

On 2026-08-11 this read 24,521 → 24,533 → **24,543** across one working session,
i.e. roughly 30 new receipts an hour while the analysis server runs. Capture
your own number, and treat gate 2's target as a floor, not an equality — the
corpus grows underneath the backfill, and the daemon's reconciliation is what
closes the remainder.

Phase 0 baseline recorded on 2026-08-11: DuckDB **1.5.5**, `/var/lib` on local
ext4 with 1.5 TB free, QNAP on nfs4 with 1.3 TB free at 81% used, preflight
suites 19 + 37 passed.

## 1. Gate 1: the database owner, publication off

Install the unit, then a drop-in that removes `--publication-root`. The drop-in
must clear `ExecStart=` before setting it, because systemd appends otherwise:

```bash
sudo install -m 0644 deploy/leo-tracker-analysis-store.service \
  /etc/systemd/system/leo-tracker-analysis-store.service

sudo mkdir -p /etc/systemd/system/leo-tracker-analysis-store.service.d
sudo tee /etc/systemd/system/leo-tracker-analysis-store.service.d/no-publication.conf \
  >/dev/null <<'EOF'
# Gates 1-3 publish nothing: every generation is a full copy of the database in
# two locations and nothing prunes them. Delete this file at gate 4, once
# snapshot retention exists.
[Service]
ExecStart=
ExecStart=/home/mouse9911/.local/bin/uv run --active --no-sync leo-radio \
  starlink-analysis-store service /var/lib/leo-tracker-analysis-store \
  --shared-root /mnt/qnap01/mouse9911/leo \
  --runtime-output /mnt/qnap01/mouse9911/leo/reports/runtime/analysis-store.json
EOF

sudo systemctl daemon-reload
systemctl show leo-tracker-analysis-store.service -p ExecStart \
  | grep -c publication-root
```

That last command must print `0`. It reads the *effective* merged `ExecStart`
rather than the file text, so it proves the override took rather than that the
drop-in merely exists. Then start it:

```bash
sudo systemctl enable --now leo-tracker-analysis-store.service
```

`StateDirectory=` creates `$STORE` owned by `mouse9911`. The owner then creates:

```text
$STORE/live/analysis.duckdb      the live database
$STORE/inbox/ running/ failed/   the single-consumer queue
$STORE/reconciliation.json        persistent lexicographic cursor
$STORE/.owner.lock                exclusive ownership
$STORE/snapshots/                 created only once publication is enabled
```

Check health. The daemon rewrites the runtime document every loop iteration, so
wait for it to exist before reading it — `status` falls back to taking the owner
lock, and against a running daemon that fails with a misleading
`another process owns the analysis store`:

```bash
systemctl status leo-tracker-analysis-store.service --no-pager -l
journalctl -u leo-tracker-analysis-store.service -n 100 --no-pager

until test -f "$SHARED/reports/runtime/analysis-store.json"; do sleep 1; done
radio starlink-analysis-store status "$STORE" --shared-root "$SHARED" \
  --runtime-output "$SHARED/reports/runtime/analysis-store.json" \
  | jq '{active: .store.initialized, db: .store.database,
         runs: .store.run_count, queue, last_error}'
```

**Gate 1 passes when** the unit is `active`, `last_error` is `null`, `queue.failed`
is `0`, and `db` starts with `/var/lib`.

## 2. Gate 2: backfill the historical corpus

Use `backfill` + `drain`, not `service --once`. Both report failure counts *and*
set a non-zero exit status (`radio/cli.py:1009`), whereas the `service` action's
report carries neither key and therefore always exits 0 — it cannot drive a loop.

The manual pair opens the live database, so the daemon must be down. Masking,
not stopping, is what keeps it down: after gate 3 the analysis-server unit
`Wants=` the store service and would restart it underneath your owner lock.

```bash
sudo systemctl mask --now leo-tracker-analysis-store.service
```

Preview, then enqueue every missing run in one pass:

```bash
radio starlink-analysis-store backfill "$STORE" --shared-root "$SHARED" \
  --limit 10 --dry-run | jq '{scanned, planned: (.planned|length), errors}'

radio starlink-analysis-store backfill "$STORE" --shared-root "$SHARED" \
  | jq '{scanned, queued: (.queued|length),
         skipped: (.skipped_existing|length), errors}'
```

`backfill` takes no cursor: `--limit` caps runs *scanned* from the start of the
corpus, so repeated limited calls rescan the same prefix and enqueue nothing.
Run it unlimited, once. It enqueues ~24.5k manifests into `$STORE/inbox/`.

Now drain in bounded rounds. Chunking is for observability and to keep the
inbox scan short — `process_next` sorts the whole inbox per manifest, so cost
falls as the queue drains. This takes hours: each run reads and hashes its
source documents over NFS. Run it under `tmux`, and interrupt it freely — the
work is idempotent, since already-ingested runs are skipped by run ID.

```bash
tmux new -s backfill

while :; do
  out=$(mktemp /tmp/drain.XXXXXX.json)
  radio starlink-analysis-store drain "$STORE" --shared-root "$SHARED" \
    --limit 2000 --continue-on-error > "$out"; rc=$?
  jq -c '{processed, inserted, duplicates, failures}' "$out"
  test "$(jq -r .processed "$out")" -eq 0 && break
  test "$rc" -ne 0 && echo "  ^ failures in this round — see \$STORE/failed"
  rm -f "$out"
done
```

Watch progress from a second shell:

```bash
watch -n 60 'ls /var/lib/leo-tracker-analysis-store/inbox | wc -l; \
  du -sh /var/lib/leo-tracker-analysis-store/live'
```

Inspect anything quarantined. A failed manifest leaves no partial rows and does
not block later runs; do not edit one to force acceptance:

```bash
find "$STORE/failed" -maxdepth 1 -name '*.error.json' \
  -exec jq -c '{run_id, error_type, error}' {} +
```

**Gate 2 passes when** the drain loop reports `processed: 0`, `queue.ready` is
`0`, and `store.run_count` accounts for the receipt count from preflight minus
whatever sits in `failed/` with an explained error. Record the database size —
it sets the per-generation cost that gate 4 depends on:

```bash
sudo systemctl unmask leo-tracker-analysis-store.service
sudo systemctl start leo-tracker-analysis-store.service

radio starlink-analysis-store status "$STORE" --shared-root "$SHARED" \
  --runtime-output "$SHARED/reports/runtime/analysis-store.json" \
  | jq '{runs: .store.run_count, recordings: .store.recording_count,
         probes: .store.probe_count, bytes: .store.database_bytes, queue}'
```

### Publication proof: exactly one generation

Take one snapshot by hand to prove the publication path end to end. One is
bounded; the daemon's automatic publication is not, which is why it stays off.

```bash
sudo systemctl stop leo-tracker-analysis-store.service

radio starlink-analysis-store publish "$STORE" --shared-root "$SHARED" \
  --publication-root "$SHARED/database" | jq '{generation, bytes, sha256, run_count}'

sudo systemctl start leo-tracker-analysis-store.service
```

Resolve it through the pointer. `current.json` stores `snapshot` *relative* to
the publication root, while the receipt under `database/publication/` stores it
absolute — use the pointer:

```bash
pointer="$SHARED/database/current.json"
snapshot="$SHARED/database/$(jq -r .snapshot "$pointer")"
jq . "$pointer"; test -r "$snapshot"
```

Query that explicit file. `query` connects read-only, so reading it across NFS
takes no write lock:

```bash
radio starlink-analysis-store query "$STORE" --shared-root "$SHARED" \
  --database "$snapshot" \
  --sql 'SELECT count(*) AS current_runs FROM current_runs'

radio starlink-analysis-store query "$STORE" --shared-root "$SHARED" \
  --database "$snapshot" \
  --sql 'SELECT lnb, count(*) AS probes FROM probes GROUP BY lnb ORDER BY lnb'
```

Counts are smoke tests, not migration proof. The parity evidence for gate 4 is a
bidirectional `EXCEPT` between the DuckDB `probes` view and the existing Parquet
projection, plus a complete reconciliation pass with nothing missing, failed, or
changed. Note the snapshot's size against `df` before scheduling gate 4.

## 3. Gate 3: connect new Kalman completions

Deliver `LEO_ANALYSIS_STORE_ROOT` as a drop-in rather than by installing the
repo unit. The repo unit also adds `Wants=`/`After=` on the store service, and a
drop-in keeps this one reversible without editing anything under `/etc` that a
later `install` would silently overwrite:

```bash
sudo mkdir -p /etc/systemd/system/leo-tracker-analysis-server.service.d
sudo tee /etc/systemd/system/leo-tracker-analysis-server.service.d/analysis-store.conf \
  >/dev/null <<'EOF'
[Service]
Environment=LEO_ANALYSIS_STORE_ROOT=/var/lib/leo-tracker-analysis-store
EOF

sudo systemctl daemon-reload
sudo systemctl restart leo-tracker-analysis-server.service
```

The restart drains up to 30 minutes. Watch it complete rather than assuming:

```bash
systemctl status leo-tracker-analysis-server.service --no-pager -l
systemctl show leo-tracker-analysis-server.service -p Environment \
  | tr ' ' '\n' | grep STORE_ROOT
```

Verify both owners and the existing analysis contract:

```bash
jq . "$SHARED/reports/runtime/analysis-server.json"
jq '{runs: .store.run_count, queue, last_error, last_reconciliation}' \
  "$SHARED/reports/runtime/analysis-store.json"

journalctl -u leo-tracker-analysis-server.service \
  --since '15 minutes ago' --no-pager | grep -c analysis_store_enqueued
journalctl -u leo-tracker-analysis-server.service \
  --since '15 minutes ago' --no-pager | grep -c analysis_store_enqueue_failed
```

An occasional `analysis_store_enqueue_failed` is recoverable — the job still
succeeds and reconciliation re-enqueues from the receipt. A continuing stream of
them, or a growing `queue.oldest_ready_age_s`, is a stop condition.

**Gate 3 passes when** `run_count` tracks new completions, `queue.failed` stays
`0`, and `last_reconciliation.errors` stays empty. Then hold here: run shadow
mode through at least one complete reconciliation pass — the cursor in
`$STORE/reconciliation.json` wrapping back past its starting key — plus the
seven-day parity window and 72-hour soak from `duckdb_analysis_store_plan.md`.
Keep the probe-index timer, JSON reports, and all retention behavior unchanged.

## 4. Gate 4: dashboard cutover — blocked on retention

**Do not start this gate yet.** It is the only gate that requires continuous
publication, and continuous publication is currently unbounded in three
directories at once. It needs, first:

1. a `prune` action that keeps the generation named by `current.json` plus the
   last N, deleting older files from `$STORE/snapshots/`,
   `database/snapshots/`, and `database/publication/`;
2. the same for SATPI01's `/var/cache/leo-tracker-analysis-store`, which
   accumulates one full copy per generation it observes;
3. a `--snapshot-interval-s` chosen against the measured database size from
   gate 2 and the `df` headroom — the 300 s default is far too aggressive for a
   multi-gigabyte database;
4. an identical pinned DuckDB version on Kalman and SATPI01. DuckDB storage is
   not forward-compatible, so a snapshot written by a newer DuckDB than the
   dashboard's cannot be opened at all. Today `>=1.0` resolves to 1.5.5; letting
   each host resolve that floor independently is a latent outage;
5. the parity and soak evidence from gate 3.

When those exist: delete
`/etc/systemd/system/leo-tracker-analysis-store.service.d/no-publication.conf`,
`daemon-reload`, restart the store owner, then deploy SATPI01's DuckDB extra and
the two `--analysis-store-*` flags in `deploy/systemd/leo-tracker-dashboard.service`
with one dashboard restart. No radio or exporter service restarts. If a pointer
or generation is bad, the dashboard keeps its last verified local copy and can
fall back to JSON.

## Rollback

Shadow rollback deletes no data. Reverse the gates in order.

Disconnect new completions (gate 3) — remove the drop-in, do not edit the unit:

```bash
sudo rm /etc/systemd/system/leo-tracker-analysis-server.service.d/analysis-store.conf
sudo systemctl daemon-reload
sudo systemctl restart leo-tracker-analysis-server.service
```

Stop the owner (gate 1). Use `mask`, not `disable`: the repo's analysis-server
unit `Wants=` the store service, and `disable` only removes the
`multi-user.target` symlink, so the next analysis-server start would pull it
back up:

```bash
sudo systemctl mask --now leo-tracker-analysis-store.service
```

Leave `$STORE` and any published generation in place for diagnosis. JSON,
Parquet, completion receipts, evidence, and queued analysis jobs remain valid.
The database is a rebuildable projection: deleting `$STORE` and repeating gate 2
reconstructs it exactly. No command in this runbook authorizes source retirement.

For design rationale and lower-level operations, see
[`duckdb_analysis_store_plan.md`](duckdb_analysis_store_plan.md) and
[`analysis_store_operations.md`](analysis_store_operations.md).
