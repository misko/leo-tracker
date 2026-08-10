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
audit_report="${shared_root}/reports/retention/storage-v2-audit.json"
audit_interval_s="${LEO_STORAGE_AUDIT_INTERVAL_S:-600}"
role="${LEO_STORAGE_REGIME_ROLE:-standalone}"
primary_state="${shared_root}/reports/runtime/storage-regime-v2-primary.json"
primary_heartbeat_s="${LEO_STORAGE_PRIMARY_HEARTBEAT_S:-30}"
primary_lease_max_age_s="${LEO_STORAGE_PRIMARY_LEASE_MAX_AGE_S:-120}"
primary_heartbeat_pid=""
drain_requested=0

request_drain() {
  drain_requested=1
  printf '[%s] storage_regime_drain_requested; active_transaction_finishes=true\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
}

trap request_drain TERM INT

stop_primary_heartbeat() {
  if [[ -n "${primary_heartbeat_pid}" ]]; then
    kill "${primary_heartbeat_pid}" 2>/dev/null || true
    wait "${primary_heartbeat_pid}" 2>/dev/null || true
    publish_primary_state stopped || true
    primary_heartbeat_pid=""
  fi
}
trap stop_primary_heartbeat EXIT

if [[ "${enabled}" != "0" && "${enabled}" != "1" ]]; then
  echo "LEO_STORAGE_REGIME_ENABLED must be 0 or 1" >&2; exit 2
fi
if [[ "${scope}" != "all" && "${scope}" != "auto" &&
      "${scope}" != "raw" && "${scope}" != "archive" ]]; then
  echo "LEO_STORAGE_REGIME_SCOPE must be all, auto, raw, or archive" >&2; exit 2
fi
if [[ "${role}" != "standalone" && "${role}" != "primary" &&
      "${role}" != "fallback" ]]; then
  echo "LEO_STORAGE_REGIME_ROLE must be standalone, primary, or fallback" >&2
  exit 2
fi
if ! [[ "${planning_limit}" =~ ^[1-9][0-9]*$ && "${workers}" =~ ^[1-9][0-9]*$ &&
        "${legacy_limit}" =~ ^[1-9][0-9]*$ &&
        "${legacy_planning_limit}" =~ ^[1-9][0-9]*$ &&
        "${audit_interval_s}" =~ ^[1-9][0-9]*$ &&
        "${primary_heartbeat_s}" =~ ^[1-9][0-9]*$ &&
        "${primary_lease_max_age_s}" =~ ^[1-9][0-9]*$ ]]; then
  echo "planning limits, batch limits, and workers must be positive integers" >&2; exit 2
fi
if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2; exit 2
fi

# `uv run --active` resolves the project command from the current directory.
# Do not make the caller's working directory a hidden prerequisite.
cd "${repo_dir}"

publish_primary_state() {
  local state="$1" temporary
  temporary="${primary_state}.next.$$"
  mkdir -p "$(dirname "${primary_state}")"
  "${repo_dir}/.venv/bin/python" - "${temporary}" "${primary_state}" \
      "${state}" "$(date +%s)" "$(hostname)" "$$" <<'PY'
import json, os, pathlib, sys
temporary, destination, state, updated, hostname, pid = sys.argv[1:]
value = {"schema": "leo-tracker.storage-regime-v2-primary/v1",
         "state": state, "updated_epoch_s": int(updated),
         "hostname": hostname, "pid": int(pid)}
path = pathlib.Path(temporary)
with path.open("w", encoding="utf-8") as stream:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
os.replace(path, destination)
PY
}

if [[ "${role}" == "primary" ]]; then
  publish_primary_state running
  (
    trap - TERM INT EXIT
    while true; do
      sleep "${primary_heartbeat_s}"
      publish_primary_state running
    done
  ) &
  primary_heartbeat_pid="$!"
fi

while true; do
  if (( drain_requested )); then
    printf '[%s] storage_regime_drain_complete\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 0
  fi
  if [[ "${role}" == "fallback" ]] &&
     "${repo_dir}/.venv/bin/python" - "${primary_state}" \
       "${primary_lease_max_age_s}" <<'PY'
import pathlib, sys
from leo_tracker.radio.beacon.storage_regime import storage_primary_lease_is_fresh
raise SystemExit(0 if storage_primary_lease_is_fresh(
    pathlib.Path(sys.argv[1]), maximum_age_s=float(sys.argv[2])) else 1)
PY
  then
    printf '[%s] storage_regime_primary_active; fallback_yield_s=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${interval_s}"
    sleep "${interval_s}" || true
    continue
  fi
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
    # A full audit walks every relevant QNAP artifact. Run it only after the
    # bounded normalizer itself proves a complete zero-work inventory, and
    # rate-limit retries while waiting for the Kalman producer contract.
    if "${repo_dir}/.venv/bin/python" - "${legacy_plan}" <<'PY'
import json, pathlib, sys
from leo_tracker.radio.beacon.legacy_normalizer import legacy_normalization_complete
try:
    plan = json.loads(pathlib.Path(sys.argv[1]).read_text())
    complete = legacy_normalization_complete(plan)
except (OSError, ValueError):
    complete = False
raise SystemExit(0 if complete else 1)
PY
    then
      now="$(date +%s)"
      previous="$(stat -c %Y "${audit_report}" 2>/dev/null || printf 0)"
      if (( now - previous >= audit_interval_s )); then
        audit_args=(starlink-storage-audit-v2 "${shared_root}" "${archive_root}"
          --minimum-age-hours "${minimum_age_hours}"
          --require-producer-contract --output "${audit_report}")
        if env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run \
            --active --no-sync leo-radio "${audit_args[@]}"; then
          printf '[%s] storage_regime_converged audit=%s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${audit_report}"
        else
          printf '[%s] storage_regime_audit_pending audit=%s retry_s=%s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${audit_report}" \
            "${audit_interval_s}" >&2
        fi
      fi
    fi
  fi
  if (( drain_requested )); then
    printf '[%s] storage_regime_drain_complete\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 0
  fi
  sleep "${interval_s}" || true
  if (( drain_requested )); then
    printf '[%s] storage_regime_drain_complete\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 0
  fi
done
