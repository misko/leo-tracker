# Running the probe index on the analysis host

The capture host no longer builds the index; its timer is disabled. Everything
below runs on the analysis host (kalman), which already mounts the shared
storage and performs every DSP stage.

Paths below assume the repo at `~/leo-tracker` and shared storage at
`/mnt/qnap01/mouse9911/leo`. Substitute if they differ — the script takes the
root as an argument and honours `LEO_TRACKER_REPO` and `LEO_OFFLOAD_ROOT`.

## 1. Get the code

```sh
cd ~/leo-tracker
git pull            # needs the commits through "Give the analysis host one
                    # command to convert reports for DuckDB"
```

## 2. Install the analysis extra

`duckdb` is deliberately optional, so that a capture host cannot fail to start
because analysis tooling is absent. The analysis host has to ask for it:

```sh
uv pip install --python ~/leo-tracker/.venv 'duckdb>=1.0'
# or, without uv:
~/leo-tracker/.venv/bin/pip install 'duckdb>=1.0'
```

## 3. Check the host is ready

```sh
~/leo-tracker/scripts/starlink-probe-index-sync.sh --check /mnt/qnap01/mouse9911/leo
```

Expect `{"root":"...","reports":<n>,"duckdb":true}`. The two failures it
distinguishes:

- exit **2**, "is shared storage mounted?" — the reports directory is not
  there. Converting against an absent directory would yield an empty index,
  which reads as a night with no detections rather than a missing filesystem.
- exit **3** — duckdb is missing; the message names the install command.

## 4. Convert

```sh
~/leo-tracker/scripts/starlink-probe-index-sync.sh /mnt/qnap01/mouse9911/leo
```

Only days whose report count has moved are rebuilt, so the first run converts
whatever is outstanding and later runs touch the current day alone. As of the
handover all days through 2026-08-11 are built and current; only the open day
needs rebuilding.

Cost measured on the Pi, which is the slower machine: a full day of about 1,200
reports takes roughly two minutes and peaks at 344 MB of RSS against a 1,200 MB
internal cap. Whole-corpus backfill of seven days was about three minutes.

## 5. Keep it fresh

```sh
sudo cp ~/leo-tracker/deploy/systemd/leo-tracker-probe-index.{service,timer} \
        /etc/systemd/system/
sudoedit /etc/systemd/system/leo-tracker-probe-index.service   # see below
sudo systemctl daemon-reload
sudo systemctl enable --now leo-tracker-probe-index.timer
```

The unit as committed carries the capture host's paths and user. Before
enabling, set on the analysis host:

- `User=` to the account that owns the venv
- `WorkingDirectory=` and the `ExecStart=` script path to the repo location
- `ConditionPathIsDirectory=` to the shared reports directory

Enable the **timer**, not the service: the service has no `[Install]` section
because the timer activates it, and enabling the service alone would convert
once at boot and never again.

Verify:

```sh
systemctl list-timers leo-tracker-probe-index.timer
journalctl -u leo-tracker-probe-index.service -n 20
```

A healthy run prints JSON ending `"skipped_locked": false`. A run that finds
another already working prints `"skipped_locked": true` and exits 0 — that is
not a failure; the refresh period is fixed while a conversion takes as long as
the day is full, so overlap is ordinary.

## 6. Query

```sh
~/leo-tracker/.venv/bin/leo-radio starlink-probe-index query \
  /mnt/qnap01/mouse9911/leo --sql "
    SELECT lnb, count(*) AS probes,
           round(100.0 * sum(candidate::INT) / count(*), 2) AS pct
    FROM probes
    WHERE lnb IS NOT NULL AND capture_utc > epoch(now()) - 6*3600
    GROUP BY lnb ORDER BY lnb"
```

See [probe_index.md](probe_index.md) for the column list and for what the index
cannot answer.

## If both hosts ever run it

Safe but redundant. Each builder stages to a file named for its host and
process and renames atomically, so correctness never rests on the advisory
lock — the index sits on an NFS mount more than one host can see, and
cross-host locking is not dependable.

## Rolling back

The index is a projection, never authoritative. Delete
`/mnt/qnap01/mouse9911/leo/reports/probe-index` and rebuild from the reports at
any time; nothing else reads it.
