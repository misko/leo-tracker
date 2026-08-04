#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
watch_root="${1:-$repo_dir/artifacts/starlink_watch}"
if [[ -f "$watch_root/status.json" ]]; then
  command sed -n '1p' "$watch_root/status.json"
else
  printf '{"stage":"not_started","watch_root":"%s"}\n' "$watch_root"
fi
if [[ -f "$watch_root/watch.log" ]]; then
  tail -n 5 "$watch_root/watch.log"
fi
