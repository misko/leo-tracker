#!/usr/bin/env bash
# Run every scanning algorithm over the interim synchronised sweeps, on all cores.
#
# WHERE THE DATA IS. The collector writes sync-<UTC>/ directories to the capture
# host's NVMe at /mnt/leo-nvme/leo-tracker/sync-scans, roughly 63 GB/h. This
# script runs on the analysis host, which reads the QNAP share and cannot see
# that NVMe. Nobody copies them by hand: leo-sync-drain.service runs
# syncdrain.sh on the capture host, which moves finished sweeps to the share in
# verified batches and deletes the local copy only once every file has matched
# its source byte for byte. Its destination is
#
#   /mnt/qnap01/mouse9911/leo-scans
#
# and that is the one and only place the sweeps land.
#
# THAT PATH IS NOT UNDER THE SHARED ROOT. leo-scans is its own top-level dataset
# on the share, a *sibling* of the shared root (/mnt/qnap01/mouse9911/leo) and
# not a directory inside it, exactly like leo-cropped. So --sweep-root does not
# default to something derived from SHARED_ROOT: the drain decides where the
# sweeps are, this script only reads them, and a default computed here would be
# a second opinion about the same path that drifts the moment either moves. Set
# LEO_SYNC_SWEEP_ROOT to override it without touching the shared root.
#
# Only whole sweeps count: the collector writes the IQ before sweep.json, so a
# directory without sweep.json is one still being written. This script skips
# those and counts them rather than reporting them as broken, so reading the
# share while the drain is mid-batch costs nothing but a second pass.
#
# The script does NOT require the share. --sweep-root takes any directory of
# sweeps and --corpus-root any destination, so it runs equally well on the
# capture host against the NVMe directly -- where the import hard-links instead
# of copying and costs no space at all.
#
# WHY TWO WORKER COUNTS. A scoring process is 3 OpenMP threads: the kernel calls
# omp_set_num_threads(fast_scan.DEFAULT_THREADS) and that overrides
# OMP_NUM_THREADS, so asking for one scoring worker per core oversubscribes the
# host threefold. 24 cores is 8 scoring workers and 20 usable cores is 6 or 7.
# Import is a read, a SHA-256 and a rename; it is I/O bound and defaults to one
# worker per core.
#
# WHICH CORPUS. ROOT/surveys/sync-corpus, never ROOT/surveys/corpus. The share's
# survey corpus is being scored by the analysis service right now, and 2,300
# entries from a twelve-arm experiment landing in it would both swamp that queue
# and pool with the four-arm pre-dwell draw in every aggregate that reads the
# corpus as one population.
#
# RE-RUNNING IS THE NORMAL CASE. Imported sweeps and scored entries are both
# skipped, so an interrupted run resumes where it stopped. Scoring the whole
# corpus is days of work; it is meant to be run again and again.
set -euo pipefail

# Usage goes to stdout when it was asked for and to stderr when it is a
# complaint, so `--help | less` shows something and an error still lands in the
# stream an operator is watching for errors.
usage() {
  cat <<'USAGE'
usage: starlink-sync-scan-analyse.sh [options] [SHARED_ROOT]

Import the interim synchronised sweeps into a survey corpus and score every
preserved probe with every candidate detector. Safe to re-run: imported sweeps
and scored entries are skipped, so an interrupted run resumes.

  --sweep-root DIR      sweep directories, or $LEO_SYNC_SWEEP_ROOT. Default:
                          /mnt/qnap01/mouse9911/leo-scans
                        That is where the drain -- leo-sync-drain.service on
                        the capture host -- moves finished sweeps, in verified
                        batches, all by itself. It is a sibling of SHARED_ROOT
                        and not a directory in it, so it is not derived from
                        SHARED_ROOT.
                        Nothing to copy by hand; if you ever must, rsync to
                        that same directory and nowhere else:
                          rsync -a /mnt/leo-nvme/leo-tracker/sync-scans/ \
                                   /mnt/qnap01/mouse9911/leo-scans/
                        Or point this at any directory of sweeps, including the
                        NVMe itself on the capture host.
  --corpus-root DIR     corpus to import into and score
                        (default: SHARED_ROOT/surveys/sync-corpus, which is
                        deliberately not the pre-dwell survey corpus)
  --import-workers N    parallel importers (default: one per core; I/O bound)
  --score-workers N     parallel scorers (default: cores/3, because each
                        scoring process is 3 OpenMP threads)
  --limit N             sweeps to import this pass
  --score-limit N       entries to score this pass
  --import-only         import and stop
  --score-only          score what is already imported
  --copy                copy the IQ instead of hard-linking it
  --rebuild             re-import and re-score everything
  --plan                print what would run, and run nothing
USAGE
}

sweep_root=""
corpus_root=""
import_workers=""
score_workers=""
limit=""
score_limit=""
do_import=1
do_score=1
plan=0
copy=0
rebuild=0
while (( $# )); do
  case "$1" in
    --sweep-root)     sweep_root="${2:-}"; shift 2 ;;
    --corpus-root)    corpus_root="${2:-}"; shift 2 ;;
    --import-workers) import_workers="${2:-}"; shift 2 ;;
    --score-workers)  score_workers="${2:-}"; shift 2 ;;
    --limit)          limit="${2:-}"; shift 2 ;;
    --score-limit)    score_limit="${2:-}"; shift 2 ;;
    --import-only)    do_score=0; shift ;;
    --score-only)     do_import=0; shift ;;
    --copy)           copy=1; shift ;;
    --rebuild)        rebuild=1; shift ;;
    --plan)           plan=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    -*)               usage >&2; exit 2 ;;
    *)                break ;;
  esac
done
if (( $# > 1 )); then usage >&2; exit 2; fi

shared_root="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
# Deliberately NOT ${shared_root}/sync-scans, and deliberately not derived from
# ${shared_root} at all: the sweeps are wherever syncdrain.sh's DST puts them,
# which is a top-level dataset beside the shared root rather than inside it.
# The corpus below is this script's own output and does belong to the shared
# root, so that one is still derived.
sweep_root="${sweep_root:-${LEO_SYNC_SWEEP_ROOT:-/mnt/qnap01/mouse9911/leo-scans}}"
corpus_root="${corpus_root:-${shared_root}/surveys/sync-corpus}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
niceness="${LEO_SYNC_ANALYSE_NICE:-15}"
score_script="${repo_dir}/scripts/starlink-survey-score-parallel.sh"

if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi
if [[ ! -x "${score_script}" ]]; then
  echo "missing ${score_script}; the scoring half is not re-implemented here" >&2
  exit 2
fi
# An unmounted share and a finished import must not look alike: both would
# otherwise print nothing and exit zero, and the operator would conclude the
# sweeps had all been analysed.
if (( do_import )) && [[ ! -d "${sweep_root}" ]]; then
  echo "no sweeps at ${sweep_root}" >&2
  echo "that directory is where leo-sync-drain.service moves finished sweeps;" >&2
  echo "if it is missing, check the drain and the share mount before copying" >&2
  echo "anything by hand:  systemctl status leo-sync-drain" >&2
  exit 2
fi

# LEO_SYNC_ANALYSE_CORES exists so the sizing can be tested without a 24-core
# host, and so an operator sharing the machine can say how much of it to use.
cores="${LEO_SYNC_ANALYSE_CORES:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"
if [[ -z "${import_workers}" ]]; then
  import_workers="${cores}"
fi
if [[ -z "${score_workers}" ]]; then
  score_workers=$(( cores / 3 ))
  (( score_workers < 1 )) && score_workers=1
fi
for pair in "import:${import_workers}" "score:${score_workers}"; do
  value="${pair#*:}"
  if ! [[ "${value}" =~ ^[0-9]+$ ]] || (( value < 1 )); then
    echo "--${pair%%:*}-workers must be a positive integer" >&2; exit 2
  fi
done
for pair in "limit:${limit}" "score-limit:${score_limit}"; do
  value="${pair#*:}"
  [[ -z "${value}" ]] && continue
  if ! [[ "${value}" =~ ^[0-9]+$ ]] || (( value < 1 )); then
    echo "--${pair%%:*} must be a positive integer" >&2; exit 2
  fi
done

echo "sweeps:         ${sweep_root}"
echo "corpus:         ${corpus_root}"
echo "cores:          ${cores}"
echo "import workers: ${import_workers} (I/O bound: a read, a SHA-256, a rename)"
echo "score workers:  ${score_workers} (each is 3 OpenMP threads, so not one per core)"
if (( plan )); then
  # Count the sweeps rather than only naming the directory. A plan that prints
  # a path proves the path was spelled, not that anything is there -- which is
  # exactly how a default pointing at an empty directory survived. It is one
  # readdir plus a stat per sweep, seconds on the share, and reads nothing.
  if (( do_import )); then
    finished=0
    unfinished=0
    for sweep in "${sweep_root}"/sync-*/; do
      if [[ -f "${sweep}sweep.json" ]]; then
        finished=$(( finished + 1 ))
      elif [[ -d "${sweep}" ]]; then
        unfinished=$(( unfinished + 1 ))
      fi
    done
    echo "finished sweeps: ${finished} (${unfinished} still being written)"
  fi
  echo "--plan: nothing was imported and nothing was scored"
  exit 0
fi

repo_env=(env "UV_CACHE_DIR=${repo_dir}/.uv-cache")
radio() {
  nice -n "${niceness}" "${repo_env[@]}" "${uv_bin}" run --active --no-sync \
    leo-radio "$@"
}

shard_root="$(mktemp -d "${TMPDIR:-/tmp}/leo-sync-import-XXXXXX")"
pids=()
# An importer that outlived the script would go on writing into the corpus with
# nothing left to name it by, so the trap takes the workers with it.
cleanup() {
  local status=$?
  if (( ${#pids[@]} )); then
    kill -TERM "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
  rm -rf "${shard_root}"
  exit "${status}"
}
trap cleanup EXIT INT TERM

if (( do_import )); then
  # Deal sweeps round-robin into one list file per worker. Lists rather than
  # symlink trees because a sweep is 6 MB to 820 MB of IQ and the importer
  # already takes a path; and round-robin rather than contiguous because the
  # names are UTC stamps and the arm is drawn per sweep, so a contiguous split
  # would hand one worker an hour of whichever arm was running then.
  mkdir -p "${corpus_root}"
  # What the corpus already holds, read in one pass. manifest.json is written
  # last and by rename, so its presence is the commit marker -- the same fact
  # the importer resumes on. Built as a set up front rather than probed per
  # sweep because the corpus is thousands of entries on a network share and a
  # glob per sweep would make the deal quadratic.
  declare -A committed=()
  if (( ! rebuild )); then
    for manifest in "${corpus_root}"/*/manifest.json; do
      [[ -f "${manifest}" ]] || continue
      entry="${manifest%/manifest.json}"
      committed["${entry##*/}"]=1
    done
  fi
  # A sweep is done when every radio it captured has a committed entry. The
  # radios are counted from the sweep's own IQ files, which is what the entries
  # are named after, so this stays a stat and never a JSON parse.
  sweep_committed() {
    local sweep="$1" sweep_id iq radio wanted=0 held=0
    sweep_id="${sweep%/}"; sweep_id="${sweep_id##*/}"
    for iq in "${sweep}"*.ci16; do
      [[ -f "${iq}" ]] || continue
      radio="${iq##*/}"; radio="${radio%.ci16}"
      wanted=$(( wanted + 1 ))
      if [[ -n "${committed["${sweep_id}-${radio}"]:-}" ]]; then
        held=$(( held + 1 ))
      fi
    done
    (( wanted > 0 && held == wanted ))
  }
  dealt=0
  done_already=0
  for sweep in "${sweep_root}"/sync-*/; do
    [[ -f "${sweep}sweep.json" ]] || continue
    # Skip what is already imported BEFORE the limit is spent on it. --limit N
    # is documented as "bound this pass, then run it again": counting sweeps
    # that are already in the corpus deals every pass the same first N, they
    # all come back "already imported", and the corpus stops growing after
    # pass one however many times the operator re-runs it.
    if (( ! rebuild )) && sweep_committed "${sweep}"; then
      done_already=$(( done_already + 1 ))
      continue
    fi
    printf '%s\n' "${sweep%/}" >> "${shard_root}/import-$(( dealt % import_workers )).list"
    dealt=$(( dealt + 1 ))
    if [[ -n "${limit}" ]] && (( dealt >= limit )); then break; fi
  done
  if (( dealt == 0 )); then
    # An empty share and a finished corpus must not print the same line either:
    # now that already-imported sweeps are skipped before the deal, "nothing to
    # deal" is the ordinary end state of a completed import and has to say so.
    if (( done_already )); then
      echo "all ${done_already} finished sweeps are already in ${corpus_root}"
    # A sweep is a directory. Observed on the share while this was written: a
    # drain was copying every sweep's three files into one flat directory, so
    # the path existed, held 102 MB of IQ, and contained not one sweep. Saying
    # "no finished sweeps" there sends the operator to look for a transfer that
    # has already run and destroyed what it moved.
    elif compgen -G "${sweep_root}/*.ci16" > /dev/null; then
      echo "${sweep_root} holds loose .ci16 files and no sync-<UTC>/ directories" >&2
      echo "a sweep is a directory of pluto-*.ci16 plus sweep.json; whatever" >&2
      echo "wrote these flattened them, and one sweep's files overwrite the next" >&2
      exit 2
    else
      echo "no finished sweeps at ${sweep_root}"
    fi
  else
    echo "dealing ${dealt} sweeps into ${import_workers} import workers" \
         "(${done_already} already imported)"
    extra=()
    (( rebuild )) && extra+=(--rebuild)
    (( copy )) && extra+=(--copy)
    started="$(date +%s)"
    for list in "${shard_root}"/import-*.list; do
      radio starlink-sync-import run "${shared_root}" \
        --sweep-root "${sweep_root}" --corpus-root "${corpus_root}" \
        --sweeps-from "${list}" "${extra[@]}" \
        > "${list%.list}.log" 2>&1 &
      pids+=("$!")
    done
    failed=0
    for pid in "${pids[@]}"; do wait "${pid}" || failed=$(( failed + 1 )); done
    pids=()
    echo "import finished in $(( $(date +%s) - started ))s, ${failed} worker(s) failed"
    "${repo_dir}/.venv/bin/python" - "${shard_root}" <<'PY'
import json, pathlib, sys
totals = {}
for log in sorted(pathlib.Path(sys.argv[1]).glob("import-*.log")):
    try:
        payload = json.loads(log.read_text())
    except ValueError:
        print(f"{log.name}: {log.read_text()[-400:]}")
        continue
    for key, value in payload.items():
        if isinstance(value, (int, float)):
            totals[key] = totals.get(key, 0) + value
    for problem in payload.get("errors") or []:
        print(f"  {problem['sweep']}: {problem['error']}")
print("imported {imported}, already imported {already_imported}, "
      "skipped radios {skipped}, unusable sweeps {unusable}, "
      "still being written {in_progress}, {gb:.1f} GB".format(
          gb=totals.get("bytes", 0) / 1e9,
          **{key: int(totals.get(key, 0)) for key in
             ("imported", "already_imported", "skipped", "unusable",
              "in_progress")}))
PY
    (( failed == 0 ))
  fi
fi

if (( do_score )); then
  # One sharding scheme, not two. starlink-survey-score-parallel.sh already
  # deals a corpus into shards of symlinks, warms the compiled kernel once
  # before fanning out, and kills its own workers on the way out; it takes the
  # corpus by environment, which is exactly the hook needed here.
  echo "scoring ${corpus_root} with ${score_workers} workers"
  score_args=(--workers "${score_workers}")
  [[ -n "${score_limit}" ]] && score_args+=(--limit "${score_limit}")
  (( rebuild )) && score_args+=(--rebuild)
  LEO_SURVEY_CORPUS_ROOT="${corpus_root}" \
    LEO_SURVEY_SCORE_NICE="${niceness}" \
    "${score_script}" "${score_args[@]}" "${shared_root}"
fi
