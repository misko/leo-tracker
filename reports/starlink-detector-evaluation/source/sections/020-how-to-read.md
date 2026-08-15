## How to read this report

Each numbered section is one idea, one figure, one takeaway, and is meant to be
readable on its own. The detailed experimental record — every intermediate
result, every disagreement between snapshots, the full claim ledger — is
[`reports/sync-scan-cross-radio-2026-08-14/REPORT.md`](../sync-scan-cross-radio-2026-08-14/REPORT.md)
and is not superseded by this document. This is the summary.

**One census note, stated once and then not revisited.** Radio collection was
paused by the operator at {{htr_collection_paused_utc}}, so sweep and pair counts are fixed
and do not move. Scoring has no timer and ran throughout, so the count of
scored sidecars does move. Every figure freezes its own list of scored
sidecars before computing and uses only that list; the four new figure groups
froze at {{htr_freeze_apparatus}}, {{htr_freeze_firerate}}, {{htr_freeze_joins}} and {{htr_freeze_armmatrix}} scored sidecars (for the two joining
groups, {{htr_paired_firerate}} and {{htr_paired_joins}} of those sit inside a pair), and the three figures
carried over from the detailed record froze earlier still, at {{htr_freeze_carried}}. A
population size quoted in one section can therefore differ from a neighbouring
section by a percent or two. Nothing here turns on that, no figure mixes
lists, and the range is {{htr_freeze_carried}}–{{htr_freeze_armmatrix}} throughout.

**Which receivers each figure uses.** `lnb-a` was excluded from the original
analysis in error (see
[section 14](#14-the-dead-port-and-the-stale-calibration-are-one-fault)) and has
been restored where restoring it was meaningful. The report therefore moves
between three- and four-receiver populations, and this is the map:

| Figure | Receivers | Why |
|---|---|---|
| `algorithm-correlation`, `channel-edge-correlation`, `edge-agreement`, `coincidence-model`, `f-strata`, `arm-matrix`, `geometry` | **all four** | fire-based; they never touch the frequency-offset axis, so the stale centre could not affect them |
| `port-bias` | **all four** | regenerated with `lnb-a` on its **measured** {{htr_lnba_measured_centre}} centre |
| `cfo-cliff` | three | offset-binned; built before `lnb-a`'s centre was measured, and its stale {{htr_lnba_recorded_centre}} would have distorted the axis |
| `edge-pilots` | one capture | a single spectrum, not a population |
| `negative-control`, `fire-rate-problem` | three | inherited from the earlier freeze |

**One limitation that does matter, and it constrains how every absolute number
here should be read.** Scoring runs in corpus order, so the scored set is a
*chronological prefix* of the campaign, not a sample of it. The share spans
{{htr_share_first_utc}} to {{htr_share_last_utc}}; of the {{htr_corpus_entries}} corpus entries only {{htr_corpus_scored}}
carry scores, and the last of them is `{{htr_last_scored}}` —
**nothing after that instant is scored at all**. Every figure in this report
therefore describes the opening stretch of the observing window.

That window is not stationary: the fire rate climbs through it. So: every
*absolute* rate in this report is a property of that opening stretch and
should not be read as a property of the sky in general. Every *comparison* —
between algorithms, between geometries, between joins — is paired inside that
window and is unaffected, which is why the results in sections 6, 7 and 8
stand regardless. It also explains why the census numbers above drift
monotonically rather than randomly: each later batch is later sky, and later
sky was busier.
