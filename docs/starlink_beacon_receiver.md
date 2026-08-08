# Starlink beacon receiver

## Objective and claim ladder

This receiver is designed to produce repeatable evidence, not to label every
sloped waterfall feature as Starlink. A result advances through these levels:

1. `structural`: excess complex autocorrelation at the published 750 Hz frame rate.
2. `pss`: a repeatable frame epoch from the exact published PSS, above folded-time controls.
3. `pilot`: the exact 2026 edge-pilot code beats a 17-symbol-shifted negative-control code.
4. `dual_rx`: RX0 and RX1 agree on frame epoch and carrier-frequency offset.
5. `doppler_lock`: carrier offset evolves continuously with physically plausible rate and curvature.
6. `identified`: the blind RF track is compatible with a retrospectively archived TLE trajectory.

Only levels 3 and above are called Starlink beacon candidates. TLEs never seed
the RF detector; they are used after detection to avoid circular identification.

## Frequencies and capture geometry

The published channel center is
`10.7 GHz + 117.1875 kHz + 250 MHz * (channel - 0.5)`. A universal LNB with a
9.75 GHz low-band LO maps channel 3 to 1.5751171875 GHz IF and channel 4 to
1.8251171875 GHz IF. Those channel centers contain a four-subcarrier gutter,
so a narrow receiver centered there is looking at the wrong structure.

Qin et al. publish two eight-subcarrier pilot bands per active 240 MHz channel.
Their centers are -115.4296875 MHz and +115.1953125 MHz relative to the channel
center. The four supported IFs are therefore:

| Channel | Region | Pluto IF | Ku RF | Captured pilot width |
|---:|---|---:|---:|---:|
| 3 | lower edge | 1.459687500 GHz | 11.209687500 GHz | 1.875 MHz |
| 3 | upper edge | 1.690312500 GHz | 11.440312500 GHz | 1.875 MHz |
| 4 | lower edge | 1.709687500 GHz | 11.459687500 GHz | 1.875 MHz |
| 4 | upper edge | 1.940312500 GHz | 11.690312500 GHz | 1.875 MHz |

The lock mode synchronously records both Pluto+ receivers as little-endian CI16
at 2.5 MS/s and 2.5 MHz RF bandwidth. This is 20 MB/s, or 2.4 GB per two-minute
dwell. The Nyquist interval and configured analog RF filter leave about 312.5
kHz per side around the 1.875 MHz pilot. This matters in practice: a replayed
beacon arc disappeared near -205 kHz with the former 2.3 MHz filter, consistent
with its roughly 212.5 kHz edge margin. The wider filter preserves another 100
kHz of one-sided Doppler and LNB error without increasing the sample rate or
storage load.

Production currently focuses on the channel-4 lower edge because that tuning
produced the strongest legacy candidates. Those events remain hypotheses: the
legacy carrier grid has since been shown to have large off-grid blind spots and
its confirmations must survive the newer detector before being called beacons.
The target list is
configurable with `LEO_BEACON_TARGETS`; the other three edge bands remain
available for controlled comparison. Every tenth cycle, production inserts a
15-second 5 MS/s, 3 MHz RF-bandwidth oversampled dwell. It demodulates natively
and also digitally downsamples the same IQ to 2.5 MS/s, yielding a controlled
same-signal comparison without confusing sky variability for a rate benefit.
The cadence is configurable with `LEO_BEACON_OVERSAMPLE_EVERY_CYCLES`.
Every fifteenth narrow lock cycle (roughly
30 minutes) is followed by a wide acquisition on the same target. The cadence is
configurable with `LEO_BEACON_WIDE_EVERY_CYCLES`. Each wide dwell captures ten seconds
at 10 MS/s and 9 MHz bandwidth,
then searches digital 2.5 MHz subbands from -3.5 through +3.5 MHz in 500 kHz
steps. Every bank receives a joint frame-epoch/CFO matched search against both
the exact waveform and a 17-symbol-rolled control. The winning exact-minus-
control bank seeds detailed symbolwise tracking. PSS remains independent
supporting evidence rather than an authoritative timing seed. Once a stable LNB
offset is acquired, the continuous narrow mode supplies the denser Doppler record.

The `coherent_grid_v1` detector joint-matches narrow captures every second using
10 ms windows. Its exact and rolled-control delay/CFO searches share a vectorized
FFT bank. Controlled tests found that its 25 kHz carrier grid can miss a perfect
pilot only 2.5--12.5 kHz off a grid point, so v1 is retained for retrospective
comparison rather than treated as the final detector.

`pss_symbolwise_v2` searches a coarse CFO bank with the published PSS, retains
multiple separated timing hypotheses, tracks the exact pilot code
noncoherently symbol by symbol, and then refines CFO on a 100 Hz grid. The
17-symbol-rolled control is evaluated at the *same* selected epoch and CFO; it
is not allowed to choose an easier unrelated hypothesis. Silent-window energy
masking prevents FFT roundoff from manufacturing PSS peaks. Detector generation
is written to every report and follow-up so their score populations cannot be
silently combined.
Dual-RX agreement remains the promotion gate, while strong single-RX evidence
is explicitly labeled and retained for dense replay because
the two independent LNBs need not have equal sensitivity.

`pilot_symbolwise_v3` is the production detector. At 2.5 MS/s the narrow PSS
contains only about eleven samples per frame, which made weak-signal timing
acquisition fragile even though v2 removed the carrier-grid blind spots. V3
instead searches every possible frame epoch by noncoherently combining 24
spread pilot-symbol anchors across the capture window and a coarse CFO bank.
It then applies the same conditioned exact-code versus rolled-control test and
fine CFO refinement as v2. The all-epoch search costs more CPU, so production
checks narrow captures every three seconds and wide captures every ten seconds;
a hit still launches the 100 ms dense follow-up.

Every elevated single-RX margin, plus any near-dual event with a common frame
epoch, triggers an automatic ±0.5 s replay at 100 ms spacing. Temporal
confirmation requires consecutive exact/control candidates with frame epochs
within 20 samples and a CFO change compatible with at most 15 kHz/s plus a
25 kHz tolerance. A separate cross-receiver rule accepts candidates that switch
between LNBs within 400 ms only when both RF chains select the same frame epoch
at each observation and the candidate CFO remains continuous. Because production
stays on one pilot band, the next dwell is already a repeat without a retune to
another edge. Isolated excursions remain follow-ups; they are not promoted.

Confirmed follow-ups are retrospectively joined to the archived pass catalog.
The report records every Starlink pass overlapping the RF event, its NORAD ID,
nearest predicted elevation/Doppler, and culmination elevation. With many
simultaneous visible spacecraft this is compatibility evidence, not unique
identification; unique TLE matching still requires a longer Doppler trajectory.

After every analyzed capture, `starlink-beacon-calibrate` rebuilds an
empirical null report from all non-confirmed observations. Acquisition methods
and narrow/wide modes are kept separate because detector and frequency-bank
maximization change the null. The
dashboard publishes receiver/check counts, match-margin percentiles, single-RX
exceedances, and complete dual-gate exceedances alongside the fixed gates.
`starlink-beacon-null-replay` creates a resumable, thermally guarded null for a
new detector by replaying stratified windows from retained strict negatives.

On 2026-08-05 v3 recovered two independent channel-4 lower-edge events. The
first produced consecutive dual-receiver links near capture time 76 s. The
second produced 21/21 dual-receiver candidate points over a dense two-second
replay: both receivers selected the same frame epoch to within one sample and
tracked parallel CFO curves from approximately -96 to -104 kHz. After those
captures were preserved and excluded, the initial v3 field null contained 48
checks with zero single- or dual-receiver gate crossings. These establish a
repeatable Starlink-specific code detection, but not a unique spacecraft ID;
many visible Starlinks overlap each event and the inexpensive LNB offsets hide
absolute Doppler. After deployment, the first live v3 observation found a third
event without retrospective targeting. Its dense replay again showed identical
dual-receiver timing and parallel CFO slopes near -4 kHz/s, demonstrating that
the production path can repeat the detection autonomously.

## Narrowband waveform decoding

Every confirmed narrow capture is now demodulated rather than stopping at a
correlation score. The decoder removes the measured carrier offset, solves the
eight edge-subcarrier coefficients for each OFDM symbol, aligns repeated 750 Hz
frames, and stacks them. It decodes the 300 published pilot symbols per frame
and the eight visible SSS subcarriers. Pilot channel estimates are cross-fitted:
each held-out symbol parity is equalized using the opposite parity, so a symbol
is not credited for fitting its own channel. The pilot and SSS sequences are
known synchronization structure; they are not decoded user payload.

Decoder revision 2 estimates and removes residual within-frame carrier slope,
weights repeated frames by coherent quality, and publishes normalized QPSK
state probabilities for every symbol. Independently equalized RX0/RX1 symbols
are inverse-noise combined, preserving per-receiver results alongside the soft
dual-RX result. On preserved event `20260805T203100Z`, this changed the separate
receiver decisions from 87.5%/86.5% to 88.0%/86.9%, while dual-RX combining
reached 95.3% pilot accuracy and 68.8% on the narrow SSS slice. A previously
weak receiver in event `20260805T200557Z` improved from 32.7% to 82.5% after a
-584 Hz residual-CFO correction; the combined pilot result reached 92.1%.

The preserved `20260805T171900Z` field event contains seven complete frames in
the selected 10 ms replay. Its held-out pilot decisions are 73.8% correct on
RX0 and 71.9% on RX1, compared with 25% random chance. The narrow SSS slice is
weaker at 44.6% and 37.5%, so it is supporting rather than conclusive evidence.
At 2.5 MS/s the receiver fully contains the 1.875 MHz pilot band, but it cannot
capture or decode the full 240 MHz Starlink channel, header, or user payload.

## Relationship to *Unveiling Starlink for PNT*

Kozhaya, Saroufim, and Kassas use a one-second, 500 MS/s, dish-assisted capture
to *learn* the otherwise unknown 240 MHz reference waveform.  Their operational
low-gain receivers do not record 240 MHz: they correlate a bandpass-filtered
2.5 or 5 MHz edge slice that retains predictable energy across nearly all 302
active symbols of each 4/3-ms frame.  This is the same reason this receiver
targets a 1.875 MHz predictable edge band. Their full-frame processing adds
about 18 dB over using PSS/SSS alone. This receiver retains the published
300-symbol, eight-pilot template as its independent acquisition anchor, then
can robustly learn the additional repeating 2.5 MHz bandpass waveform after
removing the acquired frame epoch and CFO. Learned templates are per receiver,
checksummed, trained on separate follow-up groups, and promoted only when
held-out frames beat a spectrum-preserving circularly shifted control and the
published-pilot baseline.

There are two important gaps between detection and their navigation receiver.
First, their tracking loop updates once per frame (750 Hz) and estimates carrier
phase, frequency, and frequency rate. Our conditioned per-frame tracker now
measures correlation phase at 750 Hz and robustly aggregates it into calibrated
10 Hz association observables; a complete carrier/code Kalman loop is still a
later refinement. Second, their 2024 eight-channel experiment used eight
synchronized 2.5 MHz front ends and reports
channel changes every 15 seconds.  The Pluto+'s two receivers share one LO, so
they provide valuable spatially independent validation but cannot observe two
Ku channels simultaneously.  A long identity track therefore needs fast
reacquisition across the eight channel-edge tunings after a disappearance.
The deployed low-band compromise records three back-to-back four-channel hop
surveys after every second fixed-channel dwell. The 2-second children make a
burst span roughly 40 seconds, necessarily crossing at least two documented 15-second channel
boundaries and making a 20-second association arc geometrically possible when
the new channels lie among channels 1--4. It produces 12 children per roughly
280-second super-cycle. Retained-field timing of the symbolwise acquisition
keeps that workload below the dedicated hop-worker throughput.
Each 2-second hop child evaluates three independent 20-ms acquisition windows
at 0, 0.7, and 1.4 seconds (3% temporal coverage) rather than one 10-ms instant.
The longer child can contribute roughly 20 calibrated 10 Hz bins after a hit,
while keeping acquisition work fixed at three probes. Consecutive visits to one
channel remain about 12 seconds apart, inside the documented 15-second channel
residence. The hop worker
uses the multi-hypothesis `pilot_symbolwise_v3` acquisition: a retained-field
replay confirmed a dual-RX channel-2 event that the six-probe coherent grid had
rejected. Reducing the probe count from six to three keeps the measured worker
runtime approximately unchanged while removing the coherent grid's documented
off-grid blind spot. Confirmed hop candidates reuse the qualified
full-duration template on all four low-band channels; zero full-frame support
falls back to the dense pilot artifact and never becomes a conditioned track.

Their final WNLS solution also does more than compare raw Doppler with an
uncorrected TLE.  It jointly estimates receiver position and a clock bias/drift
state for every satellite, weights measurements using estimated C/N0, and treats
TLE timing/orbit errors as corrected using a known receiver position in the
reported experiments.  Accordingly, this project first establishes blind RF
track identity and stable Doppler; receiver positioning follows with multiple
simultaneous identities, explicit satellite-clock nuisance states, and either a
reference-site calibration or jointly estimated ephemeris/time corrections.
For a linked cross-channel track, association fits one static intercept per
Pluto retune but only one bounded drift across the hypothesis. The calibrated
consensus has already removed RX1--RX0 differential LNB drift, leaving one
physical LNB and satellite-clock drift; granting every channel a separate drift
would let nuisance terms absorb the orbital curvature needed for identity.

The 10 MS/s path is intentionally described as periodic, not continuous. A
hardware trial needed 121.4 seconds of wall time to return 30 seconds of dual-RX
IQ. Investigation showed that pyadi constructed complex64 arrays from native
I/Q int16 components and the recorder immediately converted them back to CI16.
The beacon and hop recorders now request native CI16 components while FFT and
monitor paths retain complex64. In a field A/B test, a 120-second, dual-RX,
2.5 MS/s narrow capture improved from 125.550 seconds wall time (4.625% excess)
to 120.191 seconds (0.159% excess), with all 300 million samples per receiver
committed to the same 2.4 GB artifact format. This removes the measured
throughput deficit for future narrow captures without rewriting historical IQ.
Higher-rate continuous performance remains an empirical hardware question.

Before each dwell, the service applies a host thermal guard: capture pauses at
75 °C and resumes below 70 °C. This protects continuity and artifact integrity
without conflating thermal throttling with RF absence. Both host and Pluto
temperatures remain recorded in each capture manifest.

A bounded reader thread continuously refills the Pluto while a consumer thread
packs native components, hashes, and writes CI16 chunks.
`--sample-format=complex64` remains an explicit diagnostic fallback;
`native-ci16` is the
production default for raw beacon and channel-hop recording. Manifests include host-side read
duration, positive inter-read gaps, and host-read duty. These diagnose writer
stalls but are explicitly not treated as RF hardware timestamps; the current
Pluto/IIO path cannot independently prove an overrun that firmware does not
report. They are nevertheless the only measured UTC brackets available. The
10 Hz tracker therefore maps sample positions through the first/last IIO-read
midpoints retained in each five-second chunk instead of assuming that host UTC
advances at the configured RF sample rate. The report labels this piecewise
host interpolation, publishes its half-refill/gap uncertainty, and never claims
that the sample clock is a UTC clock. This matters when transport delivery takes
materially longer than nominal RF sample time.

## Operations

The production service is `leo-tracker-beacon-watch.service`. By default it
captures consecutive two-minute channel-4 lower-edge dwells and adds periodic
oversampled and wide LNB-offset comparisons on that same edge. In deployed
`offload` mode, completed IQ is atomically placed on a durable NVMe queue and
`leo-tracker-analysis-export.service` is its sole consumer. The exporter copies
each immutable recording into QNAP through a resumable partial directory and
then publishes the Kalman job marker atomically. All DSP runs on Kalman; the Pi
does not compete for CPU or queue ownership. Every fifth cycle it also keeps
one Pluto stream open for settled 1.5-second lower-edge dwells on channels
1--4. Those are the four
channels inside the installed universal LNB's 9.75 GHz low-band mode; channels
5--8 require the LNB's 22 kHz high-band selection and a 10.6 GHz LO model. Each
retune discards two complete IIO buffers and publishes a hop-session manifest
plus ordinary, independently checksummed child capture artifacts. Queue depth
is observable on disk and no capture is discarded merely because processing
falls behind. All candidate captures are preserved in the deployed
`LEO_BEACON_PRESERVE_RAW=1` mode. The code contains a bounded negative-control
ring for a future verified-retention deployment, but that policy is currently
disabled. Interrupted captures are moved to `quarantine` rather than deleted.
At startup, offload mode restores any `.running.<pid>` claim left by an older
local-mode worker back to `.job`; the persistent exporter then resumes it. Thus
a reboot or mode change between atomic capture completion and export cannot
strand or silently discard an observation. `LEO_BEACON_ANALYSIS_MODE=local`
retains the former two-worker analysis path only for an explicit offline test.

The systemd unit uses `KillMode=mixed` and a five-minute stop timeout. A normal
restart signals the watcher process first; Bash defers its shutdown trap until
the foreground checksummed capture finishes, then stops analysis children.
Only a capture that fails to drain within the bounded timeout is interrupted,
and its committed prefix remains recoverable through `quarantine`.

Data live under `/mnt/leo-nvme/leo-tracker`:

- `captures/<session>/manifest.json`: versioned parameters, timing, chunk hashes, and state.
- `captures/<session>/chunk-*.ci16`: atomic five-second, dual-RX IQ chunks.
- `reports/<session>.json`: structural, PSS, exact-pilot, control, CFO, and confidence evidence.
- `reports/plots/<session>.png`: PSS, pilot/control margin, and CFO evidence.
- `reports/followups/<session>.json`: dense replay and temporal-confirmation evidence.
- `reports/decoded/<session>.json`: decoder parameters and pilot/SSS confidence metrics.
- `reports/decoded/<session>.npz`: equalized symbols, decisions, and channel estimates.
- `reports/decoded/<session>.png`: dual-RX constellations and held-out decision maps.
- `reports/calibration/calibration.json`: cumulative empirical null distributions.
- `reports/fingerprints/*.json`: normalized cross-capture pilot/SSS and
  conditional receiver/channel signatures.
- `reports/fingerprints/index.json`: nearest matches and waveform-family
  clusters. These clusters are explicitly not satellite identity claims.
- `reports/tracks/<session>.json`: continuous full-frame 10 Hz carrier/Doppler
  observations, per-RX exact/control evidence, formal uncertainty, and relative
  LNB/RX offset/drift calibration.
- `reports/learned-beacons/*.json` and `.npz`: per-RX learned 2.5 MHz repeating
  waveforms, immutable training provenance, held-out pilot/control comparisons,
  and checksum-bound complex templates. `active.json` selects the qualified
  production template.
- `reports/associations/<session>.json`: held-out ranking of sufficiently long
  tracks against the retrospectively archived TLE catalog. Short arcs are
  explicitly rejected rather than named from pass overlap alone.
- `reports/channel-links/*.json`: conservative measured-fragment continuation
  hypotheses. Cross-channel links compare RF-normalized acceleration. Same-
  tuning links additionally require a low-residual joint quadratic through the
  measured pieces. Every observation retains its actual Ku-band RF, outages
  remain empty, and each channel has a separate oscillator nuisance group.
  These require held-out TLE association and are never identities by themselves.
- `staging/analysis-queue/*.job`: atomic pending-export work. In production the
  copy-only exporter claims it as `.exporting.<pid>`, verifies the destination,
  and only then publishes the QNAP analysis job. `.running.<pid>` is a legacy
  local-analysis claim and is restored to `.job` when offload mode starts.
- `quarantine/`: recoverable interrupted sessions.

Useful commands:

```bash
env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-capture /mnt/leo-nvme/leo-tracker/captures/test \
  --duration-s 120 --channel-number 3 --region lower-edge

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-analyze /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/test.json \
  --exact-acquisition-method pilot_symbolwise_v3

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-null-replay /mnt/leo-nvme/leo-tracker \
  /mnt/leo-nvme/leo-tracker/reports/calibration/pilot_symbolwise_v3-null

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-decode /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/followups/test.json \
  /mnt/leo-nvme/leo-tracker/reports/decoded/test.json \
  --plot /mnt/leo-nvme/leo-tracker/reports/decoded/test.png \
  --symbols /mnt/leo-nvme/leo-tracker/reports/decoded/test.npz

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-fingerprint /mnt/leo-nvme/leo-tracker

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-hop-capture /mnt/leo-nvme/leo-tracker/hop-sessions/test \
  --channels 1 2 3 4 --region lower-edge --dwell-s 1.5 --settle-buffers 2

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-template-learn /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/followups/test.json \
  /mnt/leo-nvme/leo-tracker/reports/learned-beacons/test.json \
  --samples /mnt/leo-nvme/leo-tracker/reports/learned-beacons/test.npz

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-frame-track /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/followups/test.json \
  /mnt/leo-nvme/leo-tracker/reports/frame-tracks/test.json \
  --samples /mnt/leo-nvme/leo-tracker/reports/frame-tracks/test.npz \
  --beacon-template /mnt/leo-nvme/leo-tracker/reports/learned-beacons/test.json

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-channel-link \
  /mnt/leo-nvme/leo-tracker/reports/channel-links/test.json \
  /mnt/leo-nvme/leo-tracker/reports/tracks/hop-test-*.json

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-radio \
  starlink-beacon-track /mnt/leo-nvme/leo-tracker/captures/test \
  /mnt/leo-nvme/leo-tracker/reports/followups/test.json \
  /mnt/leo-nvme/leo-tracker/reports/tracks/test.json \
  --measurement-source conditioned_frames \
  --frame-track /mnt/leo-nvme/leo-tracker/reports/frame-tracks/test.json

env UV_CACHE_DIR=.uv-cache uv run --active --no-sync leo-orbit associate \
  --observations /mnt/leo-nvme/leo-tracker/reports/tracks/test.json \
  --catalog /mnt/qnap01/mouse9911/satellites/leo-tracker/tle-history/latest.json \
  --lat 37.849165355010086 --lon -122.48567658142287 \
  --output /mnt/leo-nvme/leo-tracker/reports/associations/test.json
```

The continuous tracker starts only after blind exact-code acquisition. Each
independent dense acquisition conditions exact and rolled-control correlations
on every complete 4/3-ms frame until the next 100-ms acquisition boundary.
The v3 frame tracker then propagates the last acquired frame epoch, CFO, and CFO
rate through previously unsearched IQ in 100-ms windows. It searches only a
bounded ±12-sample timing neighborhood and terminates after three consecutive
losses. Continuation requires either three dual-RX frames with a 0.02 median
exact-minus-control margin, or a frame at the predicted timing that independently
exceeds 0.05 on both receivers. Continuous-signal, two-percent-PRF, and
seed-followed-by-noise tests cover these gates.
Eight coherent sub-frame correlations estimate CFO independently inside every
accepted 4/3-ms frame. Inactive neighboring frames therefore cannot corrupt an
active frame through noise-phase unwrapping. These
per-frame arrays, validity masks, scores, and uncertainties are compressed into
a checksum-bound NPZ artifact; no frame is inferred where the exact/control or
dual-receiver gates fail. A MAD-bounded, uncertainty-weighted stage then
aggregates dual-valid frames into calibrated 10 Hz points. Three ordinary frames
are required, while one or two frames are sufficient only when both receivers
exceed the stronger 0.05 sparse-frame margin.
Production narrow captures seed this tracker from twenty 10-ms acquisition
windows spaced six seconds apart. At the documented 2% baseline PRF, each
window spans about 7.5 frame opportunities; twenty independent windows retain
approximately 95% probability of intersecting at least one active frame. Two
successive captures raise the cumulative opportunity above 99%, while the lower
blind-search CPU keeps analysis ahead of capture and avoids thermal gaps in sky
monitoring.
When a qualified learned template is available, every narrow and low-band hop
acquisition also evaluates the full-duration frame at the independently selected
epoch and CFO on both receivers. A dual learned-minus-rolled-control margin of
0.05, matched frame epoch, and at most 15 kHz inter-receiver CFO difference form
an independent acquisition path. The report records whether published pilots,
the learned frame, or both supplied the candidate. Dense follow-up repeats the
same learned-frame test, and only its measured within-frame CFO may seed the
750 Hz tracker. This uses the paper's full-frame processing gain before lock while
retaining the two-receiver and negative-control protections; it does not convert
a single-receiver template match into RF evidence.
The consensus series remains receiver-referenced: absolute LNB, receiver, and
satellite clock terms are not mislabeled as geometric Doppler.
The production tracker may coast its frame lattice and frequency-rate prediction
through three consecutive 100-ms windows, but every missing bin remains
explicitly invalid. A tested explicit ten-window mode can bridge a half-second
synthetic silence, but real replay increased attempted windows from 57 to 150
while adding only one calibrated observation and no long-track duration, so it
is not the production default. Neither mode attempts to bridge the 15-second
channel-switch interval. Only a new dual-RX learned-frame or exact-code match
can end the coast and add measurements.
Only measured dual-RX epochs enter the orbital fit. This accommodates the
observed intermittent Starlink edge transmission without interpolating RF
evidence.
The direct tracker joins dense measured bursts across at most a five-second
outage. A second hypothesis stage may link fragments across as much as 30
seconds, but only when RF-normalized acceleration agrees and same-tuning pieces
form a joint quadratic with at most 2 kHz RMS residual. It never interpolates
the outage. A source fragment must contain at least five measured epochs over
at least 0.5 seconds; shorter detector excursions remain in their source report
but cannot inflate a continuation's wall-clock duration. This explicitly
accommodates the 15-second transmission/channel
reconfiguration reported by Kozhaya et al. while preserving the distinction
between a smooth hypothesis and a confirmed satellite identity.
The deployed watcher also rebuilds a rolling hypothesis from the eight most
recent channel-4 narrow track artifacts. Same-channel pieces on opposite sides
of a recorder boundary must pass the same joint-quadratic CFO and range-jerk
gates as pieces inside one artifact; reopening the stream does not weaken the
RF continuity test. Rolling TLE association is rate-limited to once per ten
minutes because its held-out stability sweep is substantially more expensive
than linking. The source observations remain immutable and no gap is filled.
The fixed-period epoch propagator remains an explicit fallback for synthetic or
single-seed replay. Its 200 Hz CFO bank is parabolically refined to a sub-bin
estimate and carries the resulting formal uncertainty into association.
The orbit-side association consumes the tracker's calibrated dual-receiver
consensus, fits a bounded affine LNB nuisance model, selects a small TLE epoch
adjustment on the training portion of the arc, and ranks the spacecraft on a
held-out temporal portion. Only epochs validated simultaneously on both
receivers enter that fit; single-path excursions cannot acquire a satellite
identity. Direct production tracks require at least 20 seconds, 45 measured
dual-RX epochs, and 18% temporal coverage before attempting a specific
association. Gapped hypotheses use 30 measured epochs and 10% coverage because
the held-out temporal split can place their earlier and later measured pieces
on opposite sides of the fit. In both cases an outage contributes no epoch.
The common oscillator-drift nuisance is bounded at 200 Hz/s. This is deliberately
above the empirical 90th percentile of reliable multi-second RX1-minus-RX0
calibration slopes while being 60% tighter than the original exploratory bound;
a free 500-Hz/s line could absorb enough LEO Doppler slope to change the winning
TLE without improving the RF evidence.
Current v2 association requires the specific NORAD identity to survive 50/50,
60/40, and 70/30 temporal splits plus a 20%-tighter oscillator-drift bound.
The production TLE epoch prior is bounded to ±2.5 seconds. Kassas et al. describe
the useful along-track correction as a small epoch adjustment (on the order of
milliseconds in their experiments); the wider operational bound also covers the
published host-timestamp uncertainty and modestly stale public elements without
letting an unrelated Starlink arc tens of seconds away win the catalog search.
A 0.5-second coarse grid locates the Doppler-error basin and a 0.05-second local
grid refines it, but every stability case still requires an interior epoch.
Exploratory replays may explicitly request a wider range, and must not be treated
as production identity evidence merely because that wider search finds a fit.

The first field acceptance artifact is
`ch4-lower-edge-narrow-20260806T094123Z-channel-link.json`. Replaying its frozen
IQ with piecewise host timing produced 85 calibrated dual-RX epochs over 40.751
seconds and associated them with STARLINK-36931 (NORAD 68000). The held-out RMS
was 249.7 Hz, the margin to the second candidate was 543.5 Hz, and the selected
identity survived every configured temporal-split and drift-bound case. Its
interior epoch correction was -1.75 seconds. Over the observed arc, predicted
elevation changed from 67.3 to 87.0 degrees and predicted Doppler changed by
148.7 kHz; the receiver-referenced CFO changed by the same 148.7 kHz after one
fitted LNB offset and a -7.7 Hz/s nuisance drift. The older capture publishes
0.144-second host timing uncertainty from chunk/read brackets. New captures
retain every refill midpoint and therefore use the finer
`iio_read_midpoint_interpolation` mapping automatically.

The first native-CI16 capture also supplied a controlled continuity test.
With the old five-second gap bound its 195 dual-valid observations fragmented
into three tracks no longer than 9.67 seconds, so none reached association.
Changing only the gap bound to 15 seconds (with reacquisition capped at 15 kHz)
formed a 29.57-second, 133-epoch track and qualified STARLINK-36035 (NORAD
67082). Held-out RMS was 83.8 Hz, margin to second place was 297.8 Hz, coverage
was 44.8%, and the fitted epoch correction was an interior -0.35 seconds. The
same NORAD passed all 50/50, 60/40, 70/30 and tighter-drift stability cases.
Those bounds are therefore production defaults. The grouping stage still
requires both receiver CFO trajectories and their differential offset to
extrapolate across an outage; the final orbit gates are unchanged.

The retention implementation supports bounded rings after a source has been
fully derived and independently archived. Its policy sizes are eight confirmed
captures, six negative controls, two wide captures, four oversampled captures,
and six complete channel-hop sessions; pending captures, learned-template
sources, and pinned scientific sources remain protected. However, the deployed
migration configuration sets `LEO_BEACON_PRESERVE_RAW=1` on SATPI01 and
`LEO_ANALYSIS_RETENTION_MODE=disabled` on Kalman. Those rings are therefore a
tested future policy, not the current deletion behavior. Interrupted captures
move to quarantine and no automatic procedure deletes NVMe source IQ.

If verified retention is later enabled, every qualified base, channel-linked,
or rolling TLE association is followed
back through its observation and source-track artifacts to the contributing raw
capture directories. Those paths are written atomically to
`reports/retention/qualified-capture-pins.json` before any pruning occurs. The
ledger is durable: a capture remains pinned even if a rolling association is
later overwritten by a new sky interval. A qualified child also pins its whole
hop session so its tuning manifest and sibling observations remain intact.

The fingerprint stage packs the 2,400 combined edge-pilot decisions into a
compact two-bit codeword, records the eight-subcarrier SSS consensus, and keeps
confidence, entropy, PSS, CFO, Doppler-link, and overlapping-pass context. Its
waveform score is chance-normalized symbol agreement. A separate conditional
channel score compares normalized amplitude and detrended phase through each
fixed LNB/RX path. The latter includes receiver and propagation effects and is
therefore not an emitter identifier. Satellite attribution still requires a
compatible Doppler trajectory/TLE or a future decoded identity field.

## Verification strategy

Unit tests pin published channel centers, pilot offsets, the PSS hexadecimal
sequence, its inversion/repetition pattern, all 16 pilot-code lengths, and the
4QAM code mapping. Synthetic tests exercise frame-period recovery, exact-PSS
epoch folding, pilot timing, off-grid carrier offsets from 2.5 through 337.5 kHz,
conditioned exact/control scoring, carrier offset refinement, dual-channel artifact
round trips, noise rejection, and the time-shifted pilot control. CLI E2E tests
run capture through analysis using the fake paired radio. A finite fake-radio
watcher test also exercises capture, bounded asynchronous analysis, dense
follow-up, retention, calibration, and clean pipeline drain through the same
bash entry point used by the service. Storage tests cover
chunk rollover, SHA-256 verification, interrupted manifests, discontinuity
rejection, AGC/manual-gain semantics, and evidence-aware retention.
Decoder tests pin the published SSS base-4 sequence and edge slices, recover
held-out pilots and SSS from a synthetic channel with CFO/frame phase/noise,
reject noise at chance-level accuracy, reject sample rates below the complete
1.875 MHz pilot span, recover a deliberately omitted 600 Hz carrier correction,
validate normalized soft probabilities, compare native 5 MS/s decoding against
the same IQ downsampled to 2.5 MS/s, and run JSON/NPZ/PNG generation through the public CLI.
Dashboard E2E coverage verifies both decoder artifact routes.
Fingerprint tests cover packed-state round trips, chance-level rejection,
invariance to gain and linear channel phase, legacy hard-symbol archives,
family clustering, CLI backfill, and dashboard artifact routes.

Hardware acceptance requires a complete capture with contiguous sample indexes,
monotonic timestamps, valid checksums, both receivers present, and a report.
A field detection additionally requires both receivers to pass the exact
pilot/control gates with consistent frame epoch and physically continuous CFO;
PSS is independent supporting evidence. Longer-term false-alarm thresholds are
calibrated from retained negative sky captures rather than chosen from a desired
result.

## Primary references

- Humphreys, Iannucci, Komodromos, and Graff, [Signal Structure of the Starlink
  Ku-Band Downlink](https://arxiv.org/abs/2210.11578), IEEE TAES 2023.
- Qin, Psiaki, Bowman, and Humphreys, [Pilots and Other Predictable Elements of
  the Starlink Ku-Band Downlink](https://arxiv.org/abs/2602.02627), 2026.
- Kozhaya, Saroufim, and Kassas, [Unveiling Starlink for PNT](https://navi.ion.org/content/72/1/navi.685),
  NAVIGATION 2025.
- Saroufim et al., [Navigating the Arctic Circle with Starlink and OneWeb LEO
  Satellites](https://people.engineering.osu.edu/media/document/2025-09-29/kassas_navigating_the_arctic_circle_with_starlink_and_oneweb_leo_satellites.pdf),
  2025.
