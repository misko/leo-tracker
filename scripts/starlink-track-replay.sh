#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 [--workers N] [--replay-id ID] [--limit N] SHARED_ROOT" >&2
}

workers=4
replay_id="continuity-gap15-reacq15k-v1"
limit=""
while (($#)); do
  case "$1" in
    --workers) workers="$2"; shift 2 ;;
    --replay-id) replay_id="$2"; shift 2 ;;
    --limit) limit="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "unknown option: $1" >&2; usage; exit 2 ;;
    *) break ;;
  esac
done
if (($# != 1)); then usage; exit 2; fi

root="${1%/}"
repo="${LEO_TRACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$repo"
if [[ ! -x .venv/bin/python ]]; then
  echo "missing repository environment: $repo/.venv" >&2
  exit 1
fi

timestamp() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
echo "[$(timestamp)] replay_start id=$replay_id workers=$workers root=$root repo=$repo"
uv run --active --no-sync python -m leo_tracker.replay_cli plan \
  "$root" --replay-id "$replay_id"
echo "[$(timestamp)] replay_initial_status"
uv run --active --no-sync python -m leo_tracker.replay_cli status \
  "$root" --replay-id "$replay_id"

run_args=("$root" --replay-id "$replay_id" --workers "$workers")
if [[ -n "$limit" ]]; then run_args+=(--limit "$limit"); fi
uv run --active --no-sync python -m leo_tracker.replay_cli run "${run_args[@]}"

echo "[$(timestamp)] replay_final_status"
uv run --active --no-sync python -m leo_tracker.replay_cli status \
  "$root" --replay-id "$replay_id"
