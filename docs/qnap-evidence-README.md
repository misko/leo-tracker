# LEO Tracker cropped evidence archive

This directory stores verified, lossless time clips from full dual-receiver IQ
recordings. The complete source recordings remain on SATPI01 at
`/mnt/leo-nvme/leo-tracker` during development.

- `evidence/`: exact ci16 dual-RX clips and source manifests.
- `derived/`: reports, plots, tracks, decode and fingerprint sidecars.
- `catalog/plans/`: interval selection reasons and sample ranges.
- `catalog/receipts/`: successful archive transaction records.
- `staging/`: locks and resumable `.partial` publications.

Every published bundle must contain `verification.json` with `valid: true` and
`source_verified: true`. A verified cropped bundle does not authorize deleting
its full source. See the version-controlled `docs/STORAGE.md` in leo-tracker
for commands, schemas, preservation policy and recovery procedures.
