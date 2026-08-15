"""Rig control for the CFO x sample-rate x RX-bandwidth factorial on `.165`.

Descended from ``radio-165/rig165.py`` -- same radio, same cable, same TX-off
discipline, same cyclic-buffer priming and the same broken-pipe retry.  Three
things are new, and all three exist because this experiment varies what that
rig held constant:

* ``sample_rate`` is a **parameter**, so the cyclic TX buffer is rebuilt per
  rate.  Three frames is 10,000 / 20,000 / 40,000 samples at 2.5 / 5 / 10 MS/s
  -- an exact number of samples at every rate, so the buffer wraps at the true
  750 Hz frame rate with no discontinuity at any of them.

* ``rf_bandwidth`` is **decoupled** from the sample rate.  ``fast_scan`` writes
  ``rf_bandwidth = sampling_frequency`` on every sky arm, which is exactly why
  the sky corpus cannot separate the digital Nyquist window from the analog
  AD9361 baseband filter.  Here they are set independently.

* Every write is **read back** off the phy channel and returned with the
  measurement.  The AD9361 quantises ``rf_bandwidth`` to achievable filter
  corners, so the requested value is not the applied value, and a sweep that
  records only the request is recording a number the hardware may never have
  used.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

import sys
sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.injection import frame_offsets  # noqa: E402
from leo_tracker.radio.beacon.pilots import edge_pilot_frame  # noqa: E402

URI = "ip:192.168.1.165"
EXPECT_SERIAL = "1040007c4a94000211000b009186843ef2"

LO_HZ = 1_190_312_500
TX_OFF_DB = -89.75
RX_GAIN_DB = 40.0

#: TX baseband filter, held at ONE value for the whole sweep.  The widest block
#: this experiment transmits is 1.875 MHz of occupancy slid 1 MHz off centre,
#: i.e. 3.875 MHz two-sided, so 6 MHz clears it with margin at every cell and
#: cannot become a second, moving explanation for a change in received power.
TX_BW_HZ = 6_000_000

#: RX bandwidth used for the per-(rate, offset) reference capture that proves
#: the TX DMA actually started.  Wide, fixed, and outside the factorial's
#: narrow arms, so "the DMA is dead" and "the analog filter removed it" cannot
#: be confused: the reference is taken before the cell's own bandwidth is set.
REF_BW_HZ = 10_000_000

#: TX drive the "did the DMA start" check is taken at, independent of the drive
#: the cell is measured at.  See :meth:`Rig.load`.
VERIFY_GAIN_DB = -20.0

#: Peak-normalised DAC drive, as in ``rig165``.  A carrier offset is a phase
#: rotation, |x[n]| is untouched by it, so TX power is identical at every
#: offset in this sweep by construction -- any change in received power is the
#: receive chain, never the transmitter.
TX_AMPLITUDE = 2 ** 13

#: 12-bit converter: counts run to +/-2048, which is what dBFS is taken against.
FULL_SCALE_COUNTS = 2048.0

TX_FRAMES = 3
FRAME_RATE_HZ = 750.0

SAMPLE_RATES_HZ = (2_500_000.0, 5_000_000.0, 10_000_000.0)
BANDWIDTHS_HZ = (1_250_000.0, 2_500_000.0, 5_000_000.0, 10_000_000.0)

PROBE_MS = 40.0

#: Where the radio is left when the sweep finishes.
REST_RATE_HZ = 2_500_000.0
REST_BW_HZ = 2_500_000.0


def tx_buffer_samples(sample_rate_hz: float) -> int:
    """Length of the cyclic TX buffer: three whole frames, exact at every rate."""
    samples = TX_FRAMES * sample_rate_hz / FRAME_RATE_HZ
    if abs(samples - round(samples)) > 1e-9:
        raise ValueError(f"{sample_rate_hz} Hz does not give whole frames")
    return int(round(samples))


def probe_samples(sample_rate_hz: float, probe_ms: float = PROBE_MS) -> int:
    return int(round(sample_rate_hz * probe_ms / 1000.0))


def snap_offset_hz(offset_hz: float, sample_rate_hz: float) -> float:
    """Nearest offset whose period divides the cyclic buffer exactly.

    The bin is ``sample_rate / buffer_samples`` = 750/3 = 250 Hz at every rate
    in this sweep, so one grid serves all three arms.
    """
    bin_hz = sample_rate_hz / tx_buffer_samples(sample_rate_hz)
    return float(round(float(offset_hz) / bin_hz) * bin_hz)


def tx_waveform(sample_rate_hz: float, edge: str = "lower",
                offset_hz: float = 0.0,
                amplitude: int = TX_AMPLITUDE) -> np.ndarray:
    """One seamless cyclic TX buffer of three pilot frames at ``sample_rate_hz``."""
    count = tx_buffer_samples(sample_rate_hz)
    frame = edge_pilot_frame(sample_rate_hz, edge)
    buffer = np.zeros(count, np.complex64)
    for start in frame_offsets(sample_rate_hz, TX_FRAMES):
        start = int(start)
        if start >= count:
            continue
        stop = min(start + frame.size, count)
        buffer[start:stop] += frame[:stop - start]
    if offset_hz:
        time_s = np.arange(count) / sample_rate_hz
        buffer *= np.exp(2j * np.pi * float(offset_hz) * time_s).astype(np.complex64)
    peak = float(np.max(np.abs(buffer)))
    return (buffer / peak * amplitude).astype(np.complex64)


def power_row(values: np.ndarray) -> dict:
    """Received power three ways, from one pass over the samples."""
    magnitude = np.abs(np.asarray(values, np.complex64))
    mean_power = float(np.mean(magnitude ** 2))
    return {
        "mean_power_counts2": mean_power,
        "rms_counts": float(np.sqrt(mean_power)),
        "peak_counts": float(magnitude.max()),
        "power_dbfs": float(10.0 * np.log10(max(mean_power, 1e-30)
                                            / FULL_SCALE_COUNTS ** 2)),
    }


class Rig:
    """Open `.165`, reconfigure it per cell, and read back what it applied."""

    def __init__(self, rx_gain_db: float = RX_GAIN_DB):
        import adi
        self.sdr = adi.ad9361(uri=URI)
        attrs = dict(self.sdr.ctx.attrs)
        serial = attrs.get("hw_serial", "")
        if serial != EXPECT_SERIAL:
            raise RuntimeError(f"opened serial {serial!r}, refusing: not my radio")
        self.context_attrs = {key: str(value) for key, value in attrs.items()}
        self._tx_live = False
        self._rate_hz = None
        self._probe_samples = None
        self.sdr.rx_destroy_buffer()
        self.sdr.tx_destroy_buffer()
        self.all_tx_off()
        self._silence_dds()
        self.sdr.rx_lo = LO_HZ
        self.sdr.tx_lo = LO_HZ
        self.sdr.rx_enabled_channels = [0, 1]
        self.rx_gain_db = float(rx_gain_db)
        for channel in (0, 1):
            setattr(self.sdr, f"gain_control_mode_chan{channel}", "manual")
            setattr(self.sdr, f"rx_hardwaregain_chan{channel}", self.rx_gain_db)
        self.set_rate(SAMPLE_RATES_HZ[0])
        self._prime()

    # ---- configuration --------------------------------------------------
    def set_rate(self, sample_rate_hz: float, probe_ms: float = PROBE_MS) -> None:
        """Change the sample rate.  TX must be down: this reloads the FIR."""
        self.stop_tx()
        self.sdr.sample_rate = int(sample_rate_hz)
        self.sdr.tx_rf_bandwidth = int(TX_BW_HZ)
        self._rate_hz = float(sample_rate_hz)
        self._probe_samples = probe_samples(sample_rate_hz, probe_ms)
        try:
            self.sdr.rx_destroy_buffer()
        except Exception:                                    # noqa: BLE001
            pass
        self.sdr.rx_buffer_size = self._probe_samples

    def set_rx_bandwidth(self, bandwidth_hz: float) -> None:
        self.sdr.rx_rf_bandwidth = int(bandwidth_hz)
        try:
            self.sdr.rx_destroy_buffer()
        except Exception:                                    # noqa: BLE001
            pass
        self.sdr.rx_buffer_size = self._probe_samples
        time.sleep(0.05)

    def readback(self) -> dict:
        """What the AD9361 says it is actually doing, straight off the phy.

        Read through raw libiio rather than the pyadi properties so the value
        is the driver's, unfiltered by a python-side cache, and so the TX and
        RX sides of the same attribute can be told apart.
        """
        phy = self.sdr.ctx.find_device("ad9361-phy")
        out: dict = {}
        for channel in phy.channels:
            if channel.id not in ("voltage0", "voltage1"):
                continue
            key = ("tx" if channel.output else "rx") + channel.id[-1]
            row = {}
            for name in ("sampling_frequency", "rf_bandwidth", "filter_fir_en",
                         "hardwaregain", "gain_control_mode"):
                if name in channel.attrs:
                    try:
                        row[name] = channel.attrs[name].value
                    except OSError:
                        row[name] = None
            out[key] = row
        for name in ("rx_path_rates", "tx_path_rates"):
            try:
                out[name] = phy.attrs[name].value
            except Exception:                                # noqa: BLE001
                out[name] = None
        for channel in phy.channels:
            if channel.id == "altvoltage0" and channel.output:
                out["rx_lo_hz"] = channel.attrs["frequency"].value
            if channel.id == "altvoltage1" and channel.output:
                out["tx_lo_hz"] = channel.attrs["frequency"].value
        return out

    @staticmethod
    def applied(readback: dict) -> dict:
        """The two numbers the whole experiment turns on, as floats."""
        rx = readback.get("rx0", {})
        tx = readback.get("tx0", {})
        def number(value):
            try:
                return float(str(value).split()[0])
            except (TypeError, ValueError, IndexError):
                return None
        return {
            "sample_rate_applied_hz": number(rx.get("sampling_frequency")),
            "rx_bandwidth_applied_hz": number(rx.get("rf_bandwidth")),
            "tx_sample_rate_applied_hz": number(tx.get("sampling_frequency")),
            "tx_bandwidth_applied_hz": number(tx.get("rf_bandwidth")),
            "rx_gain_applied_db": number(rx.get("hardwaregain")),
            "rx_gain_mode": rx.get("gain_control_mode"),
            "rx_fir_en": number(rx.get("filter_fir_en")),
            "tx_fir_en": number(tx.get("filter_fir_en")),
        }

    # ---- transmit -------------------------------------------------------
    def _prime(self) -> None:
        """Burn the dead first cyclic push (see ``rig165._prime``)."""
        silence = np.zeros(tx_buffer_samples(self._rate_hz), np.complex64)
        for _ in range(2):
            self.sdr.tx_enabled_channels = [1]
            self.sdr.tx_cyclic_buffer = True
            self.all_tx_off()
            self.sdr.tx(silence)
            self._tx_live = True
            time.sleep(0.05)
            self.stop_tx()

    def all_tx_off(self) -> None:
        for channel in (0, 1):
            setattr(self.sdr, f"tx_hardwaregain_chan{channel}", TX_OFF_DB)

    def _silence_dds(self) -> None:
        try:
            device = self.sdr.ctx.find_device("cf-ad9361-dds-core-lpc")
            for channel in device.channels:
                if not channel.output:
                    continue
                for name in ("raw", "scale"):
                    if name in channel.attrs:
                        try:
                            channel.attrs[name].value = "0"
                        except OSError:
                            pass
        except Exception:                                    # noqa: BLE001
            pass

    def set_tx_gain(self, tx1_db: float = TX_OFF_DB,
                    tx2_db: float = TX_OFF_DB) -> None:
        self.sdr.tx_hardwaregain_chan0 = float(tx1_db)
        self.sdr.tx_hardwaregain_chan1 = float(tx2_db)
        time.sleep(0.05)

    def _push_both(self, samples: np.ndarray) -> None:
        self.stop_tx()
        time.sleep(0.05)
        self.sdr.tx_enabled_channels = [0, 1]
        self.sdr.tx_cyclic_buffer = True
        self.all_tx_off()
        self.sdr.tx([samples, samples])
        self._tx_live = True
        time.sleep(0.20)

    def load(self, samples: np.ndarray, tx_gain_db: float, *,
             floor_rms: float, verify_gain_db: float = VERIFY_GAIN_DB,
             attempts: int = 8) -> dict:
        """Push the waveform onto both TX DMAs and prove the port is radiating.

        The proof is taken at :data:`REF_BW_HZ`, wide open, **before** the
        cell's own RX bandwidth is applied.  That ordering is the point: a
        narrow analog filter and a dead DMA both read as the noise floor, and
        this sweep deliberately drives the filter into the region where it
        removes most of the signal.  Verifying wide leaves only one thing that
        can fail the check.

        The proof is also taken at ``verify_gain_db`` rather than at the cell's
        own drive, and only then is the gain dropped to ``tx_gain_db``.
        MEASURED: verifying at the cell drive makes the whole check impossible
        below about -55 dB, because "the port is silent" and "the signal is
        under the noise" are the same reading -- a -64 dB arm lost all 27 of its
        cells to a check that no working transmitter could have passed.  What
        the check exists to catch is a cyclic buffer that never started, and
        that is a property of the DMA, not of the gain, so it is worth proving
        once loudly and then turning the gain down.

        ``floor_rms`` is this configuration's own measured TX-off floor, so the
        bar rises and falls with the noise the bandwidth admits instead of
        being a constant that a narrow arm would fail for the wrong reason.
        """
        self.set_rx_bandwidth(REF_BW_HZ)
        bar = max(3.0 * float(floor_rms), float(floor_rms) + 3.0)
        rms = 0.0
        for attempt in range(1, attempts + 1):
            self._push_both(samples)
            self.set_tx_gain(tx2_db=verify_gain_db)
            block = self.capture(discard=2)
            rows = [power_row(np.asarray(block[receiver], np.complex64))
                    for receiver in (0, 1)]
            rms = max(row["rms_counts"] for row in rows)
            if rms > bar:
                self.set_tx_gain(tx2_db=tx_gain_db)
                return {"attempts": attempt, "verified": True,
                        "verify_rms_counts": rms, "verify_bar_counts": bar,
                        "verify_gain_db": float(verify_gain_db),
                        "tx_gain_db": float(tx_gain_db),
                        # Taken at the VERIFY gain, so it is a reference for the
                        # receive chain and NOT comparable to a cell measured at
                        # a different drive.  The analysis checks the two gains
                        # match before it prices a bandwidth against it.
                        "reference": [dict(row, receiver=index + 1)
                                      for index, row in enumerate(rows)],
                        "reference_bandwidth_hz": REF_BW_HZ,
                        "reference_readback": self.readback()}
        self.all_tx_off()
        raise RuntimeError(
            f"TX DMA did not start after {attempts} pushes at a verify gain of "
            f"{verify_gain_db:+.1f} dB "
            f"(last rms {rms:.2f} counts against a bar of {bar:.2f}); "
            "refusing to report measurements from a port that is not radiating")

    def stop_tx(self) -> None:
        self.all_tx_off()
        if self._tx_live:
            try:
                self.sdr.tx_destroy_buffer()
            except Exception:                                # noqa: BLE001
                pass
            self._tx_live = False

    # ---- receive --------------------------------------------------------
    def capture(self, discard: int = 1, attempts: int = 4) -> np.ndarray:
        """One (2, probe) complex64 block after flushing stale buffers."""
        for attempt in range(1, attempts + 1):
            try:
                for _ in range(discard):
                    self.sdr.rx()
                return np.asarray(np.asarray(self.sdr.rx()), np.complex64)
            except OSError:
                if attempt == attempts:
                    raise
                try:
                    self.sdr.rx_destroy_buffer()
                except Exception:                            # noqa: BLE001
                    pass
                time.sleep(0.5 * attempt)
                self.sdr.rx_buffer_size = self._probe_samples
        raise RuntimeError("unreachable")

    def rest(self) -> dict:
        """Leave the radio where the next user expects it."""
        self.stop_tx()
        self.set_rate(REST_RATE_HZ)
        self.set_rx_bandwidth(REST_BW_HZ)
        for channel in (0, 1):
            setattr(self.sdr, f"gain_control_mode_chan{channel}", "manual")
            setattr(self.sdr, f"rx_hardwaregain_chan{channel}", self.rx_gain_db)
        return self.readback()

    def close(self) -> dict:
        try:
            state = self.rest()
        finally:
            try:
                self.sdr.rx_destroy_buffer()
            except Exception:                                # noqa: BLE001
                pass
        return state


def appender(path: Path):
    """Append-and-flush one JSON object per call; nothing is held in memory."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(record: dict) -> dict:
        with open(path, "a") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
        return record
    return write
