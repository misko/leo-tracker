#!/usr/bin/env bash
# Converge historical QNAP raw/v1 storage to replay-gated tiered-v2 evidence.
set -euo pipefail

shared_root="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
archive_root="${2:-${LEO_EVIDENCE_ROOT:-/mnt/qnap01/mouse9911/leo-cropped}}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
enabled="${LEO_STORAGE_REGIME_ENABLED:-0}"
interval_s="${LEO_STORAGE_REGIME_INTERVAL_S:-60}"
limit="${LEO_STORAGE_REGIME_LIMIT:-2}"
planning_limit="${LEO_STORAGE_REGIME_PLANNING_LIMIT:-16}"
workers="${LEO_STORAGE_REGIME_WORKERS:-1}"
minimum_age_hours="${LEO_STORAGE_REGIME_MINIMUM_AGE_HOURS:-6}"
scope="${LEO_STORAGE_REGIME_SCOPE:-auto}"
plan="${shared_root}/reports/retention/storage-regime-v2.latest.json"

if [[ "${enabled}" != "0" && "${enabled}" != "1" ]]; then
  echo "LEO_STORAGE_REGIME_ENABLED must be 0 or 1" >&2; exit 2
fi
if [[ "${scope}" != "all" && "${scope}" != "auto" &&
      "${scope}" != "raw" && "${scope}" != "archive" ]]; then
  echo "LEO_STORAGE_REGIME_SCOPE must be all, auto, raw, or archive" >&2; exit 2
fi
if ! [[ "${planning_limit}" =~ ^[1-9][0-9]*$ && "${workers}" =~ ^[1-9][0-9]*$ ]]; then
  echo "planning limit and workers must be positive integers" >&2; exit 2
fi
if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2; exit 2
fi

# `uv run --active` resolves the project command from the current directory.
# Do not make the caller's working directory a hidden prerequisite.
cd "${repo_dir}"

while true; do
  args=(starlink-storage-regime-v2 "${shared_root}" "${archive_root}"
    --minimum-age-hours "${minimum_age_hours}" --scope "${scope}"
    --planning-limit "${planning_limit}" --workers "${workers}"
    --limit "${limit}" --output "${plan}")
  if [[ "${enabled}" == "1" ]]; then
    args+=(--apply --confirm MIGRATE-TO-EVIDENCE-V2)
  fi
  if ! env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
      leo-radio "${args[@]}"; then
    printf '[%s] storage_regime_batch_failed; retrying after %ss\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${interval_s}" >&2
  fi
  sleep "${interval_s}"
done
