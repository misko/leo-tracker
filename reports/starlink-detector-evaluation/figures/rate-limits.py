"""What sample rates the capture radios will actually accept, and with what filter.

The 1.25 MS/s arm is a quarter of this corpus's rate axis, and on 2026-08-15 it
stopped working: every draw at that rate failed with `EINVAL` on the write of
``sampling_frequency`` while every other rate succeeded. The cause is not a
fault. The AD9361's bare minimum is 25 MHz / 12 ~ 2.083 MS/s, and going below it
needs a decimating FIR in the receive path. Both parts report no FIR loaded.

So the arm only ever worked because some earlier tool had left a filter in the
part, and re-plugging the radios cleared it. That matters for analysis rather
than for operations: those captures went through a receive chain no other arm
had, whose coefficients nothing recorded -- ``collect_radio`` writes
``sampling_frequency`` and ``rf_bandwidth`` and reads back neither, and the
collector records neither.

This probes the limit rather than asserting it, so the claim in the report has an
artefact behind it. It writes attributes, so it needs the radios to itself:

    sudo systemctl stop leo-sync-scan.service
    python figures/rate-limits.py
    sudo systemctl start leo-sync-scan.service

It restores each radio to 2.5 MS/s before returning.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

import iio                                              # noqa: E402

from leo_tracker.radio.pluto import _resolve_iio_uri    # noqa: E402

#: The rates the twelve-arm draw uses, plus the AD9361's own documented floor,
#: so the boundary is bracketed rather than inferred from the arms alone.
PROBE_RATES_HZ = (1_250_000, 2_083_333, 2_500_000, 5_000_000, 10_000_000)
RESTORE_HZ = 2_500_000

RADIOS = {"pluto-19f2": "10400056f695001322002d0010ad1719f2",
          "pluto-5d4d": "1040005e0b100007100010000bf33a5d4d"}


def probe(serial: str) -> dict:
    context = iio.Context(_resolve_iio_uri("pluto://usb:", serial))
    phy = context.find_device("ad9361-phy")
    device_attrs = getattr(phy, "attrs", {}) or {}
    receiver = next(c for c in phy.channels
                    if c.id == "voltage0" and not c.output)

    record = {
        "serial": serial,
        # The FIR is the whole explanation, so it is recorded whether or not it
        # is present -- "FIR Rx: 0,0 Tx: 0,0" means none loaded.
        "filter_fir_config": (device_attrs["filter_fir_config"].value
                              if "filter_fir_config" in device_attrs else None),
        "rf_bandwidth_available": (
            receiver.attrs["rf_bandwidth_available"].value
            if "rf_bandwidth_available" in receiver.attrs else None),
        "sampling_frequency_before": receiver.attrs["sampling_frequency"].value,
        "rates": {},
    }

    for rate in PROBE_RATES_HZ:
        try:
            receiver.attrs["sampling_frequency"].value = str(int(rate))
        except OSError as exc:
            record["rates"][str(rate)] = {
                "accepted": False, "errno": exc.errno, "error": str(exc)}
            continue
        # Read back rather than trust the write: a rate the part rounds is a
        # different measurement from one it takes exactly, and only the readback
        # can tell them apart.
        record["rates"][str(rate)] = {
            "accepted": True,
            "readback_hz": int(receiver.attrs["sampling_frequency"].value)}

    receiver.attrs["sampling_frequency"].value = str(RESTORE_HZ)
    record["sampling_frequency_restored"] = receiver.attrs["sampling_frequency"].value
    return record


def main() -> int:
    radios = {}
    for name, serial in sorted(RADIOS.items()):
        try:
            radios[name] = probe(serial)
        except Exception as exc:                        # noqa: BLE001
            radios[name] = {"serial": serial,
                            "unavailable": f"{type(exc).__name__}: {exc}"}

    accepted = {name: sorted(int(r) for r, v in entry.get("rates", {}).items()
                             if v.get("accepted"))
                for name, entry in radios.items() if "rates" in entry}
    rejected = {name: sorted(int(r) for r, v in entry.get("rates", {}).items()
                             if not v.get("accepted"))
                for name, entry in radios.items() if "rates" in entry}

    payload = {
        "schema": "ad9361-rate-limits/1",
        "measured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ad9361_floor_hz": 25_000_000 / 12,
        "floor_basis": ("AD9361 minimum ADC rate divided by the maximum "
                        "non-FIR decimation; below it a decimating FIR is "
                        "required in the receive path"),
        "radios": radios,
        "accepted_hz": accepted,
        "rejected_hz": rejected,
    }
    out = Path(__file__).with_suffix(".json")
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"wrote": str(out), "accepted": accepted,
                      "rejected": rejected}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
