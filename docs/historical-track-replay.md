# Historical track replay

The continuity replay reuses existing conditioned-frame observations to test
new Doppler continuity and TLE-association settings. It does not rerun beacon
acquisition or decoding and never modifies production reports or raw IQ.

The default replay is `continuity-gap15-reacq15k-v1`. Planning selects captures
that were processed with an older continuity configuration, contain at least 45
dual-receiver observations, were fragmented into at least two tracks, and have
no existing track lasting the 20 seconds required for association.

All plan inputs are checksummed. A changed or missing input fails closed. Each
worker uses an NFS advisory lock, writes into a private staging directory, and
atomically publishes a job directory only after both the track and association
artifacts are complete. Interrupted jobs can be safely retried.

Outputs live under:

```
SHARED_ROOT/reports/replays/continuity-gap15-reacq15k-v1/
  plan.json
  jobs/RECORDING_ID/track.json
  jobs/RECORDING_ID/association.json
  jobs/RECORDING_ID/completion.json
  failures/RECORDING_ID.json
```

On Kalman, after pulling the same Git commit used to create the plan:

```bash
LEO_TRACKER_REPO="$PWD" ./scripts/starlink-track-replay.sh \
  --workers 4 /mnt/qnap01/mouse9911/leo
```

The command is resumable and prints progress after every job. Four low-priority
workers are recommended while the 16-worker live-analysis service remains
active. To run it as a one-shot systemd job:

```bash
sudo systemctl link "$PWD/deploy/leo-tracker-track-replay.service"
sudo systemctl daemon-reload
sudo systemctl start leo-tracker-track-replay.service
journalctl -fu leo-tracker-track-replay.service
```

Status can be queried without starting workers:

```bash
uv run --active --no-sync python -m leo_tracker.replay_cli status \
  /mnt/qnap01/mouse9911/leo
```

This replay addresses track fragmentation only. Alternate historical timing
models must use a different replay identity and are intentionally kept out of
this experiment.
