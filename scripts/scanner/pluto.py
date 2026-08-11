"""Pluto+ adapter for the scanner. Importing this module does not import libiio.

Everything here exists because of a measurement; the numbers are in README.md.

* The analog bandwidth is set once and a second, differing write raises. A per-point
  bandwidth change costs ~14.3 ms against a ~1.9 ms tune-and-measure, so silently
  allowing it would make a scan ~8x slower with no warning.
* ``tune`` prefers an AD9361 fastlock recall (~0.64 ms) over a frequency write
  (~1.28 ms) when a profile has been stored. There are only 8 profiles, and from the
  host ``fastlock_load`` (~1.18 ms) is no cheaper than a retune, so profiles are only
  worth storing for tunings that repeat.
* Clipping is detected from the samples in FFT mode. The RSSI fast path transfers no
  IQ, so from the host it cannot self-detect clipping and reports ``None``: the
  AD9361's own CTRL_OUT overload flags are readable on the device but not over libiio.
* One pyadi handle owns the single libiio context and everything runs through it.
  A USB context is an exclusive claim, so opening a second one for IQ capture fails
  with EBUSY -- which is exactly what an earlier revision of this file did.
"""
from __future__ import annotations

import importlib
import math
from typing import Sequence

import numpy as np
import numpy.typing as npt

#: 12-bit signed full scale. Measured: both the libiio and SPF direct-USB paths
#: saturate at exactly 2048, so this is the correct normaliser for either.
ADC_FULL_SCALE = 2048.0

#: The AD9361 offers this many RX fastlock profiles.
FASTLOCK_SLOTS = 8


class PlutoScanRadio:
    """Receive-only scanning adapter over a persistent libiio context.

    A persistent context is essential rather than incidental: one attribute round trip
    costs ~0.5 ms, while spawning a process per operation costs ~67 ms.
    """

    def __init__(self, uri: str, *, gain_db: float = 41.0, channel: int = 0,
                 rssi_offset_db: float = 0.0, expect_serial: str | None = None):
        adi = _import_pyadi()
        self._sdr = adi.ad9361(uri=uri)
        self._ctx = self._sdr._ctx
        serial = str(self._ctx.attrs.get("hw_serial", "") or "")
        if expect_serial and serial != expect_serial:
            raise RuntimeError(
                f"{uri} is serial {serial[:12] or '?'}, expected {expect_serial[:12]}. "
                "USB addresses and DHCP leases both move across a firmware load, so "
                "always resolve a radio by serial."
            )
        self.serial = serial
        self._fw_version = str(self._ctx.attrs.get("fw_version", "") or "")
        self._uri = uri
        self._phy = self._sdr._ctrl
        self._lo = self._channel("altvoltage0", output=True)
        self._rx = self._channel(f"voltage{int(channel)}", output=False)
        self._channel_index = int(channel)
        self.gain_db = float(gain_db)
        self.rssi_offset_db = float(rssi_offset_db)
        self._configured: tuple[float, float] | None = None
        self._rx_channels: list[int] | None = None
        self._rx_buffer_size: int | None = None
        self._profiles: dict[int, int] = {}
        self._last_capture_clipped: bool | None = None

    # ------------------------------------------------------------------ plumbing
    def _channel(self, ident: str, *, output: bool):
        found = self._phy.find_channel(ident, output)
        if found is None:
            raise RuntimeError(
                f"ad9361-phy has no {'output' if output else 'input'} {ident}")
        return found

    def _write(self, channel, name: str, value) -> None:
        channel.attrs[name].value = str(value)

    def _read(self, channel, name: str) -> str:
        return channel.attrs[name].value

    # ------------------------------------------------------------- ScanRadio API
    def configure(self, *, sample_rate_hz: float, analog_bandwidth_hz: float) -> None:
        wanted = (float(sample_rate_hz), float(analog_bandwidth_hz))
        if self._configured is not None:
            if self._configured != wanted:
                raise RuntimeError(
                    "refusing to change sample rate or analog bandwidth mid-scan: "
                    f"{self._configured} -> {wanted}. An rf_bandwidth change triggers an "
                    "AD9361 baseband filter recalibration costing ~14.3 ms, which is "
                    "~7.6x a whole tune-and-measure. Plan one bandwidth for the scan and "
                    "synthesise per-point bandwidths digitally."
                )
            return
        self._write(self._rx, "gain_control_mode", "manual")
        self._write(self._rx, "hardwaregain", int(round(self.gain_db)))
        self._write(self._rx, "sampling_frequency", int(round(sample_rate_hz)))
        self._write(self._rx, "rf_bandwidth", int(round(analog_bandwidth_hz)))
        self._configured = wanted

    def tune(self, center_hz: float) -> None:
        slot = self._profiles.get(int(round(center_hz)))
        if slot is not None:
            self._write(self._lo, "fastlock_recall", slot)
        else:
            self._write(self._lo, "frequency", int(round(center_hz)))

    def read_power_dbfs(self) -> float:
        """Total in-band power, from RSSI, with no IQ transferred.

        RSSI is reported by the AD9361 as dB below full scale referred to the input, so
        the sign is inverted here to match the dBFS convention. ``rssi_offset_db`` is
        the uncalibrated difference between this scale and the FFT path's; see
        :meth:`calibrate_rssi_offset`.
        """
        raw = self._read(self._rx, "rssi").split()[0]
        return -float(raw) + self.rssi_offset_db

    def capture(self, sample_count: int) -> npt.NDArray[np.complexfloating]:
        rx = self._sdr
        if self._rx_channels != [self._channel_index]:
            rx.rx_destroy_buffer()
            rx.rx_enabled_channels = [self._channel_index]
            self._rx_channels = [self._channel_index]
        if self._rx_buffer_size != int(sample_count):
            rx.rx_destroy_buffer()
            rx.rx_buffer_size = int(sample_count)
            self._rx_buffer_size = int(sample_count)
        raw = np.asarray(rx.rx())
        if raw.ndim > 1:
            raw = raw[0]
        peak = float(np.max(np.abs(np.concatenate([raw.real, raw.imag])))) if raw.size else 0.0
        self._last_capture_clipped = peak >= ADC_FULL_SCALE - 1
        return raw.astype(np.complex128) / ADC_FULL_SCALE

    def overload(self) -> bool | None:
        return self._last_capture_clipped

    # ------------------------------------------------------------- extras
    def prepare_fastlock(self, tunings: Sequence[float]) -> dict[int, int]:
        """Store up to 8 tunings as fastlock profiles; later tunes recall them.

        Only worth doing for tunings that repeat, and only when the dwell is short:
        above ~2 ms of dwell a recall and a retune cost the same because the dwell
        dominates.
        """
        unique: list[int] = []
        for hz in tunings:
            key = int(round(hz))
            if key not in unique:
                unique.append(key)
        self._profiles = {}
        for slot, hz in enumerate(unique[:FASTLOCK_SLOTS]):
            self._write(self._lo, "frequency", hz)
            self._write(self._lo, "fastlock_store", slot)
            self._profiles[hz] = slot
        return dict(self._profiles)

    def calibrate_rssi_offset(self, *, sample_count: int = 8192) -> float:
        """Measure the offset between the RSSI scale and the FFT scale, in dB.

        Both are read at the current tuning with the same gain, so the difference is
        the chip's RSSI reference minus this tool's full-scale convention. Without this
        the two paths are each self-consistent but not mutually comparable.
        """
        from .execute import band_power_dbfs

        if self._configured is None:
            raise RuntimeError("configure() before calibrating")
        sample_rate, bandwidth = self._configured
        centre = float(self._read(self._lo, "frequency"))
        samples = self.capture(sample_count)
        reference, _, _ = band_power_dbfs(
            samples, sample_rate_hz=sample_rate, tune_hz=centre, center_hz=centre,
            bandwidth_hz=min(bandwidth, sample_rate * 0.8))
        self.rssi_offset_db = 0.0
        raw = self.read_power_dbfs()
        if not math.isfinite(reference) or not math.isfinite(raw):
            raise RuntimeError("cannot calibrate against a non-finite power")
        self.rssi_offset_db = reference - raw
        return self.rssi_offset_db

    def close(self) -> None:
        """Release the radio. A USB context is an exclusive claim on the interface, so
        a leaked one makes the next open fail with EBUSY."""
        sdr = self.__dict__.pop("_sdr", None)
        if sdr is not None:
            try:
                sdr.rx_destroy_buffer()
            except Exception:  # pragma: no cover - best effort on teardown
                pass
        self._ctx = None
        self._phy = None
        self._lo = None
        self._rx = None

    def __enter__(self) -> "PlutoScanRadio":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def identity(self) -> dict[str, object]:
        return {
            "serial": self.serial,
            "fw_version": self._fw_version,
            "configured": self._configured,
            "fastlock_profiles": dict(self._profiles),
            "gain_db": self.gain_db,
            "rssi_offset_db": self.rssi_offset_db,
        }


def _import_pyadi():
    try:
        return importlib.import_module("adi")
    except ImportError as exc:  # pragma: no cover - hardware only
        raise ImportError(
            "IQ capture requires pyadi-iio; install the 'hardware' extra"
        ) from exc
