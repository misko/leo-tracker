# The analysis Parquet projection

The analysis store projects authenticated Kalman analysis outputs into
date-partitioned Parquet on QNAP, queried in place by DuckDB. It replaces the
single live `analysis.duckdb` file and the snapshot publication machinery built
around it.

Nothing here is authoritative. Reports remain the system of record; a partition
can be deleted and rebuilt from the completion receipts at any time.

## Why this shape

The first implementation kept one live DuckDB file on Kalman and published
whole-file snapshots to QNAP. Two measurements ended that design.

**The file was 13.6x larger than its own data.** One hundred runs occupied
1.79 GB, of which `PRAGMA database_size` reported 3,387 free blocks against
3,436 used — half the file was slack. The same rows exported to ZSTD Parquet
came to 131 MB. The cause is the ingest pattern: one transaction per run across
roughly twenty tables, so every commit lays down fresh 256 KB blocks holding a
handful of rows, and row groups never grow large enough to compress. Projected
across the corpus that is 400 GB of file for 32 GB of data.

**Whole-file publication does not scale to any of those numbers.** Each
generation copied the entire database to Kalman *and* QNAP, with a third copy
accumulating on SATPI01, and nothing pruned any of them.

Parquet removes both problems rather than tuning them. Column compression makes
the size honest, and immutable files make publication unnecessary — there is no
generation to copy, verify, point at, cache, or prune, because readers open the
partitions directly.

It is also the safe way to put this data on QNAP at all. The share is
`nfs4,soft,local_lock=none`: a soft mount fails I/O with an error rather than
blocking, and lock state is server-mediated and lease-based. A live read-write
database file on that mount can be corrupted by an ordinary network blip. Files
that are written once and never modified have neither exposure. This is the
same conclusion `probe_index.md` reached for the same mount — "the index lives
on an NFS mount more than one host can see, and cross-host locking is not
dependable" — and this projection deliberately reuses its mechanics.

## Layout

Partitioned by pipeline, then by the recording's UTC date:

```text
reports/analysis-index/
  pipeline=kalman-full-v1/
    date=2026-08-11/
      analysis_runs.parquet
      recordings.parquet
      probe_checks.parquet
      receiver_probes.parquet
      followup_checks.parquet
      tracks.parquet
      track_points.parquet
      decodes.parquet
      associations.parquet
      dashboard_records.parquet
      partition.json
  pipeline=legacy-v1/
    date=2026-08-10/
      ...
```

The date comes from the recording stamp, the same rule `probe_index.py` uses, so
a recording lands in the same date in both projections.

**Table-per-file within a partition, not table-major above it**, because the
rebuild unit is one `(pipeline, date)`. All tables for that unit are rebuilt
together from one set of receipts, so a partition is internally consistent by
construction: there is no window in which `probe_checks` reflects a newer set of
runs than `analysis_runs` does. That property is what replaces the foreign keys
and cross-table transactions the DuckDB schema enforced.

**Pipeline is a partition level** because the corpus holds two pipelines of
near-equal size covering overlapping dates — 12,157 `kalman-full-v1` runs and
12,417 frozen `legacy-v1` runs across the same eight days. Without the pipeline
level, every rebuild triggered by a new Kalman run would rewrite 12k frozen
legacy rows for no reason, forever. With it, `legacy-v1` is built once and never
touched again, and the rebuild unit halves from roughly 2.5 GB to 1.2 GB.

This is a deliberate departure from the probe index's flat `date=` layout,
justified by that corpus shape rather than by preference.

## Build mechanics

Each build stages every table to a file named for its host and process, renames
each atomically, and writes `partition.json` **last**:

```text
probe_checks.parquet.kalman.1512034.next   ->  probe_checks.parquet
...
partition.json                                  (commit marker, written last)
```

The manifest landing is what makes a partition current. A build interrupted
partway leaves a stale manifest beside newer data files, which the currency
check treats as not-current and rebuilds — never as a silent partial answer.

`partition.json` records the schema version and the source set: each
contributing `recording_id`, its pipeline, and the SHA-256 of its
`completion.json`. A partition is current when that recorded set equals what the
receipts say today.

The scan that decides this is two-tier, because digesting the corpus costs about
280 seconds against 0.1 for `stat`, and a timer cannot spend five minutes
deciding it has nothing to do. The manifest therefore also records a cheap
signature per receipt — size and modification time. Matching signatures settle
currency outright with no reads; a moved signature is confirmed against the
recorded digest before anything is rebuilt, so a receipt merely re-copied or
touched does not provoke a rebuild of its whole partition, while one genuinely
rewritten does. Measured on the live corpus, this took a full status scan from
4m51s to 1m33s, the remainder being the directory walk itself.

`partition.json` also records, per table, any column that arrived null, omitting
those that did not. This corpus needs it: rebuilding
`pipeline=kalman-full-v1/date=2026-08-05` reports

```json
"recordings":      {"radio_id": 12, "radio_serial": 12},
"receiver_probes": {"receiver_label": 1376, "rms_magnitude": 1376,
                    "near_full_scale_fraction": 1376}
```

which is the schema drift below, stated by the partition rather than discovered
in a query months later. A date that gains a run, or whose run is re-analysed, is
rebuilt rather than answered from stale Parquet — the rule `probe_index.md`
states as "a derived table that silently disagrees with its source is worse than
no derived table, because both produce plausible answers and only one is right".

Overlapping builders stay safe without a lock, because staging names are unique
per host and process and the rename is atomic. An advisory lock is worth keeping
only to spare the duplicated work, never for correctness.

Column schemas are written out explicitly, not inferred, for the reason the
probe index gives: inference walks every nested array in a multi-megabyte
document, and more importantly a written schema is a contract — a report whose
shape has drifted fails the build instead of quietly yielding nulls that read
downstream as an absence of detections.

## Query surface

Views over `read_parquet`, registered on a fresh in-memory DuckDB. No file is
copied and no lock is taken, so any number of readers on any host may query
concurrently while a builder writes:

```sql
CREATE VIEW probe_checks AS
SELECT * FROM read_parquet(
  '/mnt/qnap01/mouse9911/leo/reports/analysis-index/date=*/probe_checks.parquet',
  hive_partitioning = true, union_by_name = true);
```

`union_by_name` lets an older partition lack a column a newer one gained, which
is how schema evolution works without rewriting history. Adding a column is
backward compatible; changing a column's meaning requires a new schema version
and a full rebuild.

The derived views carry over unchanged, because they are ordinary SQL:

```sql
CREATE VIEW current_runs AS
SELECT * EXCLUDE (_rank) FROM (
  SELECT analysis_runs.*, row_number() OVER (
    PARTITION BY recording_id ORDER BY completed_utc DESC, run_id DESC) AS _rank
  FROM analysis_runs) WHERE _rank = 1;
```

Hive partitioning means a query filtered to a date range reads only those
partitions, so the dashboard's listing query touches one or two files rather
than the corpus. `pipeline` and `date` are both available as ordinary columns
for filtering.

### Payloads are referenced, not copied

`structured_documents.payload_json` is not projected. It was 50 MB of the
131 MB — 38% of the projection — and a second copy of JSON that already exists
on QNAP as the report files. `source_documents` records each one by path, size,
and SHA-256, and DuckDB reads them in place when a query actually wants them:

```sql
CREATE VIEW payloads AS
SELECT s.run_id, s.kind, j.*
FROM source_documents s, read_json(s.path) j;
```

So SQL access to payloads survives without duplicating them, and there is no
second copy to go stale when a report is rewritten. Payload queries pay an NFS
read per file, which is the correct place for that cost: rare queries pay it,
and every other query and every rebuild does not.

One constraint to know before writing that view. DuckDB will **not** correlate a
table function to a column per row:

```sql
-- Binder Error: read_json does not support lateral join column parameters
SELECT * FROM source_documents s, read_json(s.path) j;
```

Payload access is therefore either a two-step lookup — read the path, then
`read_json` that literal — or a glob joined on the filename, which is the form
to use for a set of runs and is the one verified above:

```sql
SELECT s.run_id, s.kind, j.*
FROM source_documents s
JOIN read_json('/mnt/qnap01/mouse9911/leo/reports/*.json',
               filename = true, union_by_name = true) j
  ON j.filename = s.path
WHERE s.kind = 'analysis';
```

Narrow the glob to the runs of interest; unbounded, it reads the whole 35 GB
reports tree. This is the real ergonomic cost of referencing rather than
copying, and it is why the choice is worth recording rather than assuming.

## Validated against the ingested runs

Built from the 100 runs already in the DuckDB store, using the staging and
atomic-rename mechanics described above (two date partitions, five tables,
built in 1.0 s). Every query was run against both the source database and the
`read_parquet` views:

| Check | DuckDB | Parquet |
|---|---|---|
| `count(analysis_runs)` | 100 | 100 |
| `count(recordings)` | 100 | 100 |
| `count(probe_checks)` | 12,000 | 12,000 |
| `count(receiver_probes)` | 24,000 | 24,000 |
| `count(track_points)` | 38,620 | 38,620 |
| `count(current_runs)` | 100 | 100 |
| probes per `receiver_label` | 6 rows | 6 rows |
| qualified probes | 211 | 211 |
| distinct recordings | 100 | 100 |

All identical. `current_runs` — the one view with non-trivial window-function
semantics — carries over unchanged apart from excluding the injected `date`
partition column.

Partition pruning confirmed: `EXPLAIN` on a query filtered to one date reports
`Scanning Files: 1/2`, so a date-scoped dashboard query reads a single
partition rather than the corpus.

## Built against the live corpus

One real partition — `pipeline=kalman-full-v1/date=2026-08-06`, 82 receipts —
built from QNAP through the implementation: all seventeen tables written, 5,010
probe checks, 10,020 receiver probes, and zero orphaned child rows.

It took **119 s for 82 runs**, about 1.45 s each, nearly all of it reading and
digesting each run's documents over NFS. The full corpus is therefore roughly a
ten-hour backfill. It is resumable and idempotent, so that is a wall-clock cost
rather than a risk, but it wants a `tmux` and not an afternoon.

### Parity against the probe index

The two projections are derived from the same reports by different code, so
agreement between them is real evidence rather than a self-check. Comparing the
`probes` view over this projection with `reports/probe-index`, per probe per
receiver, across the six partitions built so far:

| date | shared recordings | rows compared | index→probe | probe→index |
|---|---|---|---|---|
| 2026-08-05 | 8 | 1,290 | 0 | 0 |
| 2026-08-06 | 51 | 9,810 | 0 | 0 |
| 2026-08-07 | 217 | 51,930 | 0 | 0 |
| 2026-08-08 | 234 | 56,130 | 0 | 0 |
| 2026-08-09 | 528 | 126,720 | 0 | 0 |
| 2026-08-10 | 630 | 148,848 | 0 | 0 |
| **total** | **1,668** | **394,728** | **0** | **0** |

Bidirectional `EXCEPT` on `(report, start_s, rx, candidate, qualified,
cfo_difference_hz, frequency_offset_hz)`, floats rounded to 3 dp. Every shared
probe is identical.

Two alignments are needed before the comparison means anything, and both are
differences of convention rather than of fact: the probe index keeps the `.json`
extension in `report` where this projection uses the bare `recording_id`, and it
indexes only `narrow` mode where this one carries wide, channel-hop and
oversample too.

### Coverage differs by construction, and it is worth knowing how

Parity holds on shared recordings; the two do not share the same *set*. The
probe index is report-driven — it globs `reports/*narrow*.json`. This projection
is receipt-driven, and deliberately "never discovers scientific inputs by
globbing a directory". Where a report has no completion receipt, it is invisible
here and visible there:

| date | narrow reports | with a receipt | without |
|---|---|---|---|
| 2026-08-05 | 355 | 8 | **347** |
| 2026-08-06 | 491 | 155 | **336** |
| 2026-08-07 | 534 | 391 | **143** |
| 2026-08-08 onward | 3,142 | 3,142 | 0 |
| **total** | **4,522** | **3,696** | **826 (18%)** |

Receipts came into use around 2026-08-08; before that, reports were produced
without them. So 826 recordings on the first three days can never appear in this
projection, and no rebuild will change that — the authentication those runs would
need does not exist. It is the intended trade, but it means the probe index
remains the more complete answer for that window.

The one gap on 2026-08-10 is different in kind and fully accounted for: a single
recording, and `partition.json` names it in `excluded` with its reason.

### The corpus changed shape mid-flight

That build returned `receiver_label` as NULL for every row, which looked like a
projection bug and is not. Sampling 25 runs per date shows receiver labels were
introduced around 2026-08-10:

| Dates | `identity.receiver_labels` |
|---|---|
| 2026-08-05 → 08-09 | absent |
| 2026-08-10 | mixed — the transition |
| 2026-08-11 → 08-12 | `[lnb-a, lnb-b]` and `[lnb-c, lnb-d]` |

So roughly the older half of the corpus carries no labels at all, and a
per-LNB query spanning the whole corpus silently mixes labelled captures with
unlabelled ones. The projection is faithful here — the reports are the system of
record and those reports genuinely lack the field.

This is precisely the hazard `probe_index.md` names: nulls "read downstream as
an absence of detections rather than as a broken pipeline". The difference is
that a *missing optional identity field* currently yields NULL rather than
failing the build, while a *structurally broken report* does fail it. Whether
that boundary is in the right place is a live question — the alternative is
recording per-column null counts in `partition.json` so the drift is visible in
the manifest rather than discovered in a query months later.

## What this removes

The whole publication subsystem becomes unnecessary, and with it the blocker
that stopped gate 4:

- no whole-file snapshots on Kalman or QNAP, and no third copy on SATPI01;
- no `current.json` pointer, generation receipts, or SHA-256 read-back verify;
- no snapshot retention or prune tooling;
- no `/var/cache/leo-tracker-analysis-store` on the dashboard host;
- no DuckDB version parity requirement between Kalman and SATPI01, because
  Parquet is a stable format and neither host opens the other's database file;
- no single-writer owner lock, no `inbox/running/failed` queue, and no
  quarantine of failed manifests — a build either produces a current partition
  or leaves the previous one in place.

## What it costs

**No cross-table transactions and no foreign keys.** The DuckDB schema declared
`REFERENCES analysis_runs(run_id)` throughout; Parquet cannot enforce that.
Consistency comes instead from the rebuild unit: a date's tables are always
written from one set of receipts. Referential checks become a validation query
run against a built partition, not a constraint enforced per row.

**No incremental single-run append.** A new run rebuilds its whole date
partition. At roughly 720 runs a day that is a bounded rebuild, and it is the
same trade the probe index already makes.

**Deletes and corrections are partition-scoped.** Retracting one run means
rebuilding its date, not issuing a `DELETE`.

## Sizing, measured

From the 100 runs already ingested, exported to ZSTD Parquet:

| | Per run | Corpus (24,563 runs) |
|---|---|---|
| DuckDB file as written | 17.89 MB | ~400 GB |
| Parquet, all tables | 1.31 MB | 32 GB |
| Parquet, payloads referenced (estimate) | 0.82 MB | 20 GB |
| **Measured on real partitions** | **0.35 MB** | **~9 GB** |

The last row is what the implementation actually writes, from building
`date=2026-08-05` and `date=2026-08-06` off the live corpus. It beats the
estimate because dropping the payloads also removed the column that compressed
worst, leaving relational rows that ZSTD handles far better than JSON text.

Ongoing growth at the observed ~720 runs/day is roughly 0.6 GB/day, about
215 GB/year against 1.3 TB free on QNAP. The historical corpus is denser than
that — 24,563 runs across eight days — because it includes bulk reprocessing;
partitions of that vintage run nearer 1.2 GB per pipeline-date.

## Migration from where we are

Gate 1 deployed the DuckDB owner; it is stopped and disabled, holding 100
ingested runs at `/var/lib/leo-tracker-analysis-store/live/analysis.duckdb`.
Nothing depends on it: the analysis server was never told about it (gate 3 was
not run), so no completion path references the store.

1. Keep the mapping layer. `analysis_store/mapping.py` already turns a
   completion receipt into typed relational rows; that work is independent of
   where the rows land and is the expensive part to get right.
2. Replace `ingest.py`/`queue.py`/`snapshot.py` with a partition builder that
   groups receipts by recording date and writes the tables for one date.
3. Reuse `probe_index.py`'s staging, rename, manifest, and currency check
   rather than writing new ones.
4. Build the corpus date by date. Idempotent and interruptible; no queue, no
   cursor, no owner lock.
5. Point the dashboard at `read_parquet` views instead of a cached snapshot.
6. Delete `/var/lib/leo-tracker-analysis-store` once the projection answers the
   parity queries. It is rebuildable and holds nothing authoritative.

The existing 100-run database is worth keeping only until step 4 reproduces its
rows, at which point it is a 1.79 GB file with no readers.

## How it runs

`deploy/leo-tracker-analysis-index.{service,timer}` on Kalman, matching
`leo-tracker-probe-index.timer`. Each firing walks the receipts, groups them by
`(pipeline, date)`, and rebuilds only those partitions whose recorded source set
no longer matches what the receipts say.

Half-hourly rather than the probe index's quarter-hour, because deciding there
is nothing to do costs about ninety seconds here — nearly all of it walking
twenty-four thousand receipt directories over NFS. At fifteen minutes that is a
tenth of the host's wall clock spent asking a question whose answer is almost
always no.

The service carries no `[Install]` section: the timer activates it. Enabling the
service directly would run one build at boot and then never again, which looks
like a working installation right up until someone asks why the projection is a
day behind.

`CacheDirectory=` gives the build a local scratch directory, and this matters
more than it looks. Each partition is assembled in a throwaway DuckDB so the
schema still enforces types and foreign keys, and that database commits once per
run — so it carries the same block slack the retired live store did, about
**9 MB per run against 0.35 MB of Parquet out**. The largest partition here is
3,785 runs, so a build wants tens of gigabytes of scratch and must never be
pointed at a tmpfs. It is thrown away either way.

The unit also runs `Nice=10` and `IOSchedulingClass=idle`: sixteen DSP workers
on that host are doing work that matters more than a projection which is
explicitly not authoritative.

Not triggered per completion. Because the rebuild unit is a whole partition,
completion-triggered builds would rewrite the same ~1.2 GB partition hundreds of
times a day and couple analysis throughput to projection writes. The timer's
cost is bounded by the number of drifted partitions, and its only downside is up
to one interval of staleness in a projection that is explicitly not
authoritative.

If the drift scan itself becomes the expense, the refinement is a dirty marker
written by the analysis server naming the affected `(pipeline, date)`, letting
the builder skip the scan. That is an optimisation to add on evidence, not up
front — a lost marker means a stale partition, so the drift scan stays the
backstop either way.

## Decisions on record

| Decision | Choice | Why |
|---|---|---|
| Embedded payloads | Referenced, not copied | 38% of the projection, duplicating authoritative reports; `read_json` keeps SQL access |
| Partition key | `pipeline=` then `date=` | Two near-equal pipelines share dates; keeps frozen `legacy-v1` from being rewritten forever |
| Rebuild trigger | Timer with drift check | Rebuild unit is a partition; per-completion triggering rewrites it hundreds of times a day |
