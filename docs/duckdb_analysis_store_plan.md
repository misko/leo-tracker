> **Superseded.** The single-file DuckDB store this plan describes was
> replaced by a partitioned Parquet projection; see
> [`analysis_parquet_projection.md`](analysis_parquet_projection.md) for what
> was built and why. This document is kept because the relational schema it
> designed is still the one the projection builds through, and because the
> reasoning about identity, authentication and NFS remains the basis of it.

# DuckDB analysis store

Plan of record for consolidating structured Kalman analysis results into a
queryable database, moving the dashboard off directory scans, and eventually
retiring the per-recording JSON sidecar layout without weakening the evidence
and replay guarantees.

Status: the shadow-store vertical slice is implemented in the repository but is
not deployed. It includes content-addressed input manifests, transactional core
and probe mappings, a single-consumer queue, verified immutable snapshots,
read-only dashboard caching, CLI/service integration, and automated fault and
concurrency tests. Corpus backfill, live parity/soak gates, authoritative-output
cutover, and verified source retirement remain operational phases and are not
claimed complete. The existing probe-index experiment remains useful evidence
for projection speed and schema handling, but hourly Parquet partitions are not
the target architecture.

## Outcome

Kalman owns one live DuckDB database containing the structured results of every
successfully validated analysis run. The sixteen analysis workers never open
that database. They continue to work independently, then publish a small durable
ingest request after the existing completion receipt has authenticated the
required outputs. The worker also records the complete, explicit set of
optional structured outputs and file artifacts it actually produced; the store
never discovers scientific inputs by globbing a directory.

A single store process consumes those requests and commits one analysis run in
one DuckDB transaction. It periodically publishes a verified immutable database
snapshot to QNAP. SATPI01 copies a complete snapshot into a local dashboard
cache and opens it read-only. The live database is therefore never opened over
NFS and never has multiple writer processes.

The database replaces JSON directory scans for dashboard and corpus queries.
Large or already-compressed artifacts remain files: raw IQ, evidence clips,
NPZ arrays, plots, and symbol samples belong in an artifact store and are
referenced from the database by path, size, and digest.

The first releases treat the database as a rebuildable projection. Individual
JSON reports are removed only in the final phase, after every consumer has moved
to the store, lossless export has been proven, verified source bundles exist,
and rollback has been exercised.

```text
Kalman, 16 DSP workers
          │ validated receipt + explicit hashed output manifest
          ▼
Kalman, one store owner ──transaction──▶ local live DuckDB
          │                                  │
          │ status                           │ checkpointed immutable generation
          ▼                                  ▼
    runtime health                 verified QNAP snapshot + pointer
                                                   │
                                                   ▼
                                  SATPI01 local read-only dashboard cache
```

## Why this shape

### One writer, because analysis is on Kalman

The deployed analysis service runs sixteen workers on Kalman. Parallel DSP is
valuable; parallel processes opening the same native DuckDB file are not. A
single long-lived owner serializes bounded database connections and transactions
while letting the workers proceed without database locks or database failure
modes.

The handoff is deliberately asynchronous during the shadow phases. A database
pause cannot lose an analysis: the completion receipt and source reports remain
durable, and the ingest request remains queued. Once the store becomes the
structured-output authority, successful database commit becomes an explicit
job-completion gate.

### Immutable snapshots, because the dashboard is on SATPI01

The production dashboard currently runs on SATPI01 and reads QNAP. It must not
open Kalman's live database over NFS. The store publisher instead:

1. pauses store ingestion, checkpoints and closes the writer connection;
2. copies the live database to a local candidate; workers remain asynchronous
   and continue producing durable queue requests during this bounded pause;
3. opens the candidate read-only and verifies its schema, commit sequence and
   row counts; the publication receipt also records the reconciliation cursor;
4. copies it to a private QNAP path, reads it back, verifies its SHA-256 and
   opens it read-only there;
5. renames it to an immutable generation name; and
6. atomically replaces a small `current.json` pointer only after verification.

The dashboard watches the pointer, copies a new generation to a private local
cache file, verifies its digest, opens it read-only, and swaps connections under
its model lock. A request observes either the old generation or the new one,
never a partially copied database. At least two verified generations are kept
for rollback. Cleanup never removes the generation named by `current.json`.

This adds no network query service. If measured snapshot publication eventually
cannot meet the freshness budget, a store-owned read-only API is the next step;
direct access to the live file is not.

### Typed facts plus artifact references

Fields used for filtering, joining, grouping, retention, and dashboard display
are ordinary typed columns. Repeated scientific observations are child rows.
Rare, schema-specific details that are not yet query dimensions may remain in a
DuckDB `JSON` column, but the primary analysis, probe, track, association, and
dashboard paths must not depend on reparsing an opaque report blob.

The schema preserves enough information to export the existing JSON contracts
losslessly before any source document is eligible for retirement. Fields that
cannot yet round-trip are a migration blocker, not silently discarded data.

## Storage layout

Illustrative paths; deployment units make them configurable and validate that
the live path is on Kalman's local filesystem.

```text
Kalman local:
  /var/lib/leo-tracker-analysis-store/
    live/analysis.duckdb
    inbox/<run-id>.json
    running/<run-id>.json
    failed/<run-id>.json
    snapshots/<generation>.duckdb

QNAP shared:
  /mnt/qnap01/mouse9911/leo/database/
    snapshots/analysis-<generation>.duckdb
    current.json
    publication/<generation>.json

SATPI01 local:
  /var/cache/leo-tracker-analysis-store/
    analysis-<generation>.duckdb
```

Inbox files are small transient transaction requests, not scientific results.
They contain the run identity, completion-receipt path and digest, and enqueue
time. A request is removed only after its immutable run ID exists in DuckDB.
Crash recovery may repeat an ingest; it must never invent one.

## Identity and versioning

`recording_id` identifies the capture. It is not sufficient to identify an
analysis result because a capture can be processed again by a new pipeline.

`run_id` is a deterministic SHA-256 over:

- recording ID;
- pipeline ID;
- completion-receipt digest;
- context bundle identity;
- the canonical capture-manifest digest derived from the authenticated analysis
  output; and
- the ordered structured-document and artifact kind, size and SHA-256 entries
  from the store-input manifest.

Every scientific table includes `run_id`. The store rejects a request whose
claimed identity does not reproduce this digest. Re-enqueuing the same run is a
no-op. A different result is a different immutable run, never an in-place
mutation hidden behind an unchanged report count.

The initial `current_runs` view selects the newest completed pipeline revision
for each recording by completion time and commit sequence. Historical runs
remain queryable and their provenance remains explicit. A later production
pipeline allowlist can make selection policy explicit without rewriting
scientific rows.

## Initial schema

The exact column lists are introduced through versioned migrations and golden
fixtures. The table boundaries are:

| Table | Grain and purpose |
|---|---|
| `schema_migrations` | Applied store schema and code compatibility |
| `store_metadata` | Commit sequence and last committed run; publication receipts retain generation metadata |
| `recordings` | One capture: time, radio, receiver labels, tuning, gain, mode, region, channel, source manifest digest |
| `analysis_runs` | One immutable pipeline result and its code, context, completion, archive, status, and timestamps |
| `analysis_parameters` | Typed common DSP parameters plus versioned JSON for method-specific settings |
| `analysis_summary` | One row per run with detector and coverage totals |
| `analysis_windows` | One row per coarse analysis window |
| `probe_checks` | One row per exact acquisition epoch |
| `receiver_probes` | One row per probe and receiver; the successor to the current probe index |
| `followup_checks` | Dense follow-up windows and qualification results |
| `confirmed_events` | De-duplicated temporal confirmation intervals |
| `tracks` | One row per continuous or linked track |
| `track_points` | Time/frequency/drift observations belonging to a track |
| `decodes` | Decode summary and quality metrics |
| `associations` | TLE association decision and catalog provenance |
| `association_candidates` | Ranked candidate fits and held-out residuals |
| `structured_documents` | Lossless versioned JSON for fragment, channel-link, fingerprint, and other uncommon contracts |
| `artifacts` | Kind, path, media type, byte count, SHA-256, storage authority, and availability |
| `source_documents` | Original document kind, schema, path, size and digest used for ingest and round-trip verification |
| `current_dashboard_records` | A view supplying listing and detail fields without reading sidecars |

Primary keys include `run_id` and the natural child index, for example
`(run_id, check_index)` and `(run_id, check_index, receiver_index)`. Foreign keys
are declared where DuckDB supports the required semantics, and mapper tests
also validate parent/child completeness explicitly.

No query derives receiver identity from array position without storing that
position and label together. No floating point observation is used as a key.
UTC instants use `TIMESTAMPTZ`; sample indexes use `BIGINT`; durations and
relative offsets use `DOUBLE` with their units in the column name.

## Ingest protocol

### Shadow mode

1. The existing Kalman worker produces its ordinary output set.
2. Evidence archive and `validate_outputs` run unchanged.
3. The atomic completion receipt is written with its current output digests.
4. The worker builds a versioned store-input manifest from its explicit stage
   outputs. It includes the completion-receipt digest plus every present
   structured document and file artifact with kind, schema where applicable,
   byte count and SHA-256. It does not glob report directories.
5. The worker atomically enqueues that manifest as one local store request.
6. The job may become `done` even if store ingestion is delayed. Health reports
   the lag and reconciliation finds the missing run.
7. The store validates the receipt, schemas, sizes, and digests again, maps all
   documents in memory, starts one transaction, inserts every table, runs
   within-transaction invariants, and commits.
8. Only after commit is the request removed.

Mapping happens before `BEGIN` where possible, keeping the write transaction
short. A mapping or schema error quarantines the request with a structured error
and leaves the existing database unchanged.

### Authoritative mode

After dashboard cutover and the shadow acceptance period, database commit joins
the completion contract:

1. Stage JSON and small arrays in a per-job Kalman work directory.
2. Run all existing scientific stages, archive publication, and validation
   against that work directory.
3. Produce the candidate completion data and complete store-input manifest,
   then enqueue and wait for the store's durable commit acknowledgment.
4. Write the final completion receipt with `store_run_id` and database commit
   sequence. Snapshot generation is recorded later by the publisher and is not
   part of the completion dependency.
5. Publish file artifacts and remove transient structured documents only after
   all retirement gates pass.

The authoritative transition is intentionally separate from initial database
delivery. It must not be attempted while evidence, replay, lifecycle, or
dashboard code still requires historical report paths.

## Query and dashboard contract

Add a small repository layer so the dashboard does not embed DuckDB calls
throughout `dashboard.py`:

```python
class AnalysisRepository:
    def summary(self) -> dict: ...
    def recent_recordings(self, limit: int) -> list[dict]: ...
    def recording_detail(self, recording_id: str) -> dict | None: ...
    def activity(self, start_utc, stop_utc) -> dict: ...
```

Implement both `JsonAnalysisRepository` and `DuckDBAnalysisRepository` during
migration. The same contract tests run against both. The dashboard chooses the
backend through configuration, making rollback one service configuration
change.

`leo-radio analysis-store query` opens only a published snapshot read-only. It
installs stable views such as `current_recordings`, `probes`, `confirmed_events`,
and `qualified_associations`, so ad-hoc callers do not need physical table
paths. Arbitrary SQL is an operator tool, not an HTTP endpoint.

## Reconciliation and observability

The database owner exposes a cheap status command and writes one atomic runtime
document containing:

- live schema version and DuckDB version;
- last committed run ID and commit time;
- ready, running, failed, and quarantined request counts;
- oldest request age;
- validated completion count versus current stored-run count;
- last successful reconciliation watermark;
- current and previous published generations, sizes and digests;
- snapshot age and last publication duration;
- last error with stage and recording ID; and
- whether the local live path and shared publication path passed their safety
  checks.

Normal polling is O(1). A slower reconciliation job walks bounded completion
receipt batches and compares their deterministic run IDs against
`analysis_runs`. It enqueues missing runs and reports collisions; it never uses
source file count as proof of freshness. A full reconciliation is explicit and
resumable through a stored lexicographic watermark.

Alerts:

- oldest ingest request exceeds five minutes;
- no verified snapshot has been published for fifteen minutes while commits
  are advancing;
- a source completion digest changes after ingest;
- reconciliation finds a completed run absent from the store;
- dashboard snapshot verification or opening fails; or
- live database backup verification fails.

## Testing strategy

Tests are written before each implementation slice. Production code does not
replace a JSON consumer until its parity and failure tests pass.

### Schema and mapper tests

Create minimal and full golden fixtures for `narrow`, `wide`, `oversample`, and
`hop` runs, including confirmed and strict-negative outcomes.

- every supported output schema maps to the declared tables;
- one exact check produces one `probe_checks` row and one row per actual
  receiver in `receiver_probes`;
- receiver labels, receiver indexes and calibration state remain paired;
- missing optional output produces absence, not a fabricated negative result;
- missing required fields, changed types, non-finite required numbers and
  unknown required schema versions fail before a transaction starts;
- UTC, sample indexes, channel, region, mode, radio and gain round-trip exactly;
- JSON exported from a stored run is semantically identical to its golden
  documents after canonical key ordering; and
- adding a nullable schema field and reading an older database is covered by a
  forward-compatibility fixture.

Suggested modules:

```text
tests/test_analysis_store_schema.py
tests/test_analysis_store_mapping.py
tests/test_analysis_store_roundtrip.py
```

### Transaction and idempotence tests

- ingesting the same request twice leaves every table unchanged;
- the same recording under two pipeline IDs creates two runs and one selected
  current run;
- a forged or mismatched `run_id`, output size, digest, schema or recording ID
  is rejected;
- injected failures after each child-table insert roll back the entire run;
- process termination before commit leaves no visible partial run;
- process termination after commit but before request removal converges by
  idempotent replay;
- a quarantined request does not block later requests; and
- current-run policy changes are atomic and never rewrite historical facts.

Suggested modules:

```text
tests/test_analysis_store_ingest.py
tests/test_analysis_store_transactions.py
```

### Concurrency and restart tests

Run process-level tests, not only mocks:

- sixteen producer processes enqueue distinct completed runs concurrently;
- duplicate and out-of-order requests are mixed into that load;
- only the owner process opens the live database read-write;
- the owner is killed while mapping, committing, acknowledging, reconciling
  and publishing, then restarted;
- all authenticated completions eventually appear exactly once;
- no worker waits on a DuckDB file lock in shadow mode; and
- sustained ingest does not starve snapshot publication.

Suggested module: `tests/test_analysis_store_concurrency.py`.

### Snapshot and dashboard tests

- a copy failure, short write, digest mismatch, failed read-back or failed
  read-only open leaves `current.json` unchanged;
- pointer replacement happens only after the generation is durable and valid;
- a dashboard request holding the old connection completes while a new
  generation is installed;
- the dashboard falls back to its last verified local generation when QNAP is
  unavailable or the new generation is invalid;
- cache cleanup cannot remove the open, current or rollback generation;
- the JSON and DuckDB repository implementations return semantically identical
  summary, listing, detail and activity responses for the same fixtures; and
- every dashboard detail link resolves through `artifacts`, with absent files
  reported rather than crashing the page.

Suggested modules:

```text
tests/test_analysis_store_snapshot.py
tests/test_radio_dashboard_store.py
```

### Backfill and migration tests

- backfill is resumable, bounded by `--limit`, and ordered deterministically;
- historical backfill resolves the finite contract paths for each recording ID
  into an explicit input manifest; it does not treat every matching directory
  entry as a scientific source;
- a report replacement with the same filename is detected by digest;
- removing one source and adding another cannot pass reconciliation because
  counts happen to match;
- unsupported reports are quarantined and named without stopping good runs;
- old and new probe results compare with bidirectional `EXCEPT`, not only
  aggregate counts;
- all primary and foreign key sets match their JSON reference extractors;
- dashboard API responses compare after removing only explicitly volatile
  fields such as snapshot generation time;
- dry-run performs no database, queue, pointer or source mutation; and
- rerunning after every simulated partial migration converges.

Suggested module: `tests/test_analysis_store_backfill.py`.

### Retirement and recovery tests

No structured source may be deleted merely because a database row exists. An
eventual retirement command requires all of:

- the run is committed and selected as expected;
- two verified QNAP database generations contain it;
- lossless JSON export reproduces the source documents and their SHA-256 list;
- any required immutable source bundle has been written and read back;
- evidence archive, replay and lifecycle references have migrated;
- the minimum quarantine age has elapsed; and
- the resolved deletion target is an ordinary file under the exact configured
  report roots.

Tests cover plan-only default behavior, explicit confirmation, symlinks,
unexpected files, hash changes after planning, interrupted bundle publication,
partial deletion, repeated application, and restoration from the retained
bundle. The operation writes a prepared receipt before deletion and a completed
receipt afterward.

Suggested module: `tests/test_analysis_store_retirement.py`.

### Performance and soak gates

Benchmark on a copy of the real corpus before choosing final budgets. Initial
acceptance targets are:

| Gate | Target |
|---|---|
| Analysis throughput | no more than 5% median jobs/hour regression with store enabled |
| Steady-state ingest lag | p95 below 60 seconds; no unbounded backlog with 16 workers |
| Dashboard summary/listing | p95 below 250 ms from SATPI01's local snapshot |
| Dashboard detail | p95 below 500 ms without reading report JSON |
| Common 30-day aggregate | below 1 second on the published snapshot |
| Snapshot freshness | verified generation no more than 5 minutes behind committed data |
| Snapshot publication | completes inside its cadence and never blocks DSP workers |
| Memory | store and dashboard remain inside explicit service limits during backfill and queries |
| File-count result | at least 90% fewer individual structured-result files after retirement, excluding binary evidence |

The year-scale fixture must use realistic cardinalities and row-group sizes,
not thousands of tiny placeholder partitions. A 72-hour soak mixes live ingest,
dashboard queries, snapshot publication, process restarts and reconciliation.
It must finish with identical authenticated completion and stored run-ID sets.

Performance regressions produce measurements and fail a dedicated benchmark
job; ordinary unit tests avoid fragile wall-clock assertions.

## Implementation slices

### 0. Inventory and freeze the contract

- inventory report kinds, schemas, sizes, counts and current consumers;
- extend the output contract to name and hash optional stage outputs explicitly,
  because the current completion receipt authenticates only the required set;
- record real Kalman ingest and dashboard baselines;
- define golden fixtures from redacted real outputs;
- decide the local live path and service user permissions; and
- add the repository interface without changing dashboard behavior.

Exit: every structured report kind is either mapped, intentionally retained as
an artifact, or explicitly out of scope. No unknown deletion candidates exist.

### 1. Store core and probe replacement

- add DuckDB migrations, deterministic run identity, mappers and transactions;
- ingest `recordings`, runs, summaries, probe checks and receiver probes;
- add status, query, backfill and verify CLI commands;
- run shadow ingestion from completion receipts; and
- compare `probes` row-for-row with the current Parquet projection.

Exit: a full corpus backfill and repeated reconciliation agree exactly. The old
probe index remains available for rollback.

### 2. Complete scientific schema

- add windows, follow-ups, confirmation events, tracks, decodes, associations,
  fragment diagnostics and artifact metadata;
- prove lossless semantic export for all supported output schemas; and
- add full transaction, concurrency and restart coverage.

Exit: every validated completion can be represented in one atomic run, and no
dashboard or evidence field requires an untracked source.

### 3. Snapshot publication and dashboard shadow read

- deploy the Kalman owner and immutable publisher;
- add SATPI01 snapshot caching and the DuckDB repository implementation;
- compute JSON and database dashboard responses side by side;
- publish parity counters without changing the rendered response; and
- soak under real ingest.

Exit: seven consecutive days meet correctness, lag, performance and snapshot
recovery gates with zero unexplained response differences.

### 4. Dashboard and query cutover

- switch the dashboard configuration to DuckDB;
- keep the JSON repository and old probe query command as immediate rollback;
- update analysis notebooks and operational queries to stable database views;
- monitor for another seven days; and
- retire the hourly-Parquet proposal rather than multiplying small files.

Exit: dashboard and routine analysis perform no historical JSON directory scan.

### 5. Make database commit part of completion

- move structured stage outputs into per-job Kalman staging;
- require durable store acknowledgment before a job becomes done;
- update evidence, replay, retention and recovery callers to repository/export
  interfaces; and
- retain dual output for a bounded quarantine period.

Exit: disabling permanent per-job JSON creation does not change scientific,
archive, dashboard, replay or recovery behavior.

### 6. Verified retirement

- produce lossless, verified source bundles for the audit material that policy
  requires retaining;
- dry-run the exact file set and expected reduction;
- retire individual structured JSON only behind explicit confirmation and
  prepared receipts;
- remove the old dashboard shards and probe partitions after their own rollback
  windows; and
- preserve binary artifacts and evidence according to the existing storage
  contract.

Exit: the structured-result file count falls by at least 90%, two database
generations and the source bundles verify, and a sampled restoration drill
reconstructs byte-authenticated legacy documents.

## Implementation layout

```text
src/leo_tracker/radio/analysis_store/
  schema.py            versioned DDL and compatibility checks
  identity.py          deterministic run IDs
  mapping.py           schema-specific document mappers
  ingest.py            transaction and invariant checks
  queue.py             atomic inbox, recovery and bounded backfill
  service.py           single-owner loop and runtime health
  snapshot.py          verified immutable publication
  repository.py        query and dashboard boundary

deploy/leo-tracker-analysis-index.service
docs/analysis_parquet_projection.md

tests/test_analysis_partition.py
```

The initial test implementation is kept in one cohesive module while the store
surface is small. Split it by schema, ingest, concurrency, snapshot, dashboard,
and retirement concern when those fixtures or files become independently large.

The existing `duckdb` analysis extra remains lazy: capture hosts do not import
it. The lockfile pins the tested DuckDB version, and a snapshot records both the
DuckDB library version and store schema version.

## Rollback

- Before dashboard cutover, stop the owner and discard/rebuild its local
  database; JSON behavior is unchanged.
- After dashboard cutover, point the dashboard back to
  `JsonAnalysisRepository` and restart it.
- If a new snapshot is bad, atomically point `current.json` to the previous
  verified generation; SATPI01 also retains its last verified local copy.
- Before structured-output authority changes, database ingestion is projection
  only and cannot invalidate an analysis receipt.
- During the dual-output window, turn authoritative mode off and resume the
  existing output paths.
- After retirement, restore authenticated source documents through the export
  or source-bundle recovery command before disabling database reads.

Rollback never asks an older schema implementation to modify a newer live
database. It either reads a compatible immutable snapshot or returns to the
unchanged JSON path.

## Non-goals

- Storing raw IQ or large numeric arrays inside DuckDB.
- Allowing analysis workers, SATPI01, or notebooks to write the live database.
- Opening the live native database over NFS.
- Treating row counts as source identity.
- Creating one database or Parquet fragment per recording or hour.
- Deleting evidence or structured sources during initial rollout.
- Building a general SQL-over-HTTP service before snapshot measurements show it
  is necessary.

## Definition of done

The project is complete when:

1. every authenticated production completion is present exactly once as an
   immutable database run;
2. restart and reconciliation tests prove that no completed analysis can be
   silently lost or double-counted;
3. dashboard listing, detail, activity and summary paths query a verified local
   snapshot and no longer scan historical report directories;
4. ordinary corpus analysis uses stable relational views, including the probe
   queries that motivated the first index;
5. two verified database generations and required source bundles exist on QNAP;
6. the full test, parity, fault-injection, performance and soak gates pass; and
7. individual structured-result file count has fallen by at least 90% without
   deleting raw IQ, evidence clips, plots, NPZ data, or any artifact required by
   the storage contract.
