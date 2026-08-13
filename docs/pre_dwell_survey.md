# The pre-dwell survey

One survey of all eight low-band edge tunings, run immediately before each
narrow dwell and filed in that capture's manifest.

## Why

A dwell commits two minutes and 2.3 GB to a single channel. Nothing recorded
alongside it says what the other seven tunings looked like at that moment, so:

- a capture that found nothing cannot be told apart from a sky that had nothing
  in it, and
- the channel choice cannot be scored after the fact — there is no record of
  what the alternatives would have given.

The survey answers both by recording, per tuning and per receiver, the same
statistic the dwell's own acquisition uses.

## What it is not

**Observational only.** It does not choose the channel, does not shorten the
dwell, and cannot prevent a capture. A survey that fails is recorded as having
failed and the recording proceeds — a missing survey is an annotation, a delayed
capture is gone for good.

## Where it runs

`scripts/starlink-beacon-watch.sh` passes `--survey-before-dwell` to
`starlink-beacon-capture` for narrow dwells only. Wide and oversampled captures
are not a choice among the eight edge tunings, so a survey of them would say
nothing about those.

Set `LEO_BEACON_SURVEY_BEFORE_DWELL=0` to turn it off without a code change.

The survey opens its own libiio context and hands it back before the capture
claims one. The two want opposite radio configurations — a survey wants a
shallow queue and a probe-sized block, a dwell wants deep buffers — and a USB
context is an exclusive claim.

`--fake` captures never run a survey; a simulated capture asking for a real
radio would open a context on whatever answers the default URI, which on this
site is hardware a live capture service owns. That is recorded as
`state: "skipped"` rather than silently omitted.

## The capture configuration is drawn, not chosen

Each survey draws uniformly from four arms:

    probe length in {80 ms, 160 ms}  x  sample rate in {2.5 MS/s, 5 MS/s}

`randomised-probe-length-and-rate-v1`. Both axes had an argument on each side
and neither argument was settled by a measurement, so the corpus is asked
instead. Probe length folds more frames (about √2 in sensitivity for twice the
air); sample rate widens the guard either side of the 1.875 MHz pilot band from
±312.5 kHz to ±1562.5 kHz, which is the difference between lnb-c's +434 kHz
offset losing subcarriers off the end of the spectrum and not.

The shell draws a raw 32-bit number from `/dev/urandom` and passes it; the
mapping to an arm lives in Python where it has a test. `2**32` divides by four,
so `draw % 4` is *exactly* uniform — no modulo bias to correct for. The
experiment id, the raw draw and the resulting assignment are all recorded, so
the split can be audited from the corpus rather than trusted.

| variable | default | effect |
|---|---|---|
| `LEO_BEACON_SURVEY_EXPERIMENT_ID` | `randomised-probe-length-and-rate-v1` | empty string disables randomisation and runs the calibrated arm |
| `LEO_BEACON_SURVEY_CONFIG` | *(unset)* | pin one arm by name, e.g. `160ms-5.0MSps`; recorded as **not** randomised |
| `LEO_BEACON_SURVEY_DEFER_SCORING` | `0` | `1` collects the IQ and scores nothing on the Pi |

## Cost

Radio time follows probe **duration**, not sample rate, because the block is
sized to the probe. Scoring is the part that moves.

| arm | samples/tuning | IQ per capture | radio | Pi score (bounded) | full score |
|---|---:|---:|---:|---:|---:|
| 80 ms 2.5 MS/s | 200,000 | 12.8 MB | 0.79 s | 2.07 s | 1.99 s |
| 80 ms 5 MS/s | 400,000 | 25.6 MB | 0.79 s | 2.85 s | 5.60 s |
| 160 ms 2.5 MS/s | 400,000 | 25.6 MB | 1.43 s | 1.94 s | 4.72 s |
| 160 ms 5 MS/s | 800,000 | 51.2 MB | 1.43 s | 2.71 s | 11.60 s |

Radio time is the survey profile's own cost model, field-anchored. Scoring was
measured on the capture Pi 5 itself, three threads, minimum of twelve
round-robin rounds so the two live capture services' load fell on every arm
alike. IQ is `8 tunings × N × 2 receivers × 2 components × 2 bytes` = 64N.

**Scoring is split from collection.** `collect_radio` does everything that needs
the radio and nothing else; `score_collection` takes samples and produces the
verdict, and runs anywhere. The capture host scores at most a 200,000-sample
prefix — the cheapest arm's worth — so its bill does not track the draw. The
full-length comparison runs on the analysis host over the preserved probe, where
it costs only **1.00 / 1.17 / 1.19 / 1.44** relative across the four arms:
quadrupling the samples costs 44%, because most of that stage is differential,
GLRT and conditioned statistics over a fixed symbol count.

At 30 captures/hour fleet-wide the survey IQ averages **28.8 MB** per capture,
about **20.7 GB/day** on the capture volume before retention removes it.

The waterfall does **not** grow: measured 2.78 MB in all four arms, because the
figure is a fixed 9 × 12 inches at 120 dpi and `imshow` downsamples whatever it
is given. Only its render time moves, 8.4 s to 12.4 s, and that is spent after
the dwell with the radio idle.

The first run after any change to `_scan_kernel.c` — including a `git pull` that
touches its mtime — pays about 900 ms more to rebuild the cached kernel.

Resolving a USB radio by serial enumerates the bus. The capture already does
this per dwell, so the survey doubles that rate rather than introducing it — but
enumeration briefly opens every context, including the other radio's, so it is
worth watching if the two capture loops start interfering.

## What is recorded

Under `metadata.pre_dwell_survey` in the capture manifest, which
`starlink-beacon-analyze` embeds whole into the report as `capture_manifest`.
So the survey travels with the capture and needs no join on wall time.

```json
{
  "schema": "leo-tracker.pre-dwell-survey/v2",
  "state": "complete",
  "threshold": 1.252,
  "threshold_calibrated": false,
  "threshold_basis": "UNCALIBRATED at this configuration. The score was taken over 40 ms at 5.0 MS/s; the only measured 1% point for this bank, 1.252, was taken over 80 ms at 2.5 MS/s over 1696 clean windows ...",
  "capture_config": {
    "name": "160ms-5.0MSps", "probe_s": 0.16, "sample_rate_hz": 5000000.0,
    "samples_per_tuning": 800000, "pilot_guard_hz": 1562500.0,
    "iq_bytes": 51200000, "scored_samples": 200000, "scored_probe_s": 0.04
  },
  "experiment": {
    "experiment_id": "randomised-probe-length-and-rate-v1",
    "randomised": true, "random_draw_u32": 2632295989,
    "assignment_probability": 0.25, "assigned_config": "160ms-5.0MSps",
    "arms": ["80ms-2.5MSps", "80ms-5.0MSps", "160ms-2.5MSps", "160ms-5.0MSps"]
  },
  "dwell": {"channel": 4, "region": "lower-edge"},
  "active_count": null,
  "active": null,
  "tunings": [
    {"channel": 1, "region": "lower-edge",
     "if_center_hz": 959687500.0, "rf_center_hz": 10709687500.0,
     "receivers": [
       {"receiver": 0, "active": null, "peak_to_median": 1.19,
        "frequency_offset_hz": -300000.0, "epoch_s": 0.00042,
        "folded_score": 0.0031, "folded_median": 0.0026}
     ]}
  ]
}
```

Both the verdict and the score behind it are kept. The threshold is a measured
property of the bank shape *and of the configuration it was measured in*, and it
has been revised once already; a bare boolean could not be re-read against a
later one, while a score can.

### `active` is a tri-state, and `null` is not `false`

`SURVEY_NOISE_CEILING` = 1.252 was measured over 1,696 clean cross-edge windows
at **80 ms and 2.5 MS/s only**. The statistic moves with probe length — p99
1.310 / 1.189 / 1.137 at 20 / 40 / 80 ms — and must move with rate too, because
rate sets the kernel taps (11 against 22) and the epoch count the fold maximises
over (3,333 against 6,667).

So in the three uncalibrated arms **no boolean is written at all**: `active` is
`null` on every receiver, and the top-level `active` and `active_count` are
`null` rather than `[]` and `0`. An empty active list has the same shape as
"looked and found nothing" and would be read as that. A stored boolean that
means one thing in some rows and another in the rest is worse than no boolean.

The score is kept beside it, so every row becomes usable the moment its bar
exists. Characterising the other three thresholds needs null populations taken
*in* those configurations, and producing them is exactly what this experiment
does — so the order is **randomise, accumulate, then calibrate**.

`threshold_calibrated` is also `false` when `LEO_BEACON_SURVEY_DEFER_SCORING=1`,
for a different reason: not that the bar is unmeasured but that nothing was held
up against it.

Both receivers are scored from the same capture. They arrive interleaved
whether or not anyone looks at the second one, so the only extra cost is the
arithmetic — and the dwell keeps both, so a survey of one port could not be set
beside what that dwell found.

### Reading `survey.ci16`

The manifest's `survey_iq` block declares the shape, the rate and the probe
length. **Take all three from it.** The file is
`(tunings, samples_per_tuning, 2, 2)` int16 LE where `samples_per_tuning` is
200,000, 400,000 or 800,000 depending on the draw, and its rate is the
*survey's*, never `manifest["sample_rate_hz"]`, which belongs to the dwell.
`survey_scoring.read_probe` does this and refuses a file whose length disagrees
with its declared shape, or a shape that disagrees with `probe_s × rate`.

## What a quiet verdict does not mean

**The spacing half of this is fixed.** The bank was three hypotheses across
±300 kHz, which left an LNB 436 kHz from its twin scoring 1.38 at −6 dB against
a 1.33 threshold while the same beacon on-centre scored 2.58. It is now
thirteen across ±700 kHz, so 436 kHz sits 31 kHz from a hypothesis instead of
136 kHz from one, and the same measurement reads a flat 2.46 / 2.41 / 2.37 at
0 / 200 / 436 kHz. `quiet_verdict_caveat` survives in the record in softened
form so that probes written under the old bank can still be told apart.

Re-centring each port's search on its own oscillator — the fix the analysis path
needed — does *not* help here and measurably hurts: moving every hypothesis onto
the signal lifts the median as much as the peak, and the statistic is a ratio.
Measured at 436 kHz and −3 dB, 1.41 re-centred against 1.61 as it stands. That
stays true at any spacing; it is a property of the statistic.

**The bandwidth half is what the 5 MS/s arm is measuring.** Spacing cannot help
a pilot that was never sampled. The eight pilot subcarriers span 1.875 MHz, so
2.5 MS/s leaves ±312.5 kHz of guard — and lnb-c sits at +434 kHz, outside it,
losing subcarriers off the end of the spectrum at about 0.58 dB for the first of
eight. At 5 MS/s the guard is ±1562.5 kHz and it fits with room to spare.
Whether that is worth the bytes is what the randomised arm exists to answer.

## Comparing the survey against the dwell

Until the probe index projects these columns, read them from the reports:

```sh
duckdb -c "
SELECT
  r.capture_manifest.metadata.pre_dwell_survey.capture_config.name AS arm,
  r.capture_manifest.metadata.pre_dwell_survey.threshold_calibrated AS comparable,
  r.capture_manifest.metadata.pre_dwell_survey.dwell.channel AS dwelt,
  s.channel, s.region, rx.receiver,
  rx.active                          AS scanner_called_active,
  rx.peak_to_median,
  r.summary.exact_qualified_count > 0 AS dwell_found_something
FROM read_json('reports/*narrow*.json') r,
     UNNEST(r.capture_manifest.metadata.pre_dwell_survey.tunings) AS t(s),
     UNNEST(s.receivers) AS u(rx)
WHERE r.capture_manifest.metadata.pre_dwell_survey.state = 'complete'
"
```

**Group by `arm`, or filter on `comparable`.** `scanner_called_active` is `null`
in three of the four arms, and `peak_to_median` is not comparable across probe
lengths even where it is not null — the fold is incoherent, so the same sky
scores lower over more frames. Pooling arms is the mistake this schema exists to
make visible.

The interesting cells are the disagreements: a channel the scanner called
active where the dwell qualified nothing, and the reverse. The first is cheap to
explain (the pass ended, or the survey caught noise at its 1% false-alarm rate);
the second is the one worth chasing, because it means an 80 ms probe missed what
120 s of the same search found.

Note the asymmetry before reading too much into it: the survey scores one probe
with the 13×8 bank at its own 1% point, and the dwell searches 120 s with the
full 7×24 bank. The dwell finding what the survey missed is expected near the
floor rather than anomalous.

## Switching the randomisation on

It is in the repository and **not running**. The watch script is read once at
service start, so the change takes effect only after:

```sh
sudo systemctl restart leo-tracker-beacon-watch@pluto-new.service
sudo systemctl restart leo-tracker-beacon-watch@pluto-old.service
```

Until then every survey stays at 80 ms / 2.5 MS/s with no `capture_config` on
its record, which is the one arm whose threshold is measured — so nothing
already recorded becomes ambiguous by waiting. Restarting mid-dwell abandons
that dwell's recording, so restart between captures.

Confirm from the journal, which prints the draw per capture:

```sh
journalctl -u leo-tracker-beacon-watch@pluto-new.service -f | grep survey_experiment
```

and from the capture output line, which now carries `survey_config`,
`survey_calibrated`, `survey_radio_ms` and `survey_compute_ms`.

## Related

- `src/leo_tracker/radio/beacon/presurvey.py` — the survey, its record, the draw
- `src/leo_tracker/radio/beacon/fast_scan.py` — the bank, the kernel, the
  profile, and the `collect_radio` / `score_collection` split
- `src/leo_tracker/radio/beacon/survey_scoring.py` — the analysis host's reader
- `docs/survey_detector_plan.md` — why the configuration is drawn rather than chosen
- `docs/probe_index.md` — the queryable projection of reports
