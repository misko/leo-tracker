#!/usr/bin/env bash
# Drain legacy Kalman analysis and cut over to direct V2 plus migration primary.
set -euo pipefail

shared_root="${1:-${LEO_OFFLOAD_ROOT:-/mnt/qnap01/mouse9911/leo}}"
archive_root="${2:-${LEO_EVIDENCE_ROOT:-/mnt/qnap01/mouse9911/leo-cropped}}"
repo_dir="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
analysis_unit="${repo_dir}/deploy/leo-tracker-analysis-server.service"
storage_unit="${repo_dir}/deploy/leo-tracker-storage-regime-v2.service"
analysis_runtime="${shared_root}/reports/runtime/analysis-server.json"
storage_runtime="${shared_root}/reports/runtime/storage-regime-v2-primary.json"
timeout_s="${LEO_KALMAN_CUTOVER_TIMEOUT_S:-180}"

if [[ ! "${timeout_s}" =~ ^[1-9][0-9]*$ ]]; then
  echo "LEO_KALMAN_CUTOVER_TIMEOUT_S must be a positive integer" >&2
  exit 2
fi
if [[ ! -x "${repo_dir}/.venv/bin/python" ||
      ! -f "${analysis_unit}" || ! -f "${storage_unit}" ]]; then
  echo "run from a current leo-tracker checkout with its existing .venv" >&2
  exit 2
fi
if [[ ! -d "${shared_root}" || ! -d "${archive_root}" ]]; then
  echo "LEO QNAP roots are not mounted: ${shared_root} ${archive_root}" >&2
  exit 2
fi

echo "repo=${repo_dir}"
echo "commit=$(git -C "${repo_dir}" rev-parse HEAD)"
echo "shared_root=${shared_root}"
echo "archive_root=${archive_root}"
echo "draining leo-tracker-analysis-server.service"
sudo systemctl stop leo-tracker-analysis-server.service

echo "installing current analysis and storage-primary units"
sudo install -m 0644 "${analysis_unit}" \
  /etc/systemd/system/leo-tracker-analysis-server.service
sudo install -m 0644 "${storage_unit}" \
  /etc/systemd/system/leo-tracker-storage-regime-v2.service
sudo systemctl daemon-reload
sudo systemctl enable --now leo-tracker-analysis-server.service \
  leo-tracker-storage-regime-v2.service

echo "waiting up to ${timeout_s}s for direct-V2 producer and migration-primary contracts"
deadline=$((SECONDS + timeout_s))
while (( SECONDS < deadline )); do
  if "${repo_dir}/.venv/bin/python" - "${analysis_runtime}" \
      "${storage_runtime}" "${archive_root}" <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

analysis_path, storage_path, archive_root = map(Path, sys.argv[1:])
try:
    analysis = json.loads(analysis_path.read_text())
    storage = json.loads(storage_path.read_text())
    heartbeat = datetime.fromisoformat(
        str(analysis["heartbeat_utc"]).replace("Z", "+00:00"))
    heartbeat_age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    valid = (
        analysis.get("schema") == "leo-tracker.analysis-server-runtime/v1" and
        analysis.get("state") == "running" and
        analysis.get("producer_contract_valid") is True and
        analysis.get("archive_mode") == "required" and
        analysis.get("evidence_policy") == "tiered-v2" and
        analysis.get("archive_command") == "starlink-evidence-archive-v2" and
        analysis.get("archive_root") == str(archive_root) and
        heartbeat_age <= 180 and
        storage.get("schema") == "leo-tracker.storage-regime-v2-primary/v1" and
        storage.get("state") == "running")
except (OSError, ValueError, KeyError, TypeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
  then
    echo "cutover_verified analysis_runtime=${analysis_runtime} storage_runtime=${storage_runtime}"
    systemctl --no-pager --full status leo-tracker-analysis-server.service \
      leo-tracker-storage-regime-v2.service | sed -n '1,28p'
    exit 0
  fi
  sleep 3
done

echo "cutover contracts did not become valid within ${timeout_s}s" >&2
systemctl --no-pager --full status leo-tracker-analysis-server.service \
  leo-tracker-storage-regime-v2.service >&2 || true
exit 1
