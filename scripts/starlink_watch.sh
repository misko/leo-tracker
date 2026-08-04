#!/usr/bin/env bash
set -u -o pipefail

# Unattended compact Starlink observer. One process owns the Pluto at a time.
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
watch_root="${1:-$repo_dir/artifacts/starlink_watch}"
mkdir -p "$watch_root/runs"
state="$watch_root/status.json"
log="$watch_root/watch.log"
lock="$watch_root/pluto.lock"
iteration="$(sed -n 's/.*"iteration":\([0-9][0-9]*\).*/\1/p' "$state" 2>/dev/null | head -1)"
iteration="${iteration:-0}"

write_state() {
  local stage="$1" detail="$2" run_dir="${3:-}"
  local temp="$state.tmp.$$"
  printf '{"updated_utc":"%s","pid":%d,"iteration":%d,"stage":"%s","detail":"%s","run_dir":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$iteration" "$stage" "$detail" "$run_dir" > "$temp"
  mv "$temp" "$state"
}

exec 9>"$lock"
if ! flock -n 9; then
  write_state "blocked" "another observer owns the Pluto lock"
  exit 2
fi

trap 'write_state "stopped" "received termination signal"; exit 0' TERM INT
write_state "starting" "watchdog initialized"

while :; do
  iteration=$((iteration + 1))
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  run_dir="$watch_root/runs/$stamp"
  write_state "observing" "discovery, scheduled regional scan, and stare" "$run_dir"
  printf '%s iteration=%d run=%s starting\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$iteration" "$run_dir" >> "$log"
  # Rotate six-center regional blocks through all 51 discovery centers. This
  # avoids repeatedly dwelling only on receiver structure ranked highest in
  # every discovery scan.
  region_count="${REGION_COUNT:-6}"
  region_offset=$(( ((iteration - 1) * region_count) % 48 ))
  if CATALOG="${CATALOG:-$repo_dir/data/starlink-current.json}" \
      REGION_COUNT="$region_count" REGION_OFFSET="$region_offset" \
      START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      END_UTC="$(date -u -d '+12 hours' +%Y-%m-%dT%H:%M:%SZ)" \
      "$repo_dir/scripts/starlink_pipeline.sh" "$run_dir" >> "$log" 2>&1; then
    candidates="$(find "$run_dir" -name monitor.json -exec sed -n 's/.*"candidate_count": \([0-9][0-9]*\).*/\1/p' {} + | awk '{s+=$1} END {print s+0}')"
    write_state "cycle_complete" "candidate_count=$candidates" "$run_dir"
    printf '%s iteration=%d candidates=%s complete\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$iteration" "$candidates" >> "$log"
  else
    code=$?
    write_state "error" "pipeline_exit=$code; retrying in 300 seconds" "$run_dir"
    printf '%s iteration=%d exit=%d failed\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$iteration" "$code" >> "$log"
    sleep 300
  fi
done
