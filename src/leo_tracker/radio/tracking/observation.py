from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np

from ..measurement import load_measurement_waterfall


@dataclass(frozen=True)
class TrackingObservation:
    source: str
    spectra_db: np.ndarray
    time_s: np.ndarray
    utc_ns: np.ndarray
    frequency_hz: np.ndarray
    center_frequency_hz: float
    sample_rate_hz: float
    bandwidth_hz: float
    lnb_lo_hz: float | None
    gain_mode: str
    hardware_gain_db: np.ndarray | None
    identity: dict

    @property
    def bin_width_hz(self) -> float:
        return float(np.median(np.diff(self.frequency_hz)))

    def window(self, start_s: float, stop_s: float) -> "TrackingObservation":
        selected = (self.time_s >= start_s) & (self.time_s <= stop_s)
        if selected.sum() < 4:
            raise ValueError("tracking window contains fewer than four spectra")
        gain = None if self.hardware_gain_db is None else self.hardware_gain_db[:, selected]
        return TrackingObservation(self.source, self.spectra_db[:, selected],
            self.time_s[selected], self.utc_ns[selected], self.frequency_hz,
            self.center_frequency_hz, self.sample_rate_hz, self.bandwidth_hz,
            self.lnb_lo_hz, self.gain_mode, gain, self.identity)

    def select_windows(self, windows) -> "TrackingObservation":
        selected = np.zeros(self.time_s.size, bool)
        for start, stop in windows:
            selected |= (self.time_s >= start)&(self.time_s <= stop)
        if selected.sum() < 4:
            raise ValueError("tracking windows contain fewer than four spectra")
        gain = None if self.hardware_gain_db is None else self.hardware_gain_db[:, selected]
        return TrackingObservation(self.source, self.spectra_db[:, selected].copy(),
            self.time_s[selected], self.utc_ns[selected], self.frequency_hz,
            self.center_frequency_hz, self.sample_rate_hz, self.bandwidth_hz,
            self.lnb_lo_hz, self.gain_mode, gain, self.identity)


def load_tracking_observation(path: Path | str) -> TrackingObservation:
    artifact = load_measurement_waterfall(Path(path))
    utc = np.asarray(artifact["utc_ns"], np.int64)
    identity_value = artifact.get("identity_json", np.array("{}"))
    identity = json.loads(str(identity_value))
    gain = artifact.get("hardware_gain_db")
    return TrackingObservation(str(path),
        np.asarray(artifact["psd_db_raw_per_hz"], np.float32),
        (utc-utc[0])/1e9, utc,
        np.asarray(artifact["frequency_offsets_hz"], float),
        float(artifact["center_frequency_hz"]), float(artifact["sample_rate_hz"]),
        float(artifact["bandwidth_hz"]),
        None if "lnb_lo_hz" not in artifact else float(artifact["lnb_lo_hz"]),
        str(artifact.get("gain_mode", "unknown")),
        None if gain is None else np.asarray(gain, float), identity)
