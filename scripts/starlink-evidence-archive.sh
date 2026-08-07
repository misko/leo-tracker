#!/usr/bin/env bash
# Copy verified, cropped evidence to QNAP without modifying local source IQ.
set -euo pipefail

usage() {
  echo "usage: $0 [--watch] [--limit N] [--recording ID] [SOURCE_ROOT] [QNAP_ROOT]" >&2
}

watch=0
limit=0
recording=""
while (( $# )); do
  case "$1" in
    --watch) watch=1; shift ;;
    --limit) limit="${2:-}"; shift 2 ;;
    --recording) recording="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) break ;;
  esac
done
if (( $# > 2 )) || ! [[ "${limit}" =~ ^[0-9]+$ ]]; then usage; exit 2; fi

source_root="${1:-${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}}"
qnap_root="${2:-${LEO_EVIDENCE_ROOT:-/mnt/qnap01/mouse9911/leo-cropped}}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
poll_s="${LEO_EVIDENCE_POLL_S:-30}"
minimum_age_s="${LEO_EVIDENCE_MINIMUM_AGE_S:-600}"
minimum_qnap_free_gb="${LEO_EVIDENCE_MINIMUM_QNAP_FREE_GB:-100}"
guard_s="${LEO_EVIDENCE_GUARD_S:-10}"
control_duration_s="${LEO_EVIDENCE_CONTROL_DURATION_S:-1}"
control_count="${LEO_EVIDENCE_CONTROL_COUNT:-3}"

if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi
mkdir -p "${qnap_root}/staging" "${qnap_root}/catalog/receipts"
exec 9>"${qnap_root}/staging/evidence-archive.lock"
if ! flock -n 9; then
  echo "another evidence archiver owns ${qnap_root}" >&2
  exit 2
fi

radio() {
  env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-radio "$@"
}

processed=0
while true; do
  found=0
  while IFS= read -r -d '' manifest; do
    capture="$(dirname "${manifest}")"
    name="$(basename "${capture}")"
    [[ -z "${recording}" || "${name}" == "${recording}" ]] || continue
    [[ ! -f "${qnap_root}/catalog/receipts/${name}.json" ]] || continue
    [[ -f "${source_root}/reports/${name}.json" &&
       -f "${source_root}/reports/followups/${name}.json" ]] || continue
    state="$(${repo_dir}/.venv/bin/python -c \
      'import json,sys; print(json.load(open(sys.argv[1])).get("state", ""))' "${manifest}")"
    [[ "${state}" == "complete" ]] || continue
    age_s=$(( $(date +%s) - $(stat -c %Y "${manifest}") ))
    [[ -n "${recording}" || ${age_s} -ge ${minimum_age_s} ]] || continue
    free_kb="$(df -Pk "${qnap_root}" | awk 'NR==2 {print $4}')"
    if (( free_kb < minimum_qnap_free_gb * 1024 * 1024 )); then
      echo "qnap_space_backoff free_kb=${free_kb} minimum_free_gb=${minimum_qnap_free_gb}" >&2
      break
    fi
    found=1
    started="$(date +%s)"
    echo "evidence_start recording=${name} source=${capture} qnap=${qnap_root}"
    radio starlink-evidence-archive "${capture}" "${source_root}/reports" "${qnap_root}" \
      --guard-s "${guard_s}" --control-duration-s "${control_duration_s}" \
      --control-count "${control_count}"
    elapsed=$(( $(date +%s) - started )); processed=$((processed + 1))
    echo "evidence_done recording=${name} elapsed_s=${elapsed} processed=${processed}"
    if (( limit > 0 && processed >= limit )); then exit 0; fi
  done < <(find "${source_root}/captures" -mindepth 2 -maxdepth 2 \
            -type f -name manifest.json -print0 | sort -z)
  (( watch == 1 )) || break
  if (( found == 0 )); then
    echo "evidence_idle processed=${processed} poll_s=${poll_s}"
  fi
  sleep "${poll_s}"
done
echo "evidence_complete processed=${processed}"
