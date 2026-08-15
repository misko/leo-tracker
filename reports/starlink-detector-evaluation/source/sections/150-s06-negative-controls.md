## 6. Did it work? The negative controls

This is the test that decides the report, and it is cheap: build joins where the
coincidence model is **definitionally false**, run the identical estimator, and
see whether the consistency check notices.

Two controls, alongside the real join:

- **Shifted** — radio B taken two instants later within the same sweep. The two
  sides are then on different tunings, so there is no shared sky cell and the
  model cannot hold.
- **Scrambled** — radio A of one sweep joined to radio B of a *different* sweep,
  matched on arm and geometry, with only the pairing broken. Partners are a
  median {{s06_scramble_median_s}} s apart (minimum {{s06_scramble_min_s}} s, maximum {{s06_scramble_max_s}} s), so the two sides
  never saw the sky at the same time.

Thresholds and the cross-edge null rate `p` are drawn **once** from the cross-edge
null arms and held fixed across all three joins, so the join is the only thing
that changes.

| Join | Model can hold? | Cells | `f` range | mean `f` | Spread across the eight | Resample p05–p95 | Spread over its own noise median |
|---|---|---:|---|---:|---:|---|---:|
| **Real** — radio A and radio B of one sweep, same instant | yes | {{s06_real_cells}} | {{s06_real_f_min}}–{{s06_real_f_max}} | {{s06_real_f_mean}} | **{{s06_real_spread}}** | {{s06_real_boot_p05}}–{{s06_real_boot_p95}} | {{s06_real_spread_ratio}}x |
| **Shifted** — radio B at instant *i*+{{s06_shift_instants}} | **no** | {{s06_shifted_cells}} | {{s06_shifted_f_min}}–{{s06_shifted_f_max}} | {{s06_shifted_f_mean}} | **{{s06_shifted_spread}}** | {{s06_shifted_boot_p05}}–{{s06_shifted_boot_p95}} | {{s06_shifted_spread_ratio}}x |
| **Scrambled** — radio A joined to another sweep | **no** | {{s06_scrambled_cells}} | {{s06_scrambled_f_min}}–{{s06_scrambled_f_max}} | {{s06_scrambled_f_mean}} | **{{s06_scrambled_spread}}** | {{s06_scrambled_boot_p05}}–{{s06_scrambled_boot_p95}} | {{s06_scrambled_spread_ratio}}x |

Read the three spread columns. The quantity offered as validation — the
tightness of the eight algorithms' agreement on `f` — is **smallest on the join
where the model is most obviously false**. Every join's observed spread sits at or below
its own sampling-noise median. There is no failure mode: whatever you feed this
check, it passes.

The result is not that the estimator returns the same answer three times. `f`
itself moves **{{s06_f_move_ratio}}x** across the joins, {{s06_real_f_mean}} to {{s06_shifted_f_mean}} to {{s06_scrambled_f_mean}}, so these really
are different data and the estimator really is responding to them. What does not
move is the thing being used as evidence.

![Sky occupancy f across three joins, and each join's spread against its own sampling noise](figures/negative-control.png)

***Figure 7 — the consistency check cannot fail.*** *Same estimator, same {{s06_real_cells}}
matched-arm cells in each of the three joins, same thresholds and same empty-sky
rate; only the pairing changes. Left: `f` moves {{s06_f_move_ratio}}x across the joins while all
eight algorithms move together, so every cluster stays tight — including on the
two joins the model forbids. Right: each join's observed spread against the
p05–p95 band of {{s06_boot_draws}} joint resamples of that same join, with the noise median
marked; all three land at or below their own noise median, the scrambled join
furthest below at {{s06_scrambled_spread_ratio}}x. Population: {{s06_pairs_matched_arm}} matched-arm paired sweeps, {{s06_entries}}
scored sidecars in a pair. Estimator
`leo_tracker.radio.beacon.cross_radio`, unmodified. Values in
[`figures/negative-control.json`](figures/negative-control.json).*

**Takeaway.** Cross-detector agreement on `f` is not evidence for the
coincidence model, and the cross-radio apparatus has not been shown to deliver
the ground truth it was built to deliver. Every `d` anywhere in this report is a
model output from this model. Most detector comparisons never run this control.
This one did, and it came back negative — that is the contribution.

---

