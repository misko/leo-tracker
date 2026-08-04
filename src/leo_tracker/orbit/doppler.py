"""Geometric one-way Doppler observables."""

SPEED_OF_LIGHT_M_S = 299_792_458.0


def predicted_doppler_hz(carrier_hz: float, range_rate_km_s: float) -> float:
    """Return received-minus-transmitted frequency; approach is positive."""
    if carrier_hz <= 0:
        raise ValueError("carrier_hz must be positive")
    return -carrier_hz * (range_rate_km_s * 1000.0) / SPEED_OF_LIGHT_M_S
