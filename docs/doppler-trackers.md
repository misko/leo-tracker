# Doppler tracker ensemble

The ensemble keeps detection, receiver association, and TLE identification as
separate claims. All PSD trackers return `TrackCandidate`; association permits
an arbitrary constant LNB offset but compares trajectory and held-out signal
evidence. JSON is persistence only: trackers import the shared Python models.

## Implemented approaches and evidence

| Approach | Implementation | Scientific reference | Principal tests |
|---|---|---|---|
| Connected spectral components and centroids | `tracking/adapters.py` | Baseline image segmentation method retained for comparison | positive/negative events, broadband classification, power redistribution |
| Direct incoherent de-Doppler slope bank | `tracking/dedoppler.py` | Siemion et al. 2013, ApJ 767:94; `UCBerkeleySETI/turbo_seti` | weak chirp accumulated below per-row threshold, positive/negative drift, stationary control, dual receiver |
| Continuity-constrained Viterbi ridges | `tracking/viterbi.py` | Viterbi, IEEE TIT 13(2), 1967, DOI 10.1109/TIT.1967.1054010 | stationary competitor, moving path, multiple ridge suppression |
| Held-out comb teeth | `tracking/adapters.py`, `blind_comb.py` | matched filter-bank principle; odd teeth validate paths fitted with even teeth | correct/wrong spacing, missing teeth, stationary and time-shuffled controls |
| Multi-pilot consensus | `tracking/broadband.py` | robust consensus over independent de-Doppler detections | supporting-pilot count, outlier slope, independent receiver populations |
| Broadband envelope | `tracking/broadband.py` | STRF/rffit wideband center fitting practice | finite width, explicit warning and rejection under power redistribution |
| Independent lower/upper edges | `tracking/broadband.py` | STRF/rffit sideband fitting practice | edge paths separated from centroid; changing-edge confound fixtures |
| Internal spectral-texture registration | `tracking/broadband.py` | translation registration by correlation; Guizar-Sicairos et al. 2008, DOI 10.1364/OL.33.000156 | common translation under power change, stationary texture, correlation gate |
| Conjugate-product FLL | `tracking/coherent.py` | GNSS-SDR FLL-assisted PLL implementation | exact carrier, within-block rate, and an inter-block dual-RX track with independent LNB offsets |
| Polynomial-phase carrier tracking | `tracking/coherent.py` | standard polynomial-phase Doppler model; GNSS-SDR carrier tracking | frequency and frequency-rate recovery, phase residual |
| Blind repetition correlation | `tracking/coherent.py` | synchronization through repeated waveform correlation | exact complex period and no-invalid-lag validation |
| Delay/Doppler cross-ambiguity | `tracking/coherent.py` | Tang, Falletti & Lo Presti 2013, nearly-ML GNSS acquisition, DOI 10.3390/s130505649 | known delay and Doppler grid recovery plus CLI E2E |
| TLE trajectory comparison | existing `tle_search.py`, after blind tracking | SGP4 range-rate prediction | exact synthetic pass, stationary, reversed and incomplete-window controls |

Primary test modules are `tests/test_radio_tracker_framework.py`,
`tests/test_radio_coherent_trackers.py`, `tests/test_radio_blind_comb.py`,
`tests/test_radio_events.py`, and `tests/test_radio_tle_search.py`.

## Operational commands

Run all waveform-agnostic PSD trackers on one or more bounded windows:

```bash
uv run --active --no-sync leo-radio doppler-trackers CAPTURE.npz TRACKERS.json \
  --window 97:101 --integration-s 0.5 --passes ARCHIVED-PASSES.json
```

Run coherent estimators on IQ that passed the evidence gate:

```bash
uv run --active --no-sync leo-radio doppler-iq-track IQ.npz COHERENT.json
```

If a known synchronization or pilot template becomes available, run its full
delay/Doppler ambiguity surface from the CLI:

```bash
uv run --active --no-sync leo-radio doppler-ambiguity IQ.npz TEMPLATE.npy AMBIGUITY.json \
  --maximum-delay-samples 64 --minimum-doppler-hz -5000 \
  --maximum-doppler-hz 5000 --doppler-step-hz 250
```

Compare runtime and candidate rates over all historical reports:

```bash
uv run --active --no-sync leo-radio doppler-tracker-summary \
  tracker-performance.json tracker-ensemble/
```

Reapply a newly archived or corrected pass catalog without rerunning the signal
trackers:

```bash
uv run --active --no-sync leo-radio doppler-tle-match \
  TRACKERS.json ARCHIVED-PASSES.json REMATCHED-TRACKERS.json
```

`scripts/starlink_hybrid_watch.sh` selects at most four finite event windows per
dwell, runs the PSD ensemble with a bounded slope grid, updates the longitudinal
summary, and analyzes triggered IQ when either the legacy analysis or ensemble
dual-receiver gate promotes it. Set `PASS_CATALOG` to an archived pass catalog;
TLE matching runs only after blind qualification and compares motion without
assuming that the two LNB frequency biases agree. A rate-compatible pass is
recorded separately from a specific identification; a satellite name is
promoted only if its confidence decisively exceeds every overlapping pass.

The experimental hybrid watcher and dashboard unit definitions live in
`deploy/systemd/leo-tracker-hybrid-watch.service` and
`deploy/systemd/leo-tracker-dashboard.service`. Both use only the existing
virtual environment through `uv run --active --no-sync`, restart after process
failure, and can be enabled for the user session's default target. The deployed
continuous sky collector is `leo-tracker-beacon-watch.service`; see
[`starlink_beacon_receiver.md`](starlink_beacon_receiver.md).

The hybrid workflow's legacy `leo-tracker-radio-archive.timer` can copy aged
artifacts at idle I/O/CPU priority, but it is not the current preservation
contract. The deployed beacon pipeline uses
`LEO_BEACON_PRESERVE_RAW=1`, exports copies atomically, and does not remove a
local source. Cropped evidence and any future retention are governed by
[`STORAGE.md`](STORAGE.md), not by this ensemble runbook.

## Required interpretation controls

No method is accepted merely because its fitted line looks plausible. Reports
retain stationary-path improvement, held-out scores, receiver agreement, gain
transitions, and method-specific warnings. The direct de-Doppler tracker fits
on alternating spectra and reports a maximum-intercept time-permutation false
alarm probability on held-out spectra. TLEs are applied
after blind detection so an expected pass cannot manufacture a detection.
