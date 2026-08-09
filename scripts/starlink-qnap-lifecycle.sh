#!/usr/bin/env bash
# Publish QNAP raw-IQ lifecycle plans; deletion requires an explicit enable gate.
set -euo pipefail

shared_root="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
archive_root="${2:-${LEO_EVIDENCE_ROOT:-/mnt/qnap01/mouse9911/leo-cropped}}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
enabled="${LEO_QNAP_RECLAIM_ENABLED:-0}"
interval_s="${LEO_QNAP_RECLAIM_INTERVAL_S:-3600}"
maximum_tier="${LEO_QNAP_RECLAIM_MAXIMUM_TIER:-0}"
minimum_age_hours="${LEO_QNAP_RECLAIM_MINIMUM_AGE_HOURS:-24}"
trigger_free_gb="${LEO_QNAP_RECLAIM_TRIGGER_FREE_GB:-500}"
target_free_gb="${LEO_QNAP_RECLAIM_TARGET_FREE_GB:-750}"
ignore_pressure="${LEO_QNAP_RECLAIM_IGNORE_PRESSURE:-0}"
plan="${shared_root}/reports/retention/qnap-lifecycle.latest.json"

if [[ "${enabled}" != "0" && "${enabled}" != "1" ]]; then
  echo "LEO_QNAP_RECLAIM_ENABLED must be 0 or 1" >&2; exit 2
fi
if [[ "${ignore_pressure}" != "0" && "${ignore_pressure}" != "1" ]]; then
  echo "LEO_QNAP_RECLAIM_IGNORE_PRESSURE must be 0 or 1" >&2; exit 2
fi
if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2; exit 2
fi

while true; do
  args=(starlink-qnap-lifecycle "${shared_root}" "${archive_root}"
    --minimum-age-hours "${minimum_age_hours}" --maximum-tier "${maximum_tier}"
    --trigger-free-gb "${trigger_free_gb}" --target-free-gb "${target_free_gb}"
    --output "${plan}")
  if [[ "${enabled}" == "1" ]]; then
    args+=(--apply --confirm DELETE-QNAP-RAW-IQ)
  fi
  [[ "${ignore_pressure}" == "1" ]] && args+=(--ignore-pressure)
  env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
    leo-radio "${args[@]}"
  sleep "${interval_s}"
done
