"""One survey of the low band, run once immediately before a dwell.

A dwell commits two minutes and 2.3 GB to a single channel.  Nothing recorded
alongside it says what the other seven tunings looked like at that moment, so a
capture that found nothing cannot be told apart from a sky that had nothing in
it, and a channel choice cannot be scored after the fact.

This surveys all eight low-band edge tunings on both receivers and writes what
it collected into the capture manifest, where it travels with the report.  It
is deliberately **observational**: it does not choose the channel, does not
shorten the dwell, and cannot prevent a capture.  A missing survey is an
annotation; a delayed capture is gone for good.

The eight tunings share the LNB low band, so one pass covers them all.  A set
spanning both bands could not: the LNB is switched between them and nothing
here can tell which band it is in.

**Which capture configuration to use is drawn, not chosen.**  Probe length and
sample rate each have an argument on both sides -- a longer probe folds more
frames and costs more air; a higher rate widens the guard band that an LNB
offset eats into, and costs bytes -- and neither argument is settled by
reasoning.  So the survey draws uniformly from all four combinations and the
corpus measures which is better.  The experiment id, the raw draw and the
resulting assignment are all recorded, so an analysis can check that the
randomisation was fair rather than take it on trust; this is the same shape as
``gain_experiment_id`` and its two companions.

**The Pi collects; the analysis host decides.**  Scoring used to happen in the
same loop as the reading, which charged the capture host for both and made the
survey's wall clock scale with the bank *and* with the configuration.  Measured
on the capture Pi itself, a full sweep costs 2.0 s of arithmetic at the cheapest
arm and 11.6 s at the dearest -- 23.9 s if all four ran, and nearly a tenth of a
120 s dwell for one arm alone.  The radio work now stands alone in
:func:`fast_scan.collect_radio`, whose cost follows probe *duration* only
(0.79 s at 80 ms, 1.43 s at 160 ms, both rates alike), and what the capture host
still scores is bounded by :data:`PI_SCORE_SAMPLE_LIMIT`.  The full comparison
runs on the analysis host, over the preserved probes, with sixteen workers.
"""
from __future__ import annotations

import time

from .fast_scan import (PILOT_BANDWIDTH_HZ, SURVEY_CEILING_PROBE_S,
                        SURVEY_CEILING_SAMPLE_RATE_HZ,
                        SURVEY_NULL_REALISATIONS, SURVEY_PROFILE, ScanProfile,
                        pilot_guard_hz, scan_radio, survey_profile,
                        survey_threshold, warm_kernel)

#: What the record calls itself, so a reader can tell a v1 verdict from a later
#: one without inferring it from which keys happen to be present.  v2 is the
#: randomised-configuration record: it carries ``capture_config``, states its
#: sample count and rate rather than leaving them to be assumed, and emits no
#: ``active`` boolean in a configuration whose threshold has not been measured.
SURVEY_SCHEMA = "leo-tracker.pre-dwell-survey/v2"

#: Written into records whose configuration matches the one the ceiling was
#: measured in.  Records written before any of this read "the 99th percentile
#: of noise for this bank shape, measured over 60 realisations" -- synthetic
#: Gaussian noise, which the field distribution does not match, and 60
#: realisations, which cannot resolve a 1% tail at all.
THRESHOLD_BASIS = (
    "1% false-alarm point of the peak-to-median statistic, measured on real "
    f"sky over {SURVEY_NULL_REALISATIONS} target-pilot-free windows: a "
    "lower-edge bank scored on an upper-edge tuning, whose pilot codes sit "
    "230.6 MHz away. No window was screened on the statistic being calibrated")

#: The eight edge-pilot tunings of the LNB low band, in channel order.
LOW_BAND_TUNINGS: tuple[tuple[int, str], ...] = tuple(
    (channel, edge) for channel in (1, 2, 3, 4) for edge in ("lower", "upper"))

#: What the randomisation calls itself.  Named rather than implied so a later
#: analysis can select exactly the captures that belong to this experiment,
#: and so that changing the arm set forces a new id rather than silently
#: pooling two different experiments into one column.
SURVEY_EXPERIMENT_ID = "randomised-probe-length-and-rate-v1"

#: The four arms: probe length crossed with sample rate.
#:
#: Probe length is the frame-folding axis.  The fold is incoherent across
#: frames, so sensitivity grows roughly as their square root: 80 ms holds about
#: 60 frame slots and 160 ms about 120, worth about 1.4x, for twice the air.
#:
#: Sample rate is the *bandwidth* axis and it is not about sensitivity at all.
#: The eight pilot subcarriers occupy 1.875 MHz, so 2.5 MS/s leaves 312.5 kHz
#: of guard either side and 5 MS/s leaves 1,562.5 kHz.  A receiver whose LNB
#: sits further out than the guard loses subcarriers off the end of the sampled
#: spectrum -- 0.58 dB for the first of eight, worse after -- and no search
#: recovers a band that was never sampled.  lnb-c sits at +434 kHz, outside the
#: guard at 2.5 MS/s and comfortably inside it at 5.
#:
#: Both arguments are arguments, not measurements, which is exactly why the
#: configuration is drawn rather than picked.  The plan retired 5 MS/s once and
#: retired probe lengthening once, each on an argument; this replaces both
#: arguments with a corpus.
SURVEY_CONFIGS: tuple[dict, ...] = tuple(
    {"name": f"{round(probe_s * 1000)}ms-{rate / 1e6:.1f}MSps",
     "probe_s": probe_s, "sample_rate_hz": rate,
     "samples_per_tuning": int(round(probe_s * rate))}
    for probe_s in (0.080, 0.160) for rate in (2_500_000.0, 5_000_000.0))

#: Uniform by construction, not by approximation.  ``2**32`` is divisible by
#: four, so ``draw % 4`` maps a uniform 32-bit draw onto four arms with exactly
#: 2**30 draws each and no modulo bias whatsoever.  The AGC experiment's
#: ``draw % 10000`` does carry a bias -- 2**32 is not a multiple of 10,000 --
#: but at one part in 430,000 of a 50% split it is far below what that
#: experiment can resolve.  Here the arms divide the space exactly and the
#: probability is stated as a rational rather than a rounded float.
SURVEY_ASSIGNMENT_PROBABILITY = 1.0 / len(SURVEY_CONFIGS)

#: Samples the *capture host* scores, whatever it collected.
#:
#: 200,000 is 80 ms at 2.5 MS/s: the cheapest of the four configurations and
#: the only one the threshold was measured in.  Correlation cost is roughly
#: linear in samples, so bounding the window bounds what the Pi spends.
#:
#: Measured on the capture Pi 5 itself, under its own two live capture
#: services, three threads, minimum of twelve round-robin rounds -- a full
#: sixteen-probe sweep costs **2.0 / 5.6 / 4.7 / 11.6 s** across the four arms
#: unbounded, and **2.1 / 2.9 / 1.9 / 2.7 s** with this cap.  The cap is not
#: perfectly flat because 5 MS/s doubles the kernel taps and the epoch count at
#: the same sample count, so it costs about 40% more per sample; what it buys
#: is that the Pi's bill stops tracking the draw.
#:
#: What it does *not* do is make the resulting verdict calibrated.  At 5 MS/s
#: this prefix is 40 ms of air, not 80, so three of the four configurations
#: still yield a score whose bar has never been measured.  The record says
#: which window was scored and refuses the boolean; the full-length scoring
#: happens on the analysis host over the preserved probes.
PI_SCORE_SAMPLE_LIMIT = int(round(SURVEY_CEILING_PROBE_S
                                  * SURVEY_CEILING_SAMPLE_RATE_HZ))


def assign_config(draw_u32: int) -> dict:
    """Which of the four configurations a raw 32-bit draw selects.

    Kept in Python rather than in the shell that draws the number, because the
    mapping is the part of a randomised assignment that can be wrong, and a
    mapping written in bash is a mapping with no test.  The shell draws and
    logs; this decides and the record keeps all three of id, draw and outcome
    so the fairness of the split can be checked from the corpus alone.
    """
    value = int(draw_u32)
    if not 0 <= value < 2 ** 32:
        raise ValueError("random draw must be an unsigned 32-bit integer")
    return SURVEY_CONFIGS[value % len(SURVEY_CONFIGS)]


def config_by_name(name: str) -> dict:
    """The arm with this name, for an operator pinning one deliberately."""
    for config in SURVEY_CONFIGS:
        if config["name"] == name:
            return config
    raise ValueError(f"unknown survey configuration {name!r}; "
                     f"expected one of {[c['name'] for c in SURVEY_CONFIGS]}")


#: The arm a survey runs when nobody randomised it: the calibrated one.
#: Falling back to a configuration whose threshold has been measured means an
#: un-randomised survey still emits a verdict that means something.
DEFAULT_SURVEY_CONFIG = config_by_name(
    f"{round(SURVEY_CEILING_PROBE_S * 1000)}ms-"
    f"{SURVEY_CEILING_SAMPLE_RATE_HZ / 1e6:.1f}MSps")

#: Evidence carried alongside the verdict without being used to reach it.
#: The verdict still rests on peak-to-median alone. Its threshold is now
#: measured on the sky against windows that hold no target pilot by
#: construction, so the 1% it claims is a number and not a hope, but it is
#: still one statistic. Which of these separates a pilot from field
#: interference *better* than peak-to-median is a different question, it is a
#: question for the corpus, and it can only be asked of probes that stored
#: them, so they are stored from the start.
CORROBORATION_FIELDS = ("peak_to_p99", "peak_to_second", "offset_contrast",
                        "offset_profile", "anchor_agreement", "anchor_count",
                        "folded_p99", "second_score", "mean_power",
                        "peak_amplitude")


def summarise(outcome: dict, *, dwell_channel: int | None = None,
              dwell_region: str | None = None,
              profile: ScanProfile = SURVEY_PROFILE,
              warm_s: float = 0.0, started_utc_ns: int | None = None,
              config: dict | None = None,
              experiment: dict | None = None) -> dict:
    """Turn a scan into the record that goes in the manifest.

    Both the verdict and the score behind it are kept.  The threshold is a
    measured property of the bank shape *and of the configuration it was
    measured in*, and it has been revised once already; a bare boolean could
    not be re-read against a later one, while a score can.

    In an uncalibrated configuration no boolean is written at all.  ``active``
    becomes ``None`` on every receiver and the top-level list is ``None``
    rather than empty, because an empty list is the same shape as "nothing was
    detected" and would be read as that.  A stored boolean that means different
    things in different rows is worse than no boolean.
    """
    config = dict(config or DEFAULT_SURVEY_CONFIG)
    # What was actually *scored*, which is the probe length the threshold has
    # to answer for.  A capped Pi-side score over 200,000 samples at 5 MS/s is
    # a 40 ms probe however long the collected one was, and pretending
    # otherwise is exactly the substitution this record exists to prevent.
    rate = float(outcome.get("sample_rate_hz") or config["sample_rate_hz"])
    scored_probe_s = float(outcome.get("scored_probe_s")
                           or outcome.get("probe_s") or config["probe_s"])
    bar = survey_threshold(tuple(profile.shape), probe_s=scored_probe_s,
                           sample_rate_hz=rate)
    threshold = bar["threshold"]
    # A survey that scored nothing is entitled to no verdict either, and for a
    # different reason: not that the bar is unmeasured but that nothing was
    # held up against it. ``active_count: 0`` would read as "looked and found
    # nothing", which is the same misreading in a second disguise.
    was_scored = outcome.get("scored", True)
    calibrated = bool(bar["calibrated"] and was_scored)
    tunings, active = [], []
    for entry in sorted(outcome["results"],
                        key=lambda item: (item["channel"], item["region"])):
        receivers = []
        for scored in entry["receivers"]:
            verdict = (scored["peak_to_median"] >= threshold
                       if calibrated else None)
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
            "threshold_calibrated": calibrated,
            "threshold_basis": (bar["basis"] if was_scored else
                                "nothing was scored on the capture host; the "
                                "probes are preserved and every verdict is "
                                "deferred to the analysis host"),
            "scored_on_capture_host": bool(was_scored),
            "threshold_configuration": {
                "measured_probe_s": bar["calibrated_probe_s"],
                "measured_sample_rate_hz": bar["calibrated_sample_rate_hz"],
                "scored_probe_s": scored_probe_s,
                "scored_sample_rate_hz": rate,
                "null_realisations": bar["null_realisations"]},
            "dwell": ({"channel": dwell_channel, "region": dwell_region}
                      if dwell_channel is not None else None),
            # None, not [], in an uncalibrated configuration: an empty list
            # reads as "looked and found nothing", which is a claim this
            # record is not entitled to make.
            "active": active if calibrated else None,
            "active_count": len(active) if calibrated else None,
            "tunings": tunings,
            # The configuration this survey was taken in, drawn rather than
            # chosen.  Every rate- and length-dependent fact downstream is
            # read from here, never assumed: the ci16 shape, the frame grid,
            # the kernel taps and the guard band all follow from these two
            # numbers.
            "capture_config": {
                "name": config["name"],
                "probe_s": config["probe_s"],
                "sample_rate_hz": config["sample_rate_hz"],
                "samples_per_tuning": int(outcome.get("samples_per_tuning")
                                          or config["samples_per_tuning"]),
                "pilot_bandwidth_hz": PILOT_BANDWIDTH_HZ,
                "pilot_guard_hz": pilot_guard_hz(config["sample_rate_hz"]),
                "iq_bytes": outcome.get("iq_bytes"),
                "scored_samples": outcome.get("scored_samples"),
                "scored_probe_s": scored_probe_s,
                "scored_on": "capture host, bounded prefix"
                             if outcome.get("scored") is not False
                             else "not scored here; deferred to the analysis host",
                "scoring_note": (
                    "the capture host scores at most "
                    f"{PI_SCORE_SAMPLE_LIMIT} samples -- the cheapest "
                    "configuration's worth -- so its cost does not grow with "
                    "the drawn configuration. Full-length scoring of every "
                    "candidate runs on the analysis host over the preserved "
                    "probe, which is why the probe is preserved")},
            "experiment": experiment,
            "sample_rate_hz": rate,
            "probe_s": outcome.get("probe_s", config["probe_s"]),
            "samples_per_tuning": outcome.get("samples_per_tuning"),
            "radio_ms": outcome.get("radio_ms"),
            "compute_ms": outcome.get("compute_ms"),
            "offset_span_hz": outcome.get("offset_span_hz"),
            # The tunings above are sorted for reading; the retained IQ is in
            # the order it was collected. These coincide for the current tuning
            # list and would stop coinciding the moment it changes — one edge
            # per channel, say — so the mapping is recorded rather than
            # inferred. Without it a later analysis silently scores one tuning's
            # samples against another tuning's label.
            "sample_order": outcome.get("sample_order"),
            # Kept as a key, and softened rather than dropped, because probes
            # written before the bank widened carry the stronger wording and a
            # reader comparing two records has to be able to see which regime
            # each was taken in.
            "quiet_verdict_caveat": (
                "the bank now spreads 13 hypotheses over +/-700 kHz, so a "
                "biased port sits within 58 kHz of a hypothesis and scores "
                "within about 10% of a centred one on the same beacon; earlier "
                "records, written against 3 hypotheses, mean something weaker "
                "by quiet than this one does"),
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


def draw_configuration(*, experiment_id: str | None = None,
                       random_draw_u32: int | None = None,
                       config_name: str | None = None) -> tuple[dict, dict]:
    """Resolve the configuration to run, and the experiment record beside it.

    Three ways in, in decreasing order of what the corpus can do with them.
    A draw gives a randomised assignment whose fairness is checkable.  A name
    pins one arm, which is what an operator debugging a single configuration
    wants and which is recorded as *not* randomised so it can be excluded from
    the comparison.  Neither gives the calibrated arm, which is the only one
    whose verdict means anything today.
    """
    if config_name is not None:
        config = config_by_name(config_name)
        return config, {"experiment_id": experiment_id, "randomised": False,
                        "random_draw_u32": None,
                        "assignment_probability": None,
                        "assigned_config": config["name"],
                        "arms": [item["name"] for item in SURVEY_CONFIGS],
                        "basis": "configuration pinned by name, not drawn; "
                                 "exclude from any comparison that assumes "
                                 "random assignment"}
    if experiment_id is None and random_draw_u32 is None:
        config = DEFAULT_SURVEY_CONFIG
        return config, {"experiment_id": None, "randomised": False,
                        "random_draw_u32": None,
                        "assignment_probability": None,
                        "assigned_config": config["name"],
                        "arms": [item["name"] for item in SURVEY_CONFIGS],
                        "basis": "no experiment requested; the calibrated "
                                 "configuration was used"}
    if experiment_id is None or random_draw_u32 is None:
        raise ValueError("a survey experiment needs both an id and a raw draw: "
                         "the id says which experiment a row belongs to and "
                         "the draw is what makes the assignment auditable")
    config = assign_config(random_draw_u32)
    return config, {
        "experiment_id": str(experiment_id), "randomised": True,
        "random_draw_u32": int(random_draw_u32),
        "assignment_probability": SURVEY_ASSIGNMENT_PROBABILITY,
        "assigned_config": config["name"],
        "arms": [item["name"] for item in SURVEY_CONFIGS],
        "assignment_rule": f"draw modulo {len(SURVEY_CONFIGS)}",
        "basis": (
            f"uniform over {len(SURVEY_CONFIGS)} arms. 2**32 is divisible by "
            f"{len(SURVEY_CONFIGS)}, so the modulo carries no bias at all and "
            "the assignment is exactly uniform rather than approximately so. "
            "The draw is kept so a later analysis can verify the split from "
            "the corpus instead of trusting this claim")}


def run_survey(*, uri: str, serial: str | None = None,
               tunings=LOW_BAND_TUNINGS, profile: ScanProfile | None = None,
               sample_rate_hz: float | None = None,
               lnb_lo_hz: float = 9_750_000_000.0,
               dwell_channel: int | None = None,
               dwell_region: str | None = None,
               keep_samples: bool = False,
               config: dict | None = None,
               experiment: dict | None = None,
               score_on_capture_host: bool = True,
               score_sample_limit: int | None = PI_SCORE_SAMPLE_LIMIT,
               simulated: bool = False) -> tuple[dict, object]:
    """Survey the low band on its own context, then hand the radio back.

    Returns the record and, when ``keep_samples`` is set, the raw ci16.  Both
    are returned always, the samples as None when not kept, so a caller cannot
    accidentally treat a record as a pair.

    The configuration is the drawn one, **not the dwell's**.  Those were the
    same number for as long as the survey inherited the capture's sample rate,
    and inheriting it is now wrong twice over: the survey may be drawn at
    5 MS/s while the dwell records at 2.5, and a wide or oversampled dwell runs
    at a rate the survey never uses.  Passing the survey's own rate down is
    what keeps the recorded shape, the bank taps and the frame grid agreeing
    with the samples on disk.

    The context is opened and closed around the survey rather than shared with
    the capture, because the two want opposite radio configurations — a survey
    wants a shallow queue and a probe-sized block — and because a USB context is
    an exclusive claim that the capture must be able to take cleanly.

    Never raises.  A failure is recorded as one.
    """
    started_utc_ns = time.time_ns()
    config = dict(config or DEFAULT_SURVEY_CONFIG)
    rate = float(sample_rate_hz if sample_rate_hz is not None
                 else config["sample_rate_hz"])
    profile = profile or survey_profile(config["probe_s"], rate)
    if simulated:
        # A simulated capture asking for a real survey is incoherent, and it is
        # worse than incoherent on a host that has a radio: this opens a
        # context on the default URI and retunes the local oscillator eight
        # times, which on this site is hardware a live capture service owns.
        # Recorded as skipped rather than failed, because those are different
        # facts and only one of them is about the radio.
        return ({"schema": SURVEY_SCHEMA, "state": "skipped",
                 "started_utc_ns": started_utc_ns,
                 "reason": "the capture source is simulated; a survey needs a "
                           "radio and would have opened one that a live "
                           "capture owns",
                 "dwell": ({"channel": dwell_channel, "region": dwell_region}
                           if dwell_channel is not None else None),
                 "capture_config": {"name": config["name"],
                                    "probe_s": config["probe_s"],
                                    "sample_rate_hz": config["sample_rate_hz"]},
                 "experiment": experiment,
                 "active": None, "active_count": None, "tunings": None,
                 "note": ("the survey is observational; its absence is "
                          "recorded and the capture proceeds")}, None)
    context = None
    try:
        # The fused kernel is built on first use and the bank on first ask, so
        # warming here keeps roughly a second of one-off cost out of a timing
        # that is otherwise all radio.  Warmed at the drawn rate: the bank is
        # cached per rate, so warming at the wrong one warms the wrong bank and
        # the first probe pays the build anyway.
        mark = time.perf_counter()
        for edge in dict.fromkeys(edge for _, edge in tunings):
            warm_kernel(profile, rate, edge)
        warm_s = time.perf_counter() - mark

        context = _open_context(uri, serial)
        _verify_serial(context, serial)
        outcome = scan_radio(context, list(tunings), profile=profile,
                             sample_rate_hz=rate, lnb_lo_hz=lnb_lo_hz,
                             keep_samples=keep_samples,
                             score=score_on_capture_host,
                             score_sample_limit=score_sample_limit)
        record = summarise(outcome, dwell_channel=dwell_channel,
                           dwell_region=dwell_region, profile=profile,
                           warm_s=warm_s, started_utc_ns=started_utc_ns,
                           config=config, experiment=experiment)
        return record, outcome.get("samples")
    # Deliberately not BaseException: an operator interrupting the run must
    # still stop it, rather than have the interrupt filed as a survey fault.
    except Exception as exc:                       # noqa: BLE001 - fail open
        return ({"schema": SURVEY_SCHEMA, "state": "failed",
                 "started_utc_ns": started_utc_ns,
                 "error": f"{type(exc).__name__}: {exc}",
                 "dwell": ({"channel": dwell_channel, "region": dwell_region}
                           if dwell_channel is not None else None),
                 # Recorded even on a failure: an arm that fails more often
                 # than the others is a finding about that arm, and a failure
                 # with no arm attached cannot contribute to it.
                 "capture_config": {"name": config["name"],
                                    "probe_s": config["probe_s"],
                                    "sample_rate_hz": config["sample_rate_hz"]},
                 "experiment": experiment,
                 "active": None, "active_count": None, "tunings": None,
                 "note": ("the survey is observational; its failure is recorded "
                          "and the capture proceeds")}, None)
    finally:
        # libiio's Python binding frees a context when the last reference goes,
        # so dropping it here is what hands the radio back before the capture
        # tries to claim it.  There is no explicit close to call.
        del context
