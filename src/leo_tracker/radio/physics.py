"""Small Doppler/radial-motion conversions shared by analysis and presentation."""

LIGHT_SPEED_M_S = 299_792_458.0


def doppler_radial_acceleration_m_s2(drift_hz_s: float, carrier_hz: float) -> float:
    """Convert received-minus-transmitted Doppler slope to radial acceleration."""
    if carrier_hz <= 0:
        raise ValueError("carrier frequency must be positive")
    return -LIGHT_SPEED_M_S * drift_hz_s / carrier_hz
