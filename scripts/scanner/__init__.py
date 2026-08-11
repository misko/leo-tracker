"""Fast N-frequency power scanning on the Pluto+.

The design and the measurements behind it are in README.md. Importing this package
does not import libiio or pyadi: the hardware adapter lives in :mod:`scanner.pluto`
and is imported only when a real scan runs, so the offline test suite needs no radio.
"""
from .plan import ScanPlan, ScanPoint, TuneGroup, plan_scan
from .execute import (FakeScanRadio, PointResult, ScanRadio, ScanReport,
                      band_power_dbfs, band_power_from_periodogram,
                      execute_scan, periodogram)

__all__ = [
    "ScanPlan", "ScanPoint", "TuneGroup", "plan_scan",
    "FakeScanRadio", "PointResult", "ScanRadio", "ScanReport",
    "band_power_dbfs", "band_power_from_periodogram", "execute_scan", "periodogram",
]
