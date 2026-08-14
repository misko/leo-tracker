"""Two Pluto rigs, one process, cabled loopback TX2 -> splitter -> 2x30dB -> RX1/RX2.

Channel map (verified on both, per the brief):
  ad9361-phy   TX1=voltage0 TX2=voltage1 (output)   RX1=voltage0 RX2=voltage1 (input)
  TX DMA cf-ad9361-dds-core-lpc  TX1=voltage0/1  TX2=voltage2/3
  RX DMA cf-ad9361-lpc           RX1=voltage0/1  RX2=voltage2/3

The cable is on TX2.  Driving TX1's DMA returns the bare noise floor with no
error, so the DMA channel indices here are asserted at bring-up, not assumed.
"""
from __future__ import annotations

import signal
import time

import iio
import numpy as np

LO_HZ = 1_190_312_500
FS_HZ = 5_000_000
RF_BW_HZ = 5_000_000
RX_GAIN_DB = 40.0
TX_IDLE_DB = -89.75          # the mandated parked state
FULL_SCALE = 32767.0


def _chan(dev, name, output):
    ch = dev.find_channel(name, output)
    if ch is None:
        raise RuntimeError(f"{dev.name}: no channel {name} output={output}")
    return ch


def _num(text) -> float:
    """First float in an iio attribute string ('40.000000 dB' -> 40.0)."""
    return float(str(text).split()[0])


def _attr(ch, name, value=None):
    if value is None:
        return ch.attrs[name].value
    ch.attrs[name].value = str(value)
    return ch.attrs[name].value


class Rig:
    """One radio.  TX2 is the driven port; RX1 is the scored port."""

    def __init__(self, uri: str, label: str):
        self.uri, self.label = uri, label
        self.ctx = iio.Context(uri)
        self.phy = self.ctx.find_device("ad9361-phy")
        self.txdma = self.ctx.find_device("cf-ad9361-dds-core-lpc")
        self.rxdma = self.ctx.find_device("cf-ad9361-lpc")
        if not all((self.phy, self.txdma, self.rxdma)):
            raise RuntimeError(f"{label}: missing an expected iio device")
        self.tx2 = _chan(self.phy, "voltage1", True)     # TX2 on the phy
        self.tx1 = _chan(self.phy, "voltage0", True)     # TX1, parked only
        self.rx1 = _chan(self.phy, "voltage0", False)    # RX1 on the phy
        self.rx2 = _chan(self.phy, "voltage1", False)
        self._txbuf = None
        self._rxbuf = None
        self._rx_n = None
        self.serial = self.ctx.attrs.get("hw_serial", "?")

    # -- bring-up ---------------------------------------------------------
    def park_tx(self):
        """Both TX chains to the idle attenuation.  Safe to call any time."""
        for ch in (self.tx1, self.tx2):
            try:
                _attr(ch, "hardwaregain", f"{TX_IDLE_DB:.2f}")
            except Exception:
                pass

    def configure(self):
        self.park_tx()
        _attr(self.phy.find_channel("altvoltage0", True), "frequency", LO_HZ)  # RX LO
        _attr(self.phy.find_channel("altvoltage1", True), "frequency", LO_HZ)  # TX LO
        for ch in (self.rx1, self.rx2):
            _attr(ch, "rf_bandwidth", RF_BW_HZ)
            _attr(ch, "gain_control_mode", "manual")
            _attr(ch, "hardwaregain", RX_GAIN_DB)
        for ch in (self.tx1, self.tx2):
            _attr(ch, "rf_bandwidth", RF_BW_HZ)
        _attr(self.rx1, "sampling_frequency", FS_HZ)
        # silence every DDS tone so the only TX energy is our DMA buffer
        for ch in self.txdma.channels:
            if ch.id.startswith("altvoltage"):
                for key in ("raw", "scale"):
                    if key in ch.attrs:
                        try:
                            _attr(ch, key, 0)
                        except Exception:
                            pass
        return self

    def state(self) -> dict:
        return {"uri": self.uri, "label": self.label, "serial": self.serial,
                "rx_lo_hz": _num(_attr(self.phy.find_channel("altvoltage0", True), "frequency")),
                "tx_lo_hz": _num(_attr(self.phy.find_channel("altvoltage1", True), "frequency")),
                "fs_hz": _num(_attr(self.rx1, "sampling_frequency")),
                "rx_bw_hz": _num(_attr(self.rx1, "rf_bandwidth")),
                "rx1_gain_db": _num(_attr(self.rx1, "hardwaregain")),
                "rx1_mode": _attr(self.rx1, "gain_control_mode"),
                "tx2_gain_db": _num(_attr(self.tx2, "hardwaregain")),
                "tx1_gain_db": _num(_attr(self.tx1, "hardwaregain"))}

    # -- TX ---------------------------------------------------------------
    def tx_gain(self, db: float):
        _attr(self.tx2, "hardwaregain", f"{float(db):.2f}")

    def tx_gain_read(self) -> float:
        return _num(_attr(self.tx2, "hardwaregain"))

    def start_tx(self, waveform: np.ndarray, drive: float):
        """Push ``waveform`` cyclically on TX2's DMA (voltage2/3).

        ``drive`` is peak digital amplitude as a fraction of full scale.
        """
        self.stop_tx()
        i_ch = _chan(self.txdma, "voltage2", True)   # TX2 I
        q_ch = _chan(self.txdma, "voltage3", True)   # TX2 Q
        for ch in self.txdma.channels:
            if ch.id.startswith("voltage"):
                ch.enabled = ch.id in ("voltage2", "voltage3")
        peak = float(np.max(np.abs(waveform)))
        scaled = waveform / peak * (drive * FULL_SCALE)
        interleaved = np.empty(scaled.size * 2, dtype=np.int16)
        interleaved[0::2] = np.rint(scaled.real).astype(np.int16)
        interleaved[1::2] = np.rint(scaled.imag).astype(np.int16)
        self._txbuf = iio.Buffer(self.txdma, scaled.size, True)  # cyclic
        self._txbuf.write(bytearray(interleaved.tobytes()))
        self._txbuf.push()
        self._tx_drive = drive
        return self

    def stop_tx(self):
        if self._txbuf is not None:
            try:
                del self._txbuf
            except Exception:
                pass
            self._txbuf = None

    # -- RX ---------------------------------------------------------------
    def open_rx(self, n: int):
        self.close_rx()
        for ch in self.rxdma.channels:
            if ch.id.startswith("voltage"):
                ch.enabled = ch.id in ("voltage0", "voltage1")   # RX1 I/Q
        self._rxbuf = iio.Buffer(self.rxdma, int(n), False)
        self._rx_n = int(n)
        return self

    def close_rx(self):
        if self._rxbuf is not None:
            try:
                del self._rxbuf
            except Exception:
                pass
            self._rxbuf = None

    def capture(self, retries: int = 4) -> np.ndarray:
        """One RX1 probe as complex64.  Rebuilds the buffer on BrokenPipeError."""
        last = None
        for attempt in range(retries):
            try:
                self._rxbuf.refill()
                raw = np.frombuffer(self._rxbuf.read(), dtype=np.int16)
                return (raw[0::2].astype(np.float32)
                        + 1j * raw[1::2].astype(np.float32)).astype(np.complex64)
            except Exception as exc:            # BrokenPipeError and friends
                last = exc
                time.sleep(0.05 * (attempt + 1))
                self.open_rx(self._rx_n)
        raise RuntimeError(f"{self.label}: capture failed after {retries}: {last!r}")

    def close(self):
        self.stop_tx()
        self.close_rx()
        self.park_tx()


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.abs(x) ** 2)))


def cyclic_pilot_buffer(frame: np.ndarray, sample_rate_hz: float,
                        frame_rate_hz: float = 750.0) -> np.ndarray:
    """A cyclic TX buffer that repeats at the true, non-integer frame period.

    At 5 MS/s a frame is 6666.667 samples, so a one-frame cyclic buffer of 6667
    samples runs 0.33 samples fast and smears the detector's fold by 10 samples
    over a 40 ms probe -- measured as a drop from 0.94 to 0.76 in every score.
    Three frames span exactly 20000 samples and their starts, rint(j*period) =
    0 / 6667 / 13333, are the same integers the detectors lay their own frame
    templates on, so this buffer stays locked to the fold forever.
    """
    period = float(sample_rate_hz) / float(frame_rate_hz)
    span = period
    count = 1
    while abs(span - round(span)) > 1e-9 and count < 64:
        count += 1
        span = count * period
    if abs(span - round(span)) > 1e-9:
        raise ValueError("no small whole number of frames lands on a sample")
    buffer = np.zeros(int(round(span)), dtype=np.complex64)
    for index in range(count):
        start = int(round(index * period))
        chunk = frame[:min(frame.size, buffer.size - start)]
        buffer[start:start + chunk.size] = chunk
    return buffer


def guard_signals():
    """Turn SIGTERM/SIGINT into SystemExit so every ``finally`` still runs.

    A plain SIGTERM tears the interpreter down without unwinding, which would
    leave a TX chain hot at whatever attenuation the run was using.  Every
    entry point here installs this before it opens a radio.
    """
    def _bail(signum, _frame):
        raise SystemExit(f"terminated by signal {signum}")
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _bail)
        except Exception:
            pass
