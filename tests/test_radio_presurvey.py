"""The survey run once before each dwell, and what it records.

Everything here runs against a fake libiio context, so the suite needs no
radio.  What it is protecting is the comparison the survey exists for: the
capture keeps two receivers, so a survey that scores one of them cannot be set
beside what the dwell found.
"""
import json
import sys
import types

import numpy as np
import pytest

from leo_tracker.radio.beacon.fast_scan import (SURVEY_BANK,
                                                SURVEY_OFFSET_SPAN_HZ,
                                                SURVEY_PROFILE,
                                                detection_threshold, scan_radio,
                                                survey_bank)
from leo_tracker.radio.beacon.presurvey import (LOW_BAND_TUNINGS, run_survey,
                                                summarise)
from leo_tracker.radio.beacon.pilots import _edge_pilot_frame_cached
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S

SAMPLE_RATE_HZ = 2_500_000.0
PERIOD = SAMPLE_RATE_HZ * STARLINK_FRAME_DURATION_S


def _noise(count, seed):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(count) + 1j * rng.standard_normal(count))
            / np.sqrt(2)).astype(np.complex64)


def _beacon(snr_db, *, count=50_000, seed=1, epoch=1234, offset_hz=0.0,
            edge="lower"):
    template = _edge_pilot_frame_cached(SAMPLE_RATE_HZ, edge, 0)
    background = _noise(count, seed)
    signal = np.zeros(count, np.complex64)
    frame = 0
    while True:
        start = epoch + round(frame * PERIOD)
        if start >= count:
            break
        piece = template[:min(len(template), count - start)]
        signal[start:start + len(piece)] += piece
        frame += 1
    time_s = np.arange(count) / SAMPLE_RATE_HZ
    signal = (signal * np.exp(-2j * np.pi * offset_hz * time_s)).astype(np.complex64)
    gain = np.sqrt(10 ** (snr_db / 10) * np.mean(np.abs(background) ** 2)
                   / max(np.mean(np.abs(signal) ** 2), 1e-30))
    return (gain * signal + background).astype(np.complex64)


def _interleave(rx0, rx1):
    """Pack two complex streams the way the driver delivers them: i0 q0 i1 q1."""
    count = min(rx0.size, rx1.size)
    raw = np.empty(count * 4, dtype=np.int16)
    raw[0::4] = np.clip(rx0.real[:count] * 200, -2047, 2047)
    raw[1::4] = np.clip(rx0.imag[:count] * 200, -2047, 2047)
    raw[2::4] = np.clip(rx1.real[:count] * 200, -2047, 2047)
    raw[3::4] = np.clip(rx1.imag[:count] * 200, -2047, 2047)
    return raw.tobytes()


class _Attr:
    def __init__(self, value=""):
        self.value = value


class _Channel:
    def __init__(self, identifier, output=False):
        self.id, self.output, self.enabled = identifier, output, False
        self.attrs = {"frequency": _Attr("0"), "sampling_frequency": _Attr("0"),
                      "rf_bandwidth": _Attr("0"), "hardwaregain": _Attr("0")}


class _Device:
    def __init__(self, channels):
        self.channels = channels
        self.kernel_buffers = None

    def set_kernel_buffers_count(self, count):
        self.kernel_buffers = count


class _Buffer:
    """Hands back whatever the context says is present at the current tuning.

    Deliberately offers only what libiio's Buffer offers: refill and read.  A
    fake with a teardown method the real binding lacks would let a leak pass.
    """

    def __init__(self, context):
        self._context = context

    def refill(self):
        self._context.refills += 1

    def read(self):
        return self._context.payload_for(self._context.tuned_hz)


class _Context:
    def __init__(self, payloads, *, quiet=None):
        self._phy = _Device([_Channel("altvoltage0", output=True),
                             _Channel("voltage0"), _Channel("voltage1")])
        self._stream = _Device([_Channel("voltage0"), _Channel("voltage1")])
        self._payloads = payloads
        self._quiet = quiet
        self.refills = 0
        self.buffers = []
        self.tune_history = []

    @property
    def tuned_hz(self):
        return int(float(self._phy.channels[0].attrs["frequency"].value or 0))

    def payload_for(self, tuned_hz):
        if tuned_hz not in self._payloads:
            return self._quiet
        return self._payloads[tuned_hz]

    def find_device(self, name):
        if name == "ad9361-phy":
            return self._phy
        if name == "cf-ad9361-lpc":
            return self._stream
        raise KeyError(name)


@pytest.fixture
def fake_iio(monkeypatch):
    """Stand in for the host-only libiio module the scan imports lazily.

    The only registry of what was created lives on the context, so a test can
    drop it and see whether anything else still holds the buffer alive.
    """
    def buffer_factory(device, size, cyclic):
        context = device.context
        made = _Buffer(context)
        context.buffers.append(made)
        return made

    module = types.ModuleType("iio")
    module.Buffer = buffer_factory
    monkeypatch.setitem(sys.modules, "iio", module)


def _context(payloads, quiet):
    context = _Context(payloads, quiet=quiet)
    context._stream.context = context
    return context


CH4_LOWER_IF_HZ = 1_709_687_500


def _tuning_payloads(*, loud_at=CH4_LOWER_IF_HZ, rx1_offset_hz=0.0, seed=5):
    """One loud tuning on both receivers, silence everywhere else."""
    loud0 = _beacon(-3, seed=seed)
    loud1 = _beacon(-3, seed=seed + 1, offset_hz=rx1_offset_hz)
    quiet = _interleave(_noise(50_000, seed + 2), _noise(50_000, seed + 3))
    return {loud_at: _interleave(loud0, loud1)}, quiet


def test_the_survey_scores_both_receivers(fake_iio):
    """The dwell keeps two receivers, so a survey of one cannot be compared."""
    payloads, quiet = _tuning_payloads()
    context = _context(payloads, quiet)

    outcome = scan_radio(context, LOW_BAND_TUNINGS, sample_rate_hz=SAMPLE_RATE_HZ)

    assert len(outcome["results"]) == len(LOW_BAND_TUNINGS)
    for entry in outcome["results"]:
        assert [item["receiver"] for item in entry["receivers"]] == [0, 1]


def test_an_offset_port_now_scores_beside_a_centred_one(fake_iio):
    """The caveat this test used to defend has been paid off, and it is checked here.

    It asserted the opposite until the bank widened: both ports carrying the
    same beacon at the same strength, the 436 kHz port scored less than half
    what the centred one did (3.71 against 1.63), so a quiet verdict on an
    offset port said nothing about the sky and the record had to say so.

    Thirteen hypotheses over +/-700 kHz put that port 31 kHz from a hypothesis
    instead of 136 kHz from one and the gap closes to 1.08x.  This is the
    assertion that has to fail before ``quiet_verdict_caveat`` goes back into
    the survey record, so it is inverted rather than deleted.
    """
    payloads, quiet = _tuning_payloads(rx1_offset_hz=436_000.0, seed=31)

    outcome = scan_radio(_context(payloads, quiet), [(4, "lower")],
                         sample_rate_hz=SAMPLE_RATE_HZ)

    centred, offset = outcome["results"][0]["receivers"]
    assert offset["peak_to_median"] > detection_threshold(SURVEY_BANK)
    assert centred["peak_to_median"] < 1.4 * offset["peak_to_median"]


def test_re_centring_the_survey_bank_does_not_rescue_the_offset_port(fake_iio):
    """Why the analysis path's calibration is deliberately not applied here.

    Moving every hypothesis onto the signal lifts the median as much as the
    peak, and the statistic is a ratio, so the score falls rather than rises.
    A finer bank does not change that -- it is a property of the statistic, not
    of the spacing -- so the comparison is made at the survey's own span on
    both sides, which is the only way it compares one choice rather than two.
    """
    from leo_tracker.radio.beacon.fast_scan import build_bank, probe

    samples = _beacon(-3, seed=41, offset_hz=436_000.0)
    as_it_stands = probe(samples, survey_bank("lower", SAMPLE_RATE_HZ))
    recentred = probe(samples, build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK,
                                          SURVEY_OFFSET_SPAN_HZ,
                                          center_hz=436_000.0))

    assert recentred["peak_to_median"] < as_it_stands["peak_to_median"]


def test_one_listen_serves_both_receivers(fake_iio):
    """Both receivers ride the same capture, so listening is charged once.

    Stated as samples read rather than as a refill count, because the number
    of refills depends on how much this fixture returns per read while the
    invariant does not: what must never double is the radio time.
    """
    payloads, quiet = _tuning_payloads()
    context = _context(payloads, quiet)
    per_read = 50_000                       # samples in the fixture's payload

    scan_radio(context, LOW_BAND_TUNINGS, sample_rate_hz=SAMPLE_RATE_HZ)

    wanted = int(SURVEY_PROFILE.probe_s * SAMPLE_RATE_HZ)
    assert context.refills * per_read == len(LOW_BAND_TUNINGS) * wanted


def test_a_tuning_is_ranked_by_its_best_receiver(fake_iio):
    """Either port seeing a pilot makes the channel worth dwelling on."""
    payloads, quiet = _tuning_payloads()

    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    top = outcome["results"][0]
    assert top["channel"] == 4 and top["region"] == "lower-edge"
    assert top["peak_to_median"] == max(item["peak_to_median"]
                                        for item in top["receivers"])
    assert [entry["peak_to_median"] for entry in outcome["results"]] == sorted(
        (entry["peak_to_median"] for entry in outcome["results"]), reverse=True)


def test_the_record_names_which_channels_were_active(fake_iio):
    """The point of the record: what the scanner called active, per receiver."""
    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    assert record["state"] == "complete"
    assert record["active"] == [{"channel": 4, "region": "lower-edge",
                                 "receiver": 0},
                                {"channel": 4, "region": "lower-edge",
                                 "receiver": 1}]
    assert record["dwell"] == {"channel": 4, "region": "lower-edge"}
    assert record["threshold"] == detection_threshold(SURVEY_BANK)


def test_the_record_keeps_the_score_that_produced_the_verdict(fake_iio):
    """A bare boolean cannot be re-thresholded once the ceiling moves."""
    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    scored = [item for entry in record["tunings"] for item in entry["receivers"]]
    assert len(scored) == 2 * len(LOW_BAND_TUNINGS)
    for item in scored:
        assert item["peak_to_median"] > 0
        assert item["active"] == (item["peak_to_median"] >= record["threshold"])
        assert "frequency_offset_hz" in item and "epoch_s" in item


def test_anchor_agreement_is_zero_on_noise_and_positive_on_a_pilot(fake_iio):
    """Corroboration, not another view of the same fold.

    Each anchor symbol picks an epoch independently. A pilot repeats at every
    anchor so several land on the same one; noise has no reason to agree with
    itself, so its count sits at zero with the occasional coincidence, against
    several for a pilot. Measured over 30 noise realisations the 95th
    percentile is 0.55, which is what makes two a separating count rather than
    a marginal one.
    """
    from leo_tracker.radio.beacon.fast_scan import build_bank, probe

    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    quiet = sorted(probe(_noise(50_000, 800 + s), bank)["anchor_agreement"]
                   for s in range(8))
    loud = [probe(_beacon(-6, seed=810 + s), bank)["anchor_agreement"]
            for s in range(8)]

    assert quiet[len(quiet) // 2] == 0 and max(quiet) <= 1
    assert min(loud) >= 2


def test_offset_contrast_separates_narrowband_from_broadband(fake_iio):
    """What peak-to-median structurally cannot see.

    A pilot is localised to one frequency hypothesis. Anything that lifts every
    hypothesis together divides out of a peak-to-median ratio, because it
    raises the peak and the median alike.
    """
    from leo_tracker.radio.beacon.fast_scan import build_bank, probe

    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    localised = np.median([probe(_beacon(-6, seed=820 + s), bank)["offset_contrast"]
                           for s in range(8)])
    flat = np.median([probe(_noise(50_000, 830 + s), bank)["offset_contrast"]
                      for s in range(8)])

    assert localised > flat


def test_mean_power_is_the_one_absolute_quantity(fake_iio):
    """Every ratio divides gain away, so none can see a dead or clipping port."""
    from leo_tracker.radio.beacon.fast_scan import build_bank, probe

    bank = survey_bank("lower", SAMPLE_RATE_HZ)
    samples = _beacon(-6, seed=840)

    quiet = probe(samples, bank)
    loud = probe((samples * 2).astype(np.complex64), bank)

    assert loud["mean_power"] == pytest.approx(4 * quiet["mean_power"], rel=1e-6)
    assert loud["peak_to_median"] == pytest.approx(quiet["peak_to_median"],
                                                   rel=1e-6)


def test_every_corroborating_field_reaches_the_record(fake_iio):
    """They are useless unless they survive to the corpus."""
    from leo_tracker.radio.beacon.presurvey import CORROBORATION_FIELDS

    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    for entry in record["tunings"]:
        for item in entry["receivers"]:
            for field in CORROBORATION_FIELDS:
                assert field in item, field


def test_the_record_carries_the_iq_ordering(fake_iio):
    """The record sorts its tunings; the IQ does not. The map must be stored.

    These two orders coincide for the current tuning list, so a reader that
    infers one from the other works today and would silently score one tuning's
    samples against another's label the moment the list changes.
    """
    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ, keep_samples=True)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    assert record["sample_order"] == [(c, f"{e}-edge") for c, e in LOW_BAND_TUNINGS]
    assert len(record["sample_order"]) == outcome["samples"].shape[0]


def test_the_record_says_which_span_the_scores_came_from(fake_iio):
    """A shape without a span does not identify the search that produced a score.

    Thirteen hypotheses mean 116.7 kHz spacing over +/-700 kHz and 50 kHz over
    +/-300 kHz -- different detectors with different thresholds.  The scan used
    to report the module-level default here rather than the profile's own span,
    which was harmless only for as long as the two were the same number.
    """
    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    assert record["offset_span_hz"] == SURVEY_OFFSET_SPAN_HZ
    assert record["profile"]["offset_span_hz"] == SURVEY_OFFSET_SPAN_HZ
    assert record["profile"]["shape"] == list(SURVEY_BANK)


def test_the_record_states_how_its_threshold_was_measured(fake_iio):
    """A stored verdict is only re-readable if its bar says what it meant.

    The corpus holds probes written under a threshold calibrated on synthetic
    Gaussian noise over 60 realisations, which cannot resolve a 1% tail, beside
    probes written under one measured on the sky over a null population that
    can.  Both are the field ``threshold``; only this string separates them.
    """
    from leo_tracker.radio.beacon.fast_scan import SURVEY_NULL_REALISATIONS

    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    assert str(SURVEY_NULL_REALISATIONS) in record["threshold_basis"]
    assert "upper-edge" in record["threshold_basis"]
    # The population bounds what the number can claim: an empirical quantile
    # over N samples cannot speak below roughly 3/N.
    assert 3 / SURVEY_NULL_REALISATIONS < 0.01


def test_the_record_is_json_serialisable(fake_iio):
    """It travels inside the capture manifest, which is written as JSON."""
    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    assert json.loads(json.dumps(record))["schema"].startswith("leo-tracker.")


def test_the_probe_is_eighty_milliseconds(fake_iio):
    """Sixty frames folded rather than fifteen, and the block sized to match."""
    assert SURVEY_PROFILE.probe_s == 0.080
    cost = SURVEY_PROFILE.cost_ms(SAMPLE_RATE_HZ)
    assert cost["buffers_listened"] == 1
    assert cost["signal_used_fraction"] == pytest.approx(1.0)


def test_the_retained_iq_is_what_the_driver_delivered(fake_iio):
    """Raw ci16, not the detector's complex64 view of it.

    A later analysis re-deciding from this one's conversion would inherit its
    choices; the point of keeping the signal is that it does not have to.
    """
    payloads, quiet = _tuning_payloads()
    context = _context(payloads, quiet)

    outcome = scan_radio(context, LOW_BAND_TUNINGS, sample_rate_hz=SAMPLE_RATE_HZ,
                         keep_samples=True)

    samples = outcome["samples"]
    assert samples.dtype == np.int16
    assert samples.shape[0] == len(LOW_BAND_TUNINGS)
    assert samples.shape[2:] == (2, 2)          # receiver, component


def test_the_iq_is_not_retained_unless_asked(fake_iio):
    """Twelve megabytes a capture is a choice, not a default of the scan."""
    payloads, quiet = _tuning_payloads()

    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    assert outcome["samples"] is None


def test_the_retained_iq_keeps_collection_order_not_score_order(fake_iio):
    """Results are sorted by score; the IQ cannot be, or they would mismatch."""
    payloads, quiet = _tuning_payloads()

    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ, keep_samples=True)

    assert outcome["sample_order"] == [(c, f"{e}-edge") for c, e in LOW_BAND_TUNINGS]
    assert [r["peak_to_median"] for r in outcome["results"]] == sorted(
        (r["peak_to_median"] for r in outcome["results"]), reverse=True)


def test_a_survey_failure_never_costs_the_capture(monkeypatch):
    """A delayed capture is gone for good; a missing survey is an annotation."""
    def explode(*args, **kwargs):
        raise RuntimeError("no radio here")

    monkeypatch.setattr("leo_tracker.radio.beacon.presurvey._open_context",
                        explode)

    record, samples = run_survey(uri="usb:9.9.9", serial=None)

    assert samples is None
    assert record["state"] == "failed"
    assert "no radio here" in record["error"]
    assert json.loads(json.dumps(record))


def test_the_survey_leaves_no_buffer_claimed(fake_iio):
    """A leaked buffer holds the context, and the dwell then cannot open it.

    libiio's binding has no explicit close, so the invariant is that no
    reference survives the call, not that some teardown method was invoked.
    """
    import gc
    import weakref

    payloads, quiet = _tuning_payloads()
    context = _context(payloads, quiet)
    scan_radio(context, LOW_BAND_TUNINGS, sample_rate_hz=SAMPLE_RATE_HZ)
    assert context.buffers
    witness = weakref.ref(context.buffers[0])

    context.buffers.clear()
    gc.collect()

    assert witness() is None


def test_the_survey_covers_the_eight_low_band_edge_tunings():
    """One LNB band, so a single pass can cover all eight and no more."""
    assert len(LOW_BAND_TUNINGS) == 8
    assert {edge for _, edge in LOW_BAND_TUNINGS} == {"lower", "upper"}
    assert sorted({channel for channel, _ in LOW_BAND_TUNINGS}) == [1, 2, 3, 4]


def test_the_survey_runs_at_its_drawn_rate_and_not_the_dwells(fake_iio,
                                                              monkeypatch):
    """These were the same number once and inheriting it is now wrong twice.

    The survey draws 2.5 or 5 MS/s; the dwell records at whatever its own mode
    wants, which for a wide capture is 10 MS/s and was never the survey's. The
    radio is programmed from the drawn configuration, and the record has to
    agree with the samples the radio then produced.
    """
    from leo_tracker.radio.beacon import presurvey

    payloads, quiet = _tuning_payloads()
    context = _context(payloads, quiet)
    monkeypatch.setattr(presurvey, "_open_context", lambda uri, serial: context)
    config = presurvey.config_by_name("80ms-5.0MSps")

    record, samples = run_survey(uri="usb:1.2.3", tunings=[(4, "lower")],
                                 config=config, keep_samples=True,
                                 lnb_lo_hz=9_750_000_000.0)

    assert record["state"] == "complete"
    assert record["sample_rate_hz"] == 5_000_000.0
    assert record["capture_config"]["name"] == "80ms-5.0MSps"
    assert record["capture_config"]["pilot_guard_hz"] == 1_562_500.0
    # The radio was told the drawn rate, not a default.
    assert context._phy.channels[1].attrs["sampling_frequency"].value == "5000000"
    assert samples.shape[1] == int(round(0.080 * 5_000_000.0))


def test_deferring_the_score_keeps_the_probes_and_costs_no_arithmetic(
        fake_iio, monkeypatch):
    """The capture host owns the radio; it need not own the detection.

    Deferring is only safe if the samples survive, because a deferred verdict
    over discarded IQ is no verdict at all. So the two have to move together:
    nothing scored here, everything kept for the host that will.
    """
    from leo_tracker.radio.beacon import presurvey

    payloads, quiet = _tuning_payloads()
    context = _context(payloads, quiet)
    monkeypatch.setattr(presurvey, "_open_context", lambda uri, serial: context)

    record, samples = run_survey(uri="usb:1.2.3", tunings=[(4, "lower")],
                                 keep_samples=True, score_on_capture_host=False)

    assert record["state"] == "complete"
    assert record["compute_ms"] == 0.0
    assert samples is not None and samples.shape[0] == 1
    # The tuning is still named and located; only the scores are absent.
    assert record["tunings"][0]["channel"] == 4
    assert record["tunings"][0]["receivers"] == []
    assert "analysis host" in record["capture_config"]["scored_on"]
    # And no verdict of any kind: zero active would read as a finding.
    assert record["scored_on_capture_host"] is False
    assert record["active"] is None and record["active_count"] is None
    assert record["threshold_calibrated"] is False


def test_a_simulated_run_never_opens_a_radio(monkeypatch):
    """A fake capture asking for a real survey would grab live hardware.

    On this site the default URI answers, so the survey opened a context on a
    Pluto a capture service owns and retuned it eight times. Nothing about
    ``--fake`` said not to.
    """
    from leo_tracker.radio.beacon import presurvey

    def explode(*args, **kwargs):
        raise AssertionError("a simulated survey must not open a radio")

    monkeypatch.setattr(presurvey, "_open_context", explode)

    record, samples = run_survey(uri="pluto://ip:192.168.2.1", simulated=True)

    assert record["state"] == "skipped" and samples is None
    assert "simulated" in record["reason"]
