"""The dual-rig injection run.

One process, two independent radios, one seeded Bernoulli schedule.  At instant
k both radios either transmit the pilot frame or stay silent -- the SAME bit for
both, drawn once -- so the only thing the two chains share is the occupancy.
Each radio hears only its own TX through its own cable.  RX1 is captured on each.

Blocks are written per sweep as they complete: libiio segfaults at interpreter
teardown, so nothing may be held only in memory.

Sweeps exist because the scrambled negative control needs donor sweeps to swap
radio B between, exactly as it does on the real corpus.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from rig import (Rig, rms, FS_HZ, TX_IDLE_DB, cyclic_pilot_buffer,   # noqa: E402
                 guard_signals)
import score as S                                                     # noqa: E402

from leo_tracker.radio.beacon.pilots import edge_pilot_frame          # noqa: E402
from leo_tracker.radio.beacon import survey_scoring as ss             # noqa: E402

PROBE_N = 100_000                 # 20 ms at 5 MS/s
PROBE_MS = 20.0
SWEEP_SIZE = 100                  # instants per sweep
F_LEVELS = (0.15, 0.30, 0.50)
SWEEPS_PER_F = 5                  # 500 instants per occupancy level
SETTLE_S = 0.03
SEED = 20260814
HEALTH_GAIN_DB = -20.0            # TX-alive probe: must read ~80 counts
HEALTH_MIN_RMS = 20.0
RUNS = HERE / "runs"
DRIVE = 0.3048


def schedule() -> list[dict]:
    """Null sweeps interleaved among the occupancy levels, so a threshold drawn
    on the null arm is drawn on the same hours as the cells it judges."""
    blocks = [{"kind": "null", "f_true": 0.0, "sweep": f"null-{i}"} for i in range(3)]
    for f in F_LEVELS:
        for s in range(SWEEPS_PER_F):
            blocks.append({"kind": "target", "f_true": f, "sweep": f"f{f:g}-s{s}"})
        blocks += [{"kind": "null", "f_true": 0.0,
                    "sweep": f"null-{f:g}-{i}"} for i in range(2)]
    return blocks


def health(rig, bank) -> dict:
    """Prove the cyclic TX buffer is still running, at a gain where it shows.

    The operating drive sits close to the noise floor by design, so rms cannot
    police the TX there.  This lifts TX2 to -20 dB for one probe, where a live
    buffer reads ~80 counts and a silently dead one reads the 1.4 floor.
    """
    rig.tx_gain(HEALTH_GAIN_DB)
    time.sleep(SETTLE_S)
    rig.capture()
    x = rig.capture()
    value = rms(x)
    rig.tx_gain(TX_IDLE_DB)
    return {"rms": value, "pk_med": S.coarse_peak_median(x, bank),
            "alive": value >= HEALTH_MIN_RMS}


def revive(rig, wave, bank, attempts: int = 4) -> dict:
    """Re-push the cyclic buffer until the health probe says it started."""
    log = []
    for attempt in range(attempts):
        state = health(rig, bank)
        log.append(state)
        if state["alive"]:
            return {"ok": True, "attempts": attempt, "log": log}
        rig.start_tx(wave, DRIVE)
        time.sleep(0.1)
    return {"ok": False, "attempts": attempts, "log": log}


def observe(rig, bank) -> dict:
    x = rig.capture()
    points = S.score_probe(x, float(FS_HZ), bank)
    return {"rms": rms(x), "pk_med": S.coarse_peak_median(x, bank),
            "points": [{"point_id": p["point_id"],
                        "epoch_sample": int(p["epoch_sample"]),
                        "cfo_hz": float(p["cfo_hz"]),
                        "claimed_by": p["claimed_by"],
                        "methods": {n: {"score": float(b["score"])}
                                    for n, b in p["methods"].items()}}
                       for p in points]}


def main(on_gain: dict):
    guard_signals()
    RUNS.mkdir(exist_ok=True)
    ss.warm((float(FS_HZ),))
    bank = S.banks(float(FS_HZ))
    wave = cyclic_pilot_buffer(edge_pilot_frame(FS_HZ, "lower"), FS_HZ)
    rng = np.random.default_rng(SEED)
    blocks = schedule()
    # Drawn up front, for every block, so that resuming a run which already has
    # some sweeps on disk cannot shift the stream for the sweeps that follow.
    for block in blocks:
        block["occupancy"] = [
            int(rng.random() < block["f_true"]) if block["kind"] == "target" else 0
            for _ in range(SWEEP_SIZE)]
    meta = {"probe_samples": PROBE_N, "probe_ms": PROBE_MS, "fs_hz": float(FS_HZ),
            "f_levels": list(F_LEVELS), "sweep_size": SWEEP_SIZE,
            "sweeps_per_f": SWEEPS_PER_F, "seed": SEED, "on_gain_db": on_gain,
            "drive": DRIVE, "settle_s": SETTLE_S, "blocks": len(blocks),
            "tx_idle_db": TX_IDLE_DB, "health_gain_db": HEALTH_GAIN_DB}
    (RUNS / "meta.json").write_text(json.dumps(meta, indent=1))
    rigs = []
    started = time.time()
    try:
        for uri, label in (("ip:192.168.1.183", "r183"), ("ip:192.168.1.165", "r165")):
            r = Rig(uri, label).configure()
            r.start_tx(wave, DRIVE)
            r.open_rx(PROBE_N)
            rigs.append(r)
        for r in rigs:
            state = revive(r, wave, bank)
            print(f"{r.label} TX start {state['ok']} "
                  f"rms={state['log'][-1]['rms']:.1f}", flush=True)
            if not state["ok"]:
                raise RuntimeError(f"{r.label}: cyclic TX never started")

        for index, block in enumerate(blocks):
            path = RUNS / f"{index:03d}-{block['sweep']}.json"
            if path.exists():
                continue
            before = {r.label: revive(r, wave, bank) for r in rigs}
            rows = []
            for k in range(SWEEP_SIZE):
                occupied = bool(block["occupancy"][k])
                for r in rigs:
                    r.tx_gain(on_gain[r.label] if occupied else TX_IDLE_DB)
                time.sleep(SETTLE_S)
                for r in rigs:
                    r.capture()                      # drop the straddling buffer
                row = {"k": k, "occupied": int(occupied), "t": time.time()}
                for r in rigs:
                    row[r.label] = observe(r, bank)
                rows.append(row)
            for r in rigs:
                r.tx_gain(TX_IDLE_DB)
            after = {r.label: revive(r, wave, bank) for r in rigs}
            path.write_text(json.dumps({
                **block, "index": index, "instants": rows,
                "health_before": before, "health_after": after}))
            done = index + 1
            rate = (time.time() - started) / done
            print(f"[{done}/{len(blocks)}] {block['sweep']} "
                  f"occ={sum(r['occupied'] for r in rows)}/{SWEEP_SIZE} "
                  f"{rate:.0f}s/sweep eta={(len(blocks)-done)*rate/60:.0f}min",
                  flush=True)
    finally:
        for r in rigs:
            r.close()
        print("parked", [r.tx_gain_read() for r in rigs], flush=True)


if __name__ == "__main__":
    gains = json.loads(sys.argv[1])
    main(gains)
