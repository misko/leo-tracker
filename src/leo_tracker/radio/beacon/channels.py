"""Published Starlink Ku downlink channel geometry."""
from __future__ import annotations

STARLINK_CHANNEL_BANDWIDTH_HZ = 240_000_000.0
STARLINK_CHANNEL_SPACING_HZ = 250_000_000.0
STARLINK_SUBCARRIER_SPACING_HZ = 234_375.0
STARLINK_EDGE_PILOT_SUBCARRIERS = {
    "upper": tuple(range(488, 496)),
    "lower": tuple(range(528, 536)),
}


def starlink_channel_center_hz(channel: int) -> float:
    """Return Humphreys et al.'s channel center for one-based channel 1..8."""
    if channel not in range(1, 9):
        raise ValueError("Starlink channel must be in 1..8")
    return (10.7e9 + STARLINK_SUBCARRIER_SPACING_HZ / 2
            + STARLINK_CHANNEL_SPACING_HZ * (channel - .5))


def starlink_if_hz(channel: int, lnb_lo_hz: float = 9_750_000_000.0) -> float:
    if lnb_lo_hz <= 0:
        raise ValueError("LNB local oscillator must be positive")
    value = starlink_channel_center_hz(channel) - lnb_lo_hz
    if value <= 0:
        raise ValueError("channel lies below the configured LNB local oscillator")
    return value


def subcarrier_offset_hz(index: int) -> float:
    """Map the paper's 0..1023 OFDM index to signed baseband frequency."""
    if index not in range(1024):
        raise ValueError("subcarrier index must be in 0..1023")
    signed = index if index < 512 else index - 1024
    return signed * STARLINK_SUBCARRIER_SPACING_HZ


def starlink_edge_pilot_offset_hz(edge: str) -> float:
    """Return the center of one published eight-subcarrier edge-pilot band."""
    try:
        indexes = STARLINK_EDGE_PILOT_SUBCARRIERS[edge]
    except KeyError as exc:
        raise ValueError("edge must be lower or upper") from exc
    return sum(map(subcarrier_offset_hz, indexes)) / len(indexes)


def starlink_edge_pilot_if_hz(channel: int, edge: str,
                              lnb_lo_hz: float = 9_750_000_000.0) -> float:
    return starlink_if_hz(channel, lnb_lo_hz) + starlink_edge_pilot_offset_hz(edge)
