#!/usr/bin/env bash
# Score the preserved survey corpus across many cores by dealing it into shards.
#
# `starlink-survey-score run` is one process and one process is three threads:
# fast_scan.DEFAULT_THREADS is 3 and the kernel calls omp_set_num_threads(), which
# overrides OMP_NUM_THREADS. So a 24-core analysis host left to itself runs at an
# eighth of its capacity and the corpus does not keep pace with the captures
# feeding it -- measured at 42 entries/hour against roughly 29 arriving.
#
# The scorer already takes --corpus-root, and an entry is a directory holding a
# manifest and a survey.ci16. So a shard is a directory of symlinks: each worker
# sees only its own entries, no worker needs a lock, and the sidecar is written
# through the link into the real corpus by the same temp-and-rename the single
# process uses. Nothing here writes to the corpus except those sidecars, and
# deleting a shard directory can never delete a probe.
#
# WORKERS defaults to cores/3 rather than cores, because each worker is already
# three threads. Asking for one worker per core oversubscribes threefold; that is
# what made a three-worker run on a four-core host only 78% efficient.
set -euo pipefail

usage() {
  echo "usage: $0 [--limit N] [--workers N] [--rebuild] [SHARED_ROOT]" >&2
  echo "  --limit N    entries to score in total (default: every unscored entry)" >&2
  echo "  --workers N  parallel workers (default: cores/3, since each takes 3 threads)" >&2
  echo "  --rebuild    also re-score entries that already carry a current sidecar" >&2
}

limit=""
workers=""
rebuild=0
while (( $# )); do
  case "$1" in
    --limit)   limit="${2:-}"; shift 2 ;;
    --workers) workers="${2:-}"; shift 2 ;;
    --rebuild) rebuild=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*)        usage; exit 2 ;;
    *)         break ;;
  esac
done
if (( $# > 1 )); then usage; exit 2; fi

shared_root="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
corpus_root="${LEO_SURVEY_CORPUS_ROOT:-${shared_root}/surveys/corpus}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
niceness="${LEO_SURVEY_SCORE_NICE:-15}"

if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi
if [[ ! -d "${corpus_root}" ]]; then
  echo "no corpus at ${corpus_root}" >&2
  exit 2
fi

if [[ -z "${workers}" ]]; then
  cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
  workers=$(( cores / 3 ))
  (( workers < 1 )) && workers=1
fi
if ! [[ "${workers}" =~ ^[0-9]+$ ]] || (( workers < 1 )); then
  echo "--workers must be a positive integer" >&2; exit 2
fi
if [[ -n "${limit}" ]] && { ! [[ "${limit}" =~ ^[0-9]+$ ]] || (( limit < 1 )); }; then
  echo "--limit must be a positive integer" >&2; exit 2
fi

radio() {
  env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
    leo-radio "$@"
}

# Shards are symlinks and live on local disk, never on the share: a shard is
# scratch, and putting scratch on the NFS mount would make every listing a
# network round trip in the hot loop.
shard_root="$(mktemp -d "${TMPDIR:-/tmp}/leo-survey-shards-XXXXXX")"
pids=()

# A worker that outlives the script would compete with the analysis host's own
# per-job scoring stage with nothing left to name it by, so the trap kills the
# process group rather than merely declining to wait for it.
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

# Deal unscored entries round-robin so every shard holds a mix rather than a
# contiguous slice: entries sort by channel first, so a contiguous split would
# give one worker every ch1 probe and skew both the load and the sample.
schema="$(radio starlink-survey-score status "${shared_root}" 2>/dev/null \
  | grep -o 'survey-detector-comparison/v[0-9]*' | head -1 || true)"
schema="${schema:-survey-detector-comparison/v2}"

dealt=0
for entry in "${corpus_root}"/*/; do
  [[ -f "${entry}survey.ci16" ]] || continue
  if (( ! rebuild )) && grep -q "${schema}" "${entry}scores.json" 2>/dev/null; then
    continue
  fi
  shard=$(( dealt % workers ))
  mkdir -p "${shard_root}/${shard}"
  ln -s "${entry%/}" "${shard_root}/${shard}/" 2>/dev/null || true
  dealt=$(( dealt + 1 ))
  if [[ -n "${limit}" ]] && (( dealt >= limit )); then break; fi
done

if (( dealt == 0 )); then
  echo "nothing to score at ${schema}"
  exit 0
fi
echo "dealt ${dealt} entries into ${workers} shards (~$(( (dealt + workers - 1) / workers )) each)"

# Build the kernel once, in one process, before any fan-out -- and only once we
# know there is work, since warming costs a compile on a host that has none.
# The cached object is keyed on the build (compiler, flags, and what
# -march=native resolved to), so a host that has never run this code has no
# object, and N workers starting together would each compile onto the same path.
# The compile is not atomic, and the live capture path loads the same object.
echo "warming the kernel cache..."
env UV_CACHE_DIR="${repo_dir}/.uv-cache" nice -n "${niceness}" \
  "${uv_bin}" run --active --no-sync python - <<'PY'
from leo_tracker.radio.beacon import fast_scan
fast_scan._load_kernel()
print("kernel ready")
PY

# The pass ends with the slowest shard, not the average one, so the estimate is
# stated as a floor rather than a promise.
echo "at ~86 s/entry this is at least $(( dealt * 86 / workers / 60 )) min; the"
echo "slowest shard sets the finish, historically about 1.5x the even split."

started="$(date +%s)"
for shard in $(seq 0 $(( workers - 1 ))); do
  [[ -d "${shard_root}/${shard}" ]] || continue
  extra=()
  (( rebuild )) && extra+=(--rebuild)
  nice -n "${niceness}" env UV_CACHE_DIR="${repo_dir}/.uv-cache" \
    "${uv_bin}" run --active --no-sync leo-radio starlink-survey-score run \
    "${shared_root}" --corpus-root "${shard_root}/${shard}" "${extra[@]}" \
    > "${shard_root}/${shard}.log" 2>&1 &
  pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
  wait "${pid}" || failed=$(( failed + 1 ))
done
pids=()

elapsed=$(( $(date +%s) - started ))
echo "finished in $(( elapsed / 60 ))m $(( elapsed % 60 ))s, ${failed} worker(s) failed"
grep -h '"scored"' "${shard_root}"/*.log 2>/dev/null | tail -n "${workers}" || true
(( failed == 0 ))
