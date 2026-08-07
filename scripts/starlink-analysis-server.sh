#!/usr/bin/env bash
# Consume offloaded Starlink IQ jobs. Run this from a leo-tracker checkout.
set -euo pipefail

usage() {
  echo "usage: $0 [--once] [--workers N] [SHARED_ROOT]" >&2
}

once=0
workers="${LEO_ANALYSIS_WORKERS:-1}"
while (( $# )); do
  case "$1" in
    --once) once=1; shift ;;
    --workers) workers="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) usage; exit 2 ;;
    *) break ;;
  esac
done
if (( $# > 1 )) || ! [[ "${workers}" =~ ^[1-9][0-9]*$ ]]; then usage; exit 2; fi

root="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
venv="${repo_dir}/.venv"
queue="${root}/staging/analysis-queue"
reports="${root}/reports"
context="${root}/context"
poll_s="${LEO_ANALYSIS_POLL_S:-3}"
if [[ -z "${uv_bin}" || ! -x "${venv}/bin/python" ]]; then
  echo "uv and an existing ${venv} are required" >&2
  exit 2
fi
mkdir -p "${queue}/done" "${queue}/failed" "${reports}"/{plots,followups,decoded,frame-tracks,tracks,associations,fingerprints}
cd "${repo_dir}"

radio() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-radio "$@"; }
orbit() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-orbit "$@"; }

process_job() {
  local name="$1" capture="$2" mode="$3"
  local report="${reports}/${name}.json" plot="${reports}/plots/${name}.png"
  local followup="${reports}/followups/${name}.json" confirmed="${root}/staging/${name}.confirmed"
  local frame="${reports}/frame-tracks/${name}.json" samples="${reports}/frame-tracks/${name}.npz"
  local decoded="${reports}/decoded/${name}.json" symbols="${reports}/decoded/${name}.npz"
  local decoded_plot="${reports}/decoded/${name}.png" track="${reports}/tracks/${name}.json"
  local association="${reports}/associations/${name}.json"
  local -a analysis_args template_args frame_args track_args passes_args
  analysis_args=(--exact-window-s .01)
  template_args=(); frame_args=(); passes_args=()
  case "${mode}" in
    wide) analysis_args+=(--exact-interval-s 10 --acquisition-span-hz 3500000 --acquisition-step-hz 500000 --exact-subband-rate-hz 2500000) ;;
    oversample) analysis_args+=(--exact-interval-s 6 --exact-subband-rate-hz 5000000) ;;
    hop) analysis_args+=(--exact-interval-s .7 --exact-window-s .02) ;;
    *) analysis_args+=(--exact-interval-s 6) ;;
  esac
  if [[ ("${mode}" == narrow || "${mode}" == hop) && -f "${context}/learned-beacon.json" ]]; then
    template_args=(--beacon-template "${context}/learned-beacon.json")
  fi
  [[ -f "${context}/passes.json" ]] && passes_args=(--passes "${context}/passes.json")
  radio starlink-beacon-analyze "${capture}" "${report}" --window-s 1 \
    --maximum-analysis-rate-hz 50000 --exact-acquisition-method pilot_symbolwise_v3 \
    "${analysis_args[@]}" "${template_args[@]}" --plot "${plot}"
  radio starlink-beacon-followup "${capture}" "${report}" "${followup}" \
    --radius-s .5 --interval-s .1 --window-s .01 "${passes_args[@]}" \
    --confirmation-marker "${confirmed}"
  [[ -f "${confirmed}" ]] || return 0
  frame_args=("${template_args[@]}")
  if radio starlink-beacon-frame-track "${capture}" "${followup}" "${frame}" \
      --samples "${samples}" "${frame_args[@]}" && \
      grep -Eq '"dual_valid_frame_count": [1-9]' "${frame}"; then
    track_args=(--measurement-source conditioned_frames --frame-track "${frame}")
  else
    track_args=(--measurement-source dense_followup)
  fi
  if [[ "${mode}" != wide && "${mode}" != hop ]]; then
    radio starlink-beacon-decode "${capture}" "${followup}" "${decoded}" \
      --plot "${decoded_plot}" --symbols "${symbols}"
  fi
  radio starlink-beacon-track "${capture}" "${followup}" "${track}" \
    --maximum-gap-s 5 --maximum-reacquisition-span-hz 5000 "${track_args[@]}"
  if [[ -f "${context}/tle-catalog.json" ]]; then
    orbit associate --observations "${track}" --catalog "${context}/tle-catalog.json" \
      --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
      --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
      --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" --output "${association}"
  fi
}

worker() {
  local worker_id="$1" marker claim name capture mode log done failed
  while true; do
    marker="$(find "${queue}" -maxdepth 1 -type f -name '*.job' -print -quit)"
    if [[ -z "${marker}" ]]; then
      (( once == 1 )) && return 0
      sleep "${poll_s}"; continue
    fi
    claim="${marker%.job}.running.${worker_id}.$$"
    mv "${marker}" "${claim}" 2>/dev/null || continue
    IFS=$'\t' read -r name capture mode < "${claim}"
    log="${reports}/${name}.worker.log"
    if process_job "${name}" "${capture}" "${mode}" > >(tee -a "${log}") 2>&1; then
      done="${queue}/done/$(basename "${marker}")"
      mv "${claim}" "${done}"
    else
      failed="${queue}/failed/$(basename "${marker}")"
      mv "${claim}" "${failed}"
      echo "analysis failed for ${name}; see ${log}" >&2
    fi
  done
}

pids=()
trap 'for pid in "${pids[@]}"; do kill "${pid}" 2>/dev/null || true; done' INT TERM EXIT
for ((index=0; index<workers; index++)); do worker "${index}" & pids+=("$!"); done
status=0
for pid in "${pids[@]}"; do wait "${pid}" || status=$?; done
pids=()
exit "${status}"
