"""Optional SPF Pluto+ adapter. Importing this module does not import SPF."""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
import importlib
import time
import numpy as np

from .source import RadioConfig, SampleBlock
from .paired import PairedSampleBlock


class PlutoSource:
    def __init__(self, config: RadioConfig, *, uri: str, block_size: int = 65536,
                 transport: str = "iio", serial: str | None = None,
                 device_factory: Callable[..., object] | None = None):
        self._config, self._block_size, self._closed = config, block_size, False
        if device_factory is None:
            try:
                module = importlib.import_module("spf.sdrpluto.sdr_controller")
            except ModuleNotFoundError:
                module = None
            if module is not None:
                rx_config = module.ReceiverConfig(
                    lo=round(config.center_frequency_hz), rf_bandwidth=round(config.bandwidth_hz),
                    sample_rate=round(config.sample_rate_hz), intermediate=0, uri=uri,
                    buffer_size=block_size, gains=[config.gain_db or 0], enabled_channels=[config.channel],
                    rx_transport=transport, direct_usb_serial=serial)
                self._device = module.PPlus(uri=uri, rx_config=rx_config)
                implementation = "spf.PPlus"
            else:
                if transport != "iio":
                    raise ImportError("direct_usb capture requires the SPF package and firmware")
                self._device = _PyadiRx(uri, config, block_size)
                implementation = "pyadi.ad9361"
        else:
            self._device = device_factory(config=config, uri=uri, block_size=block_size,
                                          transport=transport, serial=serial)
            implementation = "injected"
        self._identity = {"kind": "plutoplus", "uri": uri, "serial": serial,
                          "transport": transport, "implementation": implementation,
                          "gain_mode": config.gain_mode or ("manual" if config.gain_db is not None else "slow_attack"),
                          "configured_gain_db": config.gain_db}

    @property
    def config(self) -> RadioConfig: return self._config
    @property
    def identity(self) -> Mapping[str, object]: return dict(self._identity)

    def blocks(self) -> Iterator[SampleBlock]:
        index = 0
        while not self._closed:
            block_start_utc_ns = time.time_ns()
            raw = self._device.rx()
            values = np.asarray(raw)
            if values.ndim == 2: values = values[self._config.channel]
            values = np.asarray(values, dtype=np.complex64)
            yield SampleBlock(values, index, block_start_utc_ns)
            index += values.size

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            close = getattr(self._device, "close", None) or getattr(self._device, "close_rx", None)
            if close is not None: close()

    def gain_snapshot(self) -> tuple[float, ...] | None:
        reader = getattr(self._device, "gain_snapshot", None)
        return None if reader is None else tuple(reader())


class _PyadiRx:
    """Small receive-only fallback when SPF's unrelated ML dependencies are absent."""

    def __init__(self, uri: str, config: RadioConfig, block_size: int):
        try:
            adi = importlib.import_module("adi")
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                "IIO capture requires pyadi-iio and a native libiio in the active environment"
            ) from exc
        normalized_uri = uri.removeprefix("pluto://")
        self.sdr = adi.ad9361(uri=normalized_uri)
        self.sdr.rx_destroy_buffer()
        self.sdr.sample_rate = round(config.sample_rate_hz)
        self.sdr.rx_rf_bandwidth = round(config.bandwidth_hz)
        self.sdr.rx_lo = round(config.center_frequency_hz)
        self.sdr.rx_buffer_size = block_size
        self.sdr.rx_enabled_channels = [config.channel]
        channel = f"chan{config.channel}"
        self._channel = config.channel
        if config.gain_db is None:
            setattr(self.sdr, f"gain_control_mode_{channel}", config.gain_mode or "slow_attack")
        else:
            setattr(self.sdr, f"gain_control_mode_{channel}", "manual")
            setattr(self.sdr, f"rx_hardwaregain_{channel}", config.gain_db)

    def rx(self):
        return self.sdr.rx()

    def close(self):
        self.sdr.rx_destroy_buffer()

    def gain_snapshot(self):
        return (float(getattr(self.sdr, f"rx_hardwaregain_chan{self._channel}")),)


class PairedPlutoSource:
    """One hardware read yielding synchronous AD9361 RX0 and RX1 blocks."""
    def __init__(self, config: RadioConfig, *, uri: str, block_size: int = 65536,
                 transport: str = "iio", serial: str | None = None,
                 device_factory: Callable[..., object] | None = None):
        from dataclasses import replace
        self._configs = (replace(config, channel=0), replace(config, channel=1))
        self._closed = False
        if device_factory is not None:
            self._device = device_factory(config=config, uri=uri, block_size=block_size,
                                          transport=transport, serial=serial)
            implementation = "injected"
        else:
            if transport != "iio": raise ImportError("paired direct_usb capture is not yet supported")
            self._device = _PyadiPairedRx(uri, config, block_size); implementation = "pyadi.ad9361"
        self._identity = {"kind": "plutoplus-paired", "uri": uri, "serial": serial,
                          "transport": transport, "implementation": implementation,
                          "enabled_channels": [0, 1],
                          "timestamp_semantics": "midpoint of host UTC bracket around blocking IIO read",
                          "gain_mode": config.gain_mode or ("manual" if config.gain_db is not None else "slow_attack"),
                          "configured_gain_db": config.gain_db}
    @property
    def configs(self): return self._configs
    @property
    def identity(self): return dict(self._identity)
    def blocks(self):
        index = 0
        while not self._closed:
            before_ns = time.time_ns(); values = np.asarray(self._device.rx()); after_ns = time.time_ns()
            if values.ndim != 2 or values.shape[0] != 2:
                raise RuntimeError(f"paired Pluto read must return 2xN, got {values.shape}")
            yield PairedSampleBlock(np.asarray(values[0], np.complex64),
                                    np.asarray(values[1], np.complex64), index,
                                    (before_ns+after_ns)//2,
                                    read_duration_ns=after_ns-before_ns)
            index += values.shape[1]
    def close(self):
        if not self._closed:
            self._closed = True
            close = getattr(self._device, "close", None) or getattr(self._device, "close_rx", None)
            if close is not None: close()
    def gain_snapshot(self):
        reader = getattr(self._device, "gain_snapshot", None)
        if reader is None:
            return None
        try:
            return tuple(reader())
        except OSError:
            # Gain is diagnostic telemetry.  A transient IIO attribute-read
            # failure must not discard an otherwise healthy streaming block.
            return (float("nan"), float("nan"))
    def retune(self, center_frequency_hz: float) -> None:
        if center_frequency_hz <= 0:
            raise ValueError("center frequency must be positive")
        retune = getattr(self._device, "retune", None)
        if retune is None:
            raise RuntimeError("paired Pluto backend does not support retuning")
        retune(center_frequency_hz)


class _PyadiPairedRx:
    def __init__(self, uri: str, config: RadioConfig, block_size: int):
        try: adi = importlib.import_module("adi")
        except (ImportError, AttributeError) as exc: raise ImportError("paired capture requires pyadi-iio and libiio") from exc
        self.sdr = adi.ad9361(uri=uri.removeprefix("pluto://")); self.sdr.rx_destroy_buffer()
        self.sdr.sample_rate = round(config.sample_rate_hz); self.sdr.rx_rf_bandwidth = round(config.bandwidth_hz)
        self.sdr.rx_lo = round(config.center_frequency_hz); self.sdr.rx_buffer_size = block_size
        self.sdr.rx_enabled_channels = [0, 1]
        for channel in ("chan0", "chan1"):
            if config.gain_db is None: setattr(self.sdr, f"gain_control_mode_{channel}", config.gain_mode or "slow_attack")
            else:
                setattr(self.sdr, f"gain_control_mode_{channel}", "manual")
                setattr(self.sdr, f"rx_hardwaregain_{channel}", config.gain_db)
    def rx(self): return self.sdr.rx()
    def retune(self, center_frequency_hz: float):
        self.sdr.rx_lo = round(center_frequency_hz)
    def close(self): self.sdr.rx_destroy_buffer()
    def gain_snapshot(self):
        return tuple(float(getattr(self.sdr, f"rx_hardwaregain_chan{channel}")) for channel in (0, 1))
