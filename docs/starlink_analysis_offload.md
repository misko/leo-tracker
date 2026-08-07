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
partial capture as ready or a partial analysis as complete. The Pi raw copy is
removed only after remote verification and durable queue publication.

The acquisition-side exporter is:

```bash
scripts/starlink-analysis-export.sh \
  /mnt/leo-nvme/leo-tracker /mnt/qnap01/mouse9911/leo
```

It defaults to 20 MB/s (`LEO_OFFLOAD_BWLIMIT_KBPS=20000`) so transfer traffic
does not monopolize Wi-Fi. It uses atomic `.job -> .exporting` claims and can
run alongside the local ordinary and hop workers without analyzing a capture
twice.

On an analysis server, clone this repository and create its `.venv` with UV.
Only the base dependencies are required for production analysis; the `radio`
extra is for direct SDR/storage integrations and is not needed by this worker.

```bash
cd /path/to/leo-tracker
uv venv .venv
uv sync --frozen
```

Use `uv sync --frozen --extra dev` instead when the server will also run the
test suite. Then point the worker at the server's local path for the shared
directory:

```bash
cd /path/to/leo-tracker
LEO_TRACKER_REPO="$PWD" \
  scripts/starlink-analysis-server.sh --workers 2 /path/to/mouse9911/leo
```

Choose the worker count from the server's resources. Each DSP process can use
multiple CPU cores and roughly 0.8 GB of RAM, so two is a conservative default
for an eight-core host. `--once` drains the current queue and exits; without it,
the script monitors continuously. Outputs and per-job logs appear under
`leo/reports`; successful and failed queue records are retained under
`leo/staging/analysis-queue/{done,failed}` for audit and retry.

Each worker performs sparse beacon acquisition and plotting, dense follow-up,
conditioned frame tracking, symbol decoding for applicable captures, continuous
Doppler tracking, and TLE association. The exporter snapshots the current TLE
catalog, pass predictions, and learned beacon into `leo/context`.
