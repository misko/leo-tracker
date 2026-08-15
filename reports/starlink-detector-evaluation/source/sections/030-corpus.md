## The corpus

| Quantity | Measured | Moves? |
|---|---:|---|
| Sweep directories on the scan share | {{corpus_sweeps_on_share}} | fixed — collection paused |
| — captured with both radios, so pairable | {{corpus_pairable_sweeps}} | fixed |
| — single-radio, the collector outage | {{corpus_single_radio_sweeps}} | fixed |
| Corpus entries imported | {{corpus_entries_imported}} | fixed |
| Scored sidecars at figure-freeze time | {{corpus_freeze_low}} – {{corpus_freeze_high}} | grows; scoring is live |
| Paired sweeps the analysis could use | {{corpus_scored_pairs}} at the widest freeze | grows with scoring |
| — same-edge / opposite-edge | {{corpus_scored_pairs_same_edge}} / {{corpus_scored_pairs_opposite_edge}} | |
| Matched-arm cells behind the coincidence estimate | {{corpus_matched_arm_cells}} | |
| Live target observations behind the correlation matrix | {{corpus_live_target_observations}} | |
| Live cross-edge null observations behind the measured false-alarm rate | {{corpus_null_observations}} | |
| `lnb-a` observations excluded from the correlation matrix as a dead port — **an error, see below** | {{corpus_lnba_observations_excluded}} | |

Four receivers exist; three are live. `lnb-c` and `lnb-d` are the two inputs of
`pluto-19f2`; `lnb-b` and `lnb-a` are the two inputs of `pluto-5d4d`. All four are live in this corpus. `lnb-a` was excluded throughout on the grounds
that it "returns a flat ~1.19 peak-to-median at every tuning since 2026-08-13
04:44 UTC". **That exclusion is wrong for this corpus, and the figures inherit
it.** Re-measured on the same freeze with the repository's own fire logic,
`lnb-a` shows own-edge agreement phi {{corpus_lnba_own_edge_phi}} against {{corpus_lnba_cross_channel_phi}} across channels (ratio
{{corpus_lnba_phi_ratio}}, indistinguishable from `lnb-c`); a target/null fire ratio *equal* to
`lnb-c`'s; `differential-32` score separation close to `lnb-b`'s; and a coarse
peak-to-median that is not flat, and not distinguishable from `lnb-b`. The
cited 04:44 UTC failure falls inside `pluto-5d4d`'s
{{corpus_outage_from_utc}}–{{corpus_outage_to_utc}} outage, a window in which
that radio produced no data at all, and the scored corpus stops at
`{{corpus_last_scored}}` regardless. Within this corpus `lnb-a` was a
working receiver. Restoring it changes no headline number — redrawing
thresholds with its null included moves every receiver pair by at most
{{corpus_threshold_redraw_max_delta}} — but it supplies the same-model cross-radio contrast that section 8b
needs, and its absence is why that section previously reached a conclusion it
could not support.

Only {{corpus_scored_pairs}} of the {{corpus_pairable_sweeps}} pairable sweeps have been scored. That gap is the
cheapest available improvement to every number in this report, and it is the
first item in [section 10](#10-what-real-ground-truth-would-take).

---
