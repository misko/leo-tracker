from datetime import date

import pytest

from leo_tracker.gps import GPSFix, parse_sentence


def test_parse_valid_gga_fix():
    parsed = parse_sentence("$GPGGA,123519.00,3749.1234,N,12229.5678,W,1,08,0.9,73.0,M,-25.0,M,,*67",
                            day=date(2026, 8, 1))
    assert isinstance(parsed, GPSFix)
    assert parsed.timestamp.isoformat() == "2026-08-01T12:35:19+00:00"
    assert parsed.latitude_deg == pytest.approx(37 + 49.1234 / 60)
    assert parsed.longitude_deg == pytest.approx(-(122 + 29.5678 / 60))
    assert parsed.altitude_m == 73.0
    assert parsed.satellites == 8


def test_rejects_bad_checksum():
    with pytest.raises(ValueError, match="checksum"):
        parse_sentence("$GPGGA,123519,,,,,0,00,99.99,,,,,,*00")


def test_invalid_gga_has_no_fix():
    assert parse_sentence("$GPGGA,,,,,,0,00,99.99,,,,,,*48") is None


def test_rmc_supplies_utc_date_even_without_fix():
    assert parse_sentence("$GPRMC,123519,V,,,,,,,010826,,,N*53") == date(2026, 8, 1)
