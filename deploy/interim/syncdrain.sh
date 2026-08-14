#!/usr/bin/env bash
# Ship completed synchronised sweeps to QNAP in batches, verify, then reclaim.
#
# A sweep is COMPLETE when sweep.json exists: the collector writes both .ci16
# files first and the sidecar last, so its presence is the commit marker.  The
# newest two directories are held back regardless, because the collector may be
# mid-write in one of them and a half-copied 400 MB probe that looks whole is
# worse than one that is obviously absent.
#
# Batches rather than one long rsync so kalman always has a settled set to work
# on instead of a directory that keeps changing under it.
set -uo pipefail
SRC=/mnt/leo-nvme/leo-tracker/sync-scans
DST=/mnt/qnap01/mouse9911/leo-scans
BATCH="${LEO_SYNC_DRAIN_BATCH:-25}"
KEEP_NEWEST=2
PAUSE="${LEO_SYNC_DRAIN_PAUSE_S:-20}"

while true; do
  mapfile -t all < <(ls -d "$SRC"/sync-*/ 2>/dev/null | sort)
  (( ${#all[@]} > KEEP_NEWEST )) || { sleep "$PAUSE"; continue; }
  eligible=()
  for d in "${all[@]:0:$(( ${#all[@]} - KEEP_NEWEST ))}"; do
    [[ -f "$d/sweep.json" ]] && eligible+=("$d")
  done
  (( ${#eligible[@]} )) || { sleep "$PAUSE"; continue; }

  batch=("${eligible[@]:0:$BATCH}")
  moved=0; failed=0; bytes=0
  for d in "${batch[@]}"; do
    name=$(basename "$d")
    # -rt rather than -a: the share refuses chgrp, so -a returns 23 while copying
    # the bytes perfectly.  The exit code is therefore not trustworthy here and
    # the size check below is the authority on whether anything may be reclaimed.
    nice -n 15 rsync -rt --partial-dir=.rsync-partial "${d%/}" "$DST/" >/dev/null 2>&1
    ok=1
    while IFS= read -r f; do
      rel="${f#"$d"}"
      a=$(stat -c %s "$f" 2>/dev/null || echo -1)
      b=$(stat -c %s "$DST/$name/$rel" 2>/dev/null || echo -2)
      [[ "$a" == "$b" ]] || { ok=0; break; }
      bytes=$(( bytes + a ))
    done < <(find "$d" -type f)
    if (( ok )); then rm -rf "$d"; moved=$(( moved + 1 ))
    else failed=$(( failed + 1 )); fi
  done
  printf '%s batch: moved %d failed %d (%.1f GB) | local %d remain, qnap %d held\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$moved" "$failed" "$(echo "$bytes/1073741824" | bc -l)" \
    "$(ls -d "$SRC"/sync-*/ 2>/dev/null | wc -l)" "$(ls -d "$DST"/sync-*/ 2>/dev/null | wc -l)"
  sleep "$PAUSE"
done
