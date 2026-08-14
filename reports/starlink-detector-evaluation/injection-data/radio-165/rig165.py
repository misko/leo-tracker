"""Shared rig control for radio .165.  TX always returns to -89.75 dB.

Everything here is written so that a libiio teardown segfault cannot cost data:
callers append one JSON object per measurement to a .jsonl as they go.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.injection import frame_offsets  # noqa: E402
from leo_tracker.radio.beacon.pilots import edge_pilot_frame  # noqa: E402

URI = "ip:192.168.1.165"
EXPECT_SERIAL = "1040007c4a94000211000b009186843ef2"

LO_HZ = 1_190_312_500
FS_HZ = 5_000_000
BW_HZ = 5_000_000
TX_OFF_DB = -89.75
RX_GAIN_DB = 40.0

#: 3 frames = 20,000 samples at 5 MS/s exactly (period 20000/3), so a cyclic
#: TX buffer of this length repeats at the TRUE 750 Hz frame rate forever with
#: no wrap discontinuity.  A naive 6,667-sample loop would run at 749.96 Hz and
#: drift ~10 samples across a 40 ms probe -- half a symbol -- quietly spoiling
#: the fold the detectors depend on.
TX_FRAMES = 3
TX_BUFFER_SAMPLES = 20_000

#: 80 ms at 5 MS/s -- deliberately the sky corpus's own ``80ms-5.00MSps`` arm.
#: Matching rate AND length matters because ``cross_radio.threshold_key`` is
#: ``(sample_rate_hz, probe_ms)``: a threshold drawn at one length is not a
#: threshold at another (a clean null reaches p99 1.310 / 1.189 / 1.137 at
#: 20 / 40 / 80 ms), so matching the arm is what lets this radio's empty-channel
#: rates be read against sky numbers at all.
PROBE_SAMPLES = 400_000
PROBE_MS = 80.0
ARM_NAME = "80ms-5.00MSps"

OUT = Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
           "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/inject165")


def tx_waveform(edge: str = "lower", offset_hz: float = 0.0,
                amplitude: int = 2 ** 13) -> np.ndarray:
    """One seamless cyclic TX buffer: 3 pilot frames at the exact frame period.

    ``offset_hz`` multiplies by exp(2j*pi*f*t) over the WHOLE buffer, and the
    buffer is an integer number of cycles only when f is a multiple of 250 Hz
    (5e6/20000).  Callers sweeping offsets must snap to that grid or the wrap
    introduces a phase step; :func:`snap_offset_hz` does it.
    """
    frame = edge_pilot_frame(FS_HZ, edge)
    buffer = np.zeros(TX_BUFFER_SAMPLES, np.complex64)
    for start in frame_offsets(FS_HZ, TX_FRAMES):
        stop = min(int(start) + frame.size, TX_BUFFER_SAMPLES)
        buffer[int(start):stop] += frame[:stop - int(start)]
    if offset_hz:
        time_s = np.arange(TX_BUFFER_SAMPLES) / FS_HZ
        buffer *= np.exp(2j * np.pi * float(offset_hz) * time_s).astype(np.complex64)
    peak = float(np.max(np.abs(buffer)))
    return (buffer / peak * amplitude).astype(np.complex64)


def snap_offset_hz(offset_hz: float) -> float:
    """Nearest offset whose period divides the cyclic buffer exactly."""
    bin_hz = FS_HZ / TX_BUFFER_SAMPLES          # 250 Hz
    return float(round(float(offset_hz) / bin_hz) * bin_hz)


class Rig:
    """Open .165, hold it configured, and guarantee TX is off on the way out."""

    def __init__(self, rx_gain_db: float = RX_GAIN_DB):
        import adi
        self.sdr = adi.ad9361(uri=URI)
        serial = dict(self.sdr.ctx.attrs).get("hw_serial", "")
        if serial != EXPECT_SERIAL:
            raise RuntimeError(f"opened serial {serial!r}, refusing: not my radio")
        self._tx_live = False
        self.sdr.rx_destroy_buffer()
        self.sdr.tx_destroy_buffer()
        self.all_tx_off()
        self._silence_dds()
        self.sdr.sample_rate = FS_HZ
        self.sdr.rx_rf_bandwidth = BW_HZ
        self.sdr.tx_rf_bandwidth = BW_HZ
        self.sdr.rx_lo = LO_HZ
        self.sdr.tx_lo = LO_HZ
        self.sdr.rx_enabled_channels = [0, 1]
        for channel in (0, 1):
            setattr(self.sdr, f"gain_control_mode_chan{channel}", "manual")
            setattr(self.sdr, f"rx_hardwaregain_chan{channel}", float(rx_gain_db))
        self.sdr.rx_buffer_size = PROBE_SAMPLES
        self.rx_gain_db = float(rx_gain_db)
        self._prime()

    def _prime(self) -> None:
        """Burn the dead first cyclic push.

        MEASURED on this radio: the first cyclic buffer pushed after a context
        is opened never starts the DMA -- the port reads 1.42 counts, exactly
        the noise floor, while ``hardwaregain`` reads back the value asked for
        and ``tx_cyclic_buffer`` reads True.  Every later push works (80.2,
        80.3, 81.4, 80.5, 80.5 counts over five repeats).  A run that does not
        absorb this reports its first TX point as a dead port, which is how a
        real path came within one script of being written up as absent.
        """
        silence = np.zeros(TX_BUFFER_SAMPLES, np.complex64)
        for _ in range(2):
            self.sdr.tx_enabled_channels = [1]
            self.sdr.tx_cyclic_buffer = True
            self.all_tx_off()
            self.sdr.tx(silence)
            self._tx_live = True
            time.sleep(0.05)
            self.stop_tx()

    # ---- transmit -------------------------------------------------------
    def all_tx_off(self) -> None:
        for channel in (0, 1):
            setattr(self.sdr, f"tx_hardwaregain_chan{channel}", TX_OFF_DB)

    def _silence_dds(self) -> None:
        """Zero every DDS tone so an idle TX port is genuinely idle."""
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
        """Choose which port radiates.  Does not touch the DMA."""
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

    def load(self, samples: np.ndarray, *, verify_gain_db: float = -20.0,
             verify_floor: float = 8.0, attempts: int = 8) -> dict:
        """Load one waveform onto BOTH TX DMA channels and prove it started.

        Both ports carry the same samples and the *gain* decides which one
        radiates, so selecting a port never touches the DMA.  That matters
        because pushing a cyclic buffer on this radio silently fails to start
        perhaps a third of the time -- the port then reads the bare noise floor
        while ``hardwaregain`` reads back what was asked, ``tx_cyclic_buffer``
        reads True, and nothing raises.  An entire TX sweep was recorded as a
        dead port that way before it was caught, which is the same trap the
        brief warns about, reached through the DMA rather than the channel map.

        The check drives TX2 -- the port T1 measured as the connected one -- and
        requires the received rms to rise.  rms is the right probe because it is
        blind to the thing under test: it rises whenever energy arrives,
        regardless of frequency offset or whether any detector would match it.
        """
        for attempt in range(1, attempts + 1):
            self._push_both(samples)
            self.set_tx_gain(tx2_db=verify_gain_db)
            block = self.capture(discard=2)
            rms = max(float(np.sqrt(np.mean(np.abs(np.asarray(block[r])) ** 2)))
                      for r in (0, 1))
            if rms > verify_floor:
                self.all_tx_off()
                return {"attempts": attempt, "verified": True, "verify_rms": rms}
        self.all_tx_off()
        raise RuntimeError(
            f"TX DMA did not start after {attempts} pushes (last rms {rms:.2f}); "
            "refusing to report measurements from a port that is not radiating")

    def stop_tx(self) -> None:
        self.all_tx_off()
        if self._tx_live:
            self.sdr.tx_destroy_buffer()
            self._tx_live = False

    # ---- receive --------------------------------------------------------
    def capture(self, discard: int = 2, attempts: int = 4) -> np.ndarray:
        """One (2, PROBE_SAMPLES) complex64 block after flushing stale buffers.

        Retries a broken pipe.  MEASURED: a ~30 minute run took a transient
        ``BrokenPipeError`` out of ``iio_buffer_refill`` at the 310th capture
        and lost the remaining probes.  The link recovers immediately once the
        RX buffer is rebuilt, so the only thing that failure needed to cost was
        one probe rather than the tail of the run.
        """
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
                self.sdr.rx_buffer_size = PROBE_SAMPLES
        raise RuntimeError("unreachable")

    def close(self) -> None:
        try:
            self.stop_tx()
        finally:
            try:
                self.sdr.rx_destroy_buffer()
            except Exception:                                # noqa: BLE001
                pass


def appender(path: Path):
    """Append-and-flush one JSON object per call; nothing is held in memory."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(record: dict) -> dict:
        with open(path, "a") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
        return record
    return write
