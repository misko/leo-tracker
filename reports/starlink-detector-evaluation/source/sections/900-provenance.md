## Figures and provenance

Every figure is computed from the read-only corpus at
`/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/` and the read-only scan share
at `/mnt/qnap01/mouse9911/leo-scans/`. No value in any of them is typed in by
hand. Each PNG ships with the script that produced it and a JSON sidecar holding
every value it plots, so any number here can be re-derived — or contradicted —
without re-running anything.

| # | Figure | Sidecar | Frozen at | Population behind it |
|---:|---|---|---:|---|
| 1 | [`edge-pilots.png`](figures/edge-pilots.png) | [`edge-pilots.json`](figures/edge-pilots.json) | {{s900_f1_frozen}} | one capture, ranked over {{s900_f1_ranked}} target observations in the 640 ms / 10 MS/s arm |
| 2 | [`fire-rate-problem.png`](figures/fire-rate-problem.png) | [`fire-rate-problem.json`](figures/fire-rate-problem.json) | {{s900_f2_frozen}} | {{s900_f2_target}} target and {{s900_f2_null}} null observations; {{s900_f2_pairs}} paired sweeps |
| 3 | [`apparatus.png`](figures/apparatus.png) | [`apparatus.json`](figures/apparatus.json) | {{s900_f3_frozen}} | {{s900_f3_tunings}} paired tunings in {{s900_f3_pairs}} scored pairs |
| 4 | [`arm-matrix.png`](figures/arm-matrix.png) | [`arm-matrix.json`](figures/arm-matrix.json) | {{s900_f4_frozen}} | {{s900_f4_captured}} captured sweeps, {{s900_f4_matched}} matched-arm |
| 5 | [`geometry.png`](figures/geometry.png) | [`geometry.json`](figures/geometry.json) | {{s900_f5_frozen}} | {{s900_f5_captured}} captured, {{s900_f5_imported}} imported pairs, {{s900_f5_scored}} scored pairs |
| 6 | [`coincidence-model.png`](figures/coincidence-model.png) | [`coincidence-model.json`](figures/coincidence-model.json) | {{s900_f6_frozen}} | {{s900_f6_cells}} joined matched-arm cells from {{s900_f6_sweeps}} matched-arm sweeps |
| 7 | [`negative-control.png`](figures/negative-control.png) | [`negative-control.json`](figures/negative-control.json) | {{s900_f7_frozen}} | {{s900_f7_cells}} matched-arm cells in each of three joins; {{s900_f7_sweeps}} matched-arm sweeps |
| 8 | [`algorithm-correlation.png`](figures/algorithm-correlation.png) | [`algorithm-correlation.json`](figures/algorithm-correlation.json) | {{s900_f8_frozen}} | {{s900_f8_observations}} live target observations; {{s900_f8_pairs}} paired sweeps |
| 9 | [`channel-edge-correlation.png`](figures/channel-edge-correlation.png) | [`channel-edge-correlation.json`](figures/channel-edge-correlation.json) | {{s900_f9_frozen}} | {{s900_f9_units}} receiver-chain passes, `lnb-a` included ({{s900_f9_pairs}} pairs x {{s900_f9_receivers}} live receivers) |
| 10 | [`edge-agreement.png`](figures/edge-agreement.png) | [`edge-agreement.json`](figures/edge-agreement.json) | {{s900_f10_frozen}} | six receiver pairs, n = {{s900_f10_n}} each, `lnb-a` included |
| 11 | [`cfo-cliff.png`](figures/cfo-cliff.png) | [`cfo-cliff.json`](figures/cfo-cliff.json) | {{s900_f11_frozen}} | {{s900_f11_points}} live target points from {{s900_f11_pairs}} paired sweeps |
| 12 | [`port-bias.png`](figures/port-bias.png) | [`port-bias.json`](figures/port-bias.json) | {{s900_f12_frozen}} | {{s900_f12_pairs}} paired sweeps, {{s900_f12_points}} points, all four ports with `lnb-a` on its **measured** {{s900_f12_lnba_centre}} centre |
| 13 | [`absolute-centres.png`](figures/absolute-centres.png) | [`absolute-centres.json`](figures/absolute-centres.json) | **not stamped** | narrow sky sweeps and live narrow reports, the 2026-08-14 sync corpus; {{s900_f13_before}} detections before correction and {{s900_f13_after}} after |
| 14 | [`cross-receiver-bias.png`](figures/cross-receiver-bias.png) | [`cross-receiver-bias.json`](figures/cross-receiver-bias.json) | **census either side** | {{s900_f14_sampled}} scored sidecars and {{s900_f14_checks}} cross-receiver checks; the scored census moved {{s900_f14_before}} → {{s900_f14_after}} *while the audit read it*, which is why it records both rather than claiming a frozen count |

Figures 1–6, 8–10, 13 and 14 are new. Figures 7, 11 and 12 are carried unchanged from
[the detailed record](../sync-scan-cross-radio-2026-08-14/REPORT.md) together
with their scripts and sidecars, which is why they carry an earlier freeze.

All the scripts, their sidecars and the extractors that feed them are in
[`figures/`](figures/). Each figure runs in two steps — an extractor that
streams the corpus into a compact local cache, because the capture host is short
of memory, then the figure script itself. The extractors are prefixed by the group
they belong to: `abscal-pipeline-*` feeds Figure 13, `opening-pipeline-*` feeds Figures 1 and 3,
`firerate-pipeline-*` feeds Figures 2 and 6, `heatmaps-pipeline-*` feeds Figures
8 to 10, and `carried-pipeline-*` feeds Figures 7, 11 and 12. Figures 4 and 5
build their own cache on first run. The scripts import the repository's own
estimator from `/home/satpi01/leo-tracker/src`; change that path if the checkout
moves. The caches they build are not committed — they are large, and each is one
command to rebuild.

The census used by each figure is frozen by a snapshot step *before* the figure
computes and re-measured afterwards; both readings, and the list of sidecars
that landed in between, are recorded in each sidecar. Across every run behind
this report the recheck showed **{{s900_recheck_sweeps_added}} sweeps added and {{s900_recheck_sidecars_removed}} sidecars removed** — only
scoring advanced. **Figure 13 is the one exception**: it carries no census stamp,
so its population is bounded by the date ranges named in its own sidecar rather
than by a frozen count, and it should be re-run with a snapshot step before
anyone relies on its `n` values as a closed set. Collection stayed paused throughout, the collector, drain and
import services were neither stopped nor restarted, the radios were not touched,
and all share and NVMe paths were read only.

Raw sweeps and the format README are at `/mnt/qnap01/mouse9911/leo-scans`;
corpus entries at `/mnt/qnap01/mouse9911/leo/surveys/corpus`; collector, drain
and import implementations at `/mnt/leo-nvme/leo-tracker/bin/`. The
authoritative full-corpus estimator run that this report checks itself against
is preserved beside the detailed record as
[`review-full-corpus.txt`](../sync-scan-cross-radio-2026-08-14/review-full-corpus.txt).

### Injection provenance (sections 11, 13, 14)

The sky provenance above does not cover the injection sections, whose raw
records are archived separately at
[`injection-data/`](injection-data/) with a README giving the full rig
description. Summary:

| | |
|---|---|
| Radios | ADALM-Pluto Rev.C, fw `v0.38-plutoplus-spf-libiio-metadata-v5`. `104000bac495…` at `ip:192.168.1.183`; `1040007c4a94…` at `ip:192.168.1.165` |
| Topology | `TX2 → SMA splitter → 2× 30 dB → RX1 and RX2`, closed path, no antenna, no LNB, no RF path between the two radios |
| Waveform | `leo_tracker.radio.beacon.pilots.edge_pilot_frame` — the repository's own pilot frame, not a tone |
| LO / rate / probe | {{s900_inj_lo}}; {{s900_inj_rate}} MS/s; {{s900_inj_probe}} ms |
| Gains | RX manual {{s900_inj_rx_gain}} dB; the digital drive on `.183` and the extra cable loss on `.165` are in the README, not in any sidecar |
| Thresholds | {{s900_inj_far}}% per candidate point, drawn on TX-off input verified indistinguishable from a dark DAC |
| Carrier offset | natural offset near zero by construction (shared reference); offsets in 11c and T3 are **imposed** on the waveform |
| Records | `injection-data/{one-radio,radio-165,two-radio}/*.jsonl.gz`, plus the harnesses and the `analysis.py` helper the figure scripts import |
| Software | this repository at the commit that adds these records |

**Two gaps to be honest about.** The occupancy schedule's seed is recorded in
the run files but the schedule is not separately tabulated here; and no
environmental conditions were logged, which matters for any later attempt to
reproduce a level-dependent result on different hardware.

**Two artefacts carry no figure, and are committed anyway.** Both support claims
in [section 16](#16-the-150-khz-measured-four-oscillators-and-what-it-costs)
about hardware state rather than about the corpus, and hardware state is
overwritten:

| Artefact | Measured | What it pins |
|---|---|---|
| [`live-calibration-snapshot.json`](figures/live-calibration-snapshot.json) | {{s900_cal_utc}} | the calibration in force, and the timer that rewrites it. Section 16c's miscentring table is a claim about a file on the shared store; quoting it without capturing it would leave that claim unreproducible at the next firing |
| [`rate-limits.json`](figures/rate-limits.json) | {{s900_rl_utc}} | what sample rates the two capture radios accept, and that neither has a FIR loaded. This is why the 1.25 MS/s arm stopped working, and why its historical captures went through a filter nothing recorded |
