# scanner — N frequencies, specified bandwidths, power per point, fast

Scan N frequencies at per-point bandwidths, dwelling Y ms each, and return received
power per point. Built for finding Doppler-shifted downlinks where the candidate list is
long and the dwell is short.

```bash
# 61 points across 20 MHz -- one tuning covers all of them
scripts/scanner/scan.py --uri usb:1.90.5 --serial 1040007c4a94000211000b009186843ef2 \
    --sweep 2400e6:2420e6:333333 --dwell-ms 1

# arbitrary points and bandwidths, report to JSON
scripts/scanner/scan.py --uri usb:1.90.5 \
    --point 2401e6:1e6 --point 2402.5e6:200e3 --json scan.json

# no radio needed: plan, or run against a synthetic radio
scripts/scanner/scan.py --dry-run --tones 2401e6 --point 2401e6:400e3
```

## Worked example: the 8 edge-pilot tunings

The Starlink edge pilots, all in the LNB low band (9.75 GHz LO), 2.5 MHz each, dwelling
25 ms. `ch4 lower` at 1709.6875 MHz is the tuning that carries most beacons.

```bash
cd ~/leo-tracker

scripts/scanner/scan.py \
    --uri usb:1.90.5 \
    --serial 1040007c4a94000211000b009186843ef2 \
    --sample-rate 2.5e6 \
    --usable-fraction 1.0 \
    --dwell-ms 25 \
    --gain-db 41 \
    --point  959687500:2.5e6 \
    --point 1190312500:2.5e6 \
    --point 1209687500:2.5e6 \
    --point 1440312500:2.5e6 \
    --point 1459687500:2.5e6 \
    --point 1690312500:2.5e6 \
    --point 1709687500:2.5e6 \
    --point 1940312500:2.5e6 \
    --json edge_pilots.json
```

| ch | edge | IF (Hz) | IF (MHz) |
|---:|---|---:|---:|
| 1 | lower | 959,687,500 | 959.6875 |
| 1 | upper | 1,190,312,500 | 1190.3125 |
| 2 | lower | 1,209,687,500 | 1209.6875 |
| 2 | upper | 1,440,312,500 | 1440.3125 |
| 3 | lower | 1,459,687,500 | 1459.6875 |
| 3 | upper | 1,690,312,500 | 1690.3125 |
| 4 | lower | 1,709,687,500 | 1709.6875 |
| 4 | upper | 1,940,312,500 | 1940.3125 |

Because all eight share one LNB band, a single pass covers them all — a set spanning both
bands could not, since an LNB is switched between them and the scanner cannot tell which
band it is in. That assumption is the caller's.

**`--usable-fraction 1.0` is required for this particular request.** 2.5 MHz of bandwidth
at 2.5 MS/s is *critically sampled*: the requested band is the entire Nyquist span with no
guard, so the default 0.8 margin rejects the plan. Allowing it is legitimate on the RSSI
path, which integrates whatever the analog filter passes — but the filter is already
rolling off at the band edges, so the reading is biased slightly low and anything just
outside +/-1.25 MHz aliases in. Sampling at 3.125 MS/s or above gives real margin for the
same nominal bandwidth, and is worth it if the pilots sit near the window edges.

Measured: **252 ms** for the eight, against 202 ms predicted. The 23% gap is the RSSI read
granularity rounding each dwell up. At 25 ms dwell the per-point overhead is ~0.5 ms
against 25 ms of integration, so the sweep is ~98% dwell-bound and neither fastlock nor an
on-device sequencer would buy anything.

### Proving the scan is live

A scan of a disconnected input returns eight plausible numbers that only show nothing was
attached. To check the tool end to end, park a tone on one tuning through a loopback and
confirm that point alone moves:

```bash
python3 - <<'EOF'
import adi, time
sdr = adi.ad9361(uri="usb:1.90.5")
sdr.sample_rate = 2_500_000
sdr.tx_rf_bandwidth = 2_500_000
sdr.tx_lo = 1_709_687_500            # ch4 lower
sdr.tx_hardwaregain_chan1 = -30.0
sdr.dds_single_tone(100_000, 0.5, channel=1)
time.sleep(2); del sdr               # release the context before scanning
EOF
```

Re-running the scan then gives, on a bench loopback with no antenna:

| IF (MHz) | baseline | with the tone | delta |
|---:|---:|---:|---:|
| 959.6875 | -96.75 | -96.75 | - |
| 1190.3125 | -96.75 | -96.75 | - |
| 1209.6875 | -96.75 | -96.75 | - |
| 1440.3125 | -97.70 | -97.75 | - |
| 1459.6875 | -97.75 | -97.75 | - |
| 1690.3125 | -97.75 | -97.75 | - |
| **1709.6875** | -97.75 | **-83.78** | **+14.0** |
| 1940.3125 | -97.75 | -97.09 | - |

One point up 14 dB with its `floor` flag cleared and seven unchanged. Set
`tx_hardwaregain_chan1` back to -80.0 afterwards.

## Measured throughput (R18, RC17, over USB)

| Scan | Tunings | Wall clock | Rate |
|---|---:|---:|---:|
| 61 points clustered in 20 MHz | 1 | 34.9 ms | **1750 points/s** |
| 1000 points across 2 GHz (2 MHz each) | 84 | 601 ms | **1665 points/s** |
| Isolated points, one tuning each | N | 1.88 ms each | 505 points/s |
| Isolated points, fastlock (≤8) | N | 1.06 ms each | 947 points/s |

Grouping is what buys the 3.3x over the per-point floor. Everything else is a smaller
constant factor.

## Why it is built this way

Three measurements on the hardware, not datasheet numbers:

**Changing the analog bandwidth costs ~14.3 ms**, because it triggers an AD9361 baseband
filter recalibration — about 7.6x an entire tune-and-measure. So the analog bandwidth is
chosen **once per scan** and each point's requested bandwidth is synthesised digitally by
summing periodogram bins. That is also *better* than the analog filter: the bandwidth is
exact rather than quantised to filter settings, and one capture yields many bandwidths.
`PlutoScanRadio.configure` raises on a second differing call rather than silently
accepting an 8x slowdown.

**A tune plus a power read costs ~1.88 ms and does not depend on how far the LO moves.**
It decomposes as a 1.28 ms LO write plus a 0.54 ms RSSI read, both dominated by the USB
control path — the AD9361 is settled before the first read completes, and raising the
sample rate 10x changes nothing. So the only real lever is *fewer operations*, which
means covering more points per tuning.

**One tuning covers the usable span** (24 MHz at 30 MS/s), so points inside a common
window need one tuning between them. The planner groups them greedily.

## Two paths, chosen by the planner

- **RSSI** when every point wants the same bandwidth and none share a tuning. Power comes
  from the AD9361's own input-referred RSSI, so **no IQ is transferred at all** and the
  ~2.9 MS/s transport ceiling never applies. Dwell up to ~1.9 ms is free.
- **Digital synthesis** otherwise. One capture per tuning, one periodogram per tuning,
  then bins summed per point. The periodogram is deliberately computed once per *tuning*
  rather than once per point: doing it per point made a 61-point group take 377 ms instead
  of 34.9 ms, because the FFT cost — not the USB transfer — dominates this path.

## Reading the output

`power_dbfs` is relative to a full-scale complex sinusoid; `power_input_referred_db` is
that minus the gain. **Neither is absolute dBm** — that needs a calibrated reference this
tool does not have.

Every point carries flags, because a power without them is not usable:

- `clipped` — RSSI and band power are only valid estimates of input power while the ADC is
  out of overload; above overload the reading stops tracking the input. Detected from the
  samples in synthesis mode. **The RSSI path reports `None`**: it transfers no IQ, so from
  the host it cannot self-detect clipping. The AD9361's CTRL_OUT overload flags would fix
  this but are readable only on the device.
- `below_floor` — the band is at the noise floor.
- `partially_out_of_span` — the requested band ran off the edge of the capture.

The RSSI and synthesis scales are each self-consistent but not mutually comparable
without `--calibrate-rssi`, which measures the offset between them at the current tuning.

## Layout and testing

| File | Role |
|---|---|
| `plan.py` | pure planner: grouping, one bandwidth, mode choice, timing model |
| `execute.py` | executor against an injected `ScanRadio`, plus `FakeScanRadio` |
| `pluto.py` | hardware adapter; imports pyadi lazily, owns one libiio context |
| `cli.py`, `scan.py` | command line |

The offline suite needs no radio and no network:

```bash
pytest -m "not hardware" tests/test_scanner_plan.py tests/test_scanner_execute.py
```

The hardware suite is marked and opt-in:

```bash
LEO_SCANNER_URI=usb:1.90.5 LEO_SCANNER_SERIAL=<serial> pytest -m hardware
```

## Notes that will save you time

- **Use an interpreter that has numpy, pyadi-iio and native libiio.** On the acquisition
  host that is the repository's own environment,
  `uv run --active --no-sync scripts/scanner/scan.py ...`; on a bench box it may be
  another virtualenv. `scan.py` inserts its own parent on `sys.path`, so it runs straight
  from a checkout with no install step.

- **Resolve a radio by serial, never by address.** Both the USB address and the DHCP
  lease move across a firmware load; during development one radio inherited the other's
  IP. `--serial` asserts it and refuses the wrong radio.
- **Never spawn a process per operation.** One attribute round trip is ~0.5 ms on a
  persistent context and ~67 ms via a new process.
- **`close()` the radio**, or use it as a context manager. A USB context is an exclusive
  claim and a leaked one makes the next open fail with `EBUSY`.
- **Fastlock only pays off below ~2 ms of dwell.** Above that a recall and a retune cost
  the same because the dwell dominates, and there are only 8 profiles.
- **Both RX channels share one LO**, so there is no frequency parallelism to win.
