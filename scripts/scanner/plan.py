"""Group requested scan points into the fewest radio tunings.

Three measured facts about the Pluto+ drive every choice here (see README.md):

* Changing ``rf_bandwidth`` costs ~14.3 ms, because it triggers an AD9361 baseband
  filter recalibration. That is ~7.6x the cost of an entire tune-and-measure, so the
  analog bandwidth is chosen **once per scan** and never touched again. Each point's
  requested bandwidth is synthesised digitally instead.
* A tune plus one power read costs ~1.9 ms and does not depend on how far the LO
  moves, so covering several requested points with one tuning is the dominant saving.
* One tuning covers the usable span of the sample rate, so points that fall inside a
  common window need only one tune between them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

MeasureMode = Literal["rssi", "fft"]

#: Fraction of the sample rate treated as usable, keeping requested bands away from
#: the analog filter roll-off and the FFT edges.
DEFAULT_USABLE_FRACTION = 0.8


@dataclass(frozen=True)
class ScanPoint:
    """One requested measurement: total power in ``bandwidth_hz`` about ``center_hz``."""

    center_hz: float
    bandwidth_hz: float

    def __post_init__(self) -> None:
        if self.center_hz <= 0:
            raise ValueError("center frequency must be positive")
        if self.bandwidth_hz <= 0:
            raise ValueError("bandwidth must be positive")

    @property
    def low_hz(self) -> float:
        return self.center_hz - self.bandwidth_hz / 2

    @property
    def high_hz(self) -> float:
        return self.center_hz + self.bandwidth_hz / 2


@dataclass(frozen=True)
class TuneGroup:
    """Points measured from a single LO setting."""

    tune_hz: float
    point_indices: tuple[int, ...]
    mode: MeasureMode

    def __post_init__(self) -> None:
        if not self.point_indices:
            raise ValueError("a tune group must cover at least one point")
        if self.mode == "rssi" and len(self.point_indices) != 1:
            raise ValueError("the RSSI fast path measures exactly one point per tune")


@dataclass(frozen=True)
class ScanPlan:
    """A whole scan: one analog bandwidth, one sample rate, and the tune order."""

    points: tuple[ScanPoint, ...]
    groups: tuple[TuneGroup, ...]
    sample_rate_hz: float
    analog_bandwidth_hz: float
    usable_span_hz: float
    metadata: dict = field(default_factory=dict)

    @property
    def tunings(self) -> int:
        return len(self.groups)

    def estimated_seconds(self, dwell_s: float, *, tune_s: float = 1.278e-3,
                          read_s: float = 0.544e-3) -> float:
        """Predicted wall clock, from the model validated in README.md to within 3%."""
        total = 0.0
        for group in self.groups:
            base = tune_s + read_s
            total += base if dwell_s <= base else base + _ceil_div(dwell_s - base, read_s) * read_s
        return total


def _ceil_div(value: float, step: float) -> int:
    return int(-(-value // step))


def plan_scan(points: Sequence[ScanPoint], *, sample_rate_hz: float,
              usable_fraction: float = DEFAULT_USABLE_FRACTION,
              bandwidth_tolerance: float = 0.05) -> ScanPlan:
    """Choose one analog bandwidth and the fewest tunings that cover every point.

    ``bandwidth_tolerance`` is the fractional agreement required before a lone point
    may use the RSSI fast path, which reports power over the whole analog bandwidth
    rather than a synthesised one.
    """
    if not points:
        raise ValueError("a scan needs at least one point")
    if sample_rate_hz <= 0:
        raise ValueError("sample rate must be positive")
    if not 0 < usable_fraction <= 1:
        raise ValueError("usable fraction must be in (0, 1]")

    usable_span = sample_rate_hz * usable_fraction
    too_wide = [p for p in points if p.bandwidth_hz > usable_span]
    if too_wide:
        raise ValueError(
            f"{len(too_wide)} point(s) request more bandwidth than one tuning covers "
            f"({usable_span/1e6:.3f} MHz usable at {sample_rate_hz/1e6:.3f} MS/s); "
            "raise the sample rate or split the request"
        )

    order = sorted(range(len(points)), key=lambda i: points[i].center_hz)
    grouped: list[list[int]] = []
    for index in order:
        point = points[index]
        if grouped:
            members = grouped[-1]
            low = min(points[m].low_hz for m in members + [index])
            high = max(points[m].high_hz for m in members + [index])
            if high - low <= usable_span:
                members.append(index)
                continue
        grouped.append([index])

    uniform = _uniform_bandwidth(points, bandwidth_tolerance)
    every_group_alone = all(len(members) == 1 for members in grouped)
    if uniform is not None and every_group_alone:
        # Nothing to separate digitally: let the analog filter define the band and
        # take power straight from RSSI, which needs no IQ transferred at all.
        analog_bandwidth = min(uniform, sample_rate_hz)
        mode: MeasureMode = "rssi"
    else:
        analog_bandwidth = sample_rate_hz
        mode = "fft"

    groups = tuple(
        TuneGroup(
            tune_hz=_tune_center(points, members),
            point_indices=tuple(sorted(members)),
            mode=mode if mode == "fft" or len(members) == 1 else "fft",
        )
        for members in grouped
    )
    return ScanPlan(
        points=tuple(points),
        groups=groups,
        sample_rate_hz=sample_rate_hz,
        analog_bandwidth_hz=analog_bandwidth,
        usable_span_hz=usable_span,
        metadata={
            "requested_points": len(points),
            "tunings": len(groups),
            "grouping_saving": len(points) - len(groups),
            "measure_mode": mode,
            "bandwidth_changes": 1,
        },
    )


def _uniform_bandwidth(points: Sequence[ScanPoint], tolerance: float) -> float | None:
    widths = [p.bandwidth_hz for p in points]
    widest, narrowest = max(widths), min(widths)
    if widest - narrowest <= tolerance * widest:
        return widest
    return None


def _tune_center(points: Sequence[ScanPoint], members: Sequence[int]) -> float:
    low = min(points[m].low_hz for m in members)
    high = max(points[m].high_hz for m in members)
    return (low + high) / 2
