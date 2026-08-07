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
heartbeat_s="${LEO_ANALYSIS_HEARTBEAT_S:-30}"
if [[ -z "${uv_bin}" || ! -x "${venv}/bin/python" ]]; then
  echo "uv and an existing ${venv} are required" >&2
  exit 2
fi
if ! [[ "${heartbeat_s}" =~ ^[1-9][0-9]*$ ]]; then
  echo "LEO_ANALYSIS_HEARTBEAT_S must be a positive integer" >&2
  exit 2
fi
metrics="${queue}/metrics"
progress_log="${reports}/analysis-server.log"
progress_lock="${queue}/progress.lock"
mkdir -p "${queue}/done" "${queue}/failed" "${metrics}" \
  "${reports}"/{plots,followups,decoded,frame-tracks,tracks,associations,fingerprints}
exec 8>"${queue}/server.lock"
if ! flock -n 8; then
  echo "another analysis server already owns ${queue}" >&2
  exit 2
fi
cd "${repo_dir}"

radio() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-radio "$@"; }
orbit() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-orbit "$@"; }
protocol() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync python -m leo_tracker.radio.beacon.offload "$@"; }

emit() {
  local line
  printf -v line '[%s] %s' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
  (
    flock 9
    printf '%s\n' "${line}" | tee -a "${progress_log}"
  ) 9>"${progress_lock}"
}

count_files() {
  find "$1" -maxdepth 1 -type f -name "$2" -printf . 2>/dev/null | wc -c
}

human_duration() {
  awk -v seconds="${1:-0}" 'BEGIN {
    seconds=int(seconds); days=int(seconds/86400); seconds%=86400
    hours=int(seconds/3600); seconds%=3600; minutes=int(seconds/60); seconds%=60
    if (days) printf "%dd%02dh%02dm", days, hours, minutes
    else if (hours) printf "%dh%02dm%02ds", hours, minutes, seconds
    else printf "%dm%02ds", minutes, seconds
  }'
}

print_progress() {
  local reason="$1" ready running completed failed total finished percent
  local metric_count average_seconds eta_seconds eta average
  ready="$(count_files "${queue}" '*.job')"
  running="$(count_files "${queue}" '*.running.*')"
  completed="$(count_files "${queue}/done" '*.job')"
  failed="$(count_files "${queue}/failed" '*.job')"
  total=$((ready + running + completed + failed))
  finished=$((completed + failed))
  if (( total > 0 )); then percent="$(awk -v n="${finished}" -v d="${total}" 'BEGIN {printf "%.1f", 100*n/d}')"
  else percent="100.0"; fi
  read -r metric_count average_seconds < <(
    awk -F '\t' '{sum+=$2; count++} END {printf "%d %.3f\n", count, count ? sum/count : 0}' \
      "${metrics}"/*.tsv 2>/dev/null || printf '0 0\n')
  if (( metric_count > 0 )); then
    eta_seconds="$(awk -v jobs="$((ready + running))" -v avg="${average_seconds}" \
      -v workers="${workers}" 'BEGIN {printf "%.0f", jobs*avg/workers}')"
    eta="$(human_duration "${eta_seconds}")"
    average="$(human_duration "${average_seconds}")"
  else
    eta="estimating"; average="estimating"
  fi
  emit "progress reason=${reason} complete=${finished}/${total} percent=${percent}% ready=${ready} running=${running} succeeded=${completed} failed=${failed} workers=${workers} average_job=${average} eta=${eta}"
}

run_stage() {
  local worker_id="$1" name="$2" label="$3" started elapsed
  shift 3
  started="$(date +%s)"
  emit "stage_start worker=${worker_id} job=${name} stage=${label}"
  if "$@"; then
    elapsed=$(( $(date +%s) - started ))
    emit "stage_done worker=${worker_id} job=${name} stage=${label} elapsed=$(human_duration "${elapsed}")"
    return 0
  fi
  elapsed=$(( $(date +%s) - started ))
  emit "stage_failed worker=${worker_id} job=${name} stage=${label} elapsed=$(human_duration "${elapsed}")"
  return 1
}

process_job() {
  local worker_id="$1" name="$2" capture="$3" mode="$4" job_context="$5"
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
  if [[ ("${mode}" == narrow || "${mode}" == hop) && -f "${job_context}/learned-beacon.json" ]]; then
    template_args=(--beacon-template "${job_context}/learned-beacon.json")
  fi
  [[ -f "${job_context}/passes.json" ]] && passes_args=(--passes "${job_context}/passes.json")
  run_stage "${worker_id}" "${name}" acquire radio starlink-beacon-analyze "${capture}" "${report}" --window-s 1 \
    --maximum-analysis-rate-hz 50000 --exact-acquisition-method pilot_symbolwise_v3 \
    "${analysis_args[@]}" "${template_args[@]}" --plot "${plot}" || return 1
  run_stage "${worker_id}" "${name}" followup radio starlink-beacon-followup "${capture}" "${report}" "${followup}" \
    --radius-s .5 --interval-s .1 --window-s .01 "${passes_args[@]}" \
    --confirmation-marker "${confirmed}" || return 1
  if [[ ! -f "${confirmed}" ]]; then
    emit "signal_result worker=${worker_id} job=${name} confirmed=false derived_report=${report}"
    return 0
  fi
  emit "signal_result worker=${worker_id} job=${name} confirmed=true followup=${followup}"
  frame_args=("${template_args[@]}")
  if run_stage "${worker_id}" "${name}" frame_track radio starlink-beacon-frame-track "${capture}" "${followup}" "${frame}" \
      --samples "${samples}" "${frame_args[@]}" && \
      grep -Eq '"dual_valid_frame_count": [1-9]' "${frame}"; then
    track_args=(--measurement-source conditioned_frames --frame-track "${frame}")
  else
    track_args=(--measurement-source dense_followup)
  fi
  if [[ "${mode}" != wide && "${mode}" != hop ]]; then
    run_stage "${worker_id}" "${name}" decode radio starlink-beacon-decode "${capture}" "${followup}" "${decoded}" \
      --plot "${decoded_plot}" --symbols "${symbols}" || return 1
  fi
  run_stage "${worker_id}" "${name}" doppler_track radio starlink-beacon-track "${capture}" "${followup}" "${track}" \
    --maximum-gap-s 5 --maximum-reacquisition-span-hz 5000 "${track_args[@]}" || return 1
  if [[ -f "${job_context}/tle-catalog.json" ]]; then
    run_stage "${worker_id}" "${name}" tle_association orbit associate --observations "${track}" --catalog "${job_context}/tle-catalog.json" \
      --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
      --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
      --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" --output "${association}" || return 1
  fi
}

worker() {
  local worker_id="$1" marker claim name capture mode job_context log done failed
  local started elapsed metric temporary
  while true; do
    marker="$(find "${queue}" -maxdepth 1 -type f -name '*.job' -print -quit)"
    if [[ -z "${marker}" ]]; then
      (( once == 1 )) && return 0
      sleep "${poll_s}"; continue
    fi
    claim="${marker%.job}.running.${worker_id}.$$"
    mv "${marker}" "${claim}" 2>/dev/null || continue
    IFS=$'\t' read -r name capture mode job_context < "${claim}"
    job_context="${job_context:-${default_context}}"
    [[ "${capture}" == /* ]] || capture="${root}/${capture}"
    [[ "${job_context}" == /* ]] || job_context="${root}/${job_context}"
    log="${reports}/${name}.worker.log"
    started="$(date +%s)"
    emit "job_start worker=${worker_id} job=${name} mode=${mode} capture=${capture} context=${job_context} log=${log}"
    if process_job "${worker_id}" "${name}" "${capture}" "${mode}" "${job_context}" > >(tee -a "${log}") 2>&1 &&
       run_stage "${worker_id}" "${name}" validate_outputs protocol validate \
         "${root}" "${name}" "${mode}" --context "${job_context}" \
         --elapsed-s "$(( $(date +%s) - started ))" --write > >(tee -a "${log}") 2>&1; then
      done="${queue}/done/$(basename "${marker}")"
      mv "${claim}" "${done}"
      elapsed=$(( $(date +%s) - started ))
      metric="${metrics}/${name}.tsv"; temporary="${metric}.next.$$"
      printf 'success\t%d\t%s\t%s\n' "${elapsed}" "${mode}" "${worker_id}" > "${temporary}"
      mv "${temporary}" "${metric}"
      emit "job_done worker=${worker_id} job=${name} mode=${mode} elapsed=$(human_duration "${elapsed}") report=${reports}/${name}.json"
      print_progress "job_done"
    else
      failed="${queue}/failed/$(basename "${marker}")"
      mv "${claim}" "${failed}"
      elapsed=$(( $(date +%s) - started ))
      metric="${metrics}/${name}.tsv"; temporary="${metric}.next.$$"
      printf 'failed\t%d\t%s\t%s\n' "${elapsed}" "${mode}" "${worker_id}" > "${temporary}"
      mv "${temporary}" "${metric}"
      emit "job_failed worker=${worker_id} job=${name} mode=${mode} elapsed=$(human_duration "${elapsed}") log=${log}"
      print_progress "job_failed"
    fi
  done
}

pids=()
monitor_pid=""
terminate_process_tree() {
  local parent="$1" child
  while read -r child; do
    [[ -n "${child}" ]] || continue
    terminate_process_tree "${child}"
  done < <(pgrep -P "${parent}" 2>/dev/null || true)
  kill "${parent}" 2>/dev/null || true
}
stop_children() {
  local pid
  [[ -z "${monitor_pid}" ]] || terminate_process_tree "${monitor_pid}"
  for pid in "${pids[@]}"; do terminate_process_tree "${pid}"; done
}
trap stop_children INT TERM EXIT
if [[ -f "${context}/current.json" ]]; then
  default_context="$(protocol current "${context}")"
else
  default_context="${context}"
fi
shopt -s nullglob
for interrupted in "${queue}"/*.running.*; do
  restored="${interrupted%%.running.*}.job"
  [[ ! -e "${restored}" ]] || restored="${queue}/recovered-$(date +%s%N)-$(basename "${restored}")"
  mv "${interrupted}" "${restored}"
done
shopt -u nullglob
audit_result="$(protocol audit "${root}" --context "${default_context}")"
emit "server_start repo=${repo_dir} shared_root=${root} queue=${queue} reports=${reports} workers=${workers} once=${once} heartbeat_s=${heartbeat_s} python=$(${venv}/bin/python --version 2>&1)"
emit "recovery ${audit_result}"
print_progress startup
for ((index=0; index<workers; index++)); do worker "${index}" & pids+=("$!"); done
(
  while true; do
    sleep "${heartbeat_s}"
    print_progress heartbeat
  done
) &
monitor_pid="$!"
status=0
for pid in "${pids[@]}"; do wait "${pid}" || status=$?; done
terminate_process_tree "${monitor_pid}"
wait "${monitor_pid}" 2>/dev/null || true
monitor_pid=""
pids=()
print_progress shutdown
emit "server_stop status=${status}"
exit "${status}"
