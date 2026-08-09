#!/usr/bin/env bash
# Reclaim only acquisition-host duplicates proven complete on QNAP and analyzed.
set -euo pipefail

local_root="${1:-${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}}"
shared_root="${2:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
interval_s="${LEO_LOCAL_RECLAIM_INTERVAL_S:-60}"
minimum_age_s="${LEO_LOCAL_RECLAIM_MINIMUM_AGE_S:-300}"
plan="${shared_root}/reports/reclamation/local-plan.latest.json"

if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi

args=(starlink-storage-reconcile "${local_root}" "${shared_root}"
  --apply --watch --interval-s "${interval_s}"
  --minimum-age-s "${minimum_age_s}" --output "${plan}")
if [[ "${LEO_LOCAL_RECLAIM_VERIFY_SHA256:-0}" == "1" ]]; then
  args+=(--verify-sha256)
fi

exec env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run \
  --active --no-sync leo-radio "${args[@]}"
