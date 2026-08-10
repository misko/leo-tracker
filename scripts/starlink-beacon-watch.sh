#!/usr/bin/env bash
set -euo pipefail

repo_dir="${LEO_TRACKER_REPO:-/home/satpi01/leo-tracker}"
storage_root="${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}"
duration_s="${LEO_BEACON_DWELL_S:-120}"
wide_duration_s="${LEO_BEACON_WIDE_DWELL_S:-10}"
oversample_duration_s="${LEO_BEACON_OVERSAMPLE_DWELL_S:-15}"
# Once narrow lock has demonstrated the LNB offset is inside the 2.5 MHz
# passband, a wide reacquisition every ~30 minutes is enough to detect LO drift
# without repeatedly sacrificing near-continuous beacon coverage.
wide_every_cycles="${LEO_BEACON_WIDE_EVERY_CYCLES:-15}"
oversample_every_cycles="${LEO_BEACON_OVERSAMPLE_EVERY_CYCLES:-10}"
oversample_on_startup="${LEO_BEACON_OVERSAMPLE_ON_STARTUP:-1}"
# A short low-band channel survey keeps one Pluto stream open across retunes.
# The installed universal LNB is currently in 9.75 GHz low-band mode, making
# Starlink channels 1--4 the settled, in-spec search set without a 22 kHz tone.
# Three sessions span about 34 seconds on the installed Pluto.  That crosses
# at least two of the documented 15-second Starlink channel boundaries and can
# therefore produce the >=20-second elapsed arc required by TLE association.
# Running the burst every second 120-second fixed dwell gives the dedicated
# worker about 274 seconds to process 12 symbolwise children, above its measured
# retained-field replay time.
hop_every_cycles="${LEO_BEACON_HOP_EVERY_CYCLES:-2}"
hop_burst_sessions="${LEO_BEACON_HOP_BURST_SESSIONS:-3}"
hop_dwell_s="${LEO_BEACON_HOP_DWELL_S:-2}"
hop_channels="${LEO_BEACON_HOP_CHANNELS:-1 2 3 4}"
hop_settle_buffers="${LEO_BEACON_HOP_SETTLE_BUFFERS:-2}"
# A hop child is only 1.5 seconds long.  Sampling one 10-ms window (the
# ordinary narrow cadence) covers less than one percent of it and has poor
# probability of intersecting Starlink's documented 2% baseline frame PRF.
# Three independent 20-ms windows cover 3% of every child.  A retained-field
# replay showed that pilot_symbolwise_v3 confirmed a real dual-RX channel-2
# event that coherent_grid_v1 rejected from the identical IQ.  Halving the
# probe count keeps the measured per-child runtime approximately unchanged.
hop_exact_interval_s="${LEO_BEACON_HOP_EXACT_INTERVAL_S:-0.7}"
hop_exact_window_s="${LEO_BEACON_HOP_EXACT_WINDOW_S:-0.02}"
hop_acquisition_method="${LEO_BEACON_HOP_ACQUISITION_METHOD:-pilot_symbolwise_v3}"
keep_negative="${LEO_BEACON_KEEP_NEGATIVE:-6}"
keep_confirmed="${LEO_BEACON_KEEP_CONFIRMED:-8}"
keep_wide="${LEO_BEACON_KEEP_WIDE:-2}"
keep_oversample="${LEO_BEACON_KEEP_OVERSAMPLE:-4}"
keep_hop_sessions="${LEO_BEACON_KEEP_HOP_SESSIONS:-6}"
preserve_raw="${LEO_BEACON_PRESERVE_RAW:-0}"
minimum_free_gb="${LEO_BEACON_MINIMUM_FREE_GB:-150}"
analysis_mode="${LEO_BEACON_ANALYSIS_MODE:-local}"
if [[ "${analysis_mode}" != "local" && "${analysis_mode}" != "offload" ]]; then
  echo "LEO_BEACON_ANALYSIS_MODE must be local or offload" >&2
  exit 2
fi
uv_cache="${UV_CACHE_DIR:-${repo_dir}/.uv-cache}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
maximum_pi_temp_millic="${LEO_BEACON_MAX_PI_TEMP_MILLIC:-75000}"
resume_pi_temp_millic="${LEO_BEACON_RESUME_PI_TEMP_MILLIC:-70000}"
host_thermal_zone="${LEO_BEACON_THERMAL_ZONE:-/sys/class/thermal/thermal_zone0/temp}"

# Thermal backoff protects the Pi, so a sensor that exists but reads unusably
# is a hard error rather than a silently skipped safety check. Only a host with
# no such zone at all degrades to running without thermal management, which is
# what lets this script run off the Pi. Sets host_temperature_millic_value and
# must be called outside any subshell so a bad read can still abort the run.
host_temperature_millic_value=""
read_host_temperature() {
  host_temperature_millic_value=""
  [[ -e "${host_thermal_zone}" ]] || return 1
  if ! read -r host_temperature_millic_value < "${host_thermal_zone}" ||
     [[ ! "${host_temperature_millic_value}" =~ ^-?[0-9]+$ ]]; then
    echo "unreadable thermal zone ${host_thermal_zone}" >&2
    exit 3
  fi
}
target_spec="${LEO_BEACON_TARGETS:-4:lower-edge}"
maximum_cycles="${LEO_BEACON_MAX_CYCLES:-0}"
fake_source="${LEO_BEACON_FAKE:-0}"
radio_uri="${LEO_BEACON_RADIO_URI:-pluto://ip:192.168.2.1}"
radio_serial="${LEO_BEACON_RADIO_SERIAL:-}"
radio_id="${LEO_BEACON_RADIO_ID:-}"
receiver_label_spec="${LEO_BEACON_RECEIVER_LABELS:-rx0 rx1}"
exact_acquisition_method="${LEO_BEACON_EXACT_ACQUISITION_METHOD:-pilot_symbolwise_v3}"
agc_probability_percent="${LEO_BEACON_AGC_PERCENT:-50}"
gain_experiment_id="${LEO_BEACON_GAIN_EXPERIMENT_ID:-randomized-manual-vs-slow-attack-v1}"
# The all-epoch v3 search is deliberately more expensive than the legacy
# coherent grid.  These cadences keep analysis inside the following 120 s
# capture on the Pi while retaining enough temporal samples to trigger the
# dense 100 ms follow-up around a beacon hit.
narrow_exact_interval_s="${LEO_BEACON_NARROW_EXACT_INTERVAL_S:-6}"
wide_exact_interval_s="${LEO_BEACON_WIDE_EXACT_INTERVAL_S:-10}"
# Conditioned dual-RX frames are joined only when both CFO trajectories and
# their relative receiver offset extrapolate across the outage. The 15-second
# bound recovered a field-verified 29.6-second arc without relaxing the final
# orbit residual, margin, interior-epoch, or temporal-stability gates.
track_maximum_gap_s="${LEO_BEACON_TRACK_MAXIMUM_GAP_S:-15}"
track_maximum_reacquisition_span_hz="${LEO_BEACON_TRACK_MAXIMUM_REACQUISITION_SPAN_HZ:-15000}"
frame_maximum_extension_s="${LEO_BEACON_FRAME_MAXIMUM_EXTENSION_S:-60}"
rolling_association_interval_s="${LEO_BEACON_ROLLING_ASSOCIATION_INTERVAL_S:-600}"
observer_lat="${LEO_BEACON_OBSERVER_LAT:-37.849165355010086}"
observer_lon="${LEO_BEACON_OBSERVER_LON:--122.48567658142287}"
observer_alt_m="${LEO_BEACON_OBSERVER_ALT_M:-0}"
tle_catalog="${LEO_BEACON_TLE_CATALOG:-/mnt/qnap01/mouse9911/satellites/leo-tracker/tle-history/latest.json}"
learned_beacon="${LEO_BEACON_LEARNED_BEACON:-${storage_root}/reports/learned-beacons/active.json}"
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
if [[ "${preserve_raw}" != "0" && "${preserve_raw}" != "1" ]]; then
  echo "LEO_BEACON_PRESERVE_RAW must be 0 or 1" >&2
  exit 2
fi
if ! [[ "${minimum_free_gb}" =~ ^[1-9][0-9]*$ ]]; then
  echo "LEO_BEACON_MINIMUM_FREE_GB must be a positive integer" >&2
  exit 2
fi
if [[ -n "${radio_id}" && ! "${radio_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "LEO_BEACON_RADIO_ID must use letters, digits, dot, underscore, or dash" >&2
  exit 2
fi
read -r -a receiver_labels <<< "${receiver_label_spec}"
if (( ${#receiver_labels[@]} != 2 )) ||
   [[ ! "${receiver_labels[0]}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
   [[ ! "${receiver_labels[1]}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "LEO_BEACON_RECEIVER_LABELS must contain two safe labels" >&2
  exit 2
fi
source_args=()
if [[ "${fake_source}" == "1" ]]; then
  source_args+=(--fake)
else
  source_args+=(--uri "${radio_uri}")
fi
[[ -n "${radio_serial}" ]] && source_args+=(--serial "${radio_serial}")
[[ -n "${radio_id}" ]] && source_args+=(--radio-id "${radio_id}")
source_args+=(--receiver-labels "${receiver_labels[0]}" "${receiver_labels[1]}")
printf '{"radio_capture_config":true,"radio_id":"%s","serial":"%s","uri":"%s","receiver_labels":["%s","%s"]}\n' \
  "${radio_id}" "${radio_serial}" "${radio_uri}" \
  "${receiver_labels[0]}" "${receiver_labels[1]}"

mkdir -p "${storage_root}/captures" "${storage_root}/reports" "${storage_root}/staging"
mkdir -p "${storage_root}/hop-sessions"
mkdir -p "${storage_root}/reports/channel-links"
mkdir -p "${storage_root}/reports/plots" "${storage_root}/reports/decoded"
mkdir -p "${storage_root}/reports/fingerprints"
mkdir -p "${storage_root}/reports/tracks" "${storage_root}/reports/associations"
mkdir -p "${storage_root}/reports/frame-tracks"
mkdir -p "${storage_root}/reports/gain-experiment"
analysis_queue="${storage_root}/staging/analysis-queue"
mkdir -p "${analysis_queue}"
maintenance_lock="${storage_root}/staging/analysis-maintenance.lock"
analysis_execution_lock="${storage_root}/staging/analysis-execution.lock"
analysis_ready="${storage_root}/staging/analysis-workers.ready"
cd "${repo_dir}"

# The configured USB URI is intentionally stable and serial-selected, while
# diagnostic tools such as iio_attr require the current concrete bus address.
resolved_radio_uri="${radio_uri#pluto://}"
if [[ "${fake_source}" != "1" && -n "${radio_serial}" &&
      "${resolved_radio_uri}" == usb:* ]]; then
  resolved_radio_uri="$(env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" \
    run --active --no-sync python - "${radio_uri}" "${radio_serial}" <<'PY'
import sys
from leo_tracker.radio.pluto import _resolve_iio_uri
print(_resolve_iio_uri(sys.argv[1], sys.argv[2]))
PY
  )"
fi

capture_target() {
    local target="$1" mode="$2" channel region stamp name capture
    local pi_temp_millic pi_temp radio_temp_millic radio_temp
    local gain_draw gain_bucket gain_probability gain_mode
    local -a temperature_args capture_args gain_args
    while [[ "${preserve_raw}" == "1" ]] &&
          (( $(df -Pk "${storage_root}" | awk 'NR==2 {print $4}') < minimum_free_gb * 1024 * 1024 )); do
      printf '{"storage_backoff":true,"preserve_raw":true,"minimum_free_gb":%d}\n' \
        "${minimum_free_gb}" >&2
      sleep 60
    done
    channel="${target%%:*}"
    region="${target##*:}"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    if [[ -n "${radio_id}" ]]; then
      name="ch${channel}-${region}-${mode}-${radio_id}-${stamp}"
    else
      name="ch${channel}-${region}-${mode}-${stamp}"
    fi
    capture="${storage_root}/captures/${name}"
    if read_host_temperature &&
       (( host_temperature_millic_value >= maximum_pi_temp_millic )); then
      while (( host_temperature_millic_value >= resume_pi_temp_millic )); do
        printf '{"thermal_backoff":true,"pi_temperature_c":%.3f,"resume_below_c":%.3f}\n' \
          "$(awk -v value="${host_temperature_millic_value}" 'BEGIN {print value/1000}')" \
          "$(awk -v value="${resume_pi_temp_millic}" 'BEGIN {print value/1000}')"
        sleep 15
        read_host_temperature || break
      done
    fi
    pi_temp=""
    if read_host_temperature; then
      pi_temp_millic="${host_temperature_millic_value}"
      pi_temp="$(awk -v value="${pi_temp_millic}" 'BEGIN {printf "%.3f", value/1000}')"
    fi
    radio_temp_millic=""
    if [[ "${fake_source}" != "1" ]]; then
      radio_temp_millic="$(iio_attr -u "${resolved_radio_uri}" -c ad9361-phy temp0 2>/dev/null | sed -n "s/.*value '\([0-9-]*\)'.*/\1/p" || true)"
    fi
    temperature_args=()
    [[ -n "${pi_temp}" ]] && temperature_args=(--host-temperature-c "${pi_temp}")
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
        --bandwidth-hz 2500000 --block-size 262144)
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

capture_hop_survey() {
    local stamp session_name session_path child child_name label="${1:-}"
    local -a channel_args
    read -r -a channel_args <<< "${hop_channels}"
    stamp="${label:-$(date -u +%Y%m%dT%H%M%SZ)}"
    if [[ -n "${radio_id}" ]]; then
      session_name="hop-lower-edge-${radio_id}-${stamp}"
    else
      session_name="hop-lower-edge-${stamp}"
    fi
    session_path="${storage_root}/hop-sessions/${session_name}"
    if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-hop-capture "${session_path}" \
      --channels "${channel_args[@]}" --region lower-edge \
      --dwell-s "${hop_dwell_s}" --settle-buffers "${hop_settle_buffers}" \
      --sample-rate-hz 2500000 --bandwidth-hz 2500000 --block-size 262144 \
      --chunk-s "${hop_dwell_s}" --gain-mode manual --gain-db 50 \
      "${source_args[@]}"; then
      printf '{"hop_capture_error":true,"session":"%s"}\n' "${session_name}" >&2
      return
    fi
    for child in "${session_path}"/[0-9][0-9]-ch*-lower-edge; do
      [[ -d "${child}" ]] || continue
      child_name="${session_name}-$(basename "${child}")"
      pending_name="${child_name}"
      pending_capture="${child}"
      pending_mode="hop"
      enqueue_pending_analysis
    done
}

refresh_hop_links() {
    local name="$1" burst_prefix linked association
    local -a inputs
    [[ "${name}" == hop-lower-edge-*-b[0-9][0-9]-* ]] || return 0
    burst_prefix="${name%-b[0-9][0-9]-*}"
    shopt -s nullglob
    inputs=("${storage_root}/reports/tracks/${burst_prefix}"-b*.json)
    shopt -u nullglob
    (( ${#inputs[@]} >= 2 )) || return 0
    linked="${storage_root}/reports/channel-links/${burst_prefix}.json"
    association="${storage_root}/reports/associations/${burst_prefix}-channel-link.json"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-channel-link "${linked}" "${inputs[@]}"
    if [[ -f "${tle_catalog}" ]]; then
      env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-orbit \
        associate --observations "${linked}" --catalog "${tle_catalog}" \
        --lat "${observer_lat}" --lon "${observer_lon}" --alt-m "${observer_alt_m}" \
        --minimum-dual-epochs 30 --minimum-coverage-fraction .1 \
        --output "${association}"
    fi
}

refresh_capture_links() {
    local name="$1" track="$2" linked association
    linked="${storage_root}/reports/channel-links/${name}.json"
    association="${storage_root}/reports/associations/${name}-channel-link.json"
    # A fixed tuning can contain several pieces of one smooth Doppler path when
    # Starlink OFDM traffic goes quiet. The linker retains the outage as missing
    # data and requires a low-residual quadratic continuation before combining
    # the measured 10 Hz epochs.
    if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-channel-link "${linked}" "${track}" \
      --maximum-gap-s 30 --maximum-acceleration-difference-m-s2 35 \
      --maximum-same-tuning-quadratic-rms-hz 2000; then
      # A confirmed hop child can contain only one or two calibrated epochs.
      # The linker deliberately rejects such fragments and does not create an
      # output artifact.  That is an expected insufficient-evidence outcome,
      # not a reason to invoke association on a nonexistent path.
      printf '{"capture_link_unavailable":true,"capture":"%s"}\n' \
        "${name}" >&2
      return 0
    fi
    if [[ -f "${tle_catalog}" ]]; then
      env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-orbit \
        associate --observations "${linked}" --catalog "${tle_catalog}" \
        --lat "${observer_lat}" --lon "${observer_lon}" --alt-m "${observer_alt_m}" \
        --minimum-dual-epochs 30 --minimum-coverage-fraction .1 \
        --output "${association}"
    fi
}

refresh_rolling_narrow_links() {
    local name="$1" channel region linked association now previous
    local -a inputs
    channel="${name%%-*}"
    region="${name#${channel}-}"
    region="${region%-narrow-*}"
    mapfile -t inputs < <(find "${storage_root}/reports/tracks" -maxdepth 1 \
      -type f -name "${channel}-${region}-narrow-*.json" -printf '%p\n' | \
      sort | tail -n 8)
    (( ${#inputs[@]} >= 2 )) || return 0
    linked="${storage_root}/reports/channel-links/rolling-${channel}-${region}.json"
    association="${storage_root}/reports/associations/rolling-${channel}-${region}.json"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-channel-link "${linked}" "${inputs[@]}" \
      --maximum-gap-s 45 --maximum-acceleration-difference-m-s2 35 \
      --maximum-same-tuning-quadratic-rms-hz 2000
    now="$(date +%s)"
    previous=0
    if [[ -f "${association}" ]]; then
      previous="$(stat -c %Y "${association}")"
    fi
    if [[ -f "${tle_catalog}" ]] &&
       (( now - previous >= rolling_association_interval_s )); then
      env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-orbit \
        associate --observations "${linked}" --catalog "${tle_catalog}" \
        --lat "${observer_lat}" --lon "${observer_lon}" --alt-m "${observer_alt_m}" \
        --minimum-dual-epochs 30 --minimum-coverage-fraction .1 \
        --output "${association}"
    fi
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
    local track="${storage_root}/reports/tracks/${name}.json"
    local frame_track="${storage_root}/reports/frame-tracks/${name}.json"
    local frame_samples="${storage_root}/reports/frame-tracks/${name}.npz"
    local association="${storage_root}/reports/associations/${name}.json"
    local analysis_args track_args analysis_method template_analysis_args
    local -a plot_args
    analysis_method="${exact_acquisition_method}"
    plot_args=(--plot "${plot}")
    template_analysis_args=()
    if [[ "${mode}" == "wide" ]]; then
      analysis_args=(--exact-interval-s "${wide_exact_interval_s}" --exact-window-s .01
        --acquisition-span-hz 3500000 --acquisition-step-hz 500000
        --exact-subband-rate-hz 2500000)
    elif [[ "${mode}" == "oversample" ]]; then
      analysis_args=(--exact-interval-s "${narrow_exact_interval_s}" --exact-window-s .01
        --exact-subband-rate-hz 5000000)
    elif [[ "${mode}" == "hop" ]]; then
      analysis_args=(--exact-interval-s "${hop_exact_interval_s}"
        --exact-window-s "${hop_exact_window_s}")
      analysis_method="${hop_acquisition_method}"
      # Negative hop children are survey evidence rather than dashboard
      # products.  Avoid spending more time rendering them than recording them.
      plot_args=()
    else
      analysis_args=(--exact-interval-s "${narrow_exact_interval_s}" --exact-window-s .01)
    fi
    if [[ ("${mode}" == "narrow" || "${mode}" == "hop") &&
          -f "${learned_beacon}" ]]; then
      template_analysis_args=(--beacon-template "${learned_beacon}")
    fi
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-analyze "${capture}" "${report}" \
      --window-s 1 --maximum-analysis-rate-hz 50000 \
      --exact-acquisition-method "${analysis_method}" \
      "${analysis_args[@]}" "${template_analysis_args[@]}" "${plot_args[@]}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-followup "${capture}" "${report}" "${followup}" \
      --radius-s .5 --interval-s .1 --window-s .01 \
      --passes "${repo_dir}/artifacts/starlink_hybrid_watch/passes.json" \
      --confirmation-marker "${confirmation_marker}"
    # Wide reacquisitions can place the selected pilot bank away from baseband
    # zero; fixed-center narrow and oversampled captures are directly decodable.
    if [[ "${mode}" != "wide" && -f "${confirmation_marker}" ]] &&
       grep -Eq '"dual_receiver_confirmed": true' "${followup}"; then
      frame_track_args=()
      if [[ ("${mode}" == "narrow" || "${mode}" == "hop") &&
            -f "${learned_beacon}" ]]; then
        # The Starlink edge resource grid is channel-invariant. Pluto retuning
        # preserves the relative 2.5-MHz baseband response, so the qualified
        # full-duration template transfers across the four low-band channels.
        # A zero-support result falls back to dense pilot observations below.
        frame_track_args=(--beacon-template "${learned_beacon}")
      fi
      if env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
        starlink-beacon-frame-track "${capture}" "${followup}" "${frame_track}" \
        --samples "${frame_samples}" --maximum-extension-s "${frame_maximum_extension_s}" \
        "${frame_track_args[@]}"; then
        if grep -Eq '"dual_valid_frame_count": [1-9]' "${frame_track}"; then
          track_args=(--measurement-source conditioned_frames --frame-track "${frame_track}")
        else
          printf '{"conditioned_frame_track_empty":true,"capture":"%s"}\n' \
            "${name}" >&2
          track_args=(--measurement-source dense_followup)
        fi
      else
        printf '{"conditioned_frame_track_error":true,"capture":"%s"}\n' "${name}" >&2
        track_args=(--measurement-source dense_followup)
      fi
      if [[ "${mode}" != "hop" ]]; then
        env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
          starlink-beacon-decode "${capture}" "${followup}" "${decode}" \
          --plot "${decode_plot}" --symbols "${decode_symbols}"
        if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
          starlink-beacon-fingerprint "${storage_root}" --capture-name "${name}"; then
          printf '{"fingerprint_error":true,"capture":"%s"}\n' "${name}" >&2
        fi
      fi
      if env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
        starlink-beacon-track "${capture}" "${followup}" "${track}" \
        --maximum-gap-s "${track_maximum_gap_s}" \
        --maximum-reacquisition-span-hz "${track_maximum_reacquisition_span_hz}" \
        "${track_args[@]}"; then
        if [[ -f "${tle_catalog}" ]]; then
          if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-orbit \
            associate --observations "${track}" --catalog "${tle_catalog}" \
            --lat "${observer_lat}" --lon "${observer_lon}" --alt-m "${observer_alt_m}" \
            --output "${association}"; then
            printf '{"tle_association_error":true,"capture":"%s"}\n' "${name}" >&2
          fi
        fi
      else
        printf '{"continuous_track_error":true,"capture":"%s"}\n' "${name}" >&2
      fi
      if [[ -f "${track}" ]]; then
        refresh_capture_links "${name}" "${track}" || printf \
          '{"capture_link_error":true,"capture":"%s"}\n' "${name}" >&2
        if [[ "${mode}" == "narrow" ]]; then
          refresh_rolling_narrow_links "${name}" || printf \
            '{"rolling_link_error":true,"capture":"%s"}\n' "${name}" >&2
        fi
      fi
    fi
    if [[ "${mode}" == "hop" && -f "${track}" ]]; then
      refresh_hop_links "${name}" || printf \
        '{"channel_link_error":true,"capture":"%s"}\n' "${name}" >&2
    fi
    # Repository-wide maintenance is intentionally amortized by the next
    # ordinary capture.  A 16-child hop burst previously ran all four global
    # scans 16 times and allowed the durable analysis queue to grow without
    # bound.  Confirmed hop products still receive an immediate dashboard row.
    if [[ "${mode}" != "hop" ]]; then
      if [[ "${preserve_raw}" != "1" ]]; then
        env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
          starlink-beacon-retain "${storage_root}" --keep-negative "${keep_negative}" \
            --keep-confirmed "${keep_confirmed}" --keep-wide "${keep_wide}" \
            --keep-oversample "${keep_oversample}" \
            --keep-hop-sessions "${keep_hop_sessions}"
      fi
      env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
        starlink-beacon-calibrate "${storage_root}/reports" \
        "${storage_root}/reports/calibration/calibration.json"
      if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
        starlink-beacon-gain-summary "${storage_root}" \
        "${storage_root}/reports/gain-experiment/summary.json"; then
        printf '{"gain_summary_error":true}\n' >&2
      fi
    fi
    if [[ "${mode}" != "hop" || -f "${confirmation_marker}" ]]; then
      if ! flock "${maintenance_lock}" env UV_CACHE_DIR="${uv_cache}" \
        "${uv_bin}" run --active --no-sync leo-radio \
        starlink-beacon-dashboard-index "${storage_root}" \
        "${storage_root}/reports/dashboard-index.json" --capture-name "${name}"; then
        printf '{"dashboard_index_error":true,"capture":"%s"}\n' "${name}" >&2
      fi
    fi
}

analysis_pids=()

terminate_process_tree() {
  local parent="$1" child
  while read -r child; do
    [[ -n "${child}" ]] || continue
    terminate_process_tree "${child}"
  done < <(pgrep -P "${parent}" 2>/dev/null || true)
  kill "${parent}" 2>/dev/null || true
}

stop_analysis() {
  local pid
  for pid in "${analysis_pids[@]}"; do
    # The durable .job remains until a whole analysis succeeds. Terminate the
    # active uv/python descendants as well as the worker shell so a service
    # restart does not wait for the entire queued analysis to drain.
    terminate_process_tree "${pid}"
    wait "${pid}" 2>/dev/null || true
  done
}
handle_signal() {
  trap - EXIT INT TERM
  stop_analysis
  exit 130
}
trap stop_analysis EXIT
trap handle_signal INT TERM

enqueue_pending_analysis() {
  local marker temporary
  marker="${analysis_queue}/$(date -u +%Y%m%dT%H%M%S%NZ)-${pending_name}.job"
  temporary="${marker}.next"
  printf '%s\t%s\t%s\n' "${pending_name}" "${pending_capture}" \
    "${pending_mode}" > "${temporary}"
  mv "${temporary}" "${marker}"
}

recover_startup() {
  local running restored
  local -a queued_jobs running_jobs
  shopt -s nullglob
  running_jobs=("${analysis_queue}"/*.running.*)
  shopt -u nullglob
  for running in "${running_jobs[@]}"; do
    restored="${running%%.running.*}.job"
    if [[ -e "${restored}" ]]; then
      restored="${running%%.running.*}.recovered.$$.job"
    fi
    mv "${running}" "${restored}"
  done
  if [[ "${analysis_mode}" == "offload" ]]; then
    printf '{"analysis_mode":"offload","local_analysis_workers":0,"queue_owner":"starlink-analysis-export"}\n'
    return
  fi
  shopt -s nullglob
  queued_jobs=("${analysis_queue}"/*.job)
  shopt -u nullglob
  if (( ${#queued_jobs[@]} > 0 )); then
    printf '{"startup_recovery_deferred":true,"queued_jobs":%d}\n' \
      "${#queued_jobs[@]}"
    return
  fi
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
  if [[ "${preserve_raw}" != "1" ]]; then
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-retain "${storage_root}" --keep-negative "${keep_negative}" \
        --keep-confirmed "${keep_confirmed}" --keep-wide "${keep_wide}" \
        --keep-oversample "${keep_oversample}" \
        --keep-hop-sessions "${keep_hop_sessions}"
  fi
}

analysis_worker() {
  local accepted_mode="$1"
  local -a jobs
  local marker claim name capture mode failed selected index
  # Ordinary full captures are more expensive than their acquisition cadence,
  # so a pure FIFO queue can make current sky decisions several hours late.
  # Prefer four fresh jobs, then retire one oldest job.  This bounds decision
  # latency without abandoning durable historical recovery.
  local ordinary_selection_count=1
  if [[ "${accepted_mode}" == "ordinary" ]]; then
    # Keep capture startup independent from disk-only recovery, while ensuring
    # exactly one worker performs it and restores interrupted claims.
    recover_startup
    : > "${analysis_ready}"
  else
    while [[ ! -e "${analysis_ready}" ]]; do sleep .1; done
  fi
  while true; do
    shopt -s nullglob
    jobs=("${analysis_queue}"/*.job)
    shopt -u nullglob
    if (( ${#jobs[@]} == 0 )); then
      sleep 1
      continue
    fi
    selected=""
    if [[ "${accepted_mode}" == "ordinary" &&
          $((ordinary_selection_count % 5)) -ne 0 ]]; then
      # Bash pathname expansion is ascending; walk it backwards for freshness.
      index=$((${#jobs[@]} - 1))
    else
      index=0
    fi
    while (( index >= 0 && index < ${#jobs[@]} )); do
      marker="${jobs[index]}"
      IFS=$'\t' read -r name capture mode < "${marker}"
      if [[ ("${accepted_mode}" == "hop" && "${mode}" != "hop") ||
            ("${accepted_mode}" == "ordinary" && "${mode}" == "hop") ]]; then
        if [[ "${accepted_mode}" == "ordinary" &&
              $((ordinary_selection_count % 5)) -ne 0 ]]; then
          index=$((index - 1))
        else
          index=$((index + 1))
        fi
        continue
      fi
      claim="${marker%.job}.running.${BASHPID}"
      if mv "${marker}" "${claim}" 2>/dev/null; then
        selected="${claim}"
        break
      fi
      if [[ "${accepted_mode}" == "ordinary" &&
            $((ordinary_selection_count % 5)) -ne 0 ]]; then
        index=$((index - 1))
      else
        index=$((index + 1))
      fi
    done
    if [[ -z "${selected}" ]]; then
      sleep 1
      continue
    fi
    IFS=$'\t' read -r name capture mode < "${selected}"
    if [[ "${accepted_mode}" == "ordinary" ]]; then
      ordinary_selection_count=$((ordinary_selection_count + 1))
    fi
    # Full-frame and hop DSP can each consume more than one CPU core. Running
    # both simultaneously with live IIO capture crosses the Pi's thermal
    # ceiling and pauses RF acquisition. Claims stay disjoint, while this lock
    # gives capture the thermal budget by admitting one heavy DSP job at once.
    if (set -e
        exec 8>"${analysis_execution_lock}"
        flock 8
        process_capture "${name}" "${capture}" "${mode}"); then
      rm -f "${selected}"
    else
      failed="${selected%%.running.*}.failed"
      mv "${selected}" "${failed}"
      printf '{"analysis_job_error":true,"capture":"%s","job":"%s"}\n' \
        "${name}" "${failed}" >&2
    fi
  done
}

# In production offload mode the exporter is the sole queue consumer and
# Kalman performs every DSP stage. Local mode remains an explicit offline
# fallback. Never start local workers in offload mode: competing consumers can
# strand a capture on the Pi instead of delivering it to Kalman.
rm -f "${analysis_ready}"
if [[ "${analysis_mode}" == "local" ]]; then
  analysis_worker ordinary &
  analysis_pids+=("$!")
  analysis_worker hop &
  analysis_pids+=("$!")
else
  # No worker will call recovery in offload mode. Restore any claims left by
  # the previous local-mode process before recording more work for export.
  recover_startup
fi

cycle=0
while true; do
  for target in "${targets[@]}"; do
    capture_target "${target}" narrow
    enqueue_pending_analysis
  done
  cycle=$((cycle + 1))
  if (( hop_every_cycles > 0 && cycle % hop_every_cycles == 0 )); then
    hop_burst_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    for ((hop_session = 0; hop_session < hop_burst_sessions; hop_session++)); do
      capture_hop_survey "${hop_burst_stamp}-b$(printf '%02d' "${hop_session}")"
    done
  fi
  if (( (oversample_on_startup == 1 && cycle == 1) ||
        (oversample_every_cycles > 0 && cycle % oversample_every_cycles == 0) )); then
    for target in "${targets[@]}"; do
      capture_target "${target}" oversample
      enqueue_pending_analysis
    done
  fi
  if (( wide_every_cycles > 0 && cycle % wide_every_cycles == 0 )); then
    for target in "${targets[@]}"; do
      capture_target "${target}" wide
      enqueue_pending_analysis
    done
  fi
  if (( maximum_cycles > 0 && cycle >= maximum_cycles )); then
    if [[ "${analysis_mode}" == "local" ]]; then
      while [[ ! -e "${analysis_ready}" ]]; do sleep .1; done
      while compgen -G "${analysis_queue}/*.job" > /dev/null ||
            compgen -G "${analysis_queue}/*.running.*" > /dev/null; do
        sleep .1
      done
      stop_analysis
      analysis_pids=()
    fi
    break
  fi
done
