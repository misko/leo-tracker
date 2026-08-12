#!/usr/bin/env bash
# Convert beacon reports into the queryable probe index, on the analysis host.
#
# Reports are the system of record and stay untouched. This projects the
# per-probe columns that questions actually use into day-partitioned Parquet,
# which is roughly five hundred times smaller and answers in milliseconds what
# otherwise costs minutes of opening JSON.
#
# Safe to run repeatedly and on a timer. Only days whose report count has moved
# are rebuilt, so in steady state this touches the current day alone. Two
# builders may overlap without harm: each stages to a private file and renames
# atomically, and a run that finds another already working reports that it
# skipped rather than failing.
set -euo pipefail

usage() {
  echo "usage: $0 [--status] [--rebuild] [--check] [ROOT]" >&2
  echo "  --status   report what is indexed against its reports, build nothing" >&2
  echo "  --rebuild  rebuild every day, not only those that have changed" >&2
  echo "  --check    verify prerequisites and exit" >&2
}

mode="build"
rebuild=()
while [[ $# -gt 0 ]]; do
  case "${1}" in
    --status) mode="status"; shift ;;
    --rebuild) rebuild=(--rebuild); shift ;;
    --check) mode="check"; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) break ;;
  esac
done
if (( $# > 1 )); then usage; exit 2; fi

root="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-${HOME}/.local/bin/uv}"
uv_cache="${UV_CACHE_DIR:-${repo_dir}/.uv-cache}"

if [[ ! -d "${root}/reports" ]]; then
  echo "no reports directory under ${root}; is shared storage mounted?" >&2
  exit 2
fi

# The capture host must never be blocked by analysis tooling, so duckdb is an
# optional extra rather than a dependency. That means the analysis host has to
# ask for it explicitly, and the failure when it is missing must say so.
# Niced and idle-scheduled: the analysis host is also performing DSP for live
# captures, and a projection that is minutes stale costs nothing while a
# delayed capture is gone for good.
launcher=(nice -n "${LEO_PROBE_INDEX_NICE:-15}")
if command -v ionice >/dev/null 2>&1; then
  launcher+=(ionice -c 3 -t)
fi

run_radio() {
  if [[ -x "${repo_dir}/.venv/bin/leo-radio" ]]; then
    "${launcher[@]}" "${repo_dir}/.venv/bin/leo-radio" "$@"
  elif [[ -x "${uv_bin}" ]]; then
    env UV_CACHE_DIR="${uv_cache}" "${launcher[@]}" "${uv_bin}" run \
      --active --no-sync --directory "${repo_dir}" leo-radio "$@"
  else
    echo "found neither ${repo_dir}/.venv/bin/leo-radio nor uv at ${uv_bin}" >&2
    exit 2
  fi
}

have_duckdb() {
  if [[ -x "${repo_dir}/.venv/bin/python" ]]; then
    "${repo_dir}/.venv/bin/python" -c "import duckdb" 2>/dev/null
  else
    python3 -c "import duckdb" 2>/dev/null
  fi
}

if ! have_duckdb; then
  echo "duckdb is not installed; the probe index needs the analysis extra:" >&2
  echo "  uv pip install --python ${repo_dir}/.venv 'leo-tracker[analysis]'" >&2
  echo "  (or: ${repo_dir}/.venv/bin/pip install 'duckdb>=1.0')" >&2
  exit 3
fi

if [[ "${mode}" == "check" ]]; then
  printf '{"root":"%s","reports":%d,"duckdb":true}\n' \
    "${root}" "$(find "${root}/reports" -maxdepth 1 -name '*narrow*.json' | wc -l)"
  exit 0
fi

if [[ "${mode}" == "status" ]]; then
  run_radio starlink-probe-index status "${root}"
  exit 0
fi

run_radio starlink-probe-index build "${root}" "${rebuild[@]}"
