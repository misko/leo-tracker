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
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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

sync_context() {
  local source destination temporary
  for source in "${tle_catalog}" "${passes}" "${learned}"; do
    [[ -f "${source}" ]] || continue
    case "${source}" in
      "${tle_catalog}") destination="${context}/tle-catalog.json" ;;
      "${passes}") destination="${context}/passes.json" ;;
      *) destination="${context}/learned-beacon.json" ;;
    esac
    temporary="${destination}.next.$$"
    rsync -rtL -- "${source}" "${temporary}"
    mv "${temporary}" "${destination}"
  done
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
if manifest.get("state") != "complete":
    raise SystemExit("capture manifest is not complete")
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
         "${capture_real}" != "${source_real}/hop-sessions/"*) ]]; then
    mv "${claim}" "${claim%%.exporting.*}.failed"
    echo "invalid offload job: ${claim}" >&2
    return 1
  fi
  destination="${shared_root}/captures/${name}"
  partial="${incoming}/${name}.partial.$$"
  mkdir -p "${shared_root}/captures"
  if [[ ! -d "${destination}" ]]; then
    rm -rf -- "${partial}"
    mkdir -p "${partial}"
    if ! rsync -rt --partial --bwlimit="${bwlimit_kbps}" -- "${capture}/" "${partial}/"; then
      rm -rf -- "${partial}"
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
  printf '%s\t%s\t%s\n' "${name}" "${destination}" "${mode}" > "${remote_marker}.next.$$"
  mv "${remote_marker}.next.$$" "${remote_marker}"
  # This is a move, not a cache: remote verification and durable queueing both
  # complete before the acquisition copy is removed.
  rm -rf -- "${capture_real}"
  rm -f -- "${claim}"
  echo "offloaded ${name} (${mode})"
}

sync_context
while true; do
  shopt -s nullglob
  jobs=("${queue}"/*.job)
  shopt -u nullglob
  if (( ${#jobs[@]} == 0 )); then
    (( once == 1 )) && break
    sync_context
    sleep "${poll_s}"
    continue
  fi
  if ! export_one "${jobs[0]}"; then
    (( once == 1 )) && break
    sleep "${poll_s}"
  fi
done
