#!/usr/bin/env bash
set -euo pipefail

# Pass-aware Starlink discovery -> regional revisit -> fixed-center stare.
# All Python execution is deliberately routed through the repository venv and uv.
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
uv_bin="/home/satpi01/.local/bin/uv"
run=("$uv_bin" run --active --no-sync)

output_root="${1:-$repo_dir/artifacts/starlink_pipeline_$(date -u +%Y%m%dT%H%M%SZ)}"
catalog="${CATALOG:-$output_root/starlink_catalog.json}"
start_utc="${START_UTC:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
end_utc="${END_UTC:-$(date -u -d '+12 hours' +%Y-%m-%dT%H:%M:%SZ)}"
uri="${PLUTO_URI:-pluto://ip:192.168.2.1}"
dry_run="${DRY_RUN:-0}"
latitude="${OBSERVER_LAT:-37.849165355010086}"
longitude="${OBSERVER_LON:--122.48567658142287}"
altitude_m="${OBSERVER_ALT_M:-0}"

invoke() {
  if [[ "$dry_run" == "1" ]]; then
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

mkdir -p "$output_root"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/leo-tracker-uv-cache}"
export LD_LIBRARY_PATH="$repo_dir/.venv/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

if [[ ! -f "$catalog" ]]; then
  invoke "${run[@]}" leo-orbit fetch --output "$catalog"
fi
invoke "${run[@]}" leo-orbit schedule --catalog "$catalog" \
  --lat "$latitude" --lon "$longitude" --alt-m "$altitude_m" \
  --start "$start_utc" --end "$end_utc" --horizon-deg 30 \
  --carrier-hz 11200000000 --step-seconds 20 --padding-seconds 300 \
  --output "$output_root/schedule.json"

# Compact 950--1950 MHz discovery: 51 centers, 4.576 MHz nominal overlap.
invoke "${run[@]}" leo-radio monitor "$output_root/discovery" --uri "$uri" \
  --start-hz 950000000 --stop-hz 1950000000 --step-hz 20000000 \
  --cycles 2 --sample-rate-hz 30720000 --bandwidth-hz 24576000 \
  --samples-per-tuning 65536 --settle-seconds 3 --fft-size 8192 --psd-bins 4096
invoke "${run[@]}" leo-radio rank-regions "$output_root/discovery/monitor.json" \
  "$output_root/regions.json" --count "${REGION_COUNT:-6}" \
  --offset "${REGION_OFFSET:-0}"

# Discovery is asynchronous; regional monitoring begins inside the next
# TLE-derived recording window. Set SKIP_PASS_WAIT=1 for bench diagnostics.
wait_args=()
if [[ "${SKIP_PASS_WAIT:-0}" == "1" ]]; then wait_args+=(--no-wait); fi
invoke "${run[@]}" python "$repo_dir/scripts/wait_for_pass.py" \
  "$output_root/schedule.json" --min-remaining-seconds "${MIN_PASS_REMAINING_S:-300}" \
  "${wait_args[@]}"

# Revisit only structured tiles; six centers give roughly 18--24 s cadence.
invoke "${run[@]}" leo-radio monitor "$output_root/regional" --uri "$uri" \
  --centers-file "$output_root/regions.json" --cycles "${REGIONAL_CYCLES:-8}" \
  --sample-rate-hz 30720000 --bandwidth-hz 24576000 \
  --samples-per-tuning 65536 --settle-seconds 3 --fft-size 8192 --psd-bins 4096 \
  --min-shift-hz 30000 --max-shift-hz 600000 \
  --max-cycle-lag "${REGIONAL_MAX_CYCLE_LAG:-7}"
invoke "${run[@]}" leo-radio rank-regions "$output_root/regional/monitor.json" \
  "$output_root/stare_center.json" --count 1

# A compact fixed-center sequence. No raw IQ is retained by this pipeline.
invoke "${run[@]}" leo-radio monitor "$output_root/stare" --uri "$uri" \
  --centers-file "$output_root/stare_center.json" --cycles "${STARE_CYCLES:-60}" \
  --sample-rate-hz 30720000 --bandwidth-hz 24576000 \
  --samples-per-tuning 65536 --settle-seconds "${STARE_INTERVAL_S:-1}" \
  --fft-size 8192 --psd-bins 4096 --min-shift-hz 5000 --max-shift-hz 600000 \
  --max-cycle-lag "${STARE_MAX_CYCLE_LAG:-20}"

printf 'Pipeline artifacts: %s\n' "$output_root"
