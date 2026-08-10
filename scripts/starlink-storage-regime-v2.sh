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
legacy_limit="${LEO_STORAGE_LEGACY_LIMIT:-16}"
legacy_planning_limit="${LEO_STORAGE_LEGACY_PLANNING_LIMIT:-64}"
minimum_age_hours="${LEO_STORAGE_REGIME_MINIMUM_AGE_HOURS:-6}"
scope="${LEO_STORAGE_REGIME_SCOPE:-auto}"
plan="${shared_root}/reports/retention/storage-regime-v2.latest.json"
legacy_plan="${shared_root}/reports/retention/legacy-layout.latest.json"

if [[ "${enabled}" != "0" && "${enabled}" != "1" ]]; then
  echo "LEO_STORAGE_REGIME_ENABLED must be 0 or 1" >&2; exit 2
fi
if [[ "${scope}" != "all" && "${scope}" != "auto" &&
      "${scope}" != "raw" && "${scope}" != "archive" ]]; then
  echo "LEO_STORAGE_REGIME_SCOPE must be all, auto, raw, or archive" >&2; exit 2
fi
if ! [[ "${planning_limit}" =~ ^[1-9][0-9]*$ && "${workers}" =~ ^[1-9][0-9]*$ &&
        "${legacy_limit}" =~ ^[1-9][0-9]*$ &&
        "${legacy_planning_limit}" =~ ^[1-9][0-9]*$ ]]; then
  echo "planning limits, batch limits, and workers must be positive integers" >&2; exit 2
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
  primary_succeeded=1
  if ! env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
      leo-radio "${args[@]}"; then
    primary_succeeded=0
    printf '[%s] storage_regime_batch_failed; retrying after %ss\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${interval_s}" >&2
  fi
  # Do not spend a second archive walk on duplicates already owned by a raw or
  # v1 migration.  The normalizer becomes eligible only after auto scope has
  # exhausted raw, then completed a full archive inventory with zero eligible
  # v1 records. Blocked primary records remain visible to the final audit.
  if (( primary_succeeded )) && "${repo_dir}/.venv/bin/python" - "${plan}" <<'PY'
import json, pathlib, sys
from leo_tracker.radio.beacon.legacy_normalizer import legacy_normalization_ready
try:
    plan = json.loads(pathlib.Path(sys.argv[1]).read_text())
    ready = legacy_normalization_ready(plan)
except (OSError, ValueError):
    ready = False
raise SystemExit(0 if ready else 1)
PY
  then
    legacy_args=(starlink-storage-normalize-legacy "${shared_root}" "${archive_root}"
      --planning-limit "${legacy_planning_limit}" --limit "${legacy_limit}"
      --output "${legacy_plan}")
    if [[ "${enabled}" == "1" ]]; then
      legacy_args+=(--apply --confirm NORMALIZE-LEGACY-LAYOUT)
    fi
    if ! env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
        leo-radio "${legacy_args[@]}"; then
      printf '[%s] legacy_layout_batch_failed; retrying after %ss\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${interval_s}" >&2
    fi
  fi
  sleep "${interval_s}"
done
