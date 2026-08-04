#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
archive_dir="${TLE_ARCHIVE_DIR:-/mnt/qnap01/mouse9911/satellites/leo-tracker/tle-history}"
output="${PASS_CATALOG_OUTPUT:-$repo_dir/artifacts/starlink_hybrid_watch/passes.json}"
uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
run=("$uv_bin" run --active --no-sync)
latest="$archive_dir/latest.json"

object_rel="$("${run[@]}" python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["object"])' "$latest")"
catalog="$archive_dir/$object_rel"
start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
stop="$(date -u -d '+30 hours' +%Y-%m-%dT%H:%M:%SZ)"
temporary="${output}.next"
mkdir -p "$(dirname "$output")"

"${run[@]}" leo-orbit passes --catalog "$catalog" \
  --lat 37.849165355010086 --lon -122.48567658142287 --alt-m 0 \
  --start "$start" --end "$stop" --horizon-deg 10 \
  --carrier-hz 11500000000 --candidate-limit 512 --output "$temporary"
mv "$temporary" "$output"
