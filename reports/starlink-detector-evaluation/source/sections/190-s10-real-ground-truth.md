## 10. What real ground truth would take

The single sentence: **every limitation above traces back to having no known
input.** Injecting a known edge-pilot *waveform* — not a tone — with controlled
amplitude, carrier offset, epoch and frame occupancy converts the corpus from
model output to measurement, and it is the only item here that changes what the
apparatus can prove. A tone calibrates the local oscillator and the gain, but it
cannot validate a code-aided detector: the thing under test is a correlation
against a known code, so the injected signal must carry that code.

Short of that, in order of cost:

| Step | Cost | What it buys |
|---|---|---|
| **Score the rest of the pairable corpus** | none — no new collection, no new code | every interval in this report tightens; ~{{s10_data_multiple}}x more data, already on disk |
| Fix the skew stamp: retune before the barrier, bump the schema | one collector change | makes `skew_ms` the sample-start offset, and lets `sweep_skew_event()` certify it |
| Derive the skew bound the coincidence model actually needs | analysis only | {{s10_design_max_ms}} ms and the README's 0.2–0.5 s operator target cannot both be the requirement |
| Measure the `gen2` LO offsets with `lo_sweep` | one sweep | turns {{s10_lnbc_applied_khz}} from one number into a measurement with an error bar |
| Widen the offset search past {{s10_cliff_edge}} kHz and re-score one arm | one arm | if the cliff moves, the search span explains it; if not, that explanation is wrong |
| Add a genuinely different statistic to the bank | development | at phi {{s10_phi_min}}–{{s10_phi_max}} the eight are one detector; agreement among them is not information |
| **Inject a known signal** | hardware | replaces every model-output `d` in this report with a measurement |

**The cheapest item is first, and it is very cheap.** {{s10_captured}} sweeps were
captured; {{s10_unpaired}} of them lost `pluto-5d4d` entirely to a collector fault, leaving
**{{s10_pairable}} pairable** sweeps. That loss is not geometry-selective — {{s10_lost_same}} same-edge
against {{s10_lost_opposite}} opposite-edge — so it does not bias the comparison. But only
**{{s10_scored}}** of those {{s10_pairable}} pairs are scored. **There is roughly {{s10_data_multiple}}x more pairable
data already captured than has been analysed**, sitting on the share, needing no
radio time, and scoring has no timer driving it. Every number in this report was
computed on about a fifth of the available pairs; the counts at each stage are
in [Figure 5](#4-the-dataset-twelve-arms-and-two-geometries-for-free).

**And a definition of success, so the next attempt is checkable.** A working
test of the coincidence model is one where a join the model forbids produces a
*visibly worse* consistency check than the real join. Today it produces a better
one ([Figure 7](#6-did-it-work-the-negative-controls)). Until a negative control
fails, cross-radio agreement is not evidence, and no ranking of these eight
detectors — by fire count, by excess over the measured null, or by model output
`d` — should be reported as a result.

---
