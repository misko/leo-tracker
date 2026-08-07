#!/usr/bin/env bash
# Queue preserved Pi IQ that has not completed the selected Kalman pipeline.
set -euo pipefail

source_root="${LEO_BEACON_STORAGE:-/mnt/leo-nvme/leo-tracker}"
shared_root="${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}"
pipeline_id="${LEO_ANALYSIS_PIPELINE_ID:-kalman-full-v1}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "${uv_bin}" || ! -x "${repo_dir}/.venv/bin/python" ]]; then
  echo "uv and the existing ${repo_dir}/.venv are required" >&2
  exit 2
fi

env UV_CACHE_DIR="${repo_dir}/.uv-cache" "${uv_bin}" run --active --no-sync \
  python -m leo_tracker.radio.beacon.offload enqueue-export-backfill \
  "${source_root}" "${shared_root}" --pipeline-id "${pipeline_id}" "$@"
