## 3. The apparatus: two radios, one instant

One collector process opens both Plutos and runs one thread per radio, with a
`threading.Barrier` at every tuning — eight tunings per sweep. Both radios
therefore sit on the same tuning at the same instant. IQ is written straight to
local NVMe, copied to the QNAP share in byte-for-byte verified batches, and
`sweep.json` is written last so its presence is the commit marker.

The design rationale is independence: separate LNBs, separate Plutos, separate
USB controllers on separate buses. Different radios fail independently, so
cross-radio agreement can stand in for the injection this site does not have.
The two receivers *inside* one Pluto share an ADC clock and a bus and do not
qualify — a distinction that turns out to matter in
[section 8](#8-what-the-sky-looks-like).

**How well the two radios are actually aligned is not known, and the recorded
number cannot tell you.** `skew_ms` is stamped at **barrier release** — before
the two threads write their different local-oscillator frequencies, and those
writes take different times. Every recorded value is therefore a **lower bound**
on the sample-start offset that matters.

| Quantity | Measured |
|---|---:|
| Paired tunings measured | {{s03_tunings}}, in {{s03_pairs}} scored pairs |
| Median | {{s03_median_ms}} ms |
| p90 / p99 / max | {{s03_p90_ms}} / {{s03_p99_ms}} / {{s03_max_ms}} ms |
| Beyond the {{s03_design_bound_ms}} ms design bound | {{s03_beyond}} ({{s03_beyond_pct2}}), in {{s03_sweeps_beyond}} of {{s03_pairs}} sweeps |
| Median, same-edge sweeps (n = {{s03_same_edge_n}}) | {{s03_same_edge_median_ms}} ms |
| Median, opposite-edge sweeps (n = {{s03_opposite_edge_n}}) | {{s03_opposite_edge_median_ms}} ms |
| Ratio between the two geometries, as recorded | **{{s03_geometry_ratio}}x** |
| True sample-start offset, per the share README | {{s03_readme_same_order_low_ms}}–{{s03_readme_same_order_high_ms}} ms same-order, ~{{s03_readme_opposite_order_ms}} ms opposite-order — **~{{s03_readme_ratio}}x** |
| Manifest / scan-share copies of every per-tuning skew that disagree | {{s03_skew_mismatches}} |

The last three rows are the important ones. The recorded skew is **blind to the
very axis it would be used to stratify on**: barrier-release skew differs
between the two geometries by {{s03_geometry_ratio}}x, while the README puts the true offset
between them at about {{s03_readme_ratio}}x. Any analysis that splits cells on "within the design
bound" against "beyond it" cannot see the effect it means to bound, and no such
split appears in this report.

The provenance of that claim is worth stating plainly too: `leo_tracker`'s own
`synchronised_scan.sweep_skew_event()` **refuses to certify** the basis for this
corpus. It raises on all {{s03_refused_sweeps}} paired sweeps, because the collector wrote
`leo-tracker.interim-synchronised-scan/v1` — a schema that names no `skew.event`
and is not one of the two versions the function recognises. {{s03_manifests_with_basis}} paired
manifests carry a `skew_basis` field that says barrier release, and the share
README says so in prose. The claim rests on those, not on code.

![Schematic of the two-thread barrier design, and the measured skew distribution](figures/apparatus.png)

***Figure 3 — the apparatus works, and the number that would prove it measures
the wrong event.*** *Panel (a) is a schematic — no measured data — showing where
`skew_ms` is stamped (barrier release) against where the offset that matters
begins (first sample); the gap between them is never stamped on this build.
Panel (b) is every paired tuning in the scored corpus, log-binned, split by
geometry. It reproduces the authoritative full-corpus run ({{s03_auth_beyond}} of {{s03_auth_tunings}}
tunings, {{s03_auth_beyond_pct1}}, median {{s03_auth_median_ms}} ms, max {{s03_auth_max_ms}} ms) on a corpus grown by {{s03_pairs_added}} pairs
since: {{s03_beyond}} of {{s03_tunings}}, {{s03_beyond_pct1}}, median {{s03_median_ms}} ms, max {{s03_max_ms}} ms — the worst tuning
is {{s03_worst_vs_bound}}x the design bound. Values in
[`figures/apparatus.json`](figures/apparatus.json).*

**Takeaway.** The apparatus does put two radios on one tuning at one instant and
records it reproducibly. The one number that would say how tightly is measuring
the rendezvous, not the radios, and cannot distinguish the two geometries the
experiment was built to compare.

---
