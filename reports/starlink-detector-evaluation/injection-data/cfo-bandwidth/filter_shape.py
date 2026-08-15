"""Where the analog filter corner ACTUALLY sits, measured off the noise floor.

The factorial records ``rf_bandwidth`` before and after every write, which is
the check the brief asks for.  On this radio that check comes back clean at
every cell -- the driver returns exactly the number written, at all twelve
(rate, bandwidth) combinations -- and that is a weaker result than it looks.
The AD9361 driver stores the *requested* baseband bandwidth in
``current_rx_bw_Hz`` and reports it back; the analog corner underneath is set by
a coarse RC-tuning word, so an unchanged readback proves the driver accepted the
request and nothing at all about the filter.

So the corner is measured instead of asked for.  TX is off, the input is the
receiver's own thermal noise, and thermal noise is the ideal probe here because
it is white by construction: the received power spectrum IS the filter's power
response, with no waveform, no detector and no offset estimate in between.  The
half-corner falls out as the frequency where that spectrum sits 3 dB under its
own centre plateau.

Read alongside the factorial this converts "the driver echoed my number" into
"the hardware moved its corner to X", which is what the bandwidth arm of the
experiment actually rests on.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig_cfobw import (BANDWIDTHS_HZ, PROBE_MS, REF_BW_HZ, RX_GAIN_DB,  # noqa: E402
                       SAMPLE_RATES_HZ, Rig, appender)

SCRATCH = Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
               "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/cfobw")
RESULTS = SCRATCH / "filter_shape-165.jsonl"

#: Welch segment.  1024 bins over the probe gives ~2.4 kHz resolution at
#: 2.5 MS/s and plenty of averaging across a 40 ms capture.
SEGMENT = 1024

CAPTURES = 6

#: The plateau the corner is measured against: the mean level over the middle
#: of the band, well inside every corner under test.
PLATEAU_FRACTION = 0.10


def welch(values: np.ndarray, sample_rate_hz: float,
          segment: int = SEGMENT) -> tuple[np.ndarray, np.ndarray]:
    """Averaged periodogram, in frequency order, as power per bin."""
    usable = (values.size // segment) * segment
    blocks = values[:usable].reshape(-1, segment)
    window = np.hanning(segment).astype(np.float32)
    spectra = np.fft.fftshift(
        np.abs(np.fft.fft(blocks * window, axis=1)) ** 2, axes=1)
    power = spectra.mean(axis=0) / (np.sum(window ** 2) * segment)
    frequency = np.fft.fftshift(np.fft.fftfreq(segment, 1.0 / sample_rate_hz))
    return frequency, power


def corner_hz(frequency: np.ndarray, power_db: np.ndarray,
              plateau_fraction: float = PLATEAU_FRACTION) -> dict:
    """Half-corner: where the response first falls 3 dB under its plateau.

    Taken outward from the centre on each side independently, so an asymmetric
    filter is reported as asymmetric rather than averaged into symmetry.  The
    edges are interpolated between the two bins that straddle the crossing.
    """
    span = float(frequency.max() - frequency.min())
    inner = np.abs(frequency) <= plateau_fraction * span / 2.0
    plateau = float(np.mean(power_db[inner]))

    def edge(sign: int) -> float | None:
        mask = (frequency * sign) > 0
        axis, level = frequency[mask] * sign, power_db[mask]
        order = np.argsort(axis)
        axis, level = axis[order], level[order]
        below = np.nonzero(level < plateau - 3.0)[0]
        if below.size == 0:
            return None
        index = int(below[0])
        if index == 0:
            return float(axis[0])
        x0, x1 = axis[index - 1], axis[index]
        y0, y1 = level[index - 1], level[index]
        target = plateau - 3.0
        if y1 == y0:
            return float(x1)
        return float(x0 + (target - y0) * (x1 - x0) / (y1 - y0))

    lower, upper = edge(-1), edge(+1)
    both = [value for value in (lower, upper) if value is not None]
    return {"plateau_db": plateau,
            "half_corner_lower_hz": lower, "half_corner_upper_hz": upper,
            "half_corner_mean_hz": (float(np.mean(both)) if both else None),
            "measured_bandwidth_hz": (2.0 * float(np.mean(both)) if both else None)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", type=float, nargs="*", default=list(SAMPLE_RATES_HZ))
    parser.add_argument("--bandwidths", type=float, nargs="*",
                        default=list(BANDWIDTHS_HZ) + [REF_BW_HZ])
    parser.add_argument("--captures", type=int, default=CAPTURES)
    parser.add_argument("--out", default=str(RESULTS))
    arguments = parser.parse_args()

    write = appender(Path(arguments.out))
    rig = None
    try:
        rig = Rig(rx_gain_db=RX_GAIN_DB)
        rig.stop_tx()
        write({"kind": "header", "uri": "ip:192.168.1.165",
               "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "context": rig.context_attrs, "rx_gain_db": rig.rx_gain_db,
               "segment": SEGMENT, "captures": arguments.captures,
               "probe_ms": PROBE_MS,
               "note": "TX OFF at -89.75 dB on both ports; the input is the "
                       "receiver's own thermal noise and the measured power "
                       "spectrum is the RX filter's power response"})
        for rate in arguments.rates:
            rig.set_rate(float(rate))
            for bandwidth in arguments.bandwidths:
                rig.set_rx_bandwidth(float(bandwidth))
                time.sleep(0.2)
                back = rig.readback()
                applied = rig.applied(back)
                accumulated = None
                total = []
                for _ in range(arguments.captures):
                    block = rig.capture(discard=1)
                    for receiver in (0, 1):
                        values = np.asarray(block[receiver], np.complex64)
                        total.append(float(np.mean(np.abs(values) ** 2)))
                        frequency, power = welch(values, float(rate))
                        accumulated = (power if accumulated is None
                                       else accumulated + power)
                accumulated /= (2 * arguments.captures)
                power_db = 10.0 * np.log10(np.maximum(accumulated, 1e-30))
                shape = corner_hz(frequency, power_db)
                write({"kind": "shape", "sample_rate_requested_hz": float(rate),
                       "rx_bandwidth_requested_hz": float(bandwidth),
                       "sample_rate_readback_hz": applied["sample_rate_applied_hz"],
                       "rx_bandwidth_readback_hz": applied["rx_bandwidth_applied_hz"],
                       "rx_gain_readback_db": applied["rx_gain_applied_db"],
                       "rx_fir_en": applied["rx_fir_en"],
                       "mean_power_counts2": float(np.mean(total)),
                       "power_dbfs": float(10 * np.log10(np.mean(total) / 2048.0 ** 2)),
                       **shape,
                       "frequency_hz": [float(value) for value in frequency],
                       "power_db": [float(value) for value in power_db]})
                print(f"Fs {rate/1e6:5.2f}  B req {bandwidth/1e6:6.3f}  "
                      f"readback {applied['rx_bandwidth_applied_hz']}  "
                      f"measured half-corner "
                      f"{(shape['half_corner_mean_hz'] or float('nan'))/1e3:8.1f} kHz  "
                      f"noise {10*np.log10(np.mean(total)/2048.0**2):7.2f} dBFS",
                      flush=True)
        return 0
    except Exception:                                        # noqa: BLE001
        import traceback
        write({"kind": "error", "traceback": traceback.format_exc()})
        traceback.print_exc()
        return 1
    finally:
        if rig is not None:
            write({"kind": "rest", "readback": rig.close()})


if __name__ == "__main__":
    raise SystemExit(main())
