#!/usr/bin/env bash
set -euo pipefail

repo_dir="${LEO_TRACKER_REPO:-/home/satpi01/leo-tracker}"
storage_root="${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}"
duration_s="${LEO_BEACON_DWELL_S:-120}"
wide_duration_s="${LEO_BEACON_WIDE_DWELL_S:-10}"
wide_every_cycles="${LEO_BEACON_WIDE_EVERY_CYCLES:-2}"
keep_negative="${LEO_BEACON_KEEP_NEGATIVE:-12}"
uv_cache="${UV_CACHE_DIR:-${repo_dir}/.uv-cache}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
maximum_pi_temp_millic="${LEO_BEACON_MAX_PI_TEMP_MILLIC:-75000}"
resume_pi_temp_millic="${LEO_BEACON_RESUME_PI_TEMP_MILLIC:-70000}"

mkdir -p "${storage_root}/captures" "${storage_root}/reports" "${storage_root}/staging"
mkdir -p "${storage_root}/reports/plots"
cd "${repo_dir}"

capture_target() {
    target="$1"
    mode="$2"
    channel="${target%%:*}"
    region="${target##*:}"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    name="ch${channel}-${region}-${mode}-${stamp}"
    capture="${storage_root}/captures/${name}"
    report="${storage_root}/reports/${name}.json"
    plot="${storage_root}/reports/plots/${name}.png"
    followup="${storage_root}/reports/followups/${name}.json"
    confirmation_marker="${storage_root}/staging/${name}.confirmed"
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
    radio_temp_millic="$(iio_attr -u ip:192.168.2.1 -c ad9361-phy temp0 2>/dev/null | sed -n "s/.*value '\([0-9-]*\)'.*/\1/p" || true)"
    temperature_args=(--host-temperature-c "${pi_temp}")
    if [[ -n "${radio_temp_millic}" ]]; then
      radio_temp="$(awk -v value="${radio_temp_millic}" 'BEGIN {printf "%.3f", value/1000}')"
      temperature_args+=(--radio-temperature-c "${radio_temp}")
    fi
    if [[ "${mode}" == "wide" ]]; then
      capture_args=(--duration-s "${wide_duration_s}" --sample-rate-hz 10000000
        --bandwidth-hz 9000000 --block-size 1048576)
      analysis_args=(--exact-interval-s 5 --exact-window-s .01
        --acquisition-span-hz 3500000 --acquisition-step-hz 500000
        --exact-subband-rate-hz 2500000)
    else
      capture_args=(--duration-s "${duration_s}" --sample-rate-hz 2500000
        --bandwidth-hz 2300000 --block-size 262144)
      analysis_args=(--exact-interval-s 2 --exact-window-s .01)
    fi
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-capture "${capture}" "${capture_args[@]}" \
      --channel-number "${channel}" --region "${region}" \
      --gain-mode manual --gain-db 50 --chunk-s 5 --queue-blocks 16 \
      "${temperature_args[@]}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-analyze "${capture}" "${report}" \
      --window-s 1 --maximum-analysis-rate-hz 50000 "${analysis_args[@]}" --plot "${plot}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-followup "${capture}" "${report}" "${followup}" \
      --radius-s .5 --interval-s .1 --window-s .01 \
      --passes "${repo_dir}/artifacts/starlink_hybrid_watch/passes.json" \
      --confirmation-marker "${confirmation_marker}"
    last_confirmation_marker="${confirmation_marker}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-retain "${storage_root}" --keep-negative "${keep_negative}"
}

env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
  starlink-beacon-recover "${storage_root}" \
  --passes "${repo_dir}/artifacts/starlink_hybrid_watch/passes.json"
env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
  starlink-beacon-retain "${storage_root}" --keep-negative "${keep_negative}"

cycle=0
while true; do
  for target in 3:lower-edge 3:upper-edge 4:lower-edge 4:upper-edge; do
    capture_target "${target}" narrow
    if [[ -f "${last_confirmation_marker}" ]]; then
      capture_target "${target}" confirm
    fi
  done
  env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
    starlink-beacon-calibrate "${storage_root}/reports" \
    "${storage_root}/reports/calibration/calibration.json"
  cycle=$((cycle + 1))
  if (( wide_every_cycles > 0 && cycle % wide_every_cycles == 0 )); then
    for target in 3:lower-edge 3:upper-edge 4:lower-edge 4:upper-edge; do
      capture_target "${target}" wide
    done
  fi
done
