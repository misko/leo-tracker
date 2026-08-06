#!/usr/bin/env bash
set -euo pipefail

repo_dir="${LEO_TRACKER_REPO:-/home/satpi01/leo-tracker}"
storage_root="${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}"
duration_s="${LEO_BEACON_DWELL_S:-120}"
wide_duration_s="${LEO_BEACON_WIDE_DWELL_S:-10}"
oversample_duration_s="${LEO_BEACON_OVERSAMPLE_DWELL_S:-15}"
# Once narrow lock has demonstrated the LNB offset is inside the 2.3 MHz
# passband, a wide reacquisition every ~30 minutes is enough to detect LO drift
# without repeatedly sacrificing near-continuous beacon coverage.
wide_every_cycles="${LEO_BEACON_WIDE_EVERY_CYCLES:-15}"
oversample_every_cycles="${LEO_BEACON_OVERSAMPLE_EVERY_CYCLES:-10}"
oversample_on_startup="${LEO_BEACON_OVERSAMPLE_ON_STARTUP:-1}"
keep_negative="${LEO_BEACON_KEEP_NEGATIVE:-12}"
uv_cache="${UV_CACHE_DIR:-${repo_dir}/.uv-cache}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
maximum_pi_temp_millic="${LEO_BEACON_MAX_PI_TEMP_MILLIC:-75000}"
resume_pi_temp_millic="${LEO_BEACON_RESUME_PI_TEMP_MILLIC:-70000}"
target_spec="${LEO_BEACON_TARGETS:-4:lower-edge}"
maximum_cycles="${LEO_BEACON_MAX_CYCLES:-0}"
fake_source="${LEO_BEACON_FAKE:-0}"
exact_acquisition_method="${LEO_BEACON_EXACT_ACQUISITION_METHOD:-pilot_symbolwise_v3}"
agc_probability_percent="${LEO_BEACON_AGC_PERCENT:-50}"
gain_experiment_id="${LEO_BEACON_GAIN_EXPERIMENT_ID:-randomized-manual-vs-slow-attack-v1}"
# The all-epoch v3 search is deliberately more expensive than the legacy
# coherent grid.  These cadences keep analysis inside the following 120 s
# capture on the Pi while retaining enough temporal samples to trigger the
# dense 100 ms follow-up around a beacon hit.
narrow_exact_interval_s="${LEO_BEACON_NARROW_EXACT_INTERVAL_S:-3}"
wide_exact_interval_s="${LEO_BEACON_WIDE_EXACT_INTERVAL_S:-10}"
read -r -a targets <<< "${target_spec}"
if (( ${#targets[@]} == 0 )); then
  echo "LEO_BEACON_TARGETS must contain at least one channel:region target" >&2
  exit 2
fi
if ! [[ "${agc_probability_percent}" =~ ^[0-9]+$ ]] ||
   (( agc_probability_percent < 0 || agc_probability_percent > 100 )); then
  echo "LEO_BEACON_AGC_PERCENT must be an integer from 0 through 100" >&2
  exit 2
fi
source_args=()
if [[ "${fake_source}" == "1" ]]; then
  source_args+=(--fake)
fi

mkdir -p "${storage_root}/captures" "${storage_root}/reports" "${storage_root}/staging"
mkdir -p "${storage_root}/reports/plots" "${storage_root}/reports/decoded"
mkdir -p "${storage_root}/reports/fingerprints"
mkdir -p "${storage_root}/reports/gain-experiment"
cd "${repo_dir}"

capture_target() {
    local target="$1" mode="$2" channel region stamp name capture
    local pi_temp_millic pi_temp radio_temp_millic radio_temp
    local gain_draw gain_bucket gain_probability gain_mode
    local -a temperature_args capture_args gain_args
    channel="${target%%:*}"
    region="${target##*:}"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    name="ch${channel}-${region}-${mode}-${stamp}"
    capture="${storage_root}/captures/${name}"
    while read -r pi_temp_millic < /sys/class/thermal/thermal_zone0/temp &&
          (( pi_temp_millic >= maximum_pi_temp_millic )); do
      printf '{"thermal_backoff":true,"pi_temperature_c":%.3f,"resume_below_c":%.3f}\n' \
        "$(awk -v value="${pi_temp_millic}" 'BEGIN {print value/1000}')" \
        "$(awk -v value="${resume_pi_temp_millic}" 'BEGIN {print value/1000}')"
      sleep 15
      if read -r pi_temp_millic < /sys/class/thermal/thermal_zone0/temp &&
         (( pi_temp_millic < resume_pi_temp_millic )); then
        break
      fi
    done
    pi_temp="$(awk '{printf "%.3f", $1/1000}' /sys/class/thermal/thermal_zone0/temp)"
    radio_temp_millic=""
    if [[ "${fake_source}" != "1" ]]; then
      radio_temp_millic="$(iio_attr -u ip:192.168.2.1 -c ad9361-phy temp0 2>/dev/null | sed -n "s/.*value '\([0-9-]*\)'.*/\1/p" || true)"
    fi
    temperature_args=(--host-temperature-c "${pi_temp}")
    if [[ -n "${radio_temp_millic}" ]]; then
      radio_temp="$(awk -v value="${radio_temp_millic}" 'BEGIN {printf "%.3f", value/1000}')"
      temperature_args+=(--radio-temperature-c "${radio_temp}")
    fi
    gain_draw="$(od -An -N4 -tu4 /dev/urandom | tr -d '[:space:]')"
    gain_bucket=$((gain_draw % 10000))
    gain_probability="$(awk -v value="${agc_probability_percent}" 'BEGIN {printf "%.4f", value/100}')"
    if (( gain_bucket < agc_probability_percent * 100 )); then
      gain_mode="slow_attack"
      gain_args=(--gain-mode slow_attack --agc-settle-s 2)
    else
      gain_mode="manual"
      gain_args=(--gain-mode manual --gain-db 50)
    fi
    gain_args+=(--gain-experiment-id "${gain_experiment_id}"
      --gain-random-draw-u32 "${gain_draw}"
      --gain-assignment-probability "${gain_probability}")
    printf '{"gain_experiment":"%s","assignment":"%s","draw_u32":%s,"agc_probability":%s}\n' \
      "${gain_experiment_id}" "${gain_mode}" "${gain_draw}" "${gain_probability}"
    if [[ "${mode}" == "wide" ]]; then
      capture_args=(--duration-s "${wide_duration_s}" --sample-rate-hz 10000000
        --bandwidth-hz 9000000 --block-size 1048576)
    elif [[ "${mode}" == "oversample" ]]; then
      capture_args=(--duration-s "${oversample_duration_s}" --sample-rate-hz 5000000
        --bandwidth-hz 3000000 --block-size 524288)
    else
      capture_args=(--duration-s "${duration_s}" --sample-rate-hz 2500000
        --bandwidth-hz 2300000 --block-size 262144)
    fi
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-capture "${capture}" "${capture_args[@]}" \
      --channel-number "${channel}" --region "${region}" \
      --observation-mode "${mode}" \
      "${gain_args[@]}" --chunk-s 5 --queue-blocks 16 \
      "${temperature_args[@]}" "${source_args[@]}"
    pending_name="${name}"
    pending_capture="${capture}"
    pending_mode="${mode}"
}

process_capture() {
    local name="$1" capture="$2" mode="$3"
    local report="${storage_root}/reports/${name}.json"
    local plot="${storage_root}/reports/plots/${name}.png"
    local followup="${storage_root}/reports/followups/${name}.json"
    local confirmation_marker="${storage_root}/staging/${name}.confirmed"
    local decode="${storage_root}/reports/decoded/${name}.json"
    local decode_plot="${storage_root}/reports/decoded/${name}.png"
    local decode_symbols="${storage_root}/reports/decoded/${name}.npz"
    local analysis_args
    if [[ "${mode}" == "wide" ]]; then
      analysis_args=(--exact-interval-s "${wide_exact_interval_s}" --exact-window-s .01
        --acquisition-span-hz 3500000 --acquisition-step-hz 500000
        --exact-subband-rate-hz 2500000)
    elif [[ "${mode}" == "oversample" ]]; then
      analysis_args=(--exact-interval-s "${narrow_exact_interval_s}" --exact-window-s .01
        --exact-subband-rate-hz 5000000)
    else
      analysis_args=(--exact-interval-s "${narrow_exact_interval_s}" --exact-window-s .01)
    fi
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-analyze "${capture}" "${report}" \
      --window-s 1 --maximum-analysis-rate-hz 50000 \
      --exact-acquisition-method "${exact_acquisition_method}" \
      "${analysis_args[@]}" --plot "${plot}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-followup "${capture}" "${report}" "${followup}" \
      --radius-s .5 --interval-s .1 --window-s .01 \
      --passes "${repo_dir}/artifacts/starlink_hybrid_watch/passes.json" \
      --confirmation-marker "${confirmation_marker}"
    # Wide reacquisitions can place the selected pilot bank away from baseband
    # zero; fixed-center narrow and oversampled captures are directly decodable.
    if [[ "${mode}" != "wide" && -f "${confirmation_marker}" ]]; then
      env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
        starlink-beacon-decode "${capture}" "${followup}" "${decode}" \
        --plot "${decode_plot}" --symbols "${decode_symbols}"
      if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
        starlink-beacon-fingerprint "${storage_root}" --capture-name "${name}"; then
        printf '{"fingerprint_error":true,"capture":"%s"}\n' "${name}" >&2
      fi
    fi
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-retain "${storage_root}" --keep-negative "${keep_negative}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-calibrate "${storage_root}/reports" \
      "${storage_root}/reports/calibration/calibration.json"
    if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-gain-summary "${storage_root}" \
      "${storage_root}/reports/gain-experiment/summary.json"; then
      printf '{"gain_summary_error":true}\n' >&2
    fi
    if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-dashboard-index "${storage_root}" \
      "${storage_root}/reports/dashboard-index.json" --capture-name "${name}"; then
      printf '{"dashboard_index_error":true,"capture":"%s"}\n' "${name}" >&2
    fi
}

analysis_pid=""
analysis_name=""
wait_for_analysis() {
  if [[ -n "${analysis_pid}" ]]; then
    wait "${analysis_pid}"
    analysis_pid=""
    analysis_name=""
  fi
}

stop_analysis() {
  if [[ -n "${analysis_pid}" ]]; then
    kill "${analysis_pid}" 2>/dev/null || true
    wait "${analysis_pid}" 2>/dev/null || true
  fi
}
handle_signal() {
  trap - EXIT INT TERM
  stop_analysis
  exit 130
}
trap stop_analysis EXIT
trap handle_signal INT TERM

start_pending_analysis() {
  # Exactly one analyzer is allowed. Capture N+1 overlaps analysis N, but a
  # backlog can never grow without bound or consume the NVMe silently.
  wait_for_analysis
  process_capture "${pending_name}" "${pending_capture}" "${pending_mode}" &
  analysis_pid=$!
  analysis_name="${pending_name}"
}

env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
  starlink-beacon-recover "${storage_root}" \
  --exact-acquisition-method "${exact_acquisition_method}" \
  --narrow-exact-interval-s "${narrow_exact_interval_s}" \
  --wide-exact-interval-s "${wide_exact_interval_s}" \
  --passes "${repo_dir}/artifacts/starlink_hybrid_watch/passes.json"
if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
  starlink-beacon-fingerprint "${storage_root}"; then
  printf '{"fingerprint_backfill_error":true}\n' >&2
fi
env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
  starlink-beacon-gain-summary "${storage_root}" \
  "${storage_root}/reports/gain-experiment/summary.json"
env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
  starlink-beacon-dashboard-index "${storage_root}" \
  "${storage_root}/reports/dashboard-index.json"
env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
  starlink-beacon-retain "${storage_root}" --keep-negative "${keep_negative}"

cycle=0
while true; do
  for target in "${targets[@]}"; do
    capture_target "${target}" narrow
    start_pending_analysis
  done
  cycle=$((cycle + 1))
  if (( (oversample_on_startup == 1 && cycle == 1) ||
        (oversample_every_cycles > 0 && cycle % oversample_every_cycles == 0) )); then
    for target in "${targets[@]}"; do
      capture_target "${target}" oversample
      start_pending_analysis
    done
  fi
  if (( wide_every_cycles > 0 && cycle % wide_every_cycles == 0 )); then
    for target in "${targets[@]}"; do
      capture_target "${target}" wide
      start_pending_analysis
    done
  fi
  if (( maximum_cycles > 0 && cycle >= maximum_cycles )); then
    wait_for_analysis
    break
  fi
done
