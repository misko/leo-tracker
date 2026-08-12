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

from leo_tracker.radio.beacon.fast_scan import (SURVEY_BANK, SURVEY_PROFILE,
                                                detection_threshold, scan_radio)
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


def test_an_offset_port_scores_far_lower_on_the_same_beacon(fake_iio):
    """The search edge is real, and it is why a quiet verdict is not evidence.

    Both ports carry the same beacon at the same strength here; only the LNB
    disagreement differs.  If this margin ever narrows to nothing the caveat in
    the record can go, and until then it must not be read as a quiet sky.
    """
    payloads, quiet = _tuning_payloads(rx1_offset_hz=436_000.0, seed=31)

    outcome = scan_radio(_context(payloads, quiet), [(4, "lower")],
                         sample_rate_hz=SAMPLE_RATE_HZ)

    centred, offset = outcome["results"][0]["receivers"]
    assert centred["peak_to_median"] > 2 * offset["peak_to_median"]


def test_re_centring_the_survey_bank_does_not_rescue_the_offset_port(fake_iio):
    """Why the analysis path's calibration is deliberately not applied here.

    Moving every hypothesis onto the signal lifts the median as much as the
    peak, and the statistic is a ratio, so the score falls rather than rises.
    """
    from leo_tracker.radio.beacon.fast_scan import build_bank, probe

    samples = _beacon(-3, seed=41, offset_hz=436_000.0)
    as_it_stands = probe(samples, build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK))
    recentred = probe(samples, build_bank("lower", SAMPLE_RATE_HZ, SURVEY_BANK,
                                          center_hz=436_000.0))

    assert recentred["peak_to_median"] < as_it_stands["peak_to_median"]


def test_one_listen_serves_both_receivers(fake_iio):
    """Both receivers ride the same capture, so listening is charged once."""
    payloads, quiet = _tuning_payloads()
    context = _context(payloads, quiet)

    scan_radio(context, LOW_BAND_TUNINGS, sample_rate_hz=SAMPLE_RATE_HZ)

    assert context.refills == len(LOW_BAND_TUNINGS)


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


def test_the_record_is_json_serialisable(fake_iio):
    """It travels inside the capture manifest, which is written as JSON."""
    payloads, quiet = _tuning_payloads()
    outcome = scan_radio(_context(payloads, quiet), LOW_BAND_TUNINGS,
                         sample_rate_hz=SAMPLE_RATE_HZ)

    record = summarise(outcome, dwell_channel=4, dwell_region="lower-edge")

    assert json.loads(json.dumps(record))["schema"].startswith("leo-tracker.")


def test_a_survey_failure_never_costs_the_capture(monkeypatch):
    """A delayed capture is gone for good; a missing survey is an annotation."""
    def explode(*args, **kwargs):
        raise RuntimeError("no radio here")

    monkeypatch.setattr("leo_tracker.radio.beacon.presurvey._open_context",
                        explode)

    record = run_survey(uri="usb:9.9.9", serial=None)

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
