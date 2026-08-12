# The probe index

Beacon reports are the system of record. Each one keeps every acquisition
hypothesis so a capture can be re-examined later, which makes a report durable
and a poor thing to ask questions of: counting detections per LNB across the
corpus means opening thousands of files totalling tens of gigabytes.

The probe index is a projection of the per-probe columns that questions
actually use, written as day-partitioned Parquet under
`reports/probe-index/date=YYYY-MM-DD/probes.parquet`. It is roughly five
hundred times smaller than its source and answers in milliseconds what
otherwise takes minutes. Nothing in it is authoritative — delete it and rebuild
from the reports at any time.

## Where it runs

On the analysis host, alongside the DSP. The capture host does not need it and
should not spend cycles on it.

```sh
uv pip install --python /path/to/leo-tracker/.venv 'leo-tracker[analysis]'
scripts/starlink-probe-index-sync.sh --check   /mnt/qnap01/mouse9911/leo
scripts/starlink-probe-index-sync.sh           /mnt/qnap01/mouse9911/leo
```

`duckdb` is an optional extra rather than a dependency, so that a capture host
can never fail to start because analysis tooling is absent. The cost is that
the analysis host has to ask for it; `--check` reports whether it is present
before anything else runs.

Step-by-step setup for the analysis host, including the systemd units and what
to edit in them, is in
[probe_index_kalman_setup.md](probe_index_kalman_setup.md).

## Why it is shaped this way

**One row per probe per receiver.** A dual-receiver capture is two observations
of one instant, and the whole point of the pair is that they can disagree —
that disagreement is how a mis-pointed LNB was found. Folding them would lose
it.

**The column schema is written out, not inferred.** Inference walks every
nested array in a multi-megabyte document and exhausts memory on a small host.
More importantly it is a contract: a report whose shape has drifted fails the
ingest instead of quietly yielding nulls, which read downstream as an absence
of detections rather than as a broken pipeline.

**Partitions record what they were built from**, as a signature per report —
size and mtime — not as a count. A day that gains reports is rebuilt, and so is
a day whose reports were *rewritten*: re-analysis replaces a report in place
without changing how many there are, so a count cannot see it and the day would
answer with the superseded analysis for as long as it exists. Backfills do
exactly this. A derived table that silently disagrees with its source is worse
than no derived table, because both produce plausible answers and only one is
right.

Stat rather than a digest: reports here are written once by the analysis host
and never re-copied, so nothing produces a changed mtime without changed
content. Checking a full day costs 0.23 s against a 96.5 s rebuild. The sibling
projection in `docs/analysis_parquet_projection.md` needs a second digest tier
because its receipts *are* re-copied; this one does not.

**Overlapping builders are safe.** Each stages to a file named for its host and
process and renames atomically, so correctness never rests on the advisory
lock — the index lives on an NFS mount more than one host can see, and
cross-host locking is not dependable. The lock only spares the common case the
duplicated work.

## What it cannot tell you

The index reports what the detector found, not what was in the sky. When a
receiver's search was mis-centred, its detections pile against the edge of the
search range and every statistic drawn from them inherits that bias — the
median offset for `lnb-c` reads about +349 kHz from the index against a true
+440 kHz measured by direct sweep. Questions about detector coverage have to be
answered by re-analysis, not by aggregation.

## Querying

`probes` is bound to every partition, so queries name no paths:

```sh
leo-radio starlink-probe-index query /mnt/qnap01/mouse9911/leo --sql "
  SELECT lnb, count(*) AS probes,
         round(100.0 * sum(candidate::INT) / count(*), 2) AS pct
  FROM probes
  WHERE lnb IS NOT NULL AND capture_utc > epoch(now()) - 6*3600
  GROUP BY lnb ORDER BY lnb"
```

Columns: `report`, `radio`, `serial`, `channel`, `region`, `mode`, `gain_mode`,
`capture_utc`, `calibrated`, `start_s`, `dual_candidate`, `dual_qualified`,
`cfo_difference_hz`, `rx`, `lnb`, `candidate`, `qualified`, `offset_hz`,
`epoch`, `margin`, `rms`, `near_full_scale`, `date`.
