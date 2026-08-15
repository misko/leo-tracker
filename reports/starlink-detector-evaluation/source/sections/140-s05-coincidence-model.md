## 5. Ground truth by coincidence: the model

Two chains that share only the sky are the substitute for injection. Let a sky
cell — one channel edge at one instant — be occupied with probability `f`. Chain
A detects an occupied cell with probability d_A, chain B with d_B, and either
chain still fires on an *empty* cell with probability `p`. Then:

```
P(A)  = f * d_A + (1 - f) * p
P(B)  = f * d_B + (1 - f) * p
P(AB) = f * d_A * d_B + (1 - f) * p^2
```

`P(A)`, `P(B)` and `P(AB)` are **counted** on the corpus. `p` is **measured**,
per cell, on the cross-edge null arm. Three equations, three unknowns, and out
come `f`, d_A and d_B: a detection probability with no known input anywhere.
That is the substitute for the injection this corpus never had — a substitute,
not an equal. Injection measures `d` against a signal you put there; this
**infers** `d` from an assumption that the two chains fail independently. If
that assumption is wrong, so is every `d` beside it.

The model carries its own consistency check, and it is sharp: **`f` is a
property of the sky.** All eight detectors read the same sky at the same
instant, so all eight must return the same `f`. The spread across the eight is
the check.

| Detector | measured `p` | `f` | d_A | d_B |
|---|---:|---:|---:|---:|
| `anchor-8` | {{s05_anchor8_p}} | {{s05_anchor8_f}} | {{s05_anchor8_da}} | {{s05_anchor8_db}} |
| `glrt-32` | {{s05_glrt32_p}} | {{s05_glrt32_f}} | {{s05_glrt32_da}} | {{s05_glrt32_db}} |
| `glrt-64` | {{s05_glrt64_p}} | {{s05_glrt64_f}} | {{s05_glrt64_da}} | {{s05_glrt64_db}} |
| `differential-16` | {{s05_diff16_p}} | {{s05_diff16_f}} | {{s05_diff16_da}} | {{s05_diff16_db}} |
| `differential-32` | {{s05_diff32_p}} | {{s05_diff32_f}} | {{s05_diff32_da}} | {{s05_diff32_db}} |
| `full-frame-verify` | {{s05_ffverify_p}} | {{s05_ffverify_f}} | {{s05_ffverify_da}} | {{s05_ffverify_db}} |
| `full-frame-full` | {{s05_fffull_p}} | {{s05_fffull_f}} | {{s05_fffull_da}} | {{s05_fffull_db}} |
| `full-frame-acquire` | {{s05_ffacquire_p}} | {{s05_ffacquire_f}} | {{s05_ffacquire_da}} | {{s05_ffacquire_db}} |
| **range over the eight** | **{{s05_p_min_pct}}–{{s05_p_max_pct}}%** | **{{s05_f_min}}–{{s05_f_max}}, spread {{s05_f_spread}}** | **{{s05_da_min}}–{{s05_da_max}}** | **{{s05_db_min}}–{{s05_db_max}}** |

*{{s05_matched_arm_cells}} joined matched-arm cells across four receiver pairs, `lnb-a` included;
`p` measured per detector on the live cross-edge null observations. **Every d_A
and d_B in this table is a model output, never checked against a known input.**
The model's own consistency check is
[section 6](#6-did-it-work-the-negative-controls), and it fails.*

Two things are worth reading off that table before going further.

**The null rate is measured per cell, and the units matter.** A per-*point* {{s05_null_per_point}}
threshold, maximised over roughly {{s05_points_per_cell}} candidate points to decide one tuning,
predicts 1 − {{s05_null_per_point_survive}}^{{s05_points_per_cell}} = {{s05_predicted_per_cell}} per cell. The measured {{s05_p_min_pct}}–{{s05_p_max_pct}}% is therefore what
correct calibration looks like, not a broken threshold — framing it as "6%, not
the nominal 1%" implied a defect that is not there. What does matter is that
every `d` above depends on this denominator, and that assuming the per-point
figure at cell level pushes methods out of physical range for reasons unrelated
to the methods. Two caveats travel with it: this is the **cross-edge
target-code null**, target-code-free by construction rather than physically
empty sky, and it may still hold other Starlink energy, interference and
receiver structure; and the calibration is **in-sample**, since the same null
population sets the threshold and then measures the rate.

**The one sky parameter does not come out as one number.** Pooled, the eight
return `f` {{s05_f_min}}–{{s05_f_max}}, a spread of {{s05_f_spread}}. That is the model's own check,
already unmet. And the extremes move with geometry rather than sitting on one
misbehaving algorithm: on opposite-edge cells the minimum is `{{s05_opp_argmin}}` at {{s05_opp_f_min}}
(spread {{s05_opp_f_spread}} over {{s05_opp_cells}} cells); on same-edge cells the minimum is `{{s05_same_argmin}}`
at {{s05_same_f_min}} (spread {{s05_same_f_spread}} over {{s05_same_cells}} cells). Near-identical spread, different
argument minimum. The invariance failure is a property of the estimate, not of
one algorithm.

The solver reproduces the authoritative full-corpus run closely on the
opposite-edge cells: `anchor-8` f {{s05_verify_anchor8_recomputed}} against {{s05_verify_anchor8_published}} reported, `glrt-32`
{{s05_verify_glrt32_recomputed}} against {{s05_verify_glrt32_published}}, `full-frame-full` {{s05_verify_fffull_recomputed}} against {{s05_verify_fffull_published}} — every `f` within
{{s05_verify_max_gap}}.

![The coincidence model as a schematic, and the f, dA and dB it returns per detector](figures/coincidence-model.png)

***Figure 6 — three equations recover `d`, if the one sky they assume is really
there.*** *Left panel is a schematic — no data — of the model and of what is
counted, measured and solved. Right panel is the solution per detector, split by
geometry (filled = opposite-edge, open = same-edge): sky occupancy `f` in the
narrow band on the left, and d_A / d_B at {{s05_d_low}}–{{s05_d_high}} on the right. Every point on
the right half of that panel is a model output. n = {{s05_matched_arm_cells}} joined matched-arm
cells ({{s05_opp_cells}} opposite-edge, {{s05_same_cells}} same-edge) from {{s05_matched_arm_sweeps}} matched-arm sweeps of
{{s05_paired_sweeps}} paired. Values in
[`figures/coincidence-model.json`](figures/coincidence-model.json).*

**Takeaway.** The model is solvable and returns physically plausible numbers.
Whether it is *entitled* to is a separate question, and it has a direct
experimental answer.

---

### 5b. What the model assumes, and what this corpus already contradicts

The implementation passes **one pooled `p`** to both chains, uses **`p^2`** for
joint null firing rather than a measured joint null, and fits **one `d_A`, `d_B`
pair across every included cell**. That requires:

| Assumption | Status in this corpus |
|---|---|
| Both chains share one false-alarm rate | contradicted — `p` runs {{s05_p_min_pct}}–{{s05_p_max_pct}}% across methods and varies by receiver |
| False alarms independent across chains, so joint null firing is `p^2` | untested; common interference or shared receiver structure would break it |
| `d` constant across acquisition arms | contradicted — `f` moves {{s05_arm_f_low}} to {{s05_arm_f_high}} across the twelve arms |
| `d` constant across receivers, channels and time | contradicted on all three — see 8b, 8a, and the fire-rate swing across the window |
| Detections conditionally independent given occupancy | untestable here; latent signal-strength variation alone would break it |

Pooling heterogeneous strata can generate covariance and distort `f`, `d_A` and
`d_B` even when the chains are conditionally independent *within* each
homogeneous stratum. A version worth trusting would need separate `p_A` and
`p_B`, arm- and receiver-specific parameters, sweep-level effects, and probably
a hierarchical treatment of latent signal strength.

None of this weakens section 6 — it is a second, independent reason not to read
the `d` values as measurements.
