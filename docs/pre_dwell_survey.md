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

## Cost

Per dwell, on a Pi 5 at 2.5 MS/s:

| stage | cost | source |
|---|---:|---|
| warm the fused kernel and both banks | ~78 ms | measured |
| retune, 8 tunings | ~49 ms | model, 6.1 ms each |
| collect, 8 × 20 ms probes | ~262 ms | model, 32.8 ms each |
| score, 8 tunings × 2 receivers | ~173 ms | measured |
| **total** | **~0.56 s** | |

Against a 120 s dwell that is under half a percent. The radio stages are the
survey profile's own cost model, validated against hardware at 43.5 ms per
tuning for a single receiver; the two host stages were measured on this Pi.
Nothing here has yet been timed end to end against a radio, because both were
capturing when it was written.

The first run after any change to `_scan_kernel.c` — including a `git pull` that
touches its mtime — pays about 900 ms more to rebuild the cached kernel.

Scoring is the one part that could be overlapped with the next tuning's retune
and listen, and currently is not.

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
  "schema": "leo-tracker.pre-dwell-survey/v1",
  "state": "complete",
  "threshold": 1.33,
  "dwell": {"channel": 4, "region": "lower-edge"},
  "active_count": 2,
  "active": [{"channel": 4, "region": "lower-edge", "receiver": 0}],
  "tunings": [
    {"channel": 1, "region": "lower-edge",
     "if_center_hz": 959687500.0, "rf_center_hz": 10709687500.0,
     "receivers": [
       {"receiver": 0, "active": false, "peak_to_median": 1.19,
        "frequency_offset_hz": -300000.0, "epoch_s": 0.00042,
        "folded_score": 0.0031, "folded_median": 0.0026}
     ]}
  ]
}
```

Both the verdict and the score behind it are kept. The threshold is a measured
property of the bank shape and has been revised once already; a bare boolean
could not be re-read against a later one, while a score can.

Both receivers are scored from the same capture. They arrive interleaved
whether or not anyone looks at the second one, so the only extra cost is the
arithmetic — and the dwell keeps both, so a survey of one port could not be set
beside what that dwell found.

## What a quiet verdict does not mean

The survey bank spreads three frequency hypotheses across ±300 kHz. It
tolerates a signal between them, because its matched filter is one OFDM symbol
wide, but it degrades sharply past the outermost one. Measured at −6 dB:

| beacon offset | peak-to-median (threshold 1.33) |
|---:|---:|
| 0 Hz | 2.58 |
| 200 kHz | 1.78 |
| 436 kHz | 1.38 |

A receiver whose LNB sits ~436 kHz from its twin therefore scores barely over
threshold on a beacon that is plainly there. **Quiet on such a port is not
evidence of a quiet sky.** The record carries this caveat inline as
`quiet_verdict_caveat`.

Re-centring each port's search on its own oscillator — the fix the analysis path
needed — does *not* help here and measurably hurts: moving all three hypotheses
onto the signal lifts the median as much as the peak, and the statistic is a
ratio. Measured at 436 kHz and −3 dB, 1.41 re-centred against 1.61 as it stands.

The fix is a wider bank: five hypotheses over ±500 kHz measured 2.09 at the same
point. It is deliberately not folded in here, because it needs its own
noise-ceiling characterisation and a re-measured compute constant.

## Comparing the survey against the dwell

Until the probe index projects these columns, read them from the reports:

```sh
duckdb -c "
SELECT
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

The interesting cells are the disagreements: a channel the scanner called
active where the dwell qualified nothing, and the reverse. The first is cheap to
explain (the pass ended, or the survey caught noise at its 1% false-alarm rate);
the second is the one worth chasing, because it means a 20 ms probe missed what
120 s of the same search found.

Note the asymmetry before reading too much into it: the survey scores one 20 ms
probe with a 3×8 bank, and the dwell searches 120 s with the full 7×24 bank.
The survey is reliable to about −8 dB and the dwell to about −12 dB, so the
dwell finding what the survey missed is expected near the floor rather than
anomalous.

## Related

- `src/leo_tracker/radio/beacon/presurvey.py` — the survey and its record
- `src/leo_tracker/radio/beacon/fast_scan.py` — the bank, the kernel, the profile
- `docs/probe_index.md` — the queryable projection of reports
