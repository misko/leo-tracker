# Historical TLE service

Kalman runs one long-lived service that retrieves independent Starlink element
catalogs and publishes them atomically under `/mnt/qnap01/mouse9911/tle`.
Scheduling is internal: Space-Track runs hourly at minute 37 UTC, while the
Hugging Face mirror runs every six hours. There are no competing systemd
timers.

The service never overwrites history. Raw responses, normalized validated
catalogs, retrieval manifests, per-source indexes, and latest pointers are
stored separately:

```text
/mnt/qnap01/mouse9911/tle/
  raw/<source>/<response-sha256>.tle
  objects/<catalog-sha256>.json
  snapshots/<source>/<scope>/YYYY/MM/DD/<retrieval>-<hash>.json
  indexes/<source>/<scope>.json
  latest/<source>/<scope>.json
  state/<profile>.json
  status/<profile>.json
```

Writes use a temporary file, `fsync`, and atomic rename. A single publisher
lock protects indexes and a watcher lock prevents two daemons from issuing the
same request. Content hashes, TLE checksums, object counts, unique NORAD IDs,
scope, and newest epoch age are validated before `latest` changes. A failed
source leaves its prior catalog intact and does not stop the other source.

## Space-Track request discipline

The configured GP query obtains every currently propagable object whose name
matches Starlink in one response. It includes Space-Track's recommended
`decay_date/null-val/epoch/>now-10` filters and runs once per hour, away from
the top-of-hour boundary. Each attempt performs one login, one catalog query,
and one logout. Credentials are accepted only for the exact HTTPS hostname
`www.space-track.org`; they are never put in argv, paths, or logs.

Space-Track accounts are personal. Basic SSA data may be redistributed subject
to the site's attribution requirements; reports derived from these catalogs
must cite Space-Track.org.

Create the external credential file:

```bash
sudo install -d -m 0755 /etc/leo-tracker
sudo install -m 0600 /dev/null /etc/leo-tracker/space-track.env
sudoedit /etc/leo-tracker/space-track.env
```

Its contents are:

```text
LEO_SPACETRACK_IDENTITY=your-account-email
LEO_SPACETRACK_PASSWORD=your-password
```

Validate configuration without network access:

```bash
uv run --active --no-sync leo-orbit catalog-watch \
  --config deploy/tle-sources.json \
  --root /mnt/qnap01/mouse9911/tle --check
```

## Install on Kalman

Remove the superseded one-shot timer if it was previously installed, then link
the single daemon unit:

```bash
sudo systemctl disable --now leo-tracker-tle-archive.timer 2>/dev/null || true
sudo systemctl link "$PWD/deploy/leo-tracker-tle-watch.service"
sudo systemctl daemon-reload
sudo systemctl enable --now leo-tracker-tle-watch.service
systemctl status leo-tracker-tle-watch.service --no-pager -l
journalctl -u leo-tracker-tle-watch.service -f
```

`SIGTERM`/Ctrl-C shuts it down cleanly. `SIGHUP` reloads the configuration.
Systemd restarts unexpected failures, while persisted next-due times prevent a
restart from violating the Space-Track hourly limit.

Operational queries do not use the network:

```bash
uv run --active --no-sync leo-orbit catalog-status \
  --root /mnt/qnap01/mouse9911/tle
uv run --active --no-sync leo-orbit catalog-latest \
  --root /mnt/qnap01/mouse9911/tle --source space-track --scope starlink
uv run --active --no-sync leo-orbit catalog-history \
  --root /mnt/qnap01/mouse9911/tle --source huggingface --scope starlink
```

## Python interface

Other analysis code should use `CatalogStore`; it returns the project's native
`TLE` objects and can create `sgp4.api.Satrec` objects directly:

```python
from datetime import datetime, timezone
from leo_tracker.orbit import CatalogStore

store = CatalogStore.open("/mnt/qnap01/mouse9911/tle")
latest = store.latest(source="space-track", scope="starlink",
                      maximum_age_s=7200)
satellite = latest.by_norad(44713)
sgp4_satellites = latest.to_satrecs()

# Reproduce what was knowable during an old capture (no future-data leakage).
historic = store.at_time(
    datetime(2026, 8, 7, tzinfo=timezone.utc),
    source="space-track", scope="starlink", knowledge="available_then")

# Retrospective best-ephemeris analysis may intentionally use later retrievals.
best = store.at_time(
    datetime(2026, 8, 7, tzinfo=timezone.utc),
    source="space-track", scope="starlink", knowledge="best_ephemeris")
```

Source and scope are explicit, so adding another provider cannot silently
change which ephemeris an analysis uses.
