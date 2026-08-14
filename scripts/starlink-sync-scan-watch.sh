#!/usr/bin/env bash
# Synchronised paired scans: both Plutos on the sky at the same instant.
#
# Scans only.  This loop never dwells.  Every cycle draws an arm, a pairing and
# one edge order per radio, sweeps both radios through the eight low-band edge
# tunings in lockstep, writes both recordings to NVMe and hands them to the
# offload queue that leo-tracker-analysis-export.service already drains.
#
# This script draws the raw 32-bit numbers and logs them; every mapping from a
# draw to an assignment is computed in Python, where it is tested.  Two copies
# of an assignment rule is how they drift apart.
#
# RUN INSTRUCTIONS.  The branch must be merged into the deployed checkout at
# /home/satpi01/leo-tracker before this loop is started.  The exporter imports
# leo_tracker from that checkout and not from a worktree, so an unmerged tree
# leaves it meeting recordings it cannot classify on every reconciliation pass:
# today it counts them as unsupported and carries on, and they accumulate
# unexported; an older exporter exited non-zero instead, and `set -e` plus
# `Restart=always` made that a crash loop that stopped ALL offload to shared
# storage for every capture type.  The gate below refuses to sweep rather than
# fill a volume nothing is draining.
set -euo pipefail

repo_dir="${LEO_TRACKER_REPO:-/home/satpi01/leo-tracker}"
storage_root="${LEO_SYNC_STORAGE:-/mnt/leo-nvme/leo-tracker}"
uv_cache="${UV_CACHE_DIR:-${repo_dir}/.uv-cache}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
lnb_lo_hz="${LEO_SYNC_LNB_LO_HZ:-9750000000}"
barrier_timeout_s="${LEO_SYNC_BARRIER_TIMEOUT_S:-30}"
maximum_sweeps="${LEO_SYNC_MAX_SWEEPS:-0}"
# 30 sweeps an hour is the cadence the storage budget below was computed at.
sweep_interval_s="${LEO_SYNC_SWEEP_INTERVAL_S:-120}"
# A mean sweep writes 88 MB per radio across the twelve arms -- 6.4 MB at
# 80 ms/1.25 MS/s and 409.6 MB at 640 ms/10 MS/s -- so both radios together
# average 176 MB, about 127 GB a day at 30 sweeps an hour.  Back off rather
# than fill the volume the exporter is draining.
minimum_free_gb="${LEO_SYNC_MINIMUM_FREE_GB:-200}"
maximum_pi_temp_millic="${LEO_SYNC_MAX_PI_TEMP_MILLIC:-75000}"
resume_pi_temp_millic="${LEO_SYNC_RESUME_PI_TEMP_MILLIC:-70000}"
host_thermal_zone="${LEO_SYNC_THERMAL_ZONE:-/sys/class/thermal/thermal_zone0/temp}"

# RADIO_ID URI SERIAL RX0_LABEL RX1_LABEL, one per radio, exactly two.
radio_a_spec="${LEO_SYNC_RADIO_A:-pluto-19f2 pluto://usb: 10400056f695001322002d0010ad1719f2 lnb-c lnb-d}"
radio_b_spec="${LEO_SYNC_RADIO_B:-pluto-5d4d pluto://usb: 1040005e0b100007100010000bf33a5d4d lnb-a lnb-b}"
read -r -a radio_a <<< "${radio_a_spec}"
read -r -a radio_b <<< "${radio_b_spec}"
if (( ${#radio_a[@]} != 5 || ${#radio_b[@]} != 5 )); then
  echo "LEO_SYNC_RADIO_A and LEO_SYNC_RADIO_B each need five fields: " \
       "RADIO_ID URI SERIAL RX0_LABEL RX1_LABEL" >&2
  exit 2
fi
if [[ "${radio_a[0]}" == "${radio_b[0]}" || "${radio_a[2]}" == "${radio_b[2]}" ]]; then
  echo "the two radios must have distinct ids and serials" >&2
  exit 2
fi
if ! [[ "${minimum_free_gb}" =~ ^[1-9][0-9]*$ ]]; then
  echo "LEO_SYNC_MINIMUM_FREE_GB must be a positive integer" >&2
  exit 2
fi

mkdir -p "${storage_root}/captures" "${storage_root}/staging/analysis-queue"
cd "${repo_dir}"

# --- exporter compatibility gate ---
# Asked of the checkout the exporter actually imports, which is ${repo_dir} and
# never a worktree, and asked before a single byte is written. A sweep this
# host cannot file is a sweep that fills NVMe and never reaches the share.
if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync python -c \
  'import sys
from leo_tracker.radio.beacon.offload import OBSERVATION_MODES
from leo_tracker.radio.beacon.synchronised_scan import SCAN_OBSERVATION_MODE
sys.exit(0 if SCAN_OBSERVATION_MODE in OBSERVATION_MODES else 1)' 2>/dev/null; then
  printf '{"sync_scan_exporter_unaware":true,"repo_dir":"%s","detail":"the deployed checkout cannot classify a sync-scan recording; the branch must be merged into %s before this unit is started, or every sweep this loop writes will sit on NVMe unexported"}\n' \
    "${repo_dir}" "${repo_dir}" >&2
  exit 2
fi

# A sensor that exists but reads unusably is a hard error rather than a
# silently skipped safety check; a host with no zone at all runs without one.
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

draw_u32() {
  od -An -N4 -tu4 /dev/urandom | tr -d '[:space:]'
}

printf '{"sync_scan_config":true,"storage":"%s","radios":["%s","%s"],"dwell":false}\n' \
  "${storage_root}" "${radio_a[0]}" "${radio_b[0]}"

sweep=0
while true; do
  while (( $(df -Pk "${storage_root}" | awk 'NR==2 {print $4}') <
           minimum_free_gb * 1024 * 1024 )); do
    printf '{"storage_backoff":true,"minimum_free_gb":%d}\n' \
      "${minimum_free_gb}" >&2
    sleep 60
  done
  if read_host_temperature &&
     (( host_temperature_millic_value >= maximum_pi_temp_millic )); then
    while (( host_temperature_millic_value >= resume_pi_temp_millic )); do
      printf '{"thermal_backoff":true,"pi_temperature_c":%.3f}\n' \
        "$(awk -v v="${host_temperature_millic_value}" 'BEGIN {print v/1000}')"
      sleep 15
      read_host_temperature || break
    done
  fi

  # One second of resolution, and a recording name is an identity, so wait past
  # the boundary rather than colliding on the exclusive mkdir.
  while :; do
    sweep_id="$(date -u +%Y%m%dT%H%M%SZ)"
    if [[ ! -e "${storage_root}/captures/sync-${sweep_id}-${radio_a[0]}" &&
          ! -e "${storage_root}/captures/sync-${sweep_id}-${radio_b[0]}" ]]; then
      break
    fi
    sleep 1
  done

  arm_draw="$(draw_u32)"
  pairing_draw="$(draw_u32)"
  second_arm_draw="$(draw_u32)"
  # Independently per radio, always, on every sweep: never shared between the
  # radios and never coupled to the arm draw or to the pairing decision.
  edge_draw_a="$(draw_u32)"
  edge_draw_b="$(draw_u32)"
  printf '{"sync_experiment":"synchronised-paired-scan-v1","sweep_id":"%s","arm_draw_u32":%s,"pairing_draw_u32":%s,"second_arm_draw_u32":%s,"edge_order_draw_u32":[%s,%s],"arms":12,"arm_assignment_probability":0.0833333333,"matched_pairing_probability":0.9,"edge_order_probability":0.5}\n' \
    "${sweep_id}" "${arm_draw}" "${pairing_draw}" "${second_arm_draw}" \
    "${edge_draw_a}" "${edge_draw_b}"

  # A radio that cannot be opened, or that dies mid-sweep, leaves the other
  # sweeping solo and is recorded as such, so a failure here is a bug rather
  # than an unplugged radio and must not silently end the loop.
  if ! env UV_CACHE_DIR="${uv_cache}" "${uv_bin}" run --active --no-sync leo-radio \
    starlink-synchronised-scan --storage "${storage_root}" \
    --sweep-id "${sweep_id}" \
    --radio "${radio_a[0]}" "${radio_a[1]}" "${radio_a[2]}" "${radio_a[3]}" "${radio_a[4]}" \
    --radio "${radio_b[0]}" "${radio_b[1]}" "${radio_b[2]}" "${radio_b[3]}" "${radio_b[4]}" \
    --arm-draw-u32 "${arm_draw}" --pairing-draw-u32 "${pairing_draw}" \
    --second-arm-draw-u32 "${second_arm_draw}" \
    --edge-order-draw-u32 "${edge_draw_a}" \
    --edge-order-draw-u32 "${edge_draw_b}" \
    --lnb-lo-hz "${lnb_lo_hz}" --barrier-timeout-s "${barrier_timeout_s}"; then
    printf '{"sync_scan_error":true,"sweep_id":"%s"}\n' "${sweep_id}" >&2
    sleep 10
  fi

  sweep=$((sweep + 1))
  if (( maximum_sweeps > 0 && sweep >= maximum_sweeps )); then
    break
  fi
  (( sweep_interval_s > 0 )) && sleep "${sweep_interval_s}"
done
