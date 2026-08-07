#!/usr/bin/env bash
# Consume offloaded Starlink IQ jobs. Run this from a leo-tracker checkout.
set -euo pipefail

usage() {
  echo "usage: $0 [--once] [--workers N] [SHARED_ROOT]" >&2
  echo "       $0 --drain [SHARED_ROOT]" >&2
}

once=0
action="run"
workers="${LEO_ANALYSIS_WORKERS:-16}"
while (( $# )); do
  case "$1" in
    --once) once=1; shift ;;
    --drain) action="drain"; shift ;;
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
claim_lock="${queue}/claim.lock"
drain_request="${queue}/drain.request"
poll_s="${LEO_ANALYSIS_POLL_S:-3}"
heartbeat_s="${LEO_ANALYSIS_HEARTBEAT_S:-30}"
pipeline_id="${LEO_ANALYSIS_PIPELINE_ID:-kalman-full-v1}"
full_coverage="${LEO_ANALYSIS_FULL_COVERAGE:-1}"
archive_mode="${LEO_ANALYSIS_ARCHIVE_MODE:-shadow}"
archive_root="${LEO_ANALYSIS_ARCHIVE_ROOT:-/mnt/qnap01/mouse9911/leo-cropped}"
retention_mode="${LEO_ANALYSIS_RETENTION_MODE:-disabled}"
full_exact_interval_s="${LEO_ANALYSIS_FULL_EXACT_INTERVAL_S:-1}"
wide_exact_interval_s="${LEO_ANALYSIS_WIDE_EXACT_INTERVAL_S:-2}"
wide_acquisition_span_hz="${LEO_ANALYSIS_WIDE_ACQUISITION_SPAN_HZ:-12000000}"
wide_acquisition_step_hz="${LEO_ANALYSIS_WIDE_ACQUISITION_STEP_HZ:-2000000}"
if [[ "${action}" == "drain" ]]; then
  mkdir -p "${queue}"
  exec 7>"${claim_lock}"
  flock 7
  temporary="${drain_request}.next.$$"
  printf 'requested_utc=%s requested_by_pid=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" > "${temporary}"
  mv "${temporary}" "${drain_request}"
  echo "drain requested: workers will finish their current jobs and exit"
  exit 0
fi
if [[ -z "${uv_bin}" || ! -x "${venv}/bin/python" ]]; then
  echo "uv and an existing ${venv} are required" >&2
  exit 2
fi
if ! [[ "${heartbeat_s}" =~ ^[1-9][0-9]*$ ]]; then
  echo "LEO_ANALYSIS_HEARTBEAT_S must be a positive integer" >&2
  exit 2
fi
if [[ "${full_coverage}" != "0" && "${full_coverage}" != "1" ]]; then
  echo "LEO_ANALYSIS_FULL_COVERAGE must be 0 or 1" >&2
  exit 2
fi
if [[ "${archive_mode}" != "off" && "${archive_mode}" != "shadow" &&
      "${archive_mode}" != "required" ]]; then
  echo "LEO_ANALYSIS_ARCHIVE_MODE must be off, shadow, or required" >&2
  exit 2
fi
if [[ "${retention_mode}" != "disabled" && "${retention_mode}" != "verified" ]]; then
  echo "LEO_ANALYSIS_RETENTION_MODE must be disabled or verified" >&2
  exit 2
fi
if [[ ! "${pipeline_id}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "LEO_ANALYSIS_PIPELINE_ID contains unsafe characters" >&2
  exit 2
fi
# Sixteen process workers must not each create a second BLAS thread pool.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
metrics="${queue}/metrics"
progress_log="${reports}/analysis-server.log"
progress_lock="${queue}/progress.lock"
retention_lock="${queue}/retention.lock"
keep_negative="${LEO_ANALYSIS_KEEP_NEGATIVE:-6}"
keep_confirmed="${LEO_ANALYSIS_KEEP_CONFIRMED:-8}"
keep_wide="${LEO_ANALYSIS_KEEP_WIDE:-2}"
keep_oversample="${LEO_ANALYSIS_KEEP_OVERSAMPLE:-4}"
keep_hop_sessions="${LEO_ANALYSIS_KEEP_HOP_SESSIONS:-6}"
mkdir -p "${queue}/done" "${queue}/failed" "${metrics}" \
  "${reports}"/{plots,followups,decoded,frame-tracks,tracks,associations,fingerprints}
exec 8>"${queue}/server.lock"
if ! flock -n 8; then
  echo "another analysis server already owns ${queue}" >&2
  exit 2
fi
rm -f -- "${drain_request}"
cd "${repo_dir}"

radio() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-radio "$@"; }
orbit() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-orbit "$@"; }
protocol() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync python -m leo_tracker.radio.beacon.offload "$@"; }

run_retention() {
  local worker_id="$1" result
  (
    exec 10>"${retention_lock}"
    flock -n 10 || exit 0
    if result="$(radio starlink-beacon-retain "${root}" \
        --keep-negative "${keep_negative}" --keep-confirmed "${keep_confirmed}" \
        --keep-wide "${keep_wide}" --keep-oversample "${keep_oversample}" \
        --keep-hop-sessions "${keep_hop_sessions}")"; then
      emit "retention_done worker=${worker_id} policy=negative:${keep_negative},confirmed:${keep_confirmed},wide:${keep_wide},oversample:${keep_oversample},hop:${keep_hop_sessions}"
    else
      emit "retention_failed worker=${worker_id}"
    fi
  )
}

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
  protocol status "${root}" --workers "${workers}" --pipeline-id "${pipeline_id}" \
    --archive-root "${archive_root}" --write "${reports}/analysis-server-status.json" \
    >/dev/null || emit "status_write_failed"
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
  local has_checks=0 archived=0
  local -a analysis_args template_args frame_args track_args passes_args
  analysis_args=(--exact-window-s .01)
  template_args=(); frame_args=(); passes_args=()
  case "${mode}" in
    wide) analysis_args+=(--exact-interval-s "${wide_exact_interval_s}" --acquisition-span-hz "${wide_acquisition_span_hz}" --acquisition-step-hz "${wide_acquisition_step_hz}" --exact-subband-rate-hz 2500000) ;;
    oversample) analysis_args+=(--exact-interval-s "${full_exact_interval_s}" --exact-subband-rate-hz 5000000) ;;
    hop) analysis_args+=(--exact-interval-s .7 --exact-window-s .02) ;;
    *) analysis_args+=(--exact-interval-s "${full_exact_interval_s}") ;;
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
  if [[ -f "${confirmed}" ]]; then
    emit "signal_result worker=${worker_id} job=${name} confirmed=true followup=${followup}"
  else
    emit "signal_result worker=${worker_id} job=${name} confirmed=false derived_report=${report}"
  fi
  if protocol inspect-followup "${followup}" >/dev/null; then has_checks=1; fi
  if (( has_checks == 1 )); then
    frame_args=("${template_args[@]}")
    if run_stage "${worker_id}" "${name}" frame_track radio starlink-beacon-frame-track "${capture}" "${followup}" "${frame}" \
        --samples "${samples}" "${frame_args[@]}" && \
        grep -Eq '"dual_valid_frame_count": [1-9]' "${frame}"; then
      track_args=(--measurement-source conditioned_frames --frame-track "${frame}")
    else
      track_args=(--measurement-source auto)
    fi
    if [[ "${mode}" != wide && "${mode}" != hop ]]; then
      run_stage "${worker_id}" "${name}" decode radio starlink-beacon-decode "${capture}" "${followup}" "${decoded}" \
        --plot "${decoded_plot}" --symbols "${symbols}" || return 1
    fi
  else
    track_args=(--measurement-source periodic_epoch)
    emit "stage_skipped worker=${worker_id} job=${name} stage=decode reason=no_candidate_checks"
  fi
  if [[ "${mode}" != wide ]]; then
    run_stage "${worker_id}" "${name}" doppler_track radio starlink-beacon-track "${capture}" "${followup}" "${track}" \
      --maximum-gap-s 5 --maximum-reacquisition-span-hz 5000 "${track_args[@]}" || return 1
  else
    emit "stage_skipped worker=${worker_id} job=${name} stage=doppler_track reason=wide_analysis_contains_full_coverage_windows"
  fi
  if [[ "${mode}" != wide && -f "${job_context}/tle-catalog.json" ]]; then
    run_stage "${worker_id}" "${name}" tle_association orbit associate --observations "${track}" --catalog "${job_context}/tle-catalog.json" \
      --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
      --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
      --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" --output "${association}" || return 1
  fi
  if [[ "${archive_mode}" != off ]]; then
    if run_stage "${worker_id}" "${name}" evidence_archive radio starlink-evidence-archive \
        "${capture}" "${reports}" "${archive_root}"; then
      archived=1
    elif [[ "${archive_mode}" == required ]]; then
      return 1
    else
      emit "archive_deferred worker=${worker_id} job=${name} mode=shadow"
    fi
  fi
  emit "coverage_result worker=${worker_id} job=${name} full_coverage=${full_coverage} has_checks=${has_checks} archived=${archived}"
}

worker() {
  local worker_id="$1" marker claim name capture mode job_context log done failed
  local started elapsed metric temporary claim_fd
  exec {claim_fd}>"${claim_lock}"
  while true; do
    flock "${claim_fd}"
    if [[ -f "${drain_request}" ]]; then
      flock -u "${claim_fd}"
      emit "worker_drained worker=${worker_id}"
      return 0
    fi
    marker="$(find "${queue}" -maxdepth 1 -type f -name '*.job' -print -quit)"
    if [[ -z "${marker}" ]]; then
      flock -u "${claim_fd}"
      (( once == 1 )) && return 0
      sleep "${poll_s}"; continue
    fi
    claim="${marker%.job}.running.${worker_id}.$$"
    if ! mv "${marker}" "${claim}" 2>/dev/null; then
      flock -u "${claim_fd}"
      continue
    fi
    flock -u "${claim_fd}"
    IFS=$'\t' read -r name capture mode job_context < "${claim}"
    job_context="${job_context:-${default_context}}"
    [[ "${capture}" == /* ]] || capture="${root}/${capture}"
    [[ "${job_context}" == /* ]] || job_context="${root}/${job_context}"
    log="${reports}/${name}.worker.log"
    started="$(date +%s)"
    emit "job_start worker=${worker_id} job=${name} mode=${mode} capture=${capture} context=${job_context} log=${log}"
    validation_args=("${root}" "${name}" "${mode}" --context "${job_context}"
      --elapsed-s "$(( $(date +%s) - started ))" --pipeline-id "${pipeline_id}" --write)
    [[ "${full_coverage}" == 1 ]] && validation_args+=(--full-coverage)
    [[ "${archive_mode}" == required ]] && validation_args+=(--archive-root "${archive_root}")
    if process_job "${worker_id}" "${name}" "${capture}" "${mode}" "${job_context}" > >(tee -a "${log}") 2>&1 &&
       run_stage "${worker_id}" "${name}" validate_outputs protocol validate \
         "${validation_args[@]}" > >(tee -a "${log}") 2>&1; then
      done="${queue}/done/$(basename "${marker}")"
      mv "${claim}" "${done}"
      elapsed=$(( $(date +%s) - started ))
      metric="${metrics}/${name}.tsv"; temporary="${metric}.next.$$"
      printf 'success\t%d\t%s\t%s\n' "${elapsed}" "${mode}" "${worker_id}" > "${temporary}"
      mv "${temporary}" "${metric}"
      if [[ "${retention_mode}" == verified &&
            -f "${archive_root}/catalog/receipts/${name}.json" ]]; then
        run_retention "${worker_id}"
      else
        emit "retention_skipped worker=${worker_id} job=${name} mode=${retention_mode} archive_required=true"
      fi
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
request_local_drain() {
  local temporary
  temporary="${drain_request}.next.$$"
  printf 'requested_utc=%s requested_by_pid=%s signal=TERM\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" > "${temporary}"
  mv "${temporary}" "${drain_request}"
  emit "drain_requested reason=signal workers_finish_claimed_jobs=true"
}
trap stop_children INT EXIT
trap request_local_drain TERM
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
emit "server_start repo=${repo_dir} shared_root=${root} queue=${queue} reports=${reports} workers=${workers} once=${once} heartbeat_s=${heartbeat_s} pipeline=${pipeline_id} full_coverage=${full_coverage} archive_mode=${archive_mode} archive_root=${archive_root} retention_mode=${retention_mode} python=$(${venv}/bin/python --version 2>&1)"
emit "recovery ${audit_result}"
print_progress startup
for ((index=0; index<workers; index++)); do worker "${index}" & pids+=("$!"); done
(
  # This helper must terminate immediately when the parent tears it down; it
  # must not inherit the server's graceful-drain TERM handler.
  trap - TERM INT EXIT
  while true; do
    sleep "${heartbeat_s}"
    print_progress heartbeat
  done
) &
monitor_pid="$!"
status=0
for pid in "${pids[@]}"; do
  # A handled TERM interrupts bash's wait without terminating the child. Keep
  # waiting until the worker has observed drain.request and genuinely exited.
  while kill -0 "${pid}" 2>/dev/null; do
    wait "${pid}" && break
    child_status=$?
    kill -0 "${pid}" 2>/dev/null || { status="${child_status}"; break; }
  done
done
terminate_process_tree "${monitor_pid}"
wait "${monitor_pid}" 2>/dev/null || true
monitor_pid=""
pids=()
print_progress shutdown
if [[ -f "${drain_request}" ]]; then
  rm -f -- "${drain_request}"
  emit "drain_complete"
  status=0
fi
emit "server_stop status=${status}"
exit "${status}"
