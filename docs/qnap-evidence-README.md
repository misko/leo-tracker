# LEO Tracker cropped evidence archive

This directory stores verified, lossless time clips from full dual-receiver IQ
recordings. Source recordings remain preserved across SATPI01
`/mnt/leo-nvme/leo-tracker` and the QNAP analysis working set during migration.

- `evidence/`: exact ci16 dual-RX clips and source manifests.
- `derived/`: reports, plots, tracks, decode and fingerprint sidecars.
- `catalog/plans/`: interval selection reasons and sample ranges.
- `catalog/receipts/`: successful archive transaction records.
- `staging/`: locks and resumable `.partial` publications.

Every published bundle must contain `verification.json` with `valid: true`.
Its matching `catalog/receipts/<recording-id>.json` must have `status: verified`
and `source_verified: true`. A bundle without that receipt is not a completed
archive transaction, and a verified cropped bundle does not authorize deleting
its full source.

The directory is an operational primary archive, not proof that every
historical source has been cropped and not an independent backup. Completeness
requires reconciling receipt identifiers against both the SATPI01 NVMe sources
and the QNAP analysis working set. See the version-controlled `docs/STORAGE.md`
and `docs/KALMAN_MIGRATION.md` in leo-tracker for audit commands, schemas,
preservation policy, backfill, and recovery procedures.
