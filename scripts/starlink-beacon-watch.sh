#!/usr/bin/env bash
set -euo pipefail

repo_dir="${LEO_TRACKER_REPO:-/home/satpi01/leo-tracker}"
storage_root="${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}"
duration_s="${LEO_BEACON_DWELL_S:-120}"
keep_negative="${LEO_BEACON_KEEP_NEGATIVE:-12}"
uv_cache="${UV_CACHE_DIR:-${repo_dir}/.uv-cache}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"

mkdir -p "${storage_root}/captures" "${storage_root}/reports" "${storage_root}/staging"
mkdir -p "${storage_root}/reports/plots"
cd "${repo_dir}"

while true; do
  for target in 3:lower-edge 3:upper-edge 4:lower-edge 4:upper-edge; do
    channel="${target%%:*}"
    region="${target##*:}"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    name="ch${channel}-${region}-${stamp}"
    capture="${storage_root}/captures/${name}"
    report="${storage_root}/reports/${name}.json"
    plot="${storage_root}/reports/plots/${name}.png"
    pi_temp="$(awk '{printf "%.3f", $1/1000}' /sys/class/thermal/thermal_zone0/temp)"
    radio_temp_millic="$(iio_attr -u ip:192.168.2.1 -c ad9361-phy temp0 2>/dev/null | sed -n "s/.*value '\([0-9-]*\)'.*/\1/p" || true)"
    temperature_args=(--host-temperature-c "${pi_temp}")
    if [[ -n "${radio_temp_millic}" ]]; then
      radio_temp="$(awk -v value="${radio_temp_millic}" 'BEGIN {printf "%.3f", value/1000}')"
      temperature_args+=(--radio-temperature-c "${radio_temp}")
    fi
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-capture "${capture}" --duration-s "${duration_s}" \
      --channel-number "${channel}" --region "${region}" \
      --sample-rate-hz 2500000 --bandwidth-hz 2300000 \
      --gain-mode manual --gain-db 50 --block-size 262144 --chunk-s 5 --queue-blocks 16 \
      "${temperature_args[@]}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-analyze "${capture}" "${report}" \
      --window-s 1 --maximum-analysis-rate-hz 50000 \
      --exact-interval-s 20 --exact-window-s .02 --plot "${plot}"
    env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
      starlink-beacon-retain "${storage_root}" --keep-negative "${keep_negative}"
  done
done
