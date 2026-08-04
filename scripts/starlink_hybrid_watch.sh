#!/usr/bin/env bash
set -u -o pipefail

# Repeating full-channel survey -> ranked 4 MS/s dwell, with 2.5 MS/s fallback.
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="${1:-$repo_dir/artifacts/starlink_hybrid_watch}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
run=("$uv_bin" run --active --no-sync)
uri="${PLUTO_URI:-ip:192.168.2.1}"
dry_run="${DRY_RUN:-0}"
max_cycles="${MAX_CYCLES:-0}"
dwell_seconds="${DWELL_SECONDS:-600}"
survey_cycles="${SURVEY_CYCLES:-2}"
survey_settle_s="${SURVEY_SETTLE_S:-1}"
gain_db="${GAIN_DB:-50}"
gain_mode="${GAIN_MODE:-manual}"
minimum_duty="${MINIMUM_DUTY:-0.80}"
dither_hz="${INTERLEAVED_DITHER_HZ:-500000}"
dither_segment_s="${DITHER_SEGMENT_S:-30}"
validation_every_cycles="${VALIDATION_EVERY_CYCLES:-4}"
validation_seconds="${VALIDATION_SECONDS:-180}"
# Retune characterization showed coherent artifacts after two discarded
# buffers. At the measured ~0.127 s/read, 24 buffers provide about 3 s.
dither_discard_buffers="${DITHER_DISCARD_BUFFERS:-24}"
assume_all_shifts_doppler="${ASSUME_ALL_SHIFTS_DOPPLER:-1}"
tracker_ensemble="${TRACKER_ENSEMBLE:-1}"
tracker_max_windows="${TRACKER_MAX_WINDOWS:-4}"
pass_catalog="${PASS_CATALOG:-$root/passes.json}"
lnb_lo_hz=9750000000
plan="$root/hybrid-plan.json"
lock="$root/pluto.lock"
mkdir -p "$root/cycles" "$root/chunks" "$root/analysis" "$root/plots" \
  "$root/observations" "$root/iq" "$root/tracker-ensemble" "$root/coherent" \
  /dev/shm/leo-tracker-hybrid-iq

invoke() {
  if [[ "$dry_run" == "1" ]]; then printf '%q ' "$@"; printf '\n'
  else "$@"
  fi
}

case "$gain_mode" in
  manual) gain_args=(--gain-mode manual --gain-db "$gain_db") ;;
  slow_attack|fast_attack) gain_args=(--gain-mode "$gain_mode") ;;
  *) printf 'unsupported GAIN_MODE: %s\n' "$gain_mode" >&2; exit 2 ;;
esac

if [[ "$dry_run" != "1" ]]; then
  exec 9>"$lock"
  if ! flock -n 9; then
    printf 'another observer owns %s\n' "$lock" >&2
    exit 2
  fi
  "${run[@]}" leo-radio starlink-hybrid-plan "$plan" --dwell-seconds "$dwell_seconds"
else
  invoke "${run[@]}" leo-radio starlink-hybrid-plan "$plan" --dwell-seconds "$dwell_seconds"
fi

# Resume monotonically after a supervisor restart. Timestamps already prevent
# overwrites; preserving the sequence also keeps dashboard and review IDs clear.
cycle=0
if [[ "$dry_run" != "1" ]]; then
  shopt -s nullglob
  for prior in "$root"/cycles/cycle-*; do
    name="${prior##*/cycle-}"; number="${name%%-*}"
    if [[ "$number" =~ ^[0-9]+$ ]] && (( 10#$number >= cycle )); then
      cycle=$((10#$number + 1))
    fi
  done
  shopt -u nullglob
fi
completed_this_run=0
while (( max_cycles == 0 || completed_this_run < max_cycles )); do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  cycle_dir="$root/cycles/cycle-$(printf '%05d' "$cycle")-$stamp"
  survey_dir="$cycle_dir/survey"
  regions="$cycle_dir/ranked-regions.json"
  mkdir -p "$cycle_dir"
  invoke "${run[@]}" leo-radio monitor "$survey_dir" --uri "$uri" \
    --centers-file "$plan" --cycles "$survey_cycles" \
    --sample-rate-hz 30720000 --bandwidth-hz 20000000 \
    --samples-per-tuning 65536 --settle-seconds "$survey_settle_s" \
    --discard-buffers 1 --fft-size 8192 --psd-bins 2048 --channels 0,1
  invoke "${run[@]}" leo-radio rank-regions "$survey_dir/monitor.json" \
    "$regions" --count 6

  if [[ "$dry_run" == "1" ]]; then selected_if=1575117187.5
  else
    selected_if="$("${run[@]}" python -c \
      'import json,sys; print(json.load(open(sys.argv[1]))["centers_hz"][0])' "$regions")"
  fi
  stem="dwell-$(printf '%05d' "$cycle")-$stamp"
  measurement="$root/chunks/$stem.npz"
  analysis="$root/analysis/$stem.json"
  observations="$root/observations/$stem.json"
  plot="$root/plots/$stem.png"
  iq_stage="/dev/shm/leo-tracker-hybrid-iq/$stem.npz"
  dither_args=()
  capture_seconds="$dwell_seconds"
  observation_mode=fixed
  if (( validation_every_cycles > 0 && cycle % validation_every_cycles == 0 )); then
    observation_mode=retune-validation
    capture_seconds="$validation_seconds"
    dither_args=(--interleaved-dither-hz "$dither_hz" \
      --dither-segment-s "$dither_segment_s" \
      --dither-discard-buffers "$dither_discard_buffers")
  fi
  if [[ -f "$root/use-fallback-rate" ]]; then
    dwell_rate=2500000; dwell_bw=2300000; experiment_tag=hybrid-2.5msps-continuous
  else
    dwell_rate=4000000; dwell_bw=3600000; experiment_tag=hybrid-4msps-dwell
  fi
  dwell_snapshots="$(( (dwell_rate * ${capture_seconds%.*} + 262143) / 262144 ))"
  capture=(leo-radio starlink-measurement-capture "$measurement" \
    --center-frequency-hz "$selected_if" --lnb-lo-hz "$lnb_lo_hz" \
    --sample-rate-hz "$dwell_rate" --bandwidth-hz "$dwell_bw" \
    "${gain_args[@]}" --snapshots "$dwell_snapshots" \
    --block-size 262144 --fft-size 8192 --output-bins 4096 --discard-buffers 8 \
    --psd-quantization-db .01 --uri "$uri" --experiment-tag "$experiment_tag" \
    --observation-mode "$observation_mode" \
    --iq-evidence-output "$iq_stage" --iq-evidence-blocks 12 \
    --iq-trigger-warmup-blocks 16 --iq-trigger-threshold-db .8 \
    --iq-trigger-separation-blocks 8 --iq-trigger-stratum-blocks 50 \
    "${dither_args[@]}")
  capture_ok=0
  if [[ "$dry_run" == "1" ]]; then
    invoke "${run[@]}" "${capture[@]}"; capture_ok=1
  else
    for capture_attempt in 1 2 3; do
      if "${run[@]}" "${capture[@]}"; then capture_ok=1; break; fi
      printf 'capture open failed (attempt %d/3); retrying in 5 s\n' "$capture_attempt" >&2
      sleep 5
    done
  fi
  if [[ "$capture_ok" == "0" ]]; then
    dwell_rate=2500000; dwell_bw=2300000
    dwell_snapshots="$(( (2500000 * ${capture_seconds%.*} + 262143) / 262144 ))"
    for capture_attempt in 1 2 3; do
      if "${run[@]}" leo-radio starlink-measurement-capture "$measurement" \
        --center-frequency-hz "$selected_if" --lnb-lo-hz "$lnb_lo_hz" \
        --sample-rate-hz "$dwell_rate" --bandwidth-hz "$dwell_bw" \
        "${gain_args[@]}" --snapshots "$dwell_snapshots" \
        --block-size 262144 --fft-size 8192 --output-bins 4096 --discard-buffers 8 \
        --psd-quantization-db .01 --uri "$uri" --experiment-tag hybrid-2.5msps-fallback \
        --observation-mode "$observation_mode" \
        --iq-evidence-output "$iq_stage" --iq-evidence-blocks 12 \
        --iq-trigger-warmup-blocks 16 --iq-trigger-threshold-db .8 \
        --iq-trigger-separation-blocks 8 --iq-trigger-stratum-blocks 50 \
        "${dither_args[@]}"; then capture_ok=1; break; fi
      printf 'fallback capture open failed (attempt %d/3); retrying in 5 s\n' "$capture_attempt" >&2
      sleep 5
    done
    [[ "$capture_ok" == "1" ]] || exit 1
  fi
  carrier_hz="$(awk -v a="$selected_if" -v b="$lnb_lo_hz" 'BEGIN{printf "%.1f",a+b}')"
  observation_policy=()
  if [[ "$assume_all_shifts_doppler" == "1" ]]; then
    observation_policy+=(--assume-all-shifts-doppler)
  fi
  invoke "${run[@]}" leo-radio doppler-observations "$measurement" "$observations" \
    --event-frequency-bins 1024 --stable-guard-s .75 \
    --minimum-track-duration-s 3 "${observation_policy[@]}"
  invoke "${run[@]}" leo-radio starlink-measurement-analyze "$measurement" "$analysis" \
    --plot "$plot" --carrier-hz "$carrier_hz"
  if [[ "$dry_run" != "1" && "$tracker_ensemble" == "1" ]]; then
    mapfile -t tracker_windows < <("${run[@]}" python - "$analysis" "$tracker_max_windows" <<'PY'
import json, sys
report = json.load(open(sys.argv[1])); maximum = int(sys.argv[2]); rows = []
for receiver in report.get("events", []):
    for event in receiver:
        duration = float(event.get("duration_s", 0))
        if event.get("broadband") or duration >= 2:
            rows.append((bool(event.get("broadband")), duration,
                         float(event["start_time_s"]), float(event["stop_time_s"])))
for item in report.get("joint_events", []):
    observation = item.get("doppler_observation") or {}
    if observation.get("qualified"):
        association = item.get("association") or {}
        first = report["events"][0][int(association["rx0_index"])]
        rows.append((True, float(first.get("duration_s", 0)),
                     float(first["start_time_s"]), float(first["stop_time_s"])))
selected = []
for _, _, start, stop in sorted(rows, reverse=True):
    start, stop = max(0, start-1), stop+1
    if any(min(stop, b)-max(start, a) > 0 for a, b in selected):
        continue
    selected.append((start, stop))
    if len(selected) >= maximum:
        break
for start, stop in sorted(selected): print(f"{start:.6f}:{stop:.6f}")
PY
    )
    if (( ${#tracker_windows[@]} > 0 )); then
      tracker_args=(doppler-trackers "$measurement" "$root/tracker-ensemble/$stem.json"
        --plot "$root/plots/$stem-trackers.png"
        --integration-s .5 --dedoppler-window-s 10 --dedoppler-step-s 5
        --minimum-drift-hz-s -15000 --maximum-drift-hz-s 15000
        --drift-step-hz-s 1000)
      for tracker_window in "${tracker_windows[@]}"; do
        tracker_args+=(--window "$tracker_window")
      done
      if [[ -f "$pass_catalog" ]]; then tracker_args+=(--passes "$pass_catalog"); fi
      invoke "${run[@]}" leo-radio "${tracker_args[@]}"
    fi
  fi
  if [[ "$dry_run" != "1" ]]; then
    "${run[@]}" python - "$analysis" "$root/status.json" "$minimum_duty" "$cycle" \
      "$selected_if" "$dwell_rate" <<'PY'
import json, pathlib, sys
from leo_tracker.radio.hybrid import requires_fallback
analysis, status, threshold, cycle, center, rate = sys.argv[1:]
report = json.load(open(analysis))
duty = float(report.get("measurement", {}).get("duty_fraction", 0.0))
payload = {"schema": "leo-tracker.hybrid-status/v1", "stage": "running",
           "completed_cycle": int(cycle), "selected_if_hz": float(center),
           "sample_rate_hz": float(rate), "duty_fraction": duty,
           "minimum_duty_fraction": float(threshold),
           "latest_analysis": analysis}
pathlib.Path(status).write_text(json.dumps(payload, indent=2, sort_keys=True)+"\n")
if requires_fallback(float(rate), duty, float(threshold)):
    pathlib.Path(status).with_name("use-fallback-rate").write_text(
        f"4 MS/s duty {duty:.6f} below {float(threshold):.6f}\n")
PY
  fi
  if [[ "$dry_run" != "1" && -f "$iq_stage" ]]; then
    tracker_report="$root/tracker-ensemble/$stem.json"
    if "${run[@]}" python -c 'import json, pathlib, sys
from leo_tracker.radio.hybrid import should_retain_iq
legacy=json.load(open(sys.argv[1])); path=pathlib.Path(sys.argv[2])
ensemble=json.load(open(path)) if path.exists() else None
raise SystemExit(0 if should_retain_iq(legacy, ensemble) else 1)' \
      "$analysis" "$tracker_report"; then
      mv "$iq_stage" "$root/iq/$stem.npz"
      invoke "${run[@]}" leo-radio doppler-iq-track "$root/iq/$stem.npz" \
        "$root/coherent/$stem.json"
    else
      rm -f "$iq_stage"
    fi
  fi
  if [[ "$dry_run" != "1" && "$tracker_ensemble" == "1" ]]; then
    invoke "${run[@]}" leo-radio doppler-tracker-summary \
      "$root/tracker-performance.json" "$root/tracker-ensemble" "$root/coherent"
  fi
  cycle=$((cycle+1))
  completed_this_run=$((completed_this_run+1))
done
