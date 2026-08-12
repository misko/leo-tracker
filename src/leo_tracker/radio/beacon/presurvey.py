"""One survey of the low band, run once immediately before a dwell.

A dwell commits two minutes and 2.3 GB to a single channel.  Nothing recorded
alongside it says what the other seven tunings looked like at that moment, so a
capture that found nothing cannot be told apart from a sky that had nothing in
it, and a channel choice cannot be scored after the fact.

This surveys all eight low-band edge tunings on both receivers in roughly four
hundred milliseconds and writes the verdict into the capture manifest, where it
travels with the report.  It is deliberately **observational**: it does not
choose the channel, does not shorten the dwell, and cannot prevent a capture.
A missing survey is an annotation; a delayed capture is gone for good.

The eight tunings share the LNB low band, so one pass covers them all.  A set
spanning both bands could not: the LNB is switched between them and nothing
here can tell which band it is in.
"""
from __future__ import annotations

import time

from .fast_scan import (SURVEY_PROFILE, ScanProfile, detection_threshold,
                        scan_radio, warm_kernel)

#: What the record calls itself, so a reader can tell a v1 verdict from a later
#: one without inferring it from which keys happen to be present.
SURVEY_SCHEMA = "leo-tracker.pre-dwell-survey/v1"

#: The eight edge-pilot tunings of the LNB low band, in channel order.
LOW_BAND_TUNINGS: tuple[tuple[int, str], ...] = tuple(
    (channel, edge) for channel in (1, 2, 3, 4) for edge in ("lower", "upper"))

#: Evidence carried alongside the verdict without being used to reach it.
#: The threshold currently rests on peak-to-median alone, characterised
#: against synthetic Gaussian noise; the field distribution sits close enough
#: to it that the intended false-alarm rate is doubtful. These are what a
#: later answer would be built from, and they can only be recovered from
#: probes that stored them, so they are stored from the start.
CORROBORATION_FIELDS = ("peak_to_p99", "peak_to_second", "offset_contrast",
                        "offset_profile", "anchor_agreement", "anchor_count",
                        "folded_p99", "second_score", "mean_power",
                        "peak_amplitude")


def summarise(outcome: dict, *, dwell_channel: int | None = None,
              dwell_region: str | None = None,
              profile: ScanProfile = SURVEY_PROFILE,
              warm_s: float = 0.0, started_utc_ns: int | None = None) -> dict:
    """Turn a scan into the record that goes in the manifest.

    Both the verdict and the score behind it are kept.  The threshold is a
    measured property of the bank shape and has been revised once already; a
    bare boolean could not be re-read against a later one, while a score can.
    """
    threshold = detection_threshold(tuple(profile.shape))
    tunings, active = [], []
    for entry in sorted(outcome["results"],
                        key=lambda item: (item["channel"], item["region"])):
        receivers = []
        for scored in entry["receivers"]:
            verdict = scored["peak_to_median"] >= threshold
            receivers.append({
                "receiver": scored["receiver"],
                "active": verdict,
                "peak_to_median": scored["peak_to_median"],
                "frequency_offset_hz": scored["frequency_offset_hz"],
                "epoch_s": scored["epoch_s"],
                "folded_score": scored["folded_score"],
                "folded_median": scored["folded_median"],
                # Recorded but not yet used to decide anything. Which of these
                # separates a pilot from field interference better than
                # peak-to-median is a question for the corpus, and it can only
                # be asked of probes that kept them.
                **{key: scored[key] for key in CORROBORATION_FIELDS}})
            if verdict:
                active.append({"channel": entry["channel"],
                               "region": entry["region"],
                               "receiver": scored["receiver"]})
        tunings.append({"channel": entry["channel"], "region": entry["region"],
                        "if_center_hz": entry["if_center_hz"],
                        "rf_center_hz": entry["rf_center_hz"],
                        "receivers": receivers})
    return {"schema": SURVEY_SCHEMA, "state": "complete",
            "started_utc_ns": started_utc_ns,
            "threshold": threshold,
            "threshold_basis": ("peak-to-median at the 99th percentile of noise "
                                "for this bank shape, measured over 60 realisations"),
            "dwell": ({"channel": dwell_channel, "region": dwell_region}
                      if dwell_channel is not None else None),
            "active": active,
            "active_count": len(active),
            "tunings": tunings,
            "sample_rate_hz": outcome.get("sample_rate_hz"),
            "offset_span_hz": outcome.get("offset_span_hz"),
            "quiet_verdict_caveat": (
                "a port whose LNB sits near the edge of the search scores close "
                "to threshold on a beacon that is plainly there; quiet on such a "
                "port is not evidence of a quiet sky"),
            "warm_ms": warm_s * 1000.0,
            "timing_ms": outcome["timing_ms"],
            "total_ms": outcome["total_ms"],
            "per_tuning_ms": outcome["per_tuning_ms"],
            "profile": outcome["profile"],
            "note": ("observational only: the survey does not choose the "
                     "channel, shorten the dwell, or gate the capture")}


def _open_context(uri: str, serial: str | None):
    """Open one libiio context on the same radio the capture will open.

    Resolution is deliberately the capture path's own, private though it is:
    it strips the ``pluto://`` prefix libiio does not accept and resolves a USB
    radio by stable serial rather than by bus address, which moves across a
    firmware load.  A second copy of that logic here is how a survey ends up
    attributed to the other radio.
    """
    import iio                                    # kept local: host-only import

    from ..pluto import _resolve_iio_uri

    return iio.Context(_resolve_iio_uri(uri, serial))


def _verify_serial(context, serial: str | None) -> None:
    """Refuse the wrong radio, the way the capture source does.

    Resolution by serial should already guarantee this; asserting it costs
    nothing and a survey filed against the wrong port is worse than no survey.
    """
    if not serial:
        return
    found = (context.attrs or {}).get("hw_serial") if hasattr(context, "attrs") else None
    if found and str(found) != str(serial):
        raise ValueError(f"opened Pluto serial {found}, expected {serial}")


def run_survey(*, uri: str, serial: str | None = None,
               tunings=LOW_BAND_TUNINGS, profile: ScanProfile = SURVEY_PROFILE,
               sample_rate_hz: float = 2_500_000.0,
               lnb_lo_hz: float = 9_750_000_000.0,
               dwell_channel: int | None = None,
               dwell_region: str | None = None,
               keep_samples: bool = False) -> tuple[dict, object]:
    """Survey the low band on its own context, then hand the radio back.

    Returns the record and, when ``keep_samples`` is set, the raw ci16 the
    scores were computed from. Both are returned always, the samples as None
    when not kept, so a caller cannot accidentally treat a record as a pair.

    The context is opened and closed around the survey rather than shared with
    the capture, because the two want opposite radio configurations — a survey
    wants a shallow queue and a probe-sized block — and because a USB context is
    an exclusive claim that the capture must be able to take cleanly.

    Never raises.  A failure is recorded as one.
    """
    started_utc_ns = time.time_ns()
    context = None
    try:
        # The fused kernel is built on first use and the bank on first ask, so
        # warming here keeps roughly a second of one-off cost out of a timing
        # that is otherwise all radio.
        mark = time.perf_counter()
        for edge in dict.fromkeys(edge for _, edge in tunings):
            warm_kernel(profile, sample_rate_hz, edge)
        warm_s = time.perf_counter() - mark

        context = _open_context(uri, serial)
        _verify_serial(context, serial)
        outcome = scan_radio(context, list(tunings), profile=profile,
                             sample_rate_hz=sample_rate_hz, lnb_lo_hz=lnb_lo_hz,
                             keep_samples=keep_samples)
        record = summarise(outcome, dwell_channel=dwell_channel,
                           dwell_region=dwell_region, profile=profile,
                           warm_s=warm_s, started_utc_ns=started_utc_ns)
        return record, outcome.get("samples")
    # Deliberately not BaseException: an operator interrupting the run must
    # still stop it, rather than have the interrupt filed as a survey fault.
    except Exception as exc:                       # noqa: BLE001 - fail open
        return ({"schema": SURVEY_SCHEMA, "state": "failed",
                 "started_utc_ns": started_utc_ns,
                 "error": f"{type(exc).__name__}: {exc}",
                 "dwell": ({"channel": dwell_channel, "region": dwell_region}
                           if dwell_channel is not None else None),
                 "active": None, "tunings": None,
                 "note": ("the survey is observational; its failure is recorded "
                          "and the capture proceeds")}, None)
    finally:
        # libiio's Python binding frees a context when the last reference goes,
        # so dropping it here is what hands the radio back before the capture
        # tries to claim it.  There is no explicit close to call.
        del context
