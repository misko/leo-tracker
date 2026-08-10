#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'unsafe multi-radio service configuration: %s\n' "$1" >&2
  exit 2
}

radio_id="${LEO_BEACON_RADIO_ID:-}"
radio_uri="${LEO_BEACON_RADIO_URI:-}"
radio_serial="${LEO_BEACON_RADIO_SERIAL:-}"
receiver_labels="${LEO_BEACON_RECEIVER_LABELS:-}"
storage_root="${LEO_BEACON_STORAGE:-}"
require_hardware_preflight="${LEO_BEACON_REQUIRE_HARDWARE_PREFLIGHT:-0}"

[[ "${radio_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
  fail "LEO_BEACON_RADIO_ID must be explicit and filesystem-safe"
[[ "${radio_uri}" =~ ^pluto://usb:([0-9]+(\.[0-9]+)+)?$ ]] ||
  fail "LEO_BEACON_RADIO_URI must select the USB backend"
[[ "${radio_serial}" =~ ^[[:xdigit:]]{34}$ ]] ||
  fail "LEO_BEACON_RADIO_SERIAL must be the explicit 34-digit hardware serial"
read -r -a labels <<< "${receiver_labels}"
(( ${#labels[@]} == 2 )) || fail "LEO_BEACON_RECEIVER_LABELS must name both inputs"
[[ "${labels[0]}" != "${labels[1]}" ]] || fail "receiver labels must be distinct"
for label in "${labels[@]}"; do
  [[ "${label}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    fail "receiver labels must be filesystem-safe"
done
[[ "${storage_root}" == /mnt/leo-nvme/leo-tracker ]] ||
  fail "instances must share the exporter-owned /mnt/leo-nvme/leo-tracker root"
[[ "${LEO_BEACON_ANALYSIS_MODE:-}" == offload ]] ||
  fail "instances must use the single shared offload consumer"
[[ "${LEO_BEACON_PRESERVE_RAW:-}" == 1 ]] ||
  fail "instances must preserve raw IQ for verified reclamation"
[[ "${require_hardware_preflight}" == 0 || "${require_hardware_preflight}" == 1 ]] ||
  fail "LEO_BEACON_REQUIRE_HARDWARE_PREFLIGHT must be 0 or 1"

resolved_uri="${radio_uri#pluto://}"
if [[ "${require_hardware_preflight}" == 1 ]]; then
  repo_dir="${LEO_TRACKER_REPO:-/home/satpi01/leo-tracker}"
  uv_bin="${UV_BIN:-/home/satpi01/.local/bin/uv}"
  [[ -x "${repo_dir}/.venv/bin/python" && -x "${uv_bin}" ]] ||
    fail "hardware preflight requires uv and the repository .venv"
  if ! resolved_uri="$(cd "${repo_dir}" && env UV_CACHE_DIR="${repo_dir}/.uv-cache" \
      "${uv_bin}" run --active --no-sync python - "${radio_uri}" "${radio_serial}" <<'PY'
import sys
from leo_tracker.radio.pluto import _resolve_iio_uri
print(_resolve_iio_uri(sys.argv[1], sys.argv[2]))
PY
  )"; then
    fail "configured Pluto serial is not uniquely present on USB"
  fi
fi

printf '{"instance_config_valid":true,"radio_id":"%s","serial":"%s","uri":"%s","resolved_uri":"%s","receiver_labels":["%s","%s"]}\n' \
  "${radio_id}" "${radio_serial}" "${radio_uri}" "${resolved_uri}" \
  "${labels[0]}" "${labels[1]}"
