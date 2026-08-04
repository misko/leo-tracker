#!/usr/bin/env bash
set -u -o pipefail

# Continuous v2 Starlink measurement watcher. Each chunk is a fixed-frequency
# observation long enough to expose Doppler, followed by finite-event analysis.
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
watch_root="${1:-$repo_dir/artifacts/starlink_measurement_watch}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
run=("$uv_bin" run --active --no-sync)
uri="${PLUTO_URI:-ip:192.168.2.1}"
snapshots="${SNAPSHOTS:-900}"
block_size="${BLOCK_SIZE:-262144}"
fft_size="${FFT_SIZE:-8192}"
output_bins="${OUTPUT_BINS:-8192}"
psd_quantization_db="${PSD_QUANTIZATION_DB:-0.01}"
baseline_min_captures="${BASELINE_MIN_CAPTURES:-4}"
wide_integration_s="${WIDE_INTEGRATION_S:-1.0}"
gain_db="${GAIN_DB:-50}"
max_chunks="${MAX_CHUNKS:-0}"
max_failures="${MAX_FAILURES:-0}"
max_pi_temp_c="${MAX_PI_TEMP_C:-80}"
fake="${FAKE:-0}"
backfill_pids=()
gps_device="${GPS_DEVICE:-/dev/ttyACM0}"
use_gps="${USE_GPS:-1}"
iq_staging_dir="${IQ_STAGING_DIR:-/dev/shm/leo-tracker-iq}"
[[ "$fake" == "1" ]] && iq_staging_dir="$watch_root/iq-staging"

# Nominal/+dither pairs distinguish sky-fixed signals from baseband-fixed
# receiver artifacts. A real RF feature moves by -dither in baseband.
dither_hz="${TUNING_DITHER_HZ:-1250000}"
ch3_dither="$(awk -v c=1580117187.5 -v d="$dither_hz" 'BEGIN{printf "%.1f",c+d}')"
ch4_dither="$(awk -v c=1830117187.5 -v d="$dither_hz" 'BEGIN{printf "%.1f",c+d}')"
# Alternate pair order every cycle. This turns capture order into a controlled
# variable instead of permanently confounding nominal tuning with post-hop
# settling from the preceding channel.
centers=(1580117187.5 "$ch3_dither" 1830117187.5 "$ch4_dither" \
         "$ch3_dither" 1580117187.5 "$ch4_dither" 1830117187.5)
channels=(3 3 4 4 3 3 4 4)
dithers=(0 "$dither_hz" 0 "$dither_hz" "$dither_hz" 0 "$dither_hz" 0)
pair_complete=(0 1 0 1 0 1 0 1)
lnb_lo_hz=9750000000
passes_ch3="${PASSES_CH3:-$watch_root/passes/channel-3.json}"
passes_ch4="${PASSES_CH4:-$watch_root/passes/channel-4.json}"
auto_passes_ch3=1; auto_passes_ch4=1
[[ -v PASSES_CH3 ]] && auto_passes_ch3=0
[[ -v PASSES_CH4 ]] && auto_passes_ch4=0
tle_catalog="${TLE_CATALOG:-$repo_dir/data/starlink-current.json}"
pass_refresh="${PASS_REFRESH:-1}"
observer_lat="${OBSERVER_LAT:-37.849165355010086}"
observer_lon="${OBSERVER_LON:--122.48567658142287}"
observer_alt_m="${OBSERVER_ALT_M:-0}"
pass_horizon_deg="${PASS_HORIZON_DEG:-20}"
pass_window_hours="${PASS_WINDOW_HOURS:-25}"
pass_refresh_margin_s="${PASS_REFRESH_MARGIN_S:-3600}"

mkdir -p "$watch_root/chunks" "$watch_root/plots" "$watch_root/analysis" \
  "$watch_root/dither" "$watch_root/wide" "$watch_root/iq" "$watch_root/iq-staging" \
  "$watch_root/waveform" "$iq_staging_dir"
state="$watch_root/status.json"
log="$watch_root/watch.log"
lock="$watch_root/pluto.lock"

write_state() {
  local stage="$1" detail="$2" chunk="${3:-}"
  local temporary="$state.tmp.$$"
  printf '{"updated_utc":"%s","pid":%d,"stage":"%s","detail":"%s","chunk":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$stage" "$detail" "$chunk" >"$temporary"
  mv "$temporary" "$state"
}

pass_catalog_is_fresh() {
  local path="$1"
  [[ -f "$path" ]] || return 1
  "${run[@]}" python -c 'import datetime,json,sys
p=json.load(open(sys.argv[1])); end=p.get("window",{}).get("end")
if not end: raise SystemExit(1)
t=datetime.datetime.fromisoformat(end.replace("Z","+00:00")).timestamp()
raise SystemExit(0 if t >= datetime.datetime.now(datetime.timezone.utc).timestamp()+float(sys.argv[2]) else 1)' \
    "$path" "$pass_refresh_margin_s"
}

refresh_pass_catalog() {
  local path="$1" carrier_hz="$2" temporary
  temporary="$path.tmp.$$"
  [[ "$pass_refresh" == "1" ]] || return 0
  pass_catalog_is_fresh "$path" && return 0
  if [[ ! -f "$tle_catalog" ]]; then
    printf '%s cannot refresh passes: missing TLE catalog %s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tle_catalog" >>"$log"
    return 1
  fi
  mkdir -p "$(dirname "$path")"
  local start end
  start="$(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"
  end="$(date -u -d "+$pass_window_hours hours" +%Y-%m-%dT%H:%M:%SZ)"
  "${run[@]}" leo-orbit passes --catalog "$tle_catalog" \
    --lat "$observer_lat" --lon "$observer_lon" --alt-m "$observer_alt_m" \
    --start "$start" --end "$end" --horizon-deg "$pass_horizon_deg" \
    --carrier-hz "$carrier_hz" --step-seconds 20 --candidate-limit 300 \
    --output "$temporary" >>"$log" 2>&1 || return 1
  mv "$temporary" "$path"
  printf '%s refreshed pass catalog %s through %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$path" "$end" >>"$log"
}

exec 9>"$lock"
if ! flock -n 9; then
  write_state blocked "another observer owns the Pluto lock"
  exit 2
fi

stop=0
trap 'stop=1' TERM INT
last_chunk="$(find "$watch_root/analysis" -maxdepth 1 -type f -name 'chunk-*.json' -printf '%f\n' 2>/dev/null |
  sed -n 's/^chunk-0*\([0-9][0-9]*\)-.*/\1/p' | sort -n | tail -1)"
base_chunk="${last_chunk:--1}"
chunk_index=$((base_chunk + 1))
completed_this_run=0
failures=0
write_state starting "v2 watcher initialized"

while (( stop == 0 )); do
  if (( max_chunks > 0 && completed_this_run >= max_chunks )); then
    break
  fi

  pi_temp=""
  radio_temp=""
  if [[ "$fake" != "1" ]] && command -v vcgencmd >/dev/null 2>&1; then
    pi_temp="$(vcgencmd measure_temp | sed -n "s/.*=\([0-9.]*\).*/\1/p")"
    if awk -v actual="$pi_temp" -v maximum="$max_pi_temp_c" 'BEGIN { exit !(actual >= maximum) }'; then
      write_state cooling "pi_temp_c=$pi_temp threshold_c=$max_pi_temp_c"
      sleep 30
      continue
    fi
  fi
  if [[ "$fake" != "1" ]] && command -v iio_attr >/dev/null 2>&1; then
    radio_temp_millic="$(iio_attr -u "$uri" -c ad9361-phy temp0 2>/dev/null |
      sed -n "s/.*value '\([0-9-]*\)'.*/\1/p" | tail -1)"
    [[ -n "$radio_temp_millic" ]] && radio_temp="$(awk -v value="$radio_temp_millic" \
      'BEGIN { printf "%.3f", value/1000 }')"
  fi

  slot=$((chunk_index % ${#centers[@]}))
  center="${centers[$slot]}"
  channel="${channels[$slot]}"
  tuning_dither="${dithers[$slot]}"
  pass_catalog="$passes_ch3"
  auto_passes="$auto_passes_ch3"
  rf_carrier_hz=11325117187.5
  [[ "$channel" == "4" ]] && { pass_catalog="$passes_ch4"; auto_passes="$auto_passes_ch4"; rf_carrier_hz=11575117187.5; }
  if [[ "$auto_passes" == "1" ]] && ! refresh_pass_catalog "$pass_catalog" "$rf_carrier_hz"; then
    write_state schedule_error "could not refresh channel=$channel pass catalog"
    sleep "${ERROR_RETRY_SECONDS:-30}"
    continue
  fi
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  stem="chunk-$(printf '%05d' "$chunk_index")-ch${channel}-${stamp}"
  measurement="$watch_root/chunks/$stem.npz"
  iq_candidate="$iq_staging_dir/$stem.npz"
  analysis="$watch_root/analysis/$stem.json"
  plot="$watch_root/plots/$stem.png"
  write_state capturing "channel=$channel center_hz=$center" "$stem"

  capture_args=(
    starlink-measurement-capture "$measurement"
    --center-frequency-hz "$center"
    --lnb-lo-hz "$lnb_lo_hz"
    --sample-rate-hz 30720000
    --bandwidth-hz 20000000
    --gain-mode manual
    --gain-db "$gain_db"
    --snapshots "$snapshots"
    --block-size "$block_size"
    --fft-size "$fft_size"
    --output-bins "$output_bins"
    --adc-full-scale 2048
    --uri "$uri"
    --experiment-tag "channel-${channel}-dither-${tuning_dither}hz"
    --tuning-dither-hz "$tuning_dither"
    --discard-buffers 8
    --iq-evidence-output "$iq_candidate"
    --iq-evidence-blocks 20
    --iq-trigger-warmup-blocks 16
    --iq-trigger-threshold-db 0.8
    --iq-trigger-separation-blocks 8
    --iq-trigger-stratum-blocks 50
  )
  [[ -n "$psd_quantization_db" ]] && \
    capture_args+=(--psd-quantization-db "$psd_quantization_db")
  [[ "$fake" == "1" ]] && capture_args+=(--fake --seed "$chunk_index")
  [[ -n "$pi_temp" ]] && capture_args+=(--host-temperature-c "$pi_temp")
  [[ -n "$radio_temp" ]] && capture_args+=(--radio-temperature-c "$radio_temp")
  [[ "$fake" != "1" && "$use_gps" == "1" && -c "$gps_device" ]] && \
    capture_args+=(--gps-device "$gps_device" --gps-timeout-s 3)

  "${run[@]}" leo-radio "${capture_args[@]}" >>"$log" 2>&1
  exit_code=$?
  if (( exit_code == 0 )); then
    # The rolling 25-hour catalog is useful for the dashboard, but limiting
    # that whole-day screen to a few hundred satellites can omit an overhead
    # spacecraft at this particular chunk. Build a complete short-window
    # catalog for scientific matching of each capture.
    analysis_pass_catalog="$pass_catalog"
    if [[ "$fake" != "1" && -f "$tle_catalog" ]]; then
      targeted_pass_catalog="$watch_root/passes/$stem.json"
      read -r targeted_start targeted_end < <("${run[@]}" python - "$measurement" <<'PY'
import datetime, sys
import numpy as np
with np.load(sys.argv[1], allow_pickle=False) as stored:
    first, last = int(stored["utc_ns"][0]), int(stored["utc_ns"][-1])
start = datetime.datetime.fromtimestamp(first/1e9-300, datetime.timezone.utc)
end = datetime.datetime.fromtimestamp(last/1e9+300, datetime.timezone.utc)
print(start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"))
PY
      )
      targeted_temporary="$targeted_pass_catalog.tmp.$$"
      if "${run[@]}" leo-orbit passes --catalog "$tle_catalog" \
          --lat "$observer_lat" --lon "$observer_lon" --alt-m "$observer_alt_m" \
          --start "$targeted_start" --end "$targeted_end" --horizon-deg "$pass_horizon_deg" \
          --carrier-hz "$rf_carrier_hz" \
          --step-seconds 5 --candidate-limit 0 --output "$targeted_temporary" >>"$log" 2>&1; then
        mv "$targeted_temporary" "$targeted_pass_catalog"
        analysis_pass_catalog="$targeted_pass_catalog"
      else
        rm -f "$targeted_temporary"
        printf '%s targeted pass generation failed for %s; using rolling catalog\n' \
          "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stem" >>"$log"
      fi
    fi
    write_state analyzing "channel=$channel center_hz=$center" "$stem"
    analyze_args=(starlink-measurement-analyze "$measurement" "$analysis" --plot "$plot"
      --carrier-hz "$rf_carrier_hz")
    # Do not feed the complete constellation to the legacy exhaustive
    # tone/comb matcher: hundreds of nearly identical paths make it slow and
    # non-identifying. The wide-feature stage below performs the complete,
    # controlled trajectory comparison efficiently.
    [[ "$fake" == "1" && -f "$analysis_pass_catalog" ]] && \
      analyze_args+=(--passes "$analysis_pass_catalog")
    "${run[@]}" leo-radio "${analyze_args[@]}" >>"$log" 2>&1
    exit_code=$?
    if (( exit_code == 0 )); then
      # Baselines are resolution-specific. Mixing axes would either fail or,
      # worse, compare different RF bins. Bootstrap a robust baseline after
      # four same-resolution captures spanning both dither centers.
      rf_baseline="$watch_root/baseline/channel-${channel}-${output_bins}bins.npz"
      baseline_bootstrapped=0
      if [[ ! -f "$rf_baseline" ]]; then
        mapfile -t baseline_inputs < <("${run[@]}" python - \
            "$watch_root" "$channel" "$output_bins" "$baseline_min_captures" <<'PY'
import glob, sys
import numpy as np
root, channel, bins, needed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
matched = []
for path in reversed(sorted(glob.glob(f"{root}/chunks/chunk-*-ch{channel}-*.npz"))):
    try:
        with np.load(path, allow_pickle=False) as stored:
            if np.asarray(stored["frequency_offsets_hz"]).size != bins:
                continue
            matched.append((path, float(stored["center_frequency_hz"])))
    except (OSError, KeyError, ValueError):
        continue
    if len(matched) >= needed and len({center for _, center in matched}) >= 2:
        break
if len(matched) >= needed and len({center for _, center in matched}) >= 2:
    print(*(path for path, _ in matched), sep="\n")
PY
        )
        if (( ${#baseline_inputs[@]} >= baseline_min_captures )); then
          baseline_temporary="$rf_baseline.tmp.$$.npz"
          if "${run[@]}" leo-radio starlink-rf-baseline "$baseline_temporary" \
              "${baseline_inputs[@]}" >>"$log" 2>&1; then
            mv "$baseline_temporary" "$rf_baseline"
            baseline_bootstrapped=1
            printf '%s bootstrapped %s from %d captures\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
              "$rf_baseline" "${#baseline_inputs[@]}" >>"$log"
          else
            rm -f "$baseline_temporary"
          fi
        fi
      fi
      if [[ -f "$rf_baseline" ]]; then
        # Analyze the current capture synchronously so gating decisions are
        # complete before its staged IQ is released.
        wide_targets=("$measurement")
        for wide_measurement in "${wide_targets[@]}"; do
          wide_stem="$(basename "$wide_measurement" .npz)"
          wide_report="$watch_root/wide/$wide_stem.json"
          [[ -f "$wide_report" ]] && continue
          wide_plot="$watch_root/plots/$wide_stem-wide.png"
          wide_args=(starlink-wide-feature-analyze "$wide_measurement" "$rf_baseline" \
            "$wide_report" --plot "$wide_plot" --integration-s "$wide_integration_s")
          wide_pass_catalog="$watch_root/passes/$wide_stem.json"
          [[ -f "$wide_pass_catalog" ]] && wide_args+=(--passes "$wide_pass_catalog")
          "${run[@]}" leo-radio "${wide_args[@]}" >>"$log" 2>&1 ||
            printf '%s wide-feature analysis failed for %s\n' \
              "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$wide_stem" >>"$log"
        done
        # Historical warm-up captures do not involve the Pluto. Backfill them
        # at low CPU priority in one background worker so radio acquisition is
        # never paused for several minutes after baseline creation.
        # Rebuild the backlog on every loop. This also recovers unfinished
        # background work after a watcher restart, not only on the one loop
        # that happened to create the baseline.
        mapfile -t backfill_inputs < <("${run[@]}" python - \
            "$watch_root" "$channel" "$output_bins" "$measurement" <<'PY'
import glob, os, sys
import numpy as np
root, channel, bins, current = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
for path in sorted(glob.glob(f"{root}/chunks/chunk-*-ch{channel}-*.npz")):
    if path == current:
        continue
    try:
        with np.load(path, allow_pickle=False) as data:
            if int(np.asarray(data["frequency_offsets_hz"]).size) != bins:
                continue
    except Exception:
        continue
    stem = os.path.splitext(os.path.basename(path))[0]
    if not os.path.exists(f"{root}/wide/{stem}.json"):
        print(path)
PY
        )
        if (( ${#backfill_inputs[@]} > 0 )); then
          (
            exec 8>"$watch_root/backfill-channel-${channel}-${output_bins}.lock"
            flock -n 8 || exit 0
            for backfill_measurement in "${backfill_inputs[@]}"; do
              backfill_stem="$(basename "$backfill_measurement" .npz)"
              backfill_report="$watch_root/wide/$backfill_stem.json"
              [[ -f "$backfill_report" ]] && continue
              backfill_plot="$watch_root/plots/$backfill_stem-wide.png"
              backfill_args=(starlink-wide-feature-analyze "$backfill_measurement" \
                "$rf_baseline" "$backfill_report" --plot "$backfill_plot" \
                --integration-s "$wide_integration_s")
              backfill_pass="$watch_root/passes/$backfill_stem.json"
              [[ -f "$backfill_pass" ]] && backfill_args+=(--passes "$backfill_pass")
              nice -n 10 "${run[@]}" leo-radio "${backfill_args[@]}" ||
                printf '%s background wide-feature analysis failed for %s\n' \
                  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$backfill_stem"
            done
          ) >>"$log" 2>&1 &
          backfill_pids+=("$!")
        fi
        # The IQ gate always concerns the just-completed current capture.
        wide_report="$watch_root/wide/$stem.json"
        # Keep a compact reproducible population view current. The command
        # groups morphology only and does not assign a protocol or spacecraft.
        "${run[@]}" leo-radio starlink-wide-feature-summary \
          "$watch_root/wide/population-summary.json" "$watch_root/wide" >>"$log" 2>&1 ||
          printf '%s wide-feature population summary failed for %s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stem" >>"$log"
        "${run[@]}" leo-radio starlink-confound-analyze "$watch_root" \
          "$watch_root/confounds.json" >>"$log" 2>&1 ||
          printf '%s confound analysis failed for %s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stem" >>"$log"
        # The capture process stages a bounded, time-stratified set of raw
        # dual-RX blocks (normally 18 with the live configuration). Retain
        # them only when the independent integrated-waterfall analysis finds
        # a qualified moving feature, keeping steady-state storage bounded.
        if [[ -f "$iq_candidate" ]]; then
          iq_evidence="$watch_root/iq/$stem.npz"
          "${run[@]}" leo-radio starlink-iq-evidence-gate "$iq_candidate" \
            "$wide_report" "$iq_evidence" >>"$log" 2>&1 ||
            printf '%s IQ evidence time gate failed for %s\n' \
              "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stem" >>"$log"
          rm -f "$iq_candidate"
          if [[ -f "$iq_evidence" ]]; then
            "${run[@]}" leo-radio starlink-waveform-iq-analyze "$iq_evidence" \
              "$watch_root/waveform/$stem.json" >>"$log" 2>&1 ||
              printf '%s waveform IQ analysis failed for %s\n' \
                "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stem" >>"$log"
          fi
        fi
      elif [[ -f "$iq_candidate" ]]; then
        # No independent feature analysis exists during baseline bootstrap.
        # Never allow staged IQ to accumulate in RAM in that interval.
        rm -f "$iq_candidate"
      fi
      if [[ "${pair_complete[$slot]}" == "1" ]]; then
        previous_pair="$("${run[@]}" python - "$watch_root" "$channel" "$measurement" <<'PY'
import glob, sys
import numpy as np
root, channel, current = sys.argv[1:]
with np.load(current, allow_pickle=False) as stored:
    center = float(stored["center_frequency_hz"])
    bins = np.asarray(stored["frequency_offsets_hz"]).size
for path in reversed(sorted(glob.glob(f"{root}/chunks/chunk-*-ch{channel}-*.npz"))):
    if path == current:
        continue
    try:
        with np.load(path, allow_pickle=False) as stored:
            other = float(stored["center_frequency_hz"])
            other_bins = np.asarray(stored["frequency_offsets_hz"]).size
    except (OSError, KeyError, ValueError):
        continue
    if other_bins == bins and 100_000 < abs(other-center) < 2_000_000:
        print(path)
        break
PY
        )"
        if [[ -n "$previous_pair" ]]; then
          dither_report="$watch_root/dither/$stem.json"
          dither_first="$measurement"; dither_second="$previous_pair"
          if awk -v d="$tuning_dither" 'BEGIN { exit !(d != 0) }'; then
            dither_first="$previous_pair"; dither_second="$measurement"
          fi
          "${run[@]}" leo-radio starlink-dither-compare \
            "$dither_first" "$dither_second" "$dither_report" >>"$log" 2>&1 ||
            printf '%s dither comparison failed for %s\n' \
              "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stem" >>"$log"
        fi
      fi
      write_state chunk_complete "channel=$channel center_hz=$center" "$stem"
      chunk_index=$((chunk_index + 1))
      completed_this_run=$((completed_this_run + 1))
      continue
    fi
  fi

  failures=$((failures + 1))
  write_state error "pipeline_exit=$exit_code; retrying" "$stem"
  printf '%s %s failed exit=%d\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$stem" "$exit_code" >>"$log"
  if (( max_failures > 0 && failures >= max_failures )); then
    write_state failed "pipeline_exit=$exit_code; failure limit reached" "$stem"
    exit "$exit_code"
  fi
  sleep "${ERROR_RETRY_SECONDS:-30}"
done

# Finite fake runs are end-to-end tests: do not report completion until their
# derived background artifacts are durable. Live operation never waits here.
if [[ "$fake" == "1" ]]; then
  for backfill_pid in "${backfill_pids[@]}"; do
    wait "$backfill_pid" || true
  done
fi
write_state stopped "watcher finished" "chunk-$chunk_index"
