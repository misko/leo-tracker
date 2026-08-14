"""Cabled-loopback PlutoSDR rig: TX2 -> tee -> 30 dB pads -> RX1 + RX2.

Only ip:192.168.1.183 (serial 104000bac495...) is ever opened.  The capture
radios are never named here.

Channel map, from the bench notes and confirmed by iio_info:
  ad9361-phy            TX1=voltage0(out) TX2=voltage1(out)
                        RX1=voltage0(in)  RX2=voltage1(in)
  cf-ad9361-dds-core-lpc  TX1=voltage0/1   TX2=voltage2/3   <- cable is on TX2
  cf-ad9361-lpc           RX1=voltage0/1   RX2=voltage2/3
"""
from __future__ import annotations

import time

import numpy as np
import iio

URI = "ip:192.168.1.183"
EXPECTED_SERIAL_PREFIX = "104000bac495"

TX_OFF_DB = -89.75          # AD9361 minimum, the "transmitter is off" rung
RX_PEAK_CEILING = 1500.0    # 12-bit full scale is 2048; stay well under
FS_HZ = 5_000_000.0
LO_HZ = 1_190_312_500.0
RF_BW_HZ = 5_000_000.0
RX_GAIN_DB = 40.0

# 3 Starlink frames at 5 MS/s is exactly 20000 samples (5e6 / 750 * 3), so a
# cyclic buffer of that length repeats at precisely the 750 Hz frame rate the
# detectors fold at.  One frame (6667 samples) would drift 1/3 sample a frame.
TX_FRAMES = 3
TX_LEN = 20_000
CYCLIC_PERIOD_S = TX_LEN / FS_HZ           # 4 ms -> offsets must be n * 250 Hz
CYCLIC_FREQ_STEP_HZ = 1.0 / CYCLIC_PERIOD_S


def snap_offset_hz(hz: float) -> float:
    """Nearest offset with an integer number of cycles per cyclic TX buffer."""
    return round(float(hz) / CYCLIC_FREQ_STEP_HZ) * CYCLIC_FREQ_STEP_HZ


#: Digital drive, as a fraction of DAC full scale at the waveform's peak.
#: The bench notes fixed the analog settings but never the digital amplitude,
#: and at 0.9 the rig came out 9.4 dB hot: RX rms 246.8 counts at TX2 -20 dB
#: against the noted 83.6.  0.3048 = 0.9 * 83.6 / 246.84 puts the received level
#: on the noted figure, which is the level every safety margin in those notes
#: was established at.
AMPLITUDE = 0.3048


def pilot_stream(sample_rate_hz: float = FS_HZ, edge: str = "lower",
                 offset_hz: float = 0.0, amplitude: float = AMPLITUDE) -> np.ndarray:
    """TX_LEN samples of back-to-back pilot frames, optionally offset in Hz.

    The frame comes from the repository (``edge_pilot_frame``) -- the exact
    signal the eight detectors hunt -- and is laid down at the true fractional
    frame period rather than at its own rounded length.
    """
    from leo_tracker.radio.beacon.pilots import edge_pilot_frame
    from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S

    frame = np.asarray(edge_pilot_frame(sample_rate_hz, edge), np.complex128)
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    out = np.zeros(TX_LEN, np.complex128)
    for k in range(TX_FRAMES):
        start = int(round(k * period))
        stop = min(TX_LEN, start + frame.size)
        out[start:stop] += frame[:stop - start]
    if offset_hz:
        t = np.arange(TX_LEN) / sample_rate_hz
        out *= np.exp(2j * np.pi * float(offset_hz) * t)
    peak = np.abs(out).max()
    if peak > 0:
        out *= amplitude * 32767.0 / peak
    return out


#: One process at a time may drive this radio.  Two overlapping runs do not
#: merely collide on the DMA buffers -- each one's cleanup sets TX2 back to
#: -89.75 dB while the other is mid-ladder, so the second run silently records
#: the first run's gain.  That happened once here; the lock is why it cannot
#: happen twice.
LOCK_PATH = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/inject/.rig-183.lock"


class Rig:
    def __init__(self, uri: str = URI):
        import fcntl
        self._lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError(
                "another process already holds the bench radio; refusing to "
                "drive it concurrently") from None
        self._lock.write(f"{__import__('os').getpid()}\n")
        self._lock.flush()
        self.ctx = iio.Context(uri)
        serial = self.ctx.attrs["hw_serial"]
        if not serial.startswith(EXPECTED_SERIAL_PREFIX):
            raise RuntimeError(f"refusing to drive {serial!r}: not the bench radio")
        self.serial = serial
        self.phy = self.ctx.find_device("ad9361-phy")
        self.txdev = self.ctx.find_device("cf-ad9361-dds-core-lpc")
        self.rxdev = self.ctx.find_device("cf-ad9361-lpc")
        self.txbuf = None
        self.rxbuf = None
        self._tx_channels = []
        self._rx_channels = []

    # -- attribute helpers -------------------------------------------------
    def _phy(self, name: str, output: bool):
        chan = self.phy.find_channel(name, output)
        if chan is None:
            raise RuntimeError(f"no phy channel {name} output={output}")
        return chan

    def _set(self, chan, attr: str, value) -> None:
        chan.attrs[attr].value = str(value)

    def _get(self, chan, attr: str) -> str:
        return chan.attrs[attr].value

    # -- configuration -----------------------------------------------------
    def configure(self, *, fs_hz=FS_HZ, lo_hz=LO_HZ, rf_bw_hz=RF_BW_HZ,
                  rx_gain_db=RX_GAIN_DB) -> dict:
        tx1 = self._phy("voltage0", True)
        tx2 = self._phy("voltage1", True)
        rx1 = self._phy("voltage0", False)
        rx2 = self._phy("voltage1", False)

        # TX1 has no cable on it and must stay silent regardless.
        self._set(tx1, "hardwaregain", TX_OFF_DB)
        self._set(tx2, "hardwaregain", TX_OFF_DB)

        for chan in (tx1, tx2, rx1, rx2):
            self._set(chan, "sampling_frequency", int(fs_hz))
            self._set(chan, "rf_bandwidth", int(rf_bw_hz))
        self._set(self._phy("altvoltage1", True), "frequency", int(lo_hz))  # TX LO
        self._set(self._phy("altvoltage0", True), "frequency", int(lo_hz))  # RX LO

        for chan in (rx1, rx2):
            self._set(chan, "gain_control_mode", "manual")
            self._set(chan, "hardwaregain", rx_gain_db)
            self._set(chan, "quadrature_tracking_en", 1)
            self._set(chan, "rf_dc_offset_tracking_en", 1)
            self._set(chan, "bb_dc_offset_tracking_en", 1)

        # Silence the internal DDS tones so the DMA path owns the DAC.
        for name in ("altvoltage0", "altvoltage1", "altvoltage2", "altvoltage3",
                     "altvoltage4", "altvoltage5", "altvoltage6", "altvoltage7"):
            chan = self.txdev.find_channel(name, True)
            if chan is None:
                continue
            for attr, value in (("raw", 0), ("scale", 0.0), ("frequency", 0)):
                if attr in chan.attrs:
                    try:
                        self._set(chan, attr, value)
                    except OSError:
                        pass
        return self.state()

    def state(self) -> dict:
        tx1 = self._phy("voltage0", True)
        tx2 = self._phy("voltage1", True)
        rx1 = self._phy("voltage0", False)
        rx2 = self._phy("voltage1", False)
        return {
            "serial": self.serial,
            "tx1_gain_db": float(self._get(tx1, "hardwaregain").split()[0]),
            "tx2_gain_db": float(self._get(tx2, "hardwaregain").split()[0]),
            "rx1_gain_db": float(self._get(rx1, "hardwaregain").split()[0]),
            "rx2_gain_db": float(self._get(rx2, "hardwaregain").split()[0]),
            "rx_gain_mode": self._get(rx1, "gain_control_mode"),
            "sample_rate_hz": float(self._get(rx1, "sampling_frequency")),
            "rf_bandwidth_hz": float(self._get(rx1, "rf_bandwidth")),
            "tx_lo_hz": float(self._get(self._phy("altvoltage1", True), "frequency")),
            "rx_lo_hz": float(self._get(self._phy("altvoltage0", True), "frequency")),
        }

    def set_tx2_gain(self, db: float) -> float:
        db = float(np.clip(db, TX_OFF_DB, 0.0))
        self._set(self._phy("voltage1", True), "hardwaregain", db)
        return db

    def tx_off(self) -> None:
        for name in ("voltage0", "voltage1"):
            try:
                self._set(self._phy(name, True), "hardwaregain", TX_OFF_DB)
            except Exception:
                pass

    # -- streaming ---------------------------------------------------------
    def start_tx(self, waveform: np.ndarray) -> None:
        """Push one cyclic buffer on TX2 (DMA voltage2/voltage3)."""
        self.stop_tx()
        i_chan = self.txdev.find_channel("voltage2", True)
        q_chan = self.txdev.find_channel("voltage3", True)
        if i_chan is None or q_chan is None:
            raise RuntimeError("TX2 DMA channels voltage2/voltage3 not found")
        for chan in self.txdev.channels:
            if chan.scan_element and chan.output:
                chan.enabled = chan.id in ("voltage2", "voltage3")
        self._tx_channels = [i_chan, q_chan]
        data = np.asarray(waveform)
        interleaved = np.empty(2 * data.size, np.int16)
        interleaved[0::2] = np.clip(np.rint(data.real), -32768, 32767)
        interleaved[1::2] = np.clip(np.rint(data.imag), -32768, 32767)
        self.txbuf = iio.Buffer(self.txdev, data.size, True)
        self.txbuf.write(bytearray(interleaved.tobytes()))
        self.txbuf.push()

    def stop_tx(self) -> None:
        if self.txbuf is not None:
            try:
                self.txbuf.cancel()
            except Exception:
                pass
            self.txbuf = None
        for chan in self._tx_channels:
            try:
                chan.enabled = False
            except Exception:
                pass
        self._tx_channels = []

    def open_rx(self, samples: int) -> None:
        self.close_rx()
        chans = [self.rxdev.find_channel(name, False)
                 for name in ("voltage0", "voltage1", "voltage2", "voltage3")]
        if any(c is None for c in chans):
            raise RuntimeError("RX DMA channels not found")
        for chan in self.rxdev.channels:
            if chan.scan_element and not chan.output:
                chan.enabled = True
        self._rx_channels = chans
        self.rxbuf = iio.Buffer(self.rxdev, samples, False)
        self._rx_samples = samples

    def close_rx(self) -> None:
        if self.rxbuf is not None:
            try:
                self.rxbuf.cancel()
            except Exception:
                pass
            self.rxbuf = None
        for chan in self._rx_channels:
            try:
                chan.enabled = False
            except Exception:
                pass
        self._rx_channels = []

    def capture(self) -> tuple[np.ndarray, np.ndarray]:
        """One refill -> (rx1, rx2) as complex64 in raw ADC counts."""
        self.rxbuf.refill()
        out = []
        for i_chan, q_chan in ((self._rx_channels[0], self._rx_channels[1]),
                               (self._rx_channels[2], self._rx_channels[3])):
            i = np.frombuffer(i_chan.read(self.rxbuf), np.int16).astype(np.float32)
            q = np.frombuffer(q_chan.read(self.rxbuf), np.int16).astype(np.float32)
            out.append((i + 1j * q).astype(np.complex64))
        return out[0], out[1]

    def flush_rx(self, count: int = 2) -> None:
        for _ in range(count):
            self.rxbuf.refill()

    def close(self) -> None:
        self.tx_off()
        self.stop_tx()
        self.close_rx()
        lock = getattr(self, "_lock", None)
        if lock is not None:
            try:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            except Exception:
                pass
            self._lock = None


def levels(samples: np.ndarray) -> dict:
    mag = np.abs(samples)
    return {"rms": float(np.sqrt(np.mean(mag ** 2))),
            "peak": float(mag.max()),
            "peak_iq": float(max(np.abs(samples.real).max(),
                                 np.abs(samples.imag).max())),
            "mean_power": float(np.mean(mag ** 2))}
