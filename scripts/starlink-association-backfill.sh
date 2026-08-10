#!/usr/bin/env bash
# Associate existing tracks against each catalog provider.
#
# Association is the last and cheapest analysis stage, so provider comparison
# does not need new observations: it can be rebuilt from track artifacts that
# already exist. This never touches the receipted association, only the
# per-source comparison outputs beside it.
set -euo pipefail

usage() {
  echo "usage: $0 [--limit N] [--sources 'a b'] [--scope SCOPE] [REPORTS_ROOT]" >&2
}

limit=0
from_qualified=0
sources="${LEO_ASSOCIATION_COMPARE_SOURCES:-space-track huggingface}"
scope="${LEO_ASSOCIATION_COMPARE_SCOPE:-starlink}"
while (( $# )); do
  case "$1" in
    --limit) limit="${2:-0}"; shift 2 ;;
    --sources) sources="${2:-}"; shift 2 ;;
    --scope) scope="${2:-}"; shift 2 ;;
    --from-qualified) from_qualified=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) break ;;
  esac
done
if (( $# > 1 )) || ! [[ "${limit}" =~ ^[0-9]+$ ]]; then usage; exit 2; fi

reports="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}/reports}"
store="${LEO_CATALOG_STORE_ROOT:-/mnt/qnap01/mouse9911/tle}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi
orbit() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-orbit "$@"; }

done_count=0 skipped=0 failed=0
shopt -s nullglob
for track in "${reports}"/tracks/*.json; do
  name="$(basename "${track}" .json)"
  # Roughly one track in seventy qualifies, so an unbiased sweep of the whole
  # corpus is slow. Seeding from recordings the receipted association already
  # qualified yields paired results immediately. That sample is biased toward
  # whichever provider the receipted run used, so it measures fit quality on
  # known-good tracks, not which provider qualifies more.
  if (( from_qualified )); then
    grep -q '"qualified": true' "${reports}/associations/${name}.json" 2>/dev/null || continue
  fi
  for source in ${sources}; do
    snapshot="${store}/latest/${source}/${scope}.json"
    output="${reports}/associations/${source}/${name}.json"
    # Rerunning is safe but pointless, and the corpus is large enough that
    # skipping finished work is what makes a resumed run cheap.
    if [[ -f "${output}" ]]; then skipped=$((skipped + 1)); continue; fi
    if [[ ! -f "${snapshot}" ]]; then
      echo "no ${source} snapshot at ${snapshot}" >&2
      skipped=$((skipped + 1)); continue
    fi
    mkdir -p "$(dirname "${output}")"
    if orbit associate --observations "${track}" --catalog "${snapshot}" \
        --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
        --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
        --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" \
        --output "${output}" >/dev/null 2>&1; then
      done_count=$((done_count + 1))
    else
      # One unassociable track must not stop a corpus-wide rebuild.
      rm -f -- "${output}"
      failed=$((failed + 1))
    fi
  done
  if (( limit > 0 && done_count >= limit )); then break; fi
done
shopt -u nullglob

printf '{"associated": %d, "skipped": %d, "failed": %d, "reports": "%s"}\n' \
  "${done_count}" "${skipped}" "${failed}" "${reports}"
