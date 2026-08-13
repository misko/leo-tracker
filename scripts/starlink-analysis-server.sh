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
backfill_interval_s="${LEO_ANALYSIS_BACKFILL_INTERVAL_S:-600}"
backfill_limit="${LEO_ANALYSIS_BACKFILL_LIMIT:-100}"
duration_window="${LEO_ANALYSIS_DURATION_WINDOW:-200}"
pipeline_id="${LEO_ANALYSIS_PIPELINE_ID:-kalman-full-v1}"
full_coverage="${LEO_ANALYSIS_FULL_COVERAGE:-1}"
archive_mode="${LEO_ANALYSIS_ARCHIVE_MODE:-shadow}"
# Survey probes live inside capture directories that retention removes whole.
# Their size follows the drawn capture configuration: 12.8 MB at 80 ms /
# 2.5 MS/s, 51.2 MB at 160 ms / 5 MS/s, averaging 28.8 MB over a uniform draw.
# On by default while detector work is under way.
survey_corpus_mode="${LEO_ANALYSIS_SURVEY_CORPUS_MODE:-on}"
survey_corpus_random_fraction="${LEO_ANALYSIS_SURVEY_CORPUS_FRACTION:-0.05}"
# Shadow detector bake-off over the preserved probes. Scores every candidate on
# every probe and accumulates, so methods are compared on a day of real sky
# rather than one sweep. Gates nothing and moves no deployed threshold.
# Measured at 55-62 s per entry on this host under normal contention, and the
# corpus grows far slower than captures do, so one entry per job keeps the tail
# bounded while still keeping up.
#
# The randomised capture configuration costs much less here than the four-fold
# growth in samples suggests: measured relative cost across the four arms is
# 1.00 / 1.17 / 1.19 / 1.44, because most of this stage is differential, GLRT
# and conditioned statistics over a fixed symbol count, and only the coarse
# bank and the full-frame search scale with probe length. So the dearest arm is
# about 80-90 s rather than the 220 s a linear model would predict, and the
# 240 s budget below still admits one entry per job in every configuration.
survey_score_mode="${LEO_ANALYSIS_SURVEY_SCORE_MODE:-on}"
survey_score_limit="${LEO_ANALYSIS_SURVEY_SCORE_LIMIT:-1}"
survey_score_null_stride="${LEO_ANALYSIS_SURVEY_SCORE_NULL_STRIDE:-2}"
survey_score_maximum_s="${LEO_ANALYSIS_SURVEY_SCORE_MAXIMUM_S:-240}"
archive_root="${LEO_ANALYSIS_ARCHIVE_ROOT:-/mnt/qnap01/mouse9911/leo-cropped}"
retention_mode="${LEO_ANALYSIS_RETENTION_MODE:-disabled}"
# Optional shadow projection. Workers publish only immutable input manifests;
# the separate Kalman store service is the sole DuckDB writer.
full_exact_interval_s="${LEO_ANALYSIS_FULL_EXACT_INTERVAL_S:-1}"
wide_exact_interval_s="${LEO_ANALYSIS_WIDE_EXACT_INTERVAL_S:-2}"
# Wide captures are recorded at 10 MS/s, so 2.5 MHz subbands can only be tuned
# +-3.75 MHz before leaving the sampled band.  Match the acquisition watcher.
wide_acquisition_span_hz="${LEO_ANALYSIS_WIDE_ACQUISITION_SPAN_HZ:-3500000}"
# Provider comparison runs the same track against each independently retrieved
# catalog. Empty disables it. The receipt never depends on these outputs.
catalog_store_root="${LEO_CATALOG_STORE_ROOT:-/mnt/qnap01/mouse9911/tle}"
# Two LNBs on one radio have independent references and the pair share a tuner,
# so each receiver's acquisition search is centred on its own oscillator. The
# measurement lives beside the reports it was taken from.
calibration_root="${LEO_ANALYSIS_CALIBRATION_ROOT:-/mnt/qnap01/mouse9911/leo}"
association_compare_sources="${LEO_ASSOCIATION_COMPARE_SOURCES:-space-track huggingface}"
association_compare_scope="${LEO_ASSOCIATION_COMPARE_SCOPE:-starlink}"
wide_acquisition_step_hz="${LEO_ANALYSIS_WIDE_ACQUISITION_STEP_HZ:-2000000}"
# Conditioned dual-RX frames remain separated unless both CFO trajectories and
# their relative receiver offset extrapolate across the outage. Fifteen seconds
# recovered a field-verified 29.6 s arc while the TLE stability gates still
# rejected unrelated trajectories.
track_maximum_gap_s="${LEO_ANALYSIS_TRACK_MAXIMUM_GAP_S:-15}"
track_maximum_reacquisition_span_hz="${LEO_ANALYSIS_TRACK_MAXIMUM_REACQUISITION_SPAN_HZ:-15000}"
frame_maximum_extension_s="${LEO_ANALYSIS_FRAME_MAXIMUM_EXTENSION_S:-60}"
fragment_maximum_gap_s="${LEO_ANALYSIS_FRAGMENT_MAXIMUM_GAP_S:-30}"
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
if ! [[ "${heartbeat_s}" =~ ^[1-9][0-9]*$ &&
        "${backfill_interval_s}" =~ ^[1-9][0-9]*$ &&
        "${backfill_limit}" =~ ^[1-9][0-9]*$ &&
        "${duration_window}" =~ ^[1-9][0-9]*$ ]]; then
  echo "heartbeat, backfill interval, backfill limit, and duration window must be positive integers" >&2
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
# Job durations feed one ETA string, so the progress path reads a bounded tail
# of recent jobs rather than every historical per-job metric file. Reading all
# of them made each completion cost a network round trip per job already done,
# which is quadratic in the size of the run and starves workers of claims.
recent_durations="${metrics}/recent-durations.tsv"
progress_log="${reports}/analysis-server.log"
runtime_state="${reports}/runtime/analysis-server.json"
progress_lock="${queue}/progress.lock"
retention_lock="${queue}/retention.lock"
keep_negative="${LEO_ANALYSIS_KEEP_NEGATIVE:-6}"
keep_confirmed="${LEO_ANALYSIS_KEEP_CONFIRMED:-8}"
keep_wide="${LEO_ANALYSIS_KEEP_WIDE:-2}"
keep_oversample="${LEO_ANALYSIS_KEEP_OVERSAMPLE:-4}"
keep_hop_sessions="${LEO_ANALYSIS_KEEP_HOP_SESSIONS:-6}"
mkdir -p "${queue}/done" "${queue}/failed" "${metrics}" \
  "${reports}"/{plots,followups,decoded,frame-tracks,tracks,channel-links,associations,fragment-associations,fragment-diagnostics,fingerprints}
exec 8>"${queue}/server.lock"
if ! flock -n 8; then
  echo "another analysis server already owns ${queue}" >&2
  exit 2
fi
rm -f -- "${drain_request}"
cd "${repo_dir}"
server_started_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
git_commit="$(git rev-parse --verify HEAD 2>/dev/null || printf unknown)"

radio() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-radio "$@"; }
orbit() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync leo-orbit "$@"; }
protocol() { env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync python -m leo_tracker.radio.beacon.offload "$@"; }

# Associate one track against a named catalog-store snapshot. Used only for
# provider comparison, so it reports failure to the caller instead of aborting.
associate_against_snapshot() {
  local observations="$1" snapshot="$2" output="$3"
  orbit associate --observations "${observations}" --catalog "${snapshot}" \
    --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
    --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
    --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" --output "${output}" >/dev/null
}

run_backfill() {
  local reason="$1" result
  if result="$(protocol enqueue-backfill "${root}" --pipeline-id "${pipeline_id}" \
      --limit "${backfill_limit}" --summary-only)"; then
    emit "backfill reason=${reason} result=${result}"
  else
    emit "backfill_failed reason=${reason} result=${result}"
  fi
}

reconcile_failed() {
  local reason="$1" result
  if result="$(protocol reconcile-failed "${root}" --pipeline-id "${pipeline_id}" \
      --archive-root "${archive_root}")"; then
    emit "failed_reconciliation reason=${reason} result=${result}"
  else
    emit "failed_reconciliation_failed reason=${reason} result=${result}"
  fi
}

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

# Append one finished job to the bounded duration window. The lock is held for
# an append and an occasional rewrite, both O(duration_window), so a worker
# never waits on history to claim its next job. Trimming at a multiple of the
# window keeps that rewrite amortized while bounding what print_progress reads.
record_duration() {
  local outcome="$1" elapsed="$2" mode="$3" worker_id="$4" trimmed
  (
    flock 9
    printf '%s\t%s\t%s\t%s\n' "${outcome}" "${elapsed}" "${mode}" "${worker_id}" \
      >> "${recent_durations}"
    if (( $(wc -l < "${recent_durations}") > duration_window * 4 )); then
      trimmed="${recent_durations}.next.$$"
      tail -n "${duration_window}" "${recent_durations}" > "${trimmed}" &&
        mv "${trimmed}" "${recent_durations}"
    fi
  ) 9>"${progress_lock}"
}

publish_runtime_state() {
  local state="$1" heartbeat_utc temporary
  heartbeat_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  temporary="${runtime_state}.next.$$"
  mkdir -p "$(dirname "${runtime_state}")"
  "${venv}/bin/python" - "${temporary}" "${runtime_state}" \
      "${state}" "${server_started_utc}" "${heartbeat_utc}" "$$" \
      "$(hostname)" "${repo_dir}" "${git_commit}" "${pipeline_id}" \
      "${workers}" "${full_coverage}" "${archive_mode}" "${archive_root}" \
      "${retention_mode}" <<'PY'
import json
import os
from pathlib import Path
import sys

(temporary, destination, state, started, heartbeat, pid, hostname, repo,
 commit, pipeline, workers, full_coverage, archive_mode, archive_root,
 retention_mode) = sys.argv[1:]
value = {
    "schema": "leo-tracker.analysis-server-runtime/v1",
    "state": state,
    "started_utc": started,
    "heartbeat_utc": heartbeat,
    "pid": int(pid),
    "hostname": hostname,
    "repo": repo,
    "git_commit": commit,
    "pipeline_id": pipeline,
    "workers": int(workers),
    "full_coverage": full_coverage == "1",
    "archive_mode": archive_mode,
    "archive_root": archive_root,
    "evidence_policy": "tiered-v2",
    "archive_command": "starlink-evidence-archive-v2",
    "retention_mode": retention_mode,
    "producer_contract_valid": (
        state == "running" and full_coverage == "1" and
        archive_mode == "required"
    ),
}
temporary_path = Path(temporary)
with temporary_path.open("w", encoding="utf-8") as stream:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary_path, destination)
PY
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

# Progress is a periodic report, not a per-job one. Workers emit their own
# job_done/job_failed line and immediately claim again; only the monitor calls
# this. Calling it per job made all sixteen workers recount the queue and
# rescan history concurrently after every completion, which cost more wall
# clock than the analysis itself and left the queue draining on a few workers.
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
    tail -n "${duration_window}" "${recent_durations}" 2>/dev/null |
      awk -F '\t' '{sum+=$2; count++} END {printf "%d %.3f\n", count, count ? sum/count : 0}')
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
    --archive-root "${archive_root}" --skip-completion-count \
    --write "${reports}/analysis-server-status.json" \
    >/dev/null || emit "status_write_failed"
}

# The exact versioned completion count stats one receipt per historical run, so
# it rides the slow periodic loop instead of the heartbeat. The count is
# advisory, and a bounded staleness is worth far more than the throughput lost
# to recomputing it every thirty seconds.
publish_deep_status() {
  local reason="$1"
  if protocol status "${root}" --workers "${workers}" --pipeline-id "${pipeline_id}" \
      --archive-root "${archive_root}" \
      --write "${reports}/analysis-server-status.json" >/dev/null; then
    emit "deep_status reason=${reason}"
  else
    emit "deep_status_failed reason=${reason}"
  fi
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

template_matches_capture() {
  local template="$1" capture="$2"
  "${venv}/bin/python" - "${template}" "${capture}/manifest.json" <<'PY'
import json
import pathlib
import sys

try:
    template = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    manifest = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
    template_rate = float(template["sample_rate_hz"])
    capture_rate = float(manifest["sample_rate_hz"])
    template_region = str(template["region"])
    capture_region = str(manifest.get("metadata", {}).get("region", ""))
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit(1)

qualified = bool(template.get("summary", {}).get("qualified", False))
raise SystemExit(0 if (qualified and abs(template_rate-capture_rate) <= 1e-6 and
                       template_region == capture_region) else 1)
PY
}

process_job() {
  local worker_id="$1" name="$2" capture="$3" mode="$4" job_context="$5"
  local report="${reports}/${name}.json" plot="${reports}/plots/${name}.png"
  local followup="${reports}/followups/${name}.json" confirmed="${root}/staging/${name}.confirmed"
  local frame="${reports}/frame-tracks/${name}.json" samples="${reports}/frame-tracks/${name}.npz"
  local decoded="${reports}/decoded/${name}.json" symbols="${reports}/decoded/${name}.npz"
  local decoded_plot="${reports}/decoded/${name}.png" track="${reports}/tracks/${name}.json"
  local association="${reports}/associations/${name}.json"
  local linked="${reports}/channel-links/${name}.json"
  local linked_association="${reports}/associations/${name}-channel-link.json"
  local fragment_association="${reports}/fragment-associations/${name}.json"
  local fragment_diagnostic="${reports}/fragment-diagnostics/${name}.json"
  local has_checks=0 archived=0
  local source comparison snapshot
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
    if template_matches_capture "${job_context}/learned-beacon.json" "${capture}"; then
      template_args=(--beacon-template "${job_context}/learned-beacon.json")
    else
      emit "template_skipped worker=${worker_id} job=${name} reason=incompatible_rate_region_or_qualification"
    fi
  fi
  [[ -f "${job_context}/passes.json" ]] && passes_args=(--passes "${job_context}/passes.json")
  run_stage "${worker_id}" "${name}" acquire radio starlink-beacon-analyze "${capture}" "${report}" --window-s 1 \
    --maximum-analysis-rate-hz 50000 --exact-acquisition-method pilot_symbolwise_v3 \
    --calibration-root "${calibration_root}" \
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
        --samples "${samples}" --maximum-extension-s "${frame_maximum_extension_s}" \
        "${frame_args[@]}" && \
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
      --maximum-gap-s "${track_maximum_gap_s}" \
      --maximum-reacquisition-span-hz "${track_maximum_reacquisition_span_hz}" \
      "${track_args[@]}" || return 1
  else
    emit "stage_skipped worker=${worker_id} job=${name} stage=doppler_track reason=wide_analysis_contains_full_coverage_windows"
  fi
  if [[ "${mode}" != wide && -f "${job_context}/tle-catalog.json" ]]; then
    run_stage "${worker_id}" "${name}" tle_association orbit associate --observations "${track}" --catalog "${job_context}/tle-catalog.json" \
      --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
      --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
      --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" --output "${association}" || return 1
    # Repeat the same track against each independently retrieved catalog so the
    # providers can be compared on identical observations. This is additive:
    # the receipt still depends only on the context-bundle association above,
    # and a provider that is unavailable or stale must never fail a job.
    for source in ${association_compare_sources}; do
      comparison="${reports}/associations/${source}/${name}.json"
      snapshot="${catalog_store_root}/latest/${source}/${association_compare_scope}.json"
      if [[ ! -f "${snapshot}" ]]; then
        emit "association_compare_skipped worker=${worker_id} job=${name} source=${source} reason=no_snapshot"
        continue
      fi
      mkdir -p "$(dirname "${comparison}")"
      if associate_against_snapshot "${track}" "${snapshot}" "${comparison}"; then
        emit "association_compare worker=${worker_id} job=${name} source=${source} output=${comparison}"
      else
        emit "association_compare_failed worker=${worker_id} job=${name} source=${source}"
      fi
    done
  fi
  # Fixed-tuning beacon traffic can disappear for seconds while the same
  # spacecraft remains visible. Preserve those outages, conservatively link
  # only smooth dual-RX CFO continuations, and ask the independent held-out
  # TLE stage to accept or reject the resulting hypothesis.
  if [[ "${mode}" == narrow || "${mode}" == oversample ]]; then
    if run_stage "${worker_id}" "${name}" fragment_link radio \
        starlink-beacon-channel-link "${linked}" "${track}" \
        --maximum-gap-s "${fragment_maximum_gap_s}" \
        --maximum-acceleration-difference-m-s2 35 \
        --maximum-same-tuning-quadratic-rms-hz 2000; then
      if grep -Eq '"multi_segment_hypothesis_count": [1-9]' "${linked}"; then
        if [[ -f "${job_context}/tle-catalog.json" ]]; then
          run_stage "${worker_id}" "${name}" fragment_tle_association orbit associate \
            --observations "${linked}" --catalog "${job_context}/tle-catalog.json" \
            --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
            --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
            --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" \
            --minimum-dual-epochs 30 --minimum-coverage-fraction .1 \
            --output "${linked_association}" || return 1
          if run_stage "${worker_id}" "${name}" fragment_component_association orbit associate \
            --observations "${track}" --catalog "${job_context}/tle-catalog.json" \
            --lat "${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}" \
            --lon "${LEO_BEACON_OBSERVER_LON:--122.48567658142287}" \
            --alt-m "${LEO_BEACON_OBSERVER_ALT_M:-0}" \
            --minimum-duration-s .5 --minimum-dual-epochs 5 \
            --minimum-coverage-fraction .02 \
            --output "${fragment_association}"; then
            run_stage "${worker_id}" "${name}" fragment_diagnostic orbit \
              diagnose-fragments --tracks "${track}" --links "${linked}" \
              --joint-association "${linked_association}" \
              --fragment-association "${fragment_association}" \
              --output "${fragment_diagnostic}" || \
              emit "shadow_stage_failed worker=${worker_id} job=${name} stage=fragment_diagnostic production_affected=false"
          else
            emit "shadow_stage_failed worker=${worker_id} job=${name} stage=fragment_component_association production_affected=false"
          fi
        fi
      else
        emit "stage_skipped worker=${worker_id} job=${name} stage=fragment_tle_association reason=no_multi_fragment_hypothesis"
      fi
    else
      emit "stage_skipped worker=${worker_id} job=${name} stage=fragment_tle_association reason=no_usable_fragments"
    fi
  fi
  if [[ "${archive_mode}" != off ]]; then
    if run_stage "${worker_id}" "${name}" evidence_archive_v2 radio starlink-evidence-archive-v2 \
        "${capture}" "${reports}" "${archive_root}"; then
      archived=1
    elif [[ "${archive_mode}" == required ]]; then
      return 1
    else
      emit "archive_deferred worker=${worker_id} job=${name} mode=shadow"
    fi
  fi
  # Preserve this capture's survey probe before retention removes the whole
  # directory. Measured on the live system a probe survives about three hours,
  # and every detector measurement planned depends on probes outliving that.
  # Shadow: a corpus that failed to grow is a slower experiment, while a job
  # that failed because of one is a capture nobody analysed.
  if [[ "${survey_corpus_mode:-on}" != off ]]; then
    if ! run_stage "${worker_id}" "${name}" survey_corpus radio \
        starlink-survey-corpus sample "${root}" \
        --random-fraction "${survey_corpus_random_fraction:-0.05}"; then
      emit "shadow_stage_failed worker=${worker_id} job=${name} stage=survey_corpus production_affected=false"
    fi
  fi
  # Score whatever the corpus stage just preserved with every candidate
  # detector. This is the accumulating half of the bake-off: one sweep of real
  # sky is one draw from an uncharacterised distribution, and with no injection
  # there is no ground truth, so the comparison has to be built out of many
  # probes rather than one. Shadow for the same reason as the corpus above: a
  # comparison that stopped growing is a slower experiment, while a job that
  # failed because of one is a capture nobody analysed.
  if [[ "${survey_score_mode:-on}" != off ]]; then
    if ! run_stage "${worker_id}" "${name}" survey_score radio \
        starlink-survey-score run "${root}" \
        --limit "${survey_score_limit:-1}" \
        --null-stride "${survey_score_null_stride:-2}" \
        --maximum-seconds "${survey_score_maximum_s:-240}"; then
      emit "shadow_stage_failed worker=${worker_id} job=${name} stage=survey_score production_affected=false"
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
      metric="${metrics}/${name}.tsv"
      # $$ is the server pid in every worker subshell, so it cannot
      # separate two workers holding markers for the same job name.
      temporary="${metric}.next.${worker_id}.$$"
      printf 'success\t%d\t%s\t%s\n' "${elapsed}" "${mode}" "${worker_id}" > "${temporary}"
      mv "${temporary}" "${metric}"
      record_duration success "${elapsed}" "${mode}" "${worker_id}"
      if [[ "${retention_mode}" == verified &&
            -f "${archive_root}/catalog/v2/receipts/${name}.json" ]]; then
        run_retention "${worker_id}"
      else
        emit "retention_skipped worker=${worker_id} job=${name} mode=${retention_mode} archive_required=true"
      fi
      # Publish this recording's listing row. The analysis server writes the
      # reports, so it is the only producer that can keep a listing current;
      # the capture watcher could not, which is how the index fell thousands
      # of recordings behind. One small write-once file per job means sixteen
      # workers need no lock, and a failure here must never fail the job.
      if radio starlink-beacon-dashboard-row "${root}" "${name}" >/dev/null 2>&1; then
        emit "dashboard_row worker=${worker_id} job=${name}"
      else
        emit "dashboard_row_failed worker=${worker_id} job=${name}"
      fi
      emit "job_done worker=${worker_id} job=${name} mode=${mode} elapsed=$(human_duration "${elapsed}") report=${reports}/${name}.json"
    else
      failed="${queue}/failed/$(basename "${marker}")"
      mv "${claim}" "${failed}"
      elapsed=$(( $(date +%s) - started ))
      metric="${metrics}/${name}.tsv"
      # $$ is the server pid in every worker subshell, so it cannot
      # separate two workers holding markers for the same job name.
      temporary="${metric}.next.${worker_id}.$$"
      printf 'failed\t%d\t%s\t%s\n' "${elapsed}" "${mode}" "${worker_id}" > "${temporary}"
      mv "${temporary}" "${metric}"
      record_duration failed "${elapsed}" "${mode}" "${worker_id}"
      emit "job_failed worker=${worker_id} job=${name} mode=${mode} elapsed=$(human_duration "${elapsed}") log=${log}"
    fi
  done
}

pids=()
monitor_pid=""
startup_pid=""
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
  [[ -z "${startup_pid}" ]] || terminate_process_tree "${startup_pid}"
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
publish_runtime_state running
emit "server_start repo=${repo_dir} shared_root=${root} queue=${queue} reports=${reports} workers=${workers} once=${once} heartbeat_s=${heartbeat_s} pipeline=${pipeline_id} full_coverage=${full_coverage} archive_mode=${archive_mode} archive_root=${archive_root} evidence_policy=tiered-v2 retention_mode=${retention_mode} survey_corpus_mode=${survey_corpus_mode} survey_score_mode=${survey_score_mode} python=$(${venv}/bin/python --version 2>&1)"
startup_reconciliation() {
  local audit_result
  audit_result="$(protocol audit "${root}" --context "${default_context}" --pipeline-id "${pipeline_id}")"
  emit "recovery ${audit_result}"
  run_backfill startup
  reconcile_failed startup
  print_progress startup
}
# Startup reconciliation costs one round trip per already-finished recording,
# so it scales with the size of the archive rather than with the work waiting.
# On a high-latency share that walk outgrew the restart interval, and because
# it gated the worker spawn it left every ready capture unprocessed while the
# queue kept filling.  Run it beside the workers instead.  The audit only moves
# entries out of done/ back into the ready queue, and backfill only adds to it,
# which is precisely what the periodic monitor below already does while workers
# hold claims -- workers claim under claim.lock, so a late arrival is ordinary.
# --once keeps the original order: it must reconcile before the workers look,
# because startup backfill is what populates the queue it is asked to drain.
if (( once == 1 )); then
  startup_reconciliation
  for ((index=0; index<workers; index++)); do worker "${index}" & pids+=("$!"); done
else
  for ((index=0; index<workers; index++)); do worker "${index}" & pids+=("$!"); done
  (
    # As with the monitor, this must die on teardown rather than inherit the
    # server's graceful-drain handler and outlive the workers it feeds.
    trap - TERM INT EXIT
    startup_reconciliation
  ) &
  startup_pid="$!"
fi
(
  # This helper must terminate immediately when the parent tears it down; it
  # must not inherit the server's graceful-drain TERM handler.
  trap - TERM INT EXIT
  while true; do
    elapsed=0
    while (( elapsed < backfill_interval_s )); do
      sleep "${heartbeat_s}"
      elapsed=$((elapsed + heartbeat_s))
      print_progress heartbeat
      publish_runtime_state running
    done
    run_backfill periodic
    reconcile_failed periodic
    publish_deep_status periodic
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
if [[ -n "${startup_pid}" ]]; then
  # Reconciliation is resumed from scratch on the next start, so a drain must
  # not wait out a walk that can outlast the shutdown timeout.
  terminate_process_tree "${startup_pid}"
  wait "${startup_pid}" 2>/dev/null || true
  startup_pid=""
fi
pids=()
print_progress shutdown
if [[ -f "${drain_request}" ]]; then
  rm -f -- "${drain_request}"
  emit "drain_complete"
  status=0
fi
emit "server_stop status=${status}"
publish_runtime_state stopped
exit "${status}"
