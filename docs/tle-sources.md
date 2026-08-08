# TLE sources

Orbit prediction error is a measured term in this experiment, not a background
assumption. Doppler association fits an epoch adjustment in seconds, so
along-track ephemeris error competes directly with the receiver clock terms the
milestone is trying to isolate.

## Why Space-Track

`celestrak.org` resolves from the acquisition host but its TCP connect fails:
the host was blocked after hammering it. A HuggingFace mirror was substituted
in a systemd unit that lived only on the host, so production silently diverged
from this repository's Celestrak default and nothing recorded the decision.

Space-Track is reachable, is the authoritative origin rather than a
redistribution, and publishes operator-supplied supplemental elements for
Starlink that account for planned manoeuvres. That last point matters most
here: measured error on this source is dominated by manoeuvres, not by
propagation.

## Rate discipline

Space-Track's published rules, retrieved 2026-08-08:

- fewer than 30 requests per minute and 300 per hour overall;
- **GP class: at most one query per hour**, with query timing randomized and
  the query filtered to propagable objects;
- do not issue hundreds of per-object queries; combine them into one;
- accounts are suspended for querying too often or for queries that degrade the
  site, and repeat offenders are suspended permanently;
- using multiple accounts to work around the limits is prohibited.

The archive timer queries four times a day with `RandomizedDelaySec=15m`, which
satisfies the hourly GP limit and the randomization request, and is already
faster than the catalog changes — the previous source published 1.09 new
catalogs per day across a measured week, with gaps between 11.8 and 39.8 hours.
Each invocation performs exactly one login, one query, and one logout.

Do not shorten the timer, and do not add ad-hoc fetch loops. This host is
already blocked by Celestrak; losing the authoritative source would be worse.

## Redistribution and citation

USSPACECOM grants blanket approval to redistribute basic SSA data — TLEs, OMMs,
SATCAT and decay data — "conditioned on appropriate citation", and states that
"publications of analysis based on USSPACECOM data also require appropriate
citations". Archiving catalogs here and carrying them in evidence bundles is
therefore permitted, but any published report or artifact derived from them
must cite Space-Track.org as the source.

Accounts are personal: the agreement forbids sharing or transferring a username
or password, and requires a separate account per individual or entity. The
credentials file is root-owned and readable only by systemd, which reads it
before dropping to the service user.

## Credentials

Credentials are read from the environment, never from argv, so they appear in
neither a process listing nor a service log. Create the file the unit reads:

```bash
sudo install -d -m 0755 /etc/leo-tracker
sudo install -m 0600 /dev/null /etc/leo-tracker/space-track.env
sudo tee /etc/leo-tracker/space-track.env >/dev/null <<'ENV'
LEO_SPACETRACK_IDENTITY=you@example.com
LEO_SPACETRACK_PASSWORD=your-password
ENV
```

Then install the unit and timer:

```bash
sudo systemctl link "$PWD/deploy/leo-tracker-tle-archive.service"
sudo systemctl link "$PWD/deploy/leo-tracker-tle-archive.timer"
sudo systemctl daemon-reload
sudo systemctl enable --now leo-tracker-tle-archive.timer
```

## Measuring a source rather than trusting it

Published accuracy claims are not a substitute for measuring the elements you
actually use, against the satellites you actually observe. Propagate each
archived TLE to the epoch of the next one for the same object and decompose the
difference; the along-track component divided by orbital speed is the timing
error the association fit has to absorb.

Measured this way for STARLINK-30056 on the HuggingFace mirror, over
2026-07-31 to 2026-08-07:

| propagated | age | along-track | equivalent time |
|---|---|---|---|
| 07-31 -> 08-02 | 1.90 d | 2.83 km | 0.37 s |
| 08-02 -> 08-03 | 0.98 d | 0.33 km | 0.04 s |
| 08-03 -> 08-04 | 0.98 d | 18.29 km | 2.40 s |
| 08-04 -> 08-06 | 1.90 d | 8.43 km | 1.11 s |
| 08-06 -> 08-07 | 1.05 d | 3.43 km | 0.45 s |

Mean 5.64 km/day, but the spread is what matters: a satellite sat at 0.33 km
for a day and then jumped 18.29 km. That is a station-keeping burn, which
general-perturbations elements cannot anticipate and operator ephemerides can.

A 2.40 s error from a single day of propagation also exceeds the association's
default `--epoch-search-s 2.5` boundary, which is why fits rail there. Widen the
search on evidence like this, not to manufacture a qualification.
