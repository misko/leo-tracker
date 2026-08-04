from datetime import datetime, timezone

import pytest

from leo_tracker.radio.gps import parse_nmea_snapshot


RMC = "$GPRMC,205345.00,A,3750.94180,N,12229.13849,W,0.037,,020826,,,D*60"
GGA = "$GPGGA,205345.00,3750.94180,N,12229.13849,W,2,10,1.02,59.7,M,-29.7,M,,0000*58"


def test_nmea_snapshot_records_position_fix_and_observed_clock_offset():
    host = datetime(2026, 8, 2, 20, 53, 45, 250_000, tzinfo=timezone.utc)

    fix = parse_nmea_snapshot([RMC, GGA], host_received=host)

    assert fix["gps_utc"] == "2026-08-02T20:53:45Z"
    assert fix["host_minus_gps_s"] == pytest.approx(.25)
    assert fix["latitude_deg"] == pytest.approx(37.84903)
    assert fix["longitude_deg"] == pytest.approx(-122.4856415)
    assert fix["fix_quality"] == 2
    assert fix["satellites"] == 10
    assert fix["hdop"] == pytest.approx(1.02)
    assert fix["altitude_m"] == pytest.approx(59.7)
    assert fix["mode"] == "D"


def test_nmea_snapshot_rejects_bad_checksum_or_void_fix():
    with pytest.raises(ValueError, match="no valid"):
        parse_nmea_snapshot([RMC[:-2]+"00", GGA])
    with pytest.raises(ValueError, match="no valid"):
        parse_nmea_snapshot([RMC.replace(",A,", ",V,")])
