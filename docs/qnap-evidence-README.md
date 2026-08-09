# LEO Tracker cropped evidence archive

This directory stores verified, lossless time clips from full dual-receiver IQ
recordings. Raw sources form a six-hour working set after production-v2
publication; manual pins are the only indefinite raw exception.

- `evidence-v2/`: exact ci16 dual-RX clips and source manifests.
- `catalog/v2/plans/`: tiered interval selection reasons and sample ranges.
- `catalog/v2/references/`: freshly generated conservative replay references.
- `catalog/v2/comparisons/`: required-event replay coverage gates.
- `catalog/v2/receipts/`: successful archive transaction records.
- `staging/`: locks and resumable `.partial` publications.

Every published bundle must contain `verification.json` with `valid: true`.
Its matching `catalog/v2/receipts/<recording-id>.json` must have
`status: verified`, `source_verified: true`, and
`required_event_replay_valid: true`. Only that receipt plus the six-hour age,
analysis, classification, path and pin gates authorizes raw deletion.

The directory is an operational primary archive, not proof that every
historical source has been migrated and not an independent backup. Completeness
requires reconciling receipt identifiers against both the SATPI01 NVMe sources
and the QNAP analysis working set. See the version-controlled `docs/STORAGE.md`
and `docs/KALMAN_MIGRATION.md` in leo-tracker for audit commands, schemas,
preservation policy, backfill, and recovery procedures.
