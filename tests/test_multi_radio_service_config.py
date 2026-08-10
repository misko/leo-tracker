from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
ENVIRONMENTS = ROOT / "deploy" / "environments"


def _read_environment(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip('"')
    return result


def test_template_requires_per_instance_fail_closed_configuration():
    unit = (ROOT / "deploy/systemd/leo-tracker-beacon-watch@.service").read_text()
    assert "EnvironmentFile=/etc/leo-tracker/beacon-watch@%i.env" in unit
    assert "ExecStartPre=/home/satpi01/leo-tracker/scripts/validate-beacon-instance-env.sh" in unit
    assert "Environment=LEO_BEACON_ANALYSIS_MODE=offload" in unit
    assert "Environment=LEO_BEACON_PRESERVE_RAW=1" in unit
    assert "Environment=LEO_BEACON_REQUIRE_HARDWARE_PREFLIGHT=1" in unit
    assert "Conflicts=leo-tracker-beacon-watch.service" in unit
    assert "Requires=mnt-leo\\x2dnvme.mount" in unit
    assert "KillMode=mixed" in unit and "TimeoutStopSec=300" in unit


def test_radio_examples_are_disjoint_but_share_storage_and_queue_owner():
    new = _read_environment(ENVIRONMENTS / "beacon-watch@pluto-new.env.example")
    old = _read_environment(ENVIRONMENTS / "beacon-watch@pluto-old.env.example")
    assert new["LEO_BEACON_RADIO_SERIAL"] == "1040005e0b100007100010000bf33a5d4d"
    assert old["LEO_BEACON_RADIO_SERIAL"] == "10400056f695001322002d0010ad1719f2"
    assert new["LEO_BEACON_RADIO_URI"] == "pluto://usb:"
    assert old["LEO_BEACON_RADIO_URI"] == "pluto://usb:"
    assert new["LEO_BEACON_RADIO_ID"] != old["LEO_BEACON_RADIO_ID"]
    assert set(new["LEO_BEACON_RECEIVER_LABELS"].split()).isdisjoint(
        old["LEO_BEACON_RECEIVER_LABELS"].split())
    assert new["LEO_BEACON_STORAGE"] == old["LEO_BEACON_STORAGE"] == \
        "/mnt/leo-nvme/leo-tracker"
    assert int(new["LEO_BEACON_MINIMUM_FREE_GB"]) >= 200
    assert int(old["LEO_BEACON_MINIMUM_FREE_GB"]) >= 200
    assert old["LEO_BEACON_OVERSAMPLE_ON_STARTUP"] == "0"
    assert old["LEO_BEACON_OVERSAMPLE_EVERY_CYCLES"] == "0"
    assert old["LEO_BEACON_WIDE_EVERY_CYCLES"] == "0"
    assert old["LEO_BEACON_HOP_EVERY_CYCLES"] == "0"


def test_instance_preflight_accepts_examples_and_rejects_ambiguous_binding():
    validator = ROOT / "scripts" / "validate-beacon-instance-env.sh"
    for name in ("pluto-new", "pluto-old"):
        values = _read_environment(ENVIRONMENTS / f"beacon-watch@{name}.env.example")
        values |= {"LEO_BEACON_ANALYSIS_MODE": "offload", "LEO_BEACON_PRESERVE_RAW": "1"}
        checked = subprocess.run(["bash", str(validator)], env=os.environ | values,
                                 text=True, capture_output=True)
        assert checked.returncode == 0, checked.stderr
        assert '"instance_config_valid":true' in checked.stdout

    invalid = values | {"LEO_BEACON_RADIO_URI": "pluto://ip:192.168.2.1"}
    checked = subprocess.run(["bash", str(validator)], env=os.environ | invalid,
                             text=True, capture_output=True)
    assert checked.returncode == 2
    assert "must select the USB backend" in checked.stderr
