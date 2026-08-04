"""USB GNSS fix acquisition with no daemon or third-party runtime dependency."""

from .nmea import GPSFix, acquire_fix, parse_sentence

__all__ = ["GPSFix", "acquire_fix", "parse_sentence"]
