#!/usr/bin/env bash
# Move completed, queued IQ captures from the acquisition host to shared storage.
set -euo pipefail

usage() {
  echo "usage: $0 [--once] [SOURCE_ROOT] [SHARED_ROOT]" >&2
}

once=0
if [[ "${1:-}" == "--once" ]]; then
  once=1
  shift
fi
if (( $# > 2 )); then usage; exit 2; fi

source_root="${1:-${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}}"
shared_root="${2:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
queue="${source_root}/staging/analysis-queue"
remote_queue="${shared_root}/staging/analysis-queue"
incoming="${shared_root}/staging/incoming"
context="${shared_root}/context"
bwlimit_kbps="${LEO_OFFLOAD_BWLIMIT_KBPS:-20000}"
poll_s="${LEO_OFFLOAD_POLL_S:-5}"
source_policy="${LEO_OFFLOAD_SOURCE_POLICY:-retain}"
if [[ "${source_policy}" != "delete" && "${source_policy}" != "retain" ]]; then
  echo "LEO_OFFLOAD_SOURCE_POLICY must be delete or retain" >&2
  exit 2
fi
context_refresh_s="${LEO_OFFLOAD_CONTEXT_REFRESH_S:-600}"
reconcile_s="${LEO_OFFLOAD_RECONCILE_S:-600}"
stale_capture_age_s="${LEO_OFFLOAD_STALE_CAPTURE_AGE_S:-3600}"
pipeline_id="${LEO_ANALYSIS_PIPELINE_ID:-kalman-full-v1}"
archive_root="${LEO_EVIDENCE_ROOT:-/mnt/qnap01/mouse9911/leo-cropped}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi
tle_catalog="${LEO_BEACON_TLE_CATALOG:-/mnt/qnap01/mouse9911/satellites/leo-tracker/tle-history/latest.json}"
passes="${repo_dir}/artifacts/starlink_hybrid_watch/passes.json"
learned="${source_root}/reports/learned-beacons/active.json"

mkdir -p "${queue}" "${remote_queue}" "${incoming}" "${context}"
exec 9>"${source_root}/staging/analysis-export.lock"
if ! flock -n 9; then
  echo "another analysis exporter is already running" >&2
  exit 0
fi

# Only this singleton creates .exporting claims, so these are interrupted moves.
shopt -s nullglob
for abandoned in "${queue}"/*.exporting.*; do
  mv "${abandoned}" "${abandoned%%.exporting.*}.job"
done
shopt -u nullglob

context_bundle=""
context_synced_at=0
reconciled_at=0
sync_context() {
  local force="${1:-0}" now
  local -a args
  now="$(date +%s)"
  if (( force == 0 && now - context_synced_at < context_refresh_s )) &&
     [[ -d "${context_bundle}" ]]; then
    return
  fi
  args=("${repo_dir}/.venv/bin/python" -m leo_tracker.radio.beacon.offload
    bundle "${context}")
  [[ -f "${learned}" ]] && args+=(--learned "${learned}")
  [[ -f "${passes}" ]] && args+=(--passes "${passes}")
  [[ -f "${tle_catalog}" ]] && args+=(--tle-catalog "${tle_catalog}")
  context_bundle="$("${args[@]}")"
  context_synced_at="${now}"
  echo "published analysis context ${context_bundle}"
}

# Reconciliation is a side errand; draining the queue is the job.
#
# This runs under `set -e`, and both commands below exit non-zero when any one
# recording reports an error. A single unreadable manifest, or one written by a
# capture loop newer than this checkout, therefore used to take the exporter
# down -- and `Restart=always` then reconciled again, failed again, and stopped
# ALL offload for every capture type over one directory. The failure is logged
# and the loop carries on to the captures that are waiting.
reconcile_exports() {
  local force="${1:-0}" now reconcile_failed=0
  now="$(date +%s)"
  if (( force == 0 && now - reconciled_at < reconcile_s )); then
    return
  fi
  env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
    python -m leo_tracker.radio.beacon.offload recover-stale "${source_root}" \
    --minimum-age-s "${stale_capture_age_s}" --summary-only || reconcile_failed=1
  env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
    python -m leo_tracker.radio.beacon.offload enqueue-export-backfill \
    "${source_root}" "${shared_root}" --pipeline-id "${pipeline_id}" \
    --archive-root "${archive_root}" --summary-only || reconcile_failed=1
  if (( reconcile_failed == 1 )); then
    printf '{"reconcile_warning":true,"detail":"a reconciliation pass reported errors; the queue drain continues"}\n' >&2
  fi
  reconciled_at="${now}"
}

verify_copy() {
  local capture="$1"
  # The capture manifest is authoritative and contains every chunk's byte size
  # and SHA-256. Size verification avoids reading the NFS payload back over
  # Wi-Fi; set LEO_OFFLOAD_VERIFY_SHA256=1 for a full cryptographic replay.
  "${repo_dir}/.venv/bin/python" - "${capture}" "${LEO_OFFLOAD_VERIFY_SHA256:-0}" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
verify_hash = sys.argv[2] == "1"
manifest = json.loads((root / "manifest.json").read_text())
# An interrupted capture stopped early but wrote a checksummed contiguous
# prefix, so verify the chunks it does have. One with no chunks carries nothing.
if manifest.get("state") not in ("complete", "interrupted"):
    raise SystemExit("capture manifest is not complete")
if not manifest.get("chunks"):
    raise SystemExit("capture manifest has no chunks")
for chunk in manifest.get("chunks", []):
    path = root / chunk["path"]
    if path.stat().st_size != int(chunk["bytes"]):
        raise SystemExit(f"size mismatch: {path}")
    if verify_hash:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != chunk["sha256"]:
            raise SystemExit(f"checksum mismatch: {path}")
PY
}

export_one() {
  local marker="$1" claim name capture mode capture_real source_real partial destination remote_marker
  claim="${marker%.job}.exporting.$$"
  mv "${marker}" "${claim}" 2>/dev/null || return 0
  IFS=$'\t' read -r name capture mode < "${claim}"
  source_real="$(realpath -e "${source_root}")"
  capture_real="$(realpath -e "${capture}" 2>/dev/null || true)"
  if [[ ! "${name}" =~ ^[A-Za-z0-9._-]+$ || -z "${mode}" ||
        ("${capture_real}" != "${source_real}/captures/"* &&
         "${capture_real}" != "${source_real}/hop-sessions/"* &&
         "${capture_real}" != "${source_real}/quarantine/"* &&
         "${capture_real}" != "${source_real}/evidence/pilot_symbolwise_v3/"*) ]]; then
    mv "${claim}" "${claim%%.exporting.*}.failed"
    echo "invalid offload job: ${claim}" >&2
    return 1
  fi
  destination="${shared_root}/captures/${name}"
  # Stable partial names let rsync resume the same immutable capture after an
  # exporter or network restart. The singleton exporter lock prevents writers
  # from sharing this directory concurrently.
  partial="${incoming}/${name}.partial"
  mkdir -p "${shared_root}/captures"
  if [[ ! -d "${destination}" ]]; then
    mkdir -p "${partial}"
    if ! rsync -rt --partial --bwlimit="${bwlimit_kbps}" -- "${capture}/" "${partial}/"; then
      mv "${claim}" "${claim%%.exporting.*}.job"
      return 1
    fi
    if ! verify_copy "${partial}"; then
      rm -rf -- "${partial}"
      mv "${claim}" "${claim%%.exporting.*}.job"
      return 1
    fi
    mv "${partial}" "${destination}"
  else
    if ! verify_copy "${destination}"; then
      mv "${claim}" "${claim%%.exporting.*}.job"
      return 1
    fi
  fi
  remote_marker="${remote_queue}/$(basename "${claim%%.exporting.*}").job"
  # Paths in a v2 job are share-root relative, so the acquisition and analysis
  # hosts may mount the same QNAP export at different absolute paths.
  printf '%s\t%s\t%s\t%s\n' "${name}" "captures/${name}" "${mode}" \
    "${context_bundle#${shared_root}/}" > "${remote_marker}.next.$$"
  mv "${remote_marker}.next.$$" "${remote_marker}"
  # Evidence-archive development uses retain: QNAP receives a verified work
  # copy while the NVMe capture remains an immutable source of truth.
  if [[ "${source_policy}" == "delete" ]]; then
    rm -rf -- "${capture_real}"
  fi
  rm -f -- "${claim}"
  echo "offloaded ${name} (${mode}) source_policy=${source_policy}"
}

sync_context 1
reconcile_exports 1
while true; do
  shopt -s nullglob
  jobs=("${queue}"/*.job)
  shopt -u nullglob
  if (( ${#jobs[@]} == 0 )); then
    (( once == 1 )) && break
    sync_context
    reconcile_exports
    sleep "${poll_s}"
    continue
  fi
  sync_context
  if ! export_one "${jobs[0]}"; then
    (( once == 1 )) && break
    sleep "${poll_s}"
  fi
done
