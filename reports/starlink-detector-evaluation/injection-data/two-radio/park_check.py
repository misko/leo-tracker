"""Read the TX state on both rigs, park anything hot, and read back."""
import json
import sys

sys.path.insert(0, "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/dualrig")

import iio
from rig import TX_IDLE_DB, _attr, _num

URIS = {"r183": "ip:192.168.1.183", "r165": "ip:192.168.1.165"}
out = {}
for label, uri in URIS.items():
    rec = {"uri": uri}
    try:
        ctx = iio.Context(uri)
        phy = ctx.find_device("ad9361-phy")
        rec["serial"] = ctx.attrs.get("hw_serial", "?")
        tx1 = phy.find_channel("voltage0", True)
        tx2 = phy.find_channel("voltage1", True)
        rec["before"] = {"tx1_db": _num(_attr(tx1, "hardwaregain")),
                         "tx2_db": _num(_attr(tx2, "hardwaregain"))}
        for ch in (tx1, tx2):
            _attr(ch, "hardwaregain", f"{TX_IDLE_DB:.2f}")
        rec["after"] = {"tx1_db": _num(_attr(tx1, "hardwaregain")),
                        "tx2_db": _num(_attr(tx2, "hardwaregain"))}
        # also silence any DDS tone left running on the TX DMA
        txdma = ctx.find_device("cf-ad9361-dds-core-lpc")
        dds = {}
        for ch in txdma.channels:
            if ch.id.startswith("altvoltage"):
                for key in ("raw", "scale"):
                    if key in ch.attrs:
                        try:
                            dds[f"{ch.id}.{key}"] = _attr(ch, key)
                        except Exception as exc:
                            dds[f"{ch.id}.{key}"] = f"ERR {exc}"
        rec["dds"] = dds
    except Exception as exc:
        rec["error"] = repr(exc)
    out[label] = rec

print(json.dumps(out, indent=2))
with open("/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/dualrig/park_check.json", "w") as fh:
    json.dump(out, fh, indent=2)
