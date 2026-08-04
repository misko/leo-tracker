#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_root="${RADIO_ARTIFACT_ROOT:-$repo_dir/artifacts/starlink_hybrid_watch}"
archive_root="${RADIO_ARCHIVE_ROOT:-/mnt/qnap01/mouse9911/satellites/leo-tracker/radio-artifacts/starlink_hybrid_watch}"
minimum_age_minutes="${ARCHIVE_MINIMUM_AGE_MINUTES:-720}"
dry_run="${DRY_RUN:-0}"

if ! mountpoint -q /mnt/qnap01; then
  printf 'NFS archive mount is unavailable; leaving local artifacts untouched\n' >&2
  exit 1
fi
if ! [[ "$minimum_age_minutes" =~ ^[0-9]+$ ]] || (( minimum_age_minutes < 60 )); then
  printf 'ARCHIVE_MINIMUM_AGE_MINUTES must be an integer of at least 60\n' >&2
  exit 2
fi
mkdir -p "$archive_root"
exec 8>"$source_root/archive.lock"
flock -n 8 || exit 0

rsync_args=(--archive --relative --remove-source-files --ignore-existing)
cycle_args=(--archive --remove-source-files --ignore-existing)
if [[ "$dry_run" == "1" ]]; then
  rsync_args+=(--dry-run --itemize-changes)
  cycle_args+=(--dry-run --itemize-changes)
fi

for directory in chunks analysis observations plots tracker-ensemble coherent iq; do
  [[ -d "$source_root/$directory" ]] || continue
  while IFS= read -r -d '' path; do
    relative="${path#"$source_root"/}"
    nice -n 19 ionice -c 3 rsync "${rsync_args[@]}" \
      "$source_root/./$relative" "$archive_root/"
  done < <(find "$source_root/$directory" -maxdepth 1 -type f \
      -mmin "+$minimum_age_minutes" -print0)
done

if [[ -d "$source_root/cycles" ]]; then
  while IFS= read -r -d '' path; do
    relative="${path#"$source_root"/}"
    nice -n 19 ionice -c 3 rsync "${cycle_args[@]}" \
      "$path/" "$archive_root/$relative/"
    if [[ "$dry_run" != "1" ]]; then find "$path" -depth -type d -empty -delete; fi
  done < <(find "$source_root/cycles" -mindepth 1 -maxdepth 1 -type d \
      -mmin "+$minimum_age_minutes" -print0)
fi
