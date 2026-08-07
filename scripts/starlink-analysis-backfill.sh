#!/usr/bin/env bash
# Queue complete QNAP captures that lack a versioned Kalman completion receipt.
set -euo pipefail

root="${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}"
pipeline_id="${LEO_ANALYSIS_PIPELINE_ID:-kalman-full-v1}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi

env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
  python -m leo_tracker.radio.beacon.offload enqueue-backfill "${root}" \
  --pipeline-id "${pipeline_id}" "$@"
