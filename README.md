# leo-tracker

Test-driven experiments in estimating receiver position from LEO satellite
Doppler observations. The first milestone joins TLE/SGP4 pass predictions to
replayable Pluto+ IQ captures at a surveyed, stationary receiver.

## Development

Use the repository's existing virtual environment exclusively through `uv`:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync pytest
```

The package is split along the experiment boundary:

- `leo_tracker.orbit`: TLE provenance, SGP4 propagation, frames and geometry.
- `leo_tracker.passes`: rise/culmination/set prediction.
- `leo_tracker.radio`: replayable IQ capture and frequency-ridge extraction.
- `leo_tracker.fusion`: deliberately small Doppler nuisance models.
- `leo_tracker.contracts`: UTC/provenance contracts shared by all stages.

The multi-source historical TLE daemon, on-disk API, Kalman service setup, and
operational commands are documented in [docs/tle-sources.md](docs/tle-sources.md).

Hardware tests must be marked `hardware`; the default suite is offline and
deterministic.

## Storage and preservation

Full Pluto IQ on `/mnt/leo-nvme/leo-tracker` and the current QNAP analysis
working set are immutable sources during archive migration. The separate
`/mnt/qnap01/mouse9911/leo-cropped` archive contains exact dual-receiver time
clips, analysis products, read-back verification reports, and transaction
receipts. It is operational but must not be assumed complete merely because
live jobs publish successfully. No cleanup decision should be made from an
analysis completion receipt or an unverified evidence directory. See
[docs/STORAGE.md](docs/STORAGE.md) for the authoritative directory map,
completeness audit, preservation invariants, archive commands, capacity
guardrails, and recovery procedure.

Kalman full-coverage analysis, its 16-worker service, shadow-to-required archive
promotion, graceful draining, and historical backfill are documented in
[docs/KALMAN_MIGRATION.md](docs/KALMAN_MIGRATION.md).

The live deployment currently uses archive `shadow` mode and retention
`disabled`: new jobs attempt cropped publication, but raw sources are retained
and a crop failure does not falsely mark the source disposable. Historical
reports below `reports/` are dated experiment records and are not rewritten to
match later production settings.

Build and verify one cropped archive record without modifying its source:

```bash
scripts/starlink-evidence-archive.sh --recording RECORDING_ID \
  /mnt/leo-nvme/leo-tracker /mnt/qnap01/mouse9911/leo-cropped
```

Orbit and radio remain separate Python modules with independent CLIs. They may
import one another directly; intermediate JSON files are durable experiment
artifacts, not an inter-process architecture requirement.

```bash
# Orbit/TLE pass planning
/home/satpi01/.local/bin/uv run --active --no-sync leo-orbit --help

# Pluto capture and TLE-blind radio analysis
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio --help

# Dual-receiver qualification
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio qualify-pair --help

# Narrow-carrier measurement and center-shift validated IF survey
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio carrier --help
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio validated-scan --help

# Compact dual-RX broadband-motion monitor
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio monitor --help
```

Spectrum scans and IQ captures accept a unified receiver selection. Dual mode
opens the Pluto+ once and samples RX0 and RX1 from the same hardware refill;
do not run two independent IIO processes against the device.

```bash
# Either receiver independently
leo-radio scan OUTPUT ... --channels 0
leo-radio scan OUTPUT ... --channels 1
leo-radio capture OUTPUT ... --channels 0
leo-radio capture OUTPUT ... --channels 1

# Both receivers synchronously
leo-radio scan OUTPUT ... --channels 0,1
leo-radio capture OUTPUT ... --channels 0,1
```

The legacy single-channel `--channel 0|1` option remains accepted, but cannot
be combined with `--channels`. A dual scan writes a session manifest and
combined plot at the output root plus independent `rx0/` and `rx1/` scan
artifacts. A dual capture writes synchronized, independently checksummed IQ
artifacts below `rx0/` and `rx1/` with a shared session identifier and first-
buffer timestamp.

`validated-scan` defaults to a three-second retune settle on each primary and
shifted acquisition. Shorter runs are persisted as diagnostic-only and cannot
promote a radio candidate. It also requires two consecutive center-shift pairs
per center: the field Pluto+/LNB chain produced a repeatable post-jump transient
that survived one pair even after three seconds but disappeared on the
immediate confirmation pair.
Use `--repeats N` with a single center to measure an intermittent candidate's
duty cycle while preserving every acquisition timestamp in the scan JSON.

`monitor` keeps one Pluto+ context open for multiple survey cycles. It discards
at least one post-retune buffer, writes compact PSD arrays to `spectra.npz`, and
never persists raw IQ. Candidate promotion requires a non-zero spectral shift
on both receivers, sufficient normalized spectral correlation, and agreement
between RX0 and RX1. A stationary signal, tuner-center spur, or one-channel
event therefore remains diagnostic rather than becoming a Doppler claim.

The field-tested dual-RX ceiling is 30.72 MS/s in the current 2RX/2TX firmware
configuration. Use that rate for short discovery snapshots, not continuous IQ:
the IP/IIO plus complex64 storage path cannot sustain two channels at that
rate. A complete universal-LNBF low-band monitor run is:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio monitor OUTPUT \
  --uri pluto://ip:192.168.2.1 --channels 0,1 \
  --start-hz 950000000 --stop-hz 1950000000 --step-hz 20000000 \
  --cycles 2 --sample-rate-hz 30720000 --bandwidth-hz 24576000 \
  --samples-per-tuning 65536 --settle-seconds 3
```

The 9.75 GHz low-band LNBF oscillator maps this 950--1950 MHz IF range to
10.70--11.70 GHz at the feed. Once `monitor` promotes a region, retune away
from DC and use a lower-rate `capture` plus `carrier`, `moving`, or `comb`
analysis for the multi-minute dwell. Raw IQ is a promoted-candidate artifact,
not routine survey output.

### Pass-aware Starlink pipeline

The orchestration pipeline freezes a TLE catalog, creates a 12-hour pass
schedule, runs a compact full-band discovery survey, ranks repeatable dual-RX
spectral structure, waits for the next scheduled recording window, revisits
six regions, and finally stares at the strongest region. It stores compressed
PSDs and JSON metadata only; it does not retain raw IQ or change LNBF voltage.

```bash
scripts/starlink_pipeline.sh artifacts/my_starlink_run
```

Useful controls are `CATALOG=/path/to/catalog.json` to remain completely
offline, `START_UTC` and `END_UTC` to define the schedule, `REGION_COUNT`,
`REGIONAL_CYCLES`, and `STARE_CYCLES` to control cadence, and
`SKIP_PASS_WAIT=1` for a bench run. Inspect every command without touching the
radio with:

```bash
DRY_RUN=1 scripts/starlink_pipeline.sh /tmp/starlink-dry-run
```

The underlying stages remain independently callable:

```bash
leo-radio monitor discovery ...
leo-radio rank-regions discovery/monitor.json regions.json --count 6
leo-radio monitor regional --centers-file regions.json ...
leo-radio rank-regions regional/monitor.json stare.json --count 1
leo-radio monitor stare --centers-file stare.json ...
```

Run all offline tests through the existing environment:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync pytest -q
```

Tests cover region ranking, malformed center plans, pass-window selection,
synthetic spectral shifts, the fake dual-radio acquisition path, CLI artifacts,
and a dry run of the complete Bash pipeline. Hardware remains outside the
default pytest suite.

### Continuous Starlink channel observer

The preferred acquisition path targets the published Starlink channel centers
continuously instead of taking millisecond snapshots throughout the IF band.
List the RF-to-IF mapping with:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio starlink-channels
```

With the universal LNBF in its low band (9.75 GHz LO), the primary targets are
channel 3 at 1,575,117,187.5 Hz IF and channel 4 at 1,825,117,187.5 Hz IF.
Observe one receiver for two minutes:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio starlink-observe \
  artifacts/starlink_ch3 --channel-number 3 --channels 0 \
  --duration-s 120 --sample-rate-hz 2500000 --bandwidth-hz 2500000 \
  --block-size 262144 --gain-db 40 --uri pluto://ip:192.168.2.1
```

Both receivers may be sampled synchronously with `--channels 0,1`. Each block
is scored independently for the published 937.5 kHz channel-center gutter,
the nominal 750 Hz frame periodicity, and the approximately 43.9495 kHz center
tone comb. Two independent forms of evidence are required for promotion. The
routine artifact is `observation.json`; a quantized IQ ring is written to
`event_iq.npz` only on the first promotion. Reprocess that event with:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio starlink-analyze \
  artifacts/starlink_ch3/event_iq.npz artifacts/starlink_ch3/reanalysis.json
```

Channels 5--8 use the 10.6 GHz high-band LO and require the external 22 kHz
LNBF selection tone. The CLI refuses them unless `--high-band-selected` is
given; this acknowledgement does not itself alter tone or supply voltage.
The old spectrum watchdog is disabled while this methodology is validated.

For bare-LNBF wideband discovery, retain compact spectra instead of raw IQ:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio \
  starlink-waterfall-capture artifacts/ch4.npz --channel-number 4 \
  --if-offset-hz 5000000 --channels 0,1 --snapshots 2048

/home/satpi01/.local/bin/uv run --active --no-sync leo-radio \
  starlink-waterfall-analyze artifacts/ch4.npz artifacts/ch4-analysis.json \
  --integration-s 1 --max-drift-hz-s 10000 --permutations 128 \
  --plot artifacts/ch4-waterfall.png
```

The analyzer subtracts the stationary median spectrum, finds moving spectral
depressions without TLE assistance, and reports empirical false-alarm
probabilities from time-scrambled controls.  TLE fitting is performed only
after blind RF extraction.  See
`reports/starlink_doppler_signature_20260802.md` for the first bare-LNBF
channel-4 signature and its limitations.

The current dual-LNBF measurement workflow records absolute raw-code PSD,
hardware gain, clipping, and real snapshot timestamps at 30.72 MS/s. It uses
8,192 output bins (3.75 kHz/bin) with 0.01 dB int16 PSD storage, alternates
channels 3 and 4, and deliberately places the nominal channel center 5 MHz away
from Pluto DC. Resolution-specific baselines bootstrap automatically from four
captures spanning both dither centers. Start or resume the continuous watcher
with:

```bash
scripts/starlink_measurement_watch.sh artifacts/starlink_measurement_watch
```

Analyze any saved chunk independently, including TLE-constrained approximately
30-second beam-dwell searches and the nine-tone center comb described in the
Starlink signal-structure literature:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio \
  starlink-measurement-analyze chunks/chunk.npz analysis/chunk.json \
  --passes passes/channel-3.json --plot plots/chunk.png \
  --event-frequency-bins 1024 \
  --tle-dwell-window-s 30 --tle-dwell-step-s 10 \
  --tle-minimum-window-s 8 --tle-comb-spacing-hz 43900 \
  --tle-minimum-comb-spacing-improvement 0.03
```

Generic event segmentation uses the 1,024-bin power-averaged view while the TLE
and comb search retains all 4,096 bins. The matched search fits an independent constant frequency bias for each LNBF,
so nominal LO error does not masquerade as Doppler. Promotion requires resolved
predicted motion, dual-receiver evidence, improvement over a stationary-path
control, and—when using the comb model—preference for 43.9 kHz over 35 and
52 kHz wrong-spacing controls. The analysis JSON records the selected dwell,
signal model, receiver biases, scores, controls, and all search settings.

Run the same workflow continuously for twelve hours with incremental status,
one PNG per chunk, and separate promoted-detection records:

```bash
/home/satpi01/.local/bin/uv run --active --no-sync leo-radio \
  starlink-waterfall-watch artifacts/starlink_watch_12h \
  --hours 12 --channel-number 4 --if-offset-hz 5000000 \
  --channels 0,1 --chunk-snapshots 4096 --permutations 32 \
  --plot-mode all
```

Progress is available without opening a partial capture in `summary.json` and
`index.jsonl`. Compact spectra and analyses are stored below `chunks/`, every
track waterfall below `plots/`, and promoted Doppler records below
`detections/`. Repeating the command with the same output directory continues
chunk numbering and preserves existing records.

### Tracker ensemble and legacy hybrid pipeline

The waveform-agnostic tracker ensemble and older hybrid orchestration remain
available for controlled comparison. The hybrid workflow combines a gap-free
30.72 MS/s survey with a long 2.5 or 4 MS/s dwell, captures both Pluto+
receivers, and analyzes bounded event windows with independent Doppler methods:

```bash
scripts/starlink_hybrid_watch.sh artifacts/starlink_hybrid_watch

uv run --active --no-sync leo-radio doppler-trackers CAPTURE.npz TRACKERS.json \
  --window 94:104 --plot TRACKERS.png --passes PASSES.json

uv run --active --no-sync leo-radio doppler-iq-track TRIGGERED-IQ.npz COHERENT.json
```

The ensemble includes connected components, direct de-Doppler integration,
Viterbi ridges, comb tracking, multi-pilot consensus, broadband envelope and
edge paths, and internal spectral-texture registration. Triggered IQ adds FLL,
polynomial-phase, repetition, and optional known-template cross-ambiguity
analysis. TLE matching happens only after blind dual-receiver qualification.
All paths, capture parameters, controls, false-alarm evidence, and confidence
metrics are persisted in versioned JSON; annotated tracks are published to the
dashboard at `http://localhost:8765/`. See
[docs/doppler-trackers.md](docs/doppler-trackers.md) for method references,
tests, schemas, and operational commands.

The deployed continuous sky collector is the edge-beacon service documented in
[docs/starlink_beacon_receiver.md](docs/starlink_beacon_receiver.md); the hybrid
watcher is not the authority for current capture cadence or retention.

## Current milestone

The pipeline has progressed from blind edge-beacon acquisition through
dual-receiver carrier tracking and one held-out, stability-tested Starlink TLE
association. That result is field evidence for the method, not yet a navigation
solution. The current milestone is to repeat robust identities across many
passes and spacecraft, retain enough simultaneous or overlapping tracks to
separate receiver/LNB and satellite clock terms, and then estimate receiver
position with held-out truth and wrong-satellite/time-shift controls. Cropped
archive backfill and replay equivalence are part of this milestone because a
position result must remain reproducible after raw retention is eventually
bounded.

See [docs/first-field-experiment.md](docs/first-field-experiment.md) for the
field runbook.
