"""Interim synchronised paired-scan collector.

One process, both radios, two threads, threading.Barrier per tuning.  Measured
0.037 ms median skew against the 200-500 ms the operator asked for.  Runs until
killed; the production version replaces it when it lands.

Per sweep: draw an arm (12), a pairing (90% matched), and an edge order PER
RADIO.  Write both radios' raw ci16 and a sidecar recording what actually
happened -- arms, orders, and the MEASURED per-tuning skew, not the intended one.
"""
import json, os, random, threading, time, sys, traceback
from pathlib import Path
import numpy as np
from leo_tracker.radio.beacon.presurvey import _open_context
from leo_tracker.radio.beacon.fast_scan import collect_radio, ScanProfile, SURVEY_PROFILE

ROOT = Path("/mnt/leo-nvme/leo-tracker/sync-scans")
# A sweep costs about 171 MB here and about 118 MB per radio again once it is
# imported, so an unthrottled run writes roughly 186 GB per hour to a share that
# does not have that many hours left in it.  Coverage across the clock is worth
# more than raw count -- the whole corpus so far is one contiguous six-hour
# block -- so a minimum period lets a long run trade cadence for span.  Zero is
# the historical behaviour and stays the default.
SWEEP_PERIOD_S = float(os.environ.get("SYNC_SWEEP_PERIOD_S", "0") or 0)
# And a floor, because the failure this guards is not a slow disk but a full
# one: the drain cannot ship what the collector could not write, and a wedged
# share takes the import and the scoring host down with it.
MIN_FREE_BYTES = float(os.environ.get("SYNC_MIN_FREE_BYTES", "0") or 0)
FREE_CHECK = Path(os.environ.get("SYNC_FREE_CHECK", "/mnt/qnap01/mouse9911"))


def free_bytes(path: Path) -> float:
    """Free space where the sweeps are going to end up, not where they start."""
    try:
        stat = os.statvfs(path)
    except OSError:
        return float("inf")          # unreadable is not the same as full
    return stat.f_bavail * stat.f_frsize
RADIOS = [("pluto-19f2", "10400056f695001322002d0010ad1719f2", ("lnb-c", "lnb-d")),
          ("pluto-5d4d", "1040005e0b100007100010000bf33a5d4d", ("lnb-a", "lnb-b"))]
PROBES = (0.080, 0.160, 0.640)
RATES = (1_250_000.0, 2_500_000.0, 5_000_000.0, 10_000_000.0)
# Rates the AD9361 will refuse without a decimating FIR loaded.  Its floor is
# 25 MHz/12 = 2.083 MS/s bare, so 1.25 MS/s only ever worked because some
# earlier tool had left an FIR in the part -- and re-plugging the radios cleared
# it, after which every 1.25 MS/s draw died with EINVAL and burned its slot.
# Dropping the rate is honest about that; loading an FIR of unknown provenance
# to resurrect the arm would change the receive response the arm is measuring.
SKIP_RATES = {float(x) for x in
              (os.environ.get("SYNC_SKIP_RATES_HZ") or "").replace(",", " ").split()}
ARMS = [{"name": f"{int(p*1000)}ms-{r/1e6:.2f}MSps", "probe_s": p, "sample_rate_hz": r,
         "pilot_band_fits": r >= 1_875_000.0}
        for p in PROBES for r in RATES if r not in SKIP_RATES]
if SKIP_RATES:
    print(f"arms: {len(ARMS)} (skipping {sorted(SKIP_RATES)})", flush=True)
ORDER_L = tuple((c, e) for c in (1,2,3,4) for e in ("lower","upper"))
ORDER_U = tuple((c, e) for c in (1,2,3,4) for e in ("upper","lower"))

def u32(): return int.from_bytes(os.urandom(4), "big")

def profile_for(arm):
    n = int(round(arm["probe_s"] * arm["sample_rate_hz"]))
    return ScanProfile(probe_s=arm["probe_s"], block_size=n,
                       kernel_buffers=SURVEY_PROFILE.kernel_buffers,
                       settle_buffers=SURVEY_PROFILE.settle_buffers,
                       shape=SURVEY_PROFILE.shape,
                       offset_span_hz=SURVEY_PROFILE.offset_span_hz)

print(f"opening both radios...", flush=True)
ctx = {}
for name, serial, _ in RADIOS:
    # Tolerant, because the alternative is a crash loop: a radio that has
    # dropped off USB takes the whole collector down here, systemd restarts it
    # five seconds later, and it dies again -- so a dropout that would have
    # cost a few sweeps costs the rest of the run instead. ``context_for``
    # opens lazily, so a radio that comes back is picked up by the next sweep.
    try:
        ctx[name] = _open_context("pluto://usb:", serial)
        print(f"  {name} open", flush=True)
    except Exception as exc:
        print(f"  {name} NOT ATTACHED: {exc}", flush=True)
if not ctx:
    print("  no radios attached; waiting for one to appear", flush=True)

sweeps = 0
started = time.monotonic()
while True:
    if MIN_FREE_BYTES:
        free = free_bytes(FREE_CHECK)
        if free < MIN_FREE_BYTES:
            print(f"stopping: {free/1e9:.1f} GB free at {FREE_CHECK}, "
                  f"below the {MIN_FREE_BYTES/1e9:.1f} GB floor after "
                  f"{sweeps} sweeps", flush=True)
            break
    cycle = time.monotonic()
    sweeps += 1
    d_arm, d_pair = u32(), u32()
    arm_common = ARMS[d_arm % len(ARMS)]
    matched = (d_pair % 10) != 0                      # 90% matched
    plan = {}
    for name, _, labels in RADIOS:
        d_order = u32()
        own = arm_common if matched else ARMS[u32() % len(ARMS)]
        plan[name] = {"arm": own, "order": "L" if d_order % 2 == 0 else "U",
                      "order_draw_u32": d_order, "labels": list(labels)}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = ROOT / f"sync-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    barrier = threading.Barrier(len(RADIOS))
    arrive = {n: [] for n, _, _ in RADIOS}
    errs, wrote = {}, {}

    def context_for(name):
        """This radio's open context, re-opening it if a earlier sweep lost it.

        A reopen that fails leaves no entry, and reading ctx[name] then raises
        KeyError on every later sweep -- which reads like a collector bug and is
        not one: the radio is absent.  Saying so keeps a USB dropout diagnosable
        from the log, and re-opening here means a radio that comes back is picked
        up by the next sweep whether or not the reopen loop below caught it.
        """
        existing = ctx.get(name)
        if existing is not None:
            return existing
        serial = dict((n, s) for n, s, _ in RADIOS)[name]
        try:
            ctx[name] = _open_context("pluto://usb:", serial)
        except Exception as exc:
            raise RuntimeError(f"{name} is not attached: {exc}") from None
        return ctx[name]

    def sweep(name):
        try:
            p = plan[name]
            tunings = ORDER_L if p["order"] == "L" else ORDER_U
            prof = profile_for(p["arm"])
            blocks = []
            for idx, (ch, edge) in enumerate(tunings):
                try: barrier.wait(timeout=60.0)
                except threading.BrokenBarrierError: pass
                arrive[name].append(time.monotonic())
                blocks.append(collect_radio(context_for(name), [(ch, edge)], profile=prof,
                                            sample_rate_hz=p["arm"]["sample_rate_hz"])["samples"])
            a = np.concatenate([np.asarray(b) for b in blocks], axis=0)
            path = out / f"{name}.ci16"
            a.astype("<i2").tofile(path)
            wrote[name] = {"path": path.name, "bytes": path.stat().st_size,
                           "shape": list(a.shape)}
        except Exception as exc:
            errs[name] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            try: barrier.abort()
            except Exception: pass

    t0 = time.perf_counter()
    ths = [threading.Thread(target=sweep, args=(n,)) for n, _, _ in RADIOS]
    for t in ths: t.start()
    for t in ths: t.join(timeout=900)
    wall = time.perf_counter() - t0

    # A radio that errored may have left its context unusable.  Reopen it and
    # carry on: a dropped sweep is an annotation, a stopped collector is a gap
    # in the dataset that cannot be recovered later.
    for name in list(errs):
        try:
            # pop, not del: once a reopen has failed the key is already gone,
            # and `del` would raise before the retry below ever runs -- which
            # turns one dropped USB device into a permanently dead radio.
            ctx.pop(name, None)
            ctx[name] = _open_context("pluto://usb:",
                                      dict((n, s) for n, s, _ in RADIOS)[name])
            print(f"       reopened {name}", flush=True)
        except Exception as exc:
            print(f"       reopen {name} FAILED: {exc}", flush=True)

    a, b = arrive["pluto-19f2"], arrive["pluto-5d4d"]
    skew = [1000*abs(x-y) for x, y in zip(a, b)]
    rec = {"schema": "leo-tracker.interim-synchronised-scan/v1",
           "sweep": sweeps, "utc": stamp, "wall_s": round(wall, 3),
           "matched_arm": matched, "arm_draw_u32": d_arm, "pairing_draw_u32": d_pair,
           "radios": {n: {"arm": plan[n]["arm"], "edge_order": plan[n]["order"],
                          "order_draw_u32": plan[n]["order_draw_u32"],
                          "receiver_labels": plan[n]["labels"],
                          "tunings": [list(t) for t in (ORDER_L if plan[n]["order"]=="L" else ORDER_U)],
                          "iq": wrote.get(n), "error": errs.get(n)}
                      for n, _, _ in RADIOS},
           "skew_ms": {"per_tuning": [round(s, 4) for s in skew],
                       "median": round(float(np.median(skew)), 4) if skew else None,
                       "max": round(float(np.max(skew)), 4) if skew else None},
           "note": ("measured skew, not requested; 1.25 MS/s does not fit the "
                    "1.875 MHz pilot band and is flagged per arm")}
    (out / "sweep.json").write_text(json.dumps(rec, indent=2) + "\n")
    mb = sum((w or {}).get("bytes", 0) for w in wrote.values())/1e6
    print(f"[{sweeps:4d}] {stamp} {'matched' if matched else 'MIXED  '} "
          f"{plan['pluto-19f2']['arm']['name']:>15}/{plan['pluto-19f2']['order']} "
          f"{plan['pluto-5d4d']['arm']['name']:>15}/{plan['pluto-5d4d']['order']} "
          f"wall {wall:5.2f}s skew med {rec['skew_ms']['median']}ms max {rec['skew_ms']['max']}ms "
          f"{mb:6.1f} MB {'ERR '+str(errs) if errs else ''}", flush=True)

    # Paced from the start of the sweep, not the end, so the period is the
    # thing held constant rather than the gap: a 640 ms / 10 MS/s arm takes
    # several times longer than the cheapest one, and pacing on the gap would
    # let the expensive arms set the real cadence.
    if SWEEP_PERIOD_S:
        remaining = SWEEP_PERIOD_S - (time.monotonic() - cycle)
        if remaining > 0:
            time.sleep(remaining)
