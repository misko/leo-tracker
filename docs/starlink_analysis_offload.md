# Starlink analysis offload

Acquisition and historical analysis use separate durable queues. The protocol
has four atomic boundaries:

1. The Pi copies a completed capture into `staging/incoming/*.partial`, checks
   every manifest byte size, atomically renames it into `captures`, and only
   then publishes its `.job` marker. Set `LEO_OFFLOAD_VERIFY_SHA256=1` to also
   recompute every IQ chunk checksum across NFS.
2. TLEs, passes, and the learned-beacon JSON **and NPZ dependency** are copied
   into an immutable, checksummed `context/bundles/<id>` directory. Job paths
   are share-root relative, allowing different mount points on the two hosts.
3. One server process owns `server.lock`; its workers claim disjoint jobs using
   atomic `.job -> .running.<worker>` renames. On restart, interrupted claims
   return to the ready queue.
4. A job moves to `done` only after every required output exists, parses, has
   the expected schema, and an atomic receipt is written. Startup audit requeues
   legacy or partial `done` entries. Persistent errors move to `failed` without
   blocking other work.

Thus neither a network loss, process crash, nor server reboot can expose a
partial capture as ready or a partial analysis as complete. Source handling is
controlled by `LEO_OFFLOAD_SOURCE_POLICY`. The deployed `retain` policy leaves
the Pi raw directory untouched after remote verification and durable queue
publication. The legacy `delete` policy is explicit and must not be used during
the preservation evaluation.

The acquisition-side exporter is:

```bash
scripts/starlink-analysis-export.sh \
  /mnt/leo-nvme/leo-tracker /mnt/qnap01/mouse9911/leo
```

It defaults to 20 MB/s (`LEO_OFFLOAD_BWLIMIT_KBPS=20000`) so transfer traffic
does not monopolize Wi-Fi. It uses atomic `.job -> .exporting` claims and can
run alongside the local ordinary and hop workers without analyzing a capture
twice.

On the analysis server, use the repository's existing `.venv` exclusively.
Only the base dependencies are required for production analysis; the `radio`
extra is for direct SDR/storage integrations and is not needed by this worker.

```bash
cd /path/to/leo-tracker
test -x .venv/bin/python
uv run --active --no-sync python --version
```

Dependency changes are installed deliberately with `uv sync --active --frozen`
from the checkout; normal service launches use `--no-sync`. Point the worker at
the server's local path for the shared directory:

```bash
cd /path/to/leo-tracker
LEO_TRACKER_REPO="$PWD" \
  scripts/starlink-analysis-server.sh --workers 16 /mnt/qnap01/mouse9911/leo
```

Progress is reported on the heartbeat, not per job. Each worker emits its own
`job_start`/`job_done` line and claims again immediately; the monitor emits the
aggregate `progress` line and writes `reports/analysis-server-status.json`. Job
completion therefore costs the same whether the run holds ten jobs or a hundred
thousand. Two figures in that report are deliberately not exact:

- `average_job` and `eta` come from the last `LEO_ANALYSIS_DURATION_WINDOW`
  jobs (200 by default), recorded in `analysis-queue/metrics/recent-durations.tsv`.
  Recent jobs also predict a remaining queue better than a lifetime mean spanning
  configuration changes. The per-job `metrics/<job>.tsv` records are still
  written and remain the forensic record.
- `versioned_completion_count` stats one receipt per completed run, so it is
  refreshed only on the slow periodic loop (`LEO_ANALYSIS_BACKFILL_INTERVAL_S`,
  600s by default) and reads `null` in reports written between refreshes. A
  `deep_status` log line marks each refresh.

Neither figure gates correctness; the queue directories remain the source of
truth for what ran. Recomputing them per job previously cost more wall clock
than the analysis itself and left most workers idle.

Choose the worker count from the server's resources. Kalman is deployed with 16
workers and BLAS thread counts pinned to one. `--once` drains the current queue
and exits; without it, the script monitors continuously. Outputs and per-job
logs appear under `leo/reports`; successful and failed queue records are retained under
`leo/staging/analysis-queue/{done,failed}` for audit and retry.

Each worker performs sparse beacon acquisition and plotting, dense follow-up,
conditioned frame tracking, symbol decoding for applicable captures, continuous
Doppler tracking, TLE association, and cropped evidence publication. A valid
conditioned-frame artifact that yields no compatible dual-RX group produces an
explicit zero-track result rather than failing the job. The exporter snapshots
the current TLE catalog, pass predictions, learned beacon JSON, and its NPZ
dependency into `leo/context`. A learned template is an optional detector: the
worker uses it only when it is qualified and its sample rate and edge region
match the capture. An incompatible template is logged and the worker falls back
to the published pilot detector, so a lower-edge context cannot strand valid
upper-edge history.

Archive `shadow` mode is not a completeness guarantee. Use the receipt and
cross-store audits in [`STORAGE.md`](STORAGE.md) and
[`KALMAN_MIGRATION.md`](KALMAN_MIGRATION.md) before describing the historical
archive as complete or enabling retention.
