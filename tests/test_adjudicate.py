"""The adjudicator's whole value is that its number was not used to choose the point.

Every other property here is checkable by inspection; that one is not, because a
statistic inflated by selection looks exactly like a statistic that found
something.  So the central test measures it directly, on noise, with enough
trials to say something, and beside it runs the same construction *without* the
withheld split so the size of the bias it removes is on the record too.

The rest of the file pins the ways this could quietly stop being a conditioned
test: a control that is allowed to search epoch and re-finds the real signal 187
samples early; a refinement that grows into an epoch search; a frame grid
rounded to 3333; a wrapped relative-phase offset accepted because the score
still looks convincing.  Each of those turns a verdict into a rediscovery of the
thing it was meant to check.

Backgrounds are real recorded probes wherever the corpus is mounted.  Synthetic
noise is used where a *guaranteed* signal-free null is the point, because a real
probe may hold a real transmission and there is no way to know from inside.
"""
import json
from pathlib import Path

import numpy as np
import pytest
from scipy import stats

from leo_tracker.radio.beacon.adjudicate import (
    CONTROL_SYMBOL_ROLL, DEFAULT_REFINE_CELLS, FRAME_FREQUENCY_RESOLUTION_HZ,
    SCHEMA, Certificate, acquire_certificate, adjudicate, conditioned_scores,
    control_epoch_shift_samples, epoch_score_scan, exhaustive_cost_estimate,
    exhaustive_sensitivity_bound, extreme_value_threshold, pilot_sample_indexes,
    search_penalty_db, symbol_split)
from leo_tracker.radio.beacon.injection import frame_offsets, inject
from leo_tracker.radio.beacon.pilots import conditioned_pilot_score, edge_pilot_frame
from leo_tracker.radio.beacon.relative_phase import AMBIGUITY_SPAN_HZ
from leo_tracker.radio.beacon.structure import STARLINK_FRAME_DURATION_S

RATE = 2_500_000.0
PERIOD = RATE * STARLINK_FRAME_DURATION_S      # 3333.333..., deliberately not an int
PROBE = 100_000                                # 40 ms: 30 frame slots, quick enough
CORPUS = Path("/mnt/qnap01/mouse9911/leo/surveys/corpus")


def _noise(count, seed):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(count) + 1j * rng.standard_normal(count))
            / np.sqrt(2)).astype(np.complex64)


def _replica(*, count=PROBE, epoch=0, cfo_hz=0.0, period=PERIOD):
    """A noiseless pilot in every slot, on the fractional frame grid.

    Built here rather than by ``inject`` because a known answer needs a signal
    with no noise in it at all, and ``inject`` sets its strength against the
    host's noise power.
    """
    frame = edge_pilot_frame(RATE, "lower")
    values = np.zeros(count, np.complex128)
    slots = int((count - epoch - frame.size) // period) + 1
    for slot in range(slots):
        start = epoch + int(round(slot * period))
        values[start:start + frame.size] += frame[:count - start]
    values *= np.exp(2j * np.pi * cfo_hz * np.arange(count) / RATE)
    return values.astype(np.complex64)


def _corpus_streams(limit=4, receivers=(0, 1)):
    """Real recorded probes, each with the edge its tuning actually was.

    ``sample_order`` is the only thing that says which block of the IQ is which
    tuning; the survey record's own list is sorted by score, not by collection
    order.  Entries that lack it are skipped rather than guessed at, because a
    guessed order would silently score an upper-edge tuning with lower-edge
    codes and quietly become a cross-edge null.

    The block shape comes from the manifest too, for the same reason: another
    change is altering bank shapes, and a hard-coded ``(8, 200000, 2, 2)`` would
    reinterpret a differently shaped capture rather than skip it.
    """
    if not CORPUS.is_dir():
        pytest.skip(f"survey corpus is not mounted at {CORPUS}")
    found = []
    for entry in sorted(CORPUS.iterdir()):
        try:
            manifest = json.loads((entry / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        record = (manifest.get("metadata") or {}).get("pre_dwell_survey") or {}
        order = record.get("sample_order")
        shape = manifest.get("survey_iq") or {}
        if not order or not (entry / "survey.ci16").is_file():
            continue
        if shape.get("dtype") != "ci16_le" or not shape.get("samples_per_tuning"):
            continue
        block = np.memmap(entry / "survey.ci16", dtype="<i2", mode="r").reshape(
            int(shape["tunings"]), int(shape["samples_per_tuning"]),
            manifest.get("receiver_count", 2), 2)
        for index, (_, region) in enumerate(order[:block.shape[0]]):
            for receiver in receivers:
                piece = np.asarray(block[index, :, receiver, :])
                found.append((f"{entry.name}#{index}rx{receiver}",
                              region.replace("-edge", ""),
                              (piece[:, 0] + 1j * piece[:, 1]).astype(np.complex64)))
                if len(found) >= limit:
                    return found
    if not found:
        pytest.skip("no corpus entry carries sample_order")
    return found


# --------------------------------------------------------------------------
# What a verdict says
# --------------------------------------------------------------------------

def test_a_true_certificate_is_confirmed_and_a_false_one_is_not():
    """The claim is the hypothesis, and the point of the exercise is to refute it.

    Conditioned at the injected epoch and offset the score is far above the
    null; one frame period away in time, or at a frequency the signal is not at,
    it sits at the null.  A statistic that could not tell those apart would
    confirm every certificate handed to it.
    """
    host = _noise(PROBE, seed=1)
    made = inject(host, sample_rate_hz=RATE, epoch_sample=900, cfo_hz=7_000.0,
                  snr_db=-6.0, occupancy=1.0, seed=2)
    at = lambda epoch, cfo: adjudicate(
        made["samples"], {"epoch_sample": epoch, "cfo_hz": cfo},
        sample_rate_hz=RATE)["conditioned"]["verify"]

    true = at(900, 7_000.0)
    late = at(900 + int(round(PERIOD)) + 500, 7_000.0)
    elsewhere = at(900, -7_000.0)

    assert true["score"] > 0.4 and true["control_score"] < 0.03
    assert true["margin"] > 0.4
    for wrong in (late, elsewhere):
        assert wrong["score"] < 0.05
        assert abs(wrong["margin"]) < 0.02


def test_the_verdict_carries_every_number_a_reader_could_want_to_rethreshold_with():
    """A bare boolean would make the threshold un-revisable without the IQ.

    The IQ is 12.8 MB a probe and is deleted on a retention timer; a verdict
    outlives it.  So the verdict has to carry the per-frame scores for both
    codes and both symbol sets — enough for a later reader to apply a different
    frame combiner for sparse occupancy, or a different threshold entirely —
    and it has to survive the JSON round trip into whatever stores it.
    """
    made = inject(_noise(PROBE, seed=3), sample_rate_hz=RATE, epoch_sample=512,
                  cfo_hz=0.0, snr_db=-9.0, occupancy=1.0, seed=4)

    verdict = adjudicate(made["samples"], {"epoch_sample": 512, "cfo_hz": 0.0},
                         sample_rate_hz=RATE)
    restored = json.loads(json.dumps(verdict))

    assert restored["schema"] == SCHEMA
    assert set(restored["conditioned"]) == {"full", "acquire", "verify"}
    for block in restored["conditioned"].values():
        assert len(block["frame_scores"]) == block["frames"] == verdict["frames"]
        assert len(block["control_frame_scores"]) == block["frames"]
        assert block["score"] == pytest.approx(
            float(np.mean(block["frame_scores"])), rel=1e-9)
    assert restored["refinement"]["cells"] == DEFAULT_REFINE_CELLS
    assert restored["epoch_sample"] == 512
    assert "verdict_basis" in restored and "withheld_basis" in restored


def test_a_verdict_never_claims_the_verify_set_was_withheld_unless_it_can_show_it():
    """"Withheld" is a claim about the proposer, not about this module.

    Nothing here can tell whether the detector that produced a certificate
    looked at the verify symbols. A certificate that does not say gets
    ``unknown``; one that names an overlapping set gets ``no`` and a caveat, and
    only a genuinely disjoint proposer earns ``yes``. Reporting ``yes`` by
    default would be the exact failure this component exists to prevent.
    """
    values = _replica(epoch=200)
    split = symbol_split()
    silent = {"epoch_sample": 200, "cfo_hz": 0.0}
    honest = dict(silent, acquire_symbols=[int(i) for i in split.acquire])
    greedy = dict(silent, acquire_symbols=list(range(2, 302)))

    assert adjudicate(values, silent, sample_rate_hz=RATE,
                      split=split)["withheld"] == "unknown"
    assert adjudicate(values, honest, sample_rate_hz=RATE,
                      split=split)["withheld"] == "yes"
    refused = adjudicate(values, greedy, sample_rate_hz=RATE, split=split)
    assert refused["withheld"] == "no"
    assert any("took part in selection" in note for note in refused["caveats"])


# --------------------------------------------------------------------------
# The test this component exists to pass
# --------------------------------------------------------------------------

def test_selecting_the_epoch_on_acquire_does_not_inflate_the_verify_score():
    """The whole withheld construction is worthless if this fails.

    On noise, choose the epoch that maximises the ACQUIRE symbols and then score
    the disjoint VERIFY symbols there.  If selection leaks, VERIFY at the chosen
    epoch is drawn from a higher distribution than VERIFY at an epoch chosen at
    random, and every threshold built on the verdict is optimistic.

    The statistic is a **rank**, because a rank needs no assumptions: if the
    chosen epoch is independent of the VERIFY curve then its rank among all 3333
    epochs of that curve is exactly Uniform(0,1), however strongly neighbouring
    epochs are correlated.  Measured over 600 seeded trials, each scoring all
    3,333 epochs on both symbol sets: mean rank **0.4898** against 0.5000
    expected, 0.87 sigma; KS against Uniform gives D=0.033, p=0.53; the fraction
    above 0.9 is 0.102 against 0.100 expected; and VERIFY at the chosen epoch is
    **0.10% below** VERIFY at a random one, in an experiment that resolves 1.5%.

    Beside it, the same construction with the split violated — selecting and
    scoring on the *same* symbols — gives mean rank 0.9998 and **+48%**
    inflation.  That is the bias being removed, and it is not subtle.
    """
    split = symbol_split("interleaved")
    draw = np.random.default_rng(4242)
    withheld, reused, drawn = [], [], []
    for trial in range(600):
        values = _noise(60_000, seed=1000 + trial)
        acquire = epoch_score_scan(values, RATE, 0.0, symbols=split.acquire)
        verify = epoch_score_scan(values, RATE, 0.0, symbols=split.verify)
        epoch = int(np.argmax(acquire["scores"]))
        rank = lambda curve, value: float(np.mean(curve < value))
        withheld.append((rank(verify["scores"], verify["scores"][epoch]),
                         float(verify["scores"][epoch])))
        reused.append(rank(acquire["scores"], acquire["scores"][epoch]))
        drawn.append(float(verify["scores"][draw.integers(verify["epochs"])]))

    ranks = np.asarray([item[0] for item in withheld])
    selected = np.asarray([item[1] for item in withheld])
    drawn = np.asarray(drawn)
    sigma = (ranks.mean() - 0.5) * np.sqrt(12 * ranks.size)
    inflation = selected.mean() / drawn.mean() - 1.0
    resolvable = 2 * np.sqrt(selected.var(ddof=1) + drawn.var(ddof=1)) / (
        np.sqrt(ranks.size) * drawn.mean())

    assert abs(sigma) < 4.0, f"mean rank {ranks.mean():.4f} is {sigma:.1f} sigma off"
    assert stats.kstest(ranks, "uniform").statistic < 0.079   # 99.9% critical, n=600
    assert abs(np.mean(ranks > 0.9) - 0.1) < 0.04
    assert abs(inflation) < max(resolvable, 0.01), inflation
    assert resolvable < 0.02, "too few trials for this assertion to mean anything"
    # and the contrast, which is what selection bias actually looks like
    assert float(np.mean(reused)) > 0.99


# --------------------------------------------------------------------------
# The null, and what it can and cannot support
# --------------------------------------------------------------------------

def test_the_conditioned_null_is_narrow_because_it_averages_sixty_frames():
    """Where a threshold comes from, and why it is not the exponential one.

    Each frame's normalised correlation against noise is Rayleigh-ish, whose
    coefficient of variation is 0.523.  The statistic averages ~59 of them, so
    its own cv is 0.523/sqrt(59) = 0.068 and its tail is nearly Gaussian rather
    than exponential.  That matters: the plan's 6.6 dB search penalty is derived
    from an exponential tail and does **not** apply to this combiner, and this
    test pins the shape the real penalty has to be computed from.

    Measured over 600 draws at random (epoch, cfo) on synthetic noise: mean
    0.0218, cv 0.070, p99 0.0254, largest of 600 draws 0.0263.
    """
    draw = np.random.default_rng(5)
    split = symbol_split()
    scores, controls = [], []
    for probe in range(10):
        values = _noise(200_000, seed=60_000 + probe)
        for _ in range(20):
            epoch = int(draw.integers(0, round(PERIOD)))
            cfo = float(draw.uniform(-350_000, 350_000))
            scores.append(float(conditioned_scores(
                values, RATE, epoch, [cfo], symbols=split.verify).mean_scores[0]))
            controls.append(float(conditioned_scores(
                values, RATE, epoch, [cfo], symbols=split.verify,
                symbol_roll=CONTROL_SYMBOL_ROLL).mean_scores[0]))
    scores = np.asarray(scores)

    assert scores.mean() == pytest.approx(0.0218, rel=0.05)
    assert scores.std(ddof=1) / scores.mean() == pytest.approx(0.070, rel=0.25)
    assert np.percentile(scores, 99) < 0.028
    # 200 draws bound a false-alarm rate no smaller than 3/200 at 95%; anything
    # finer than that is extrapolation and this test refuses to imply otherwise.
    assert scores.size >= 200
    assert 3.0 / scores.size > 0.01
    # and the wrong code, held at the same point, is the same distribution
    assert float(np.mean(controls)) == pytest.approx(scores.mean(), rel=0.05)


def test_the_real_sky_null_is_heavier_tailed_than_synthetic_noise():
    """Which is why the plan forbids calibrating on synthetic noise.

    Two nulls on real probes, both at random (epoch, cfo): the tuning's own edge
    — where a real transmission is possible but a randomly drawn epoch is
    essentially never on it — and the *other* edge, whose codes sit 230 MHz away
    and are pilot-free by construction.  They agree, which is the plan's
    two-null argument; and both run above the synthetic null in the tail.

    Measured over 960 draws from 12 corpus probes: own edge mean 0.0224, p99
    0.0271, max 0.0301; cross edge 0.0224 / 0.0261 / 0.0290; synthetic 0.0218 /
    0.0254 / 0.0263.  A threshold set on synthetic noise is about 6% low at p99
    and 14% low at the extreme, in a statistic whose whole spread is 7%.
    """
    draw = np.random.default_rng(6)
    split = symbol_split()
    own, cross = [], []
    for _, edge, values in _corpus_streams(limit=6):
        other = "upper" if edge == "lower" else "lower"
        for _ in range(20):
            epoch = int(draw.integers(0, round(PERIOD)))
            cfo = float(draw.uniform(-350_000, 350_000))
            own.append(float(conditioned_scores(
                values, RATE, epoch, [cfo], edge=edge,
                symbols=split.verify).mean_scores[0]))
            cross.append(float(conditioned_scores(
                values, RATE, epoch, [cfo], edge=other,
                symbols=split.verify).mean_scores[0]))
    own, cross = np.asarray(own), np.asarray(cross)

    assert own.mean() == pytest.approx(cross.mean(), rel=0.05)
    assert np.percentile(own, 99) == pytest.approx(np.percentile(cross, 99),
                                                   rel=0.15)
    assert own.mean() > 0.0218 * 0.95
    assert own.max() > 0.0263 * 0.95        # heavier than the synthetic extreme


def test_conditioning_beats_searching_at_a_common_false_alarm_rate():
    """The architectural claim, measured rather than cited — and smaller than cited.

    Both arms calibrate their own 1% threshold on signal-free probes and are
    then run on the same injections.  The searched arm sweeps 3,333 epochs by 11
    CFO cells; the conditioned arm evaluates the one point it was handed.  The
    searched threshold sits higher, so at a strength between the two thresholds
    the conditioned test confirms what the search cannot find.

    **The measured gap is about 2.3 dB, not 6.6.**  A larger run of the same
    experiment — 200 null probes, 6,000 conditioned null points — put the
    thresholds at 0.02575 searched against 0.02000 conditioned, a ratio of
    1.288; here, with a Gumbel fit to 60 searched nulls, 1.32.  At -34 dB the
    searched arm detects 6 of 30 injections and the conditioned arm 29 of 30.

    The 6.6 dB figure assumes an exponential tail, which is right for a
    single-frame power statistic and wrong for this one: averaging ~17 frames
    leaves a nearly Gaussian tail, and a Gaussian maximum grows with the square
    root of the log of the cell count rather than with the log.  A Gaussian-CLT
    model of this space predicts 1.91 dB and the exponential one 5.16 dB.  The
    direction of the claim survives; its size does not, and quoting 6.6 dB for
    this combiner would overstate the case by about 3 dB.

    The absolute strengths here are far below the plan's -21 to -6 dB because
    occupancy is 1.0 and every frame is averaged; they say nothing about field
    sensitivity and are not meant to.
    """
    offsets = np.arange(-1875.0, 1876.0, 375.0)
    searched = lambda values: max(
        float(epoch_score_scan(values, RATE, float(offset))["score"])
        for offset in offsets)
    draw = np.random.default_rng(7)

    search_null, point_null = [], []
    for probe in range(60):
        values = _noise(60_000, seed=90_000 + probe)
        search_null.append(searched(values))
        for _ in range(8):
            point_null.append(float(conditioned_scores(
                values, RATE, int(draw.integers(0, round(PERIOD))),
                [float(draw.uniform(-4000, 4000))]).mean_scores[0]))
    search_null, point_null = np.asarray(search_null), np.asarray(point_null)
    # Each arm gets the best estimate of its own 1% point. The searched arm is a
    # maximum over many cells, so its null is Gumbel and right-skewed and a
    # mean+2.326sd rule would understate it; 60 probes fit a Gumbel well and
    # cannot resolve a 1% tail empirically. The conditioned arm is cheap enough
    # to take empirically, and is near-Gaussian anyway.
    scale = search_null.std(ddof=1) * np.sqrt(6) / np.pi
    search_threshold = search_null.mean() + scale * (
        -np.log(-np.log(0.99)) - np.euler_gamma)
    point_threshold = float(np.percentile(point_null, 99))

    hits_searched = hits_point = 0
    for trial in range(30):
        epoch = int(draw.integers(0, round(PERIOD)))
        cfo = float(draw.choice(offsets))
        made = inject(_noise(60_000, seed=200_000 + trial), sample_rate_hz=RATE,
                      epoch_sample=epoch, cfo_hz=cfo, snr_db=-34.0,
                      occupancy=1.0, seed=300_000 + trial)
        hits_searched += searched(made["samples"]) > search_threshold
        hits_point += float(conditioned_scores(
            made["samples"], RATE, epoch, [cfo]).mean_scores[0]) > point_threshold

    assert search_threshold > point_threshold
    assert hits_point >= 26 and hits_searched <= 14
    # the gap is real but it is not the exponential-model gap
    measured_db = 20 * np.log10(search_threshold / point_threshold)
    assert 1.0 < measured_db < 4.0
    assert search_penalty_db(3333 * offsets.size) > 5.0


def test_the_extreme_value_arithmetic_is_the_plan_s_arithmetic():
    """The numbers the design argument rests on, pinned so they cannot drift.

    4.61 for one conditioned cell at 1%, 20.94 for the exhaustive sweep, 6.6 dB
    between them.  Kept as a separate function from anything that measures, so
    that a model figure is never mistaken for a measurement.
    """
    assert extreme_value_threshold(1) == pytest.approx(4.605, abs=0.001)
    assert extreme_value_threshold(3333 * 3734) == pytest.approx(20.94, abs=0.01)
    assert search_penalty_db(3333 * 3734) == pytest.approx(6.58, abs=0.02)
    assert search_penalty_db(1) == 0.0
    assert search_penalty_db(2.0) < 1.0             # what a refinement costs
    with pytest.raises(ValueError):
        extreme_value_threshold(0)


# --------------------------------------------------------------------------
# The ways a conditioned test stops being one
# --------------------------------------------------------------------------

def test_the_wrong_code_control_is_a_null_only_while_the_epoch_is_held():
    """The trap, pinned, because a comment would not survive the next change.

    Rolling the pilot codes by 17 symbols shifts the waveform by 17 symbol
    periods — 187 samples at 2.5 MS/s.  A control allowed to choose its own
    epoch therefore re-finds the *same real signal* 187 samples early and
    reports a wrong-code score that is really the right code seen late; on the
    corpus that inflates its p99 to 1.851 against a cross-edge null's 1.252.
    Held at the claimed epoch it collapses, which is the only mode this module
    ever uses it in.
    """
    made = inject(_noise(PROBE, seed=8), sample_rate_hz=RATE, epoch_sample=900,
                  cfo_hz=0.0, snr_db=-6.0, occupancy=1.0, seed=9)
    held = lambda epoch, roll: float(conditioned_scores(
        made["samples"], RATE, epoch, [0.0], symbol_roll=roll).mean_scores[0])

    exact = held(900, 0)
    control = held(900, CONTROL_SYMBOL_ROLL)
    searching = epoch_score_scan(made["samples"], RATE, 0.0,
                                 symbol_roll=CONTROL_SYMBOL_ROLL)

    assert control_epoch_shift_samples() == 187
    assert exact > 0.4 and control < 0.03           # held, it is a null
    assert searching["epoch_sample"] == 900 - 187   # searching, it is not
    assert searching["score"] > 0.35
    assert held(900 - 187, CONTROL_SYMBOL_ROLL) == pytest.approx(
        searching["score"], rel=1e-3)


def test_the_refinement_refines_frequency_and_never_the_epoch():
    """Because letting the epoch move re-opens the control's escape route.

    The verdict's epoch must be the certificate's epoch to the sample, whatever
    the neighbouring epochs score — and here they score three times lower one
    sample away, which is exactly the pull a search would follow.
    """
    made = inject(_noise(PROBE, seed=10), sample_rate_hz=RATE, epoch_sample=901,
                  cfo_hz=0.0, snr_db=-6.0, occupancy=1.0, seed=11)

    verdict = adjudicate(made["samples"], {"epoch_sample": 900, "cfo_hz": 0.0},
                         sample_rate_hz=RATE)

    assert verdict["epoch_sample"] == 900
    assert verdict["refinement"]["searched"].startswith("frequency only")
    neighbours = {item["epoch_offset_samples"]: item["score"]
                  for item in verdict["epoch_neighbourhood"]}
    assert neighbours[1] > 3 * verdict["conditioned"]["verify"]["score"]
    assert verdict["conditioned"]["verify"]["score"] < 0.2      # the claim is off
    assert "diagnostic only" in verdict["neighbourhood_basis"]


def test_the_refinement_prices_its_own_cells_instead_of_hiding_them():
    """A refined peak is a searched peak, however narrow the search.

    Fifteen cells across one frame resolution is about two independent
    hypotheses and costs 0.61 dB by the exponential model; measured on noise the
    refined score runs ~6% above the conditioned one at the mean and ~4% at p99,
    so the model is the conservative side of the truth.  Both numbers are in the
    verdict, and a reader who wants the unrefined result has it in the same
    object.
    """
    draw = np.random.default_rng(12)
    plain, refined = [], []
    for probe in range(6):
        values = _noise(PROBE, seed=70_000 + probe)
        for _ in range(10):
            verdict = adjudicate(values, {"epoch_sample": int(draw.integers(0, 3333)),
                                          "cfo_hz": float(draw.uniform(-1e5, 1e5))},
                                 sample_rate_hz=RATE, neighbourhood_samples=())
            plain.append(verdict["conditioned"]["verify"]["score"])
            refined.append(verdict["refinement"]["score"])
    inflation = float(np.mean(refined)) / float(np.mean(plain)) - 1.0

    assert verdict["refinement"]["window_hz"] == [-375.0 + verdict[
        "claimed_frequency_offset_hz"], 375.0 + verdict[
        "claimed_frequency_offset_hz"]]
    assert verdict["refinement"]["cells"] == 15
    assert verdict["refinement"]["independent_cells"] == pytest.approx(2.0)
    assert verdict["refinement"]["frame_frequency_resolution_hz"] == 750.0
    assert 0.55 < verdict["refinement"]["search_penalty_db"] < 0.65
    assert 0.02 < inflation < 0.10
    assert verdict["conditioned_penalty_db"] == 0.0


def test_a_relative_phase_claim_that_wrapped_is_refuted_rather_than_confirmed():
    """The one failure mode the cheap phase detectors cannot see themselves.

    An adjacent-symbol offset estimate repeats every 227.3 kHz, and a residual
    outside that window is reported 227 kHz wrong with the score barely dented.
    The full-frame conditioned statistic is not periodic — its resolution is one
    frame, 750 Hz — so a wrapped certificate lands at the null here.  That makes
    adjudication the stage that catches the wrap, which is worth knowing before
    anyone trusts a phase estimate on its own.
    """
    made = inject(_noise(PROBE, seed=13), sample_rate_hz=RATE, epoch_sample=900,
                  cfo_hz=40_000.0, snr_db=-6.0, occupancy=1.0, seed=14)
    at = lambda cfo: float(conditioned_scores(
        made["samples"], RATE, 900, [cfo]).mean_scores[0])

    assert at(40_000.0) > 0.4
    assert at(40_000.0 + AMBIGUITY_SPAN_HZ) < 0.03
    assert at(40_000.0 - AMBIGUITY_SPAN_HZ) < 0.03


def test_an_offset_is_confirmed_in_both_signs_and_refuted_when_negated():
    """A sign convention only ever tested at zero is not tested.

    This repository carries an unresolved positive-slope sign bug on a quarter
    of qualified Doppler tracks, and an adjudicator that silently negated
    frequency would confirm every zero-offset certificate ever written while
    refuting every real one.
    """
    host = _noise(PROBE, seed=15)
    for offset in (-7_000.0, +7_000.0):
        made = inject(host, sample_rate_hz=RATE, epoch_sample=900,
                      cfo_hz=offset, snr_db=-6.0, occupancy=1.0, seed=16)
        at = lambda cfo: float(conditioned_scores(
            made["samples"], RATE, 900, [cfo]).mean_scores[0])

        assert at(offset) > 0.4
        assert at(-offset) < 0.03


def test_frames_are_read_off_the_fractional_period_not_a_rounded_one():
    """3333.333 samples, not 3333.  Twenty samples of drift over sixty slots.

    The consequence for a certificate is sharper than for a detector: because
    the grid is ``round(m * 3333.333)`` and not a multiple of anything, moving a
    claimed epoch by 3333 samples is **not** the same claim — two frames in six
    land a sample late and the score falls by a quarter.  Moving it by 10,000,
    which is on the grid, is free.  An epoch therefore has to be given in the
    probe's own coordinates and must not be folded by a rounded period.
    """
    values = _replica(epoch=900)
    at = lambda epoch: float(conditioned_scores(
        values, RATE, epoch, [0.0]).mean_scores[0])

    assert frame_offsets(RATE, 4).tolist() == [0, 3333, 6667, 10_000]
    assert at(900) == pytest.approx(1.0, abs=1e-6)
    assert at(900 + 10_000) == pytest.approx(1.0, abs=1e-6)     # on the grid
    assert 0.7 < at(900 + 3333) < 0.85                          # off it
    # joint translation of signal and claim, away from the boundaries, is free
    assert float(conditioned_scores(_replica(epoch=1500), RATE, 1500,
                                    [0.0]).mean_scores[0]) == pytest.approx(
        1.0, abs=1e-6)
    assert float(conditioned_scores(_replica(epoch=900, period=3333.0), RATE, 900,
                                    [0.0]).mean_scores[0]) < 0.8


# --------------------------------------------------------------------------
# The primitives
# --------------------------------------------------------------------------

def test_a_noiseless_replica_scores_one_whatever_the_symbol_subset():
    """Known answer, and the reason an ACQUIRE score and a VERIFY score compare.

    ``|r^H s| / sqrt(|r|^2 |s|^2)`` is 1.0 for a perfect match by construction,
    and normalising over only the chosen symbols' samples keeps it 1.0 for a
    half-sized set.  Normalising over the whole frame instead would put a
    150-symbol score at 0.707 and make every cross-set comparison a subtraction
    of two different conventions.
    """
    values = _replica()
    split = symbol_split()

    for symbols in (None, split.acquire, split.verify, split.all_symbols):
        assert float(conditioned_scores(values, RATE, 0, [0.0],
                                        symbols=symbols).mean_scores[0]
                     ) == pytest.approx(1.0, abs=1e-6)
    assert float(conditioned_scores(values, RATE, 0, [0.0],
                                    symbol_roll=CONTROL_SYMBOL_ROLL
                                    ).mean_scores[0]) < 0.2


def test_the_conditioned_score_agrees_with_the_repositorys_full_frame_scorer():
    """Two implementations of one statistic, so neither can drift alone.

    ``pilots.conditioned_pilot_score`` normalises the data energy over the whole
    3333-sample frame; this one normalises over the 3300 samples the published
    symbols actually occupy, which is the only difference and is worth exactly
    ``sqrt(3333/3300) = 1.005``.  Anything larger than that is a real
    disagreement.
    """
    made = inject(_noise(PROBE, seed=17), sample_rate_hz=RATE, epoch_sample=777,
                  cfo_hz=1_500.0, snr_db=-6.0, occupancy=1.0, seed=18)
    mine = float(conditioned_scores(made["samples"], RATE, 777,
                                    [1_500.0]).mean_scores[0])
    theirs = conditioned_pilot_score(made["samples"], RATE, 777, 1_500.0)

    assert mine / theirs["score"] == pytest.approx(1.005, abs=0.002)
    assert pilot_sample_indexes(RATE, np.arange(2, 302)).size == 3300


def test_the_epoch_scan_agrees_with_the_conditioned_reference_at_every_epoch():
    """The optimised path is gated against the obvious one, which is what stops
    optimisation quietly becoming redesign.

    The scan folds the CFO rotation into the template so a single FFT
    correlation serves all 3333 epochs; the reference gathers samples and sums
    them.  They are the same arithmetic and must agree to single-precision
    rounding, not merely correlate.
    """
    made = inject(_noise(PROBE, seed=19), sample_rate_hz=RATE, epoch_sample=640,
                  cfo_hz=2_000.0, snr_db=-6.0, occupancy=1.0, seed=20)
    scan = epoch_score_scan(made["samples"], RATE, 2_000.0)

    for epoch in (0, 137, 640, 3332):
        reference = conditioned_scores(made["samples"], RATE, epoch, [2_000.0])
        folded = float(reference.frame_scores[0][:scan["frames"]].mean())
        assert scan["scores"][epoch] == pytest.approx(folded, rel=2e-6)
    assert scan["epoch_sample"] == 640


def test_every_score_is_invariant_to_receiver_gain_and_a_global_phase():
    """Neither is knowable, so neither may change a verdict.

    Gain is an AGC setting and the global phase is where the LO happened to be
    when the buffer opened.  A statistic that moved with either would be
    measuring the receiver.
    """
    made = inject(_noise(PROBE, seed=21), sample_rate_hz=RATE, epoch_sample=300,
                  cfo_hz=0.0, snr_db=-6.0, occupancy=1.0, seed=22)
    turned = (made["samples"] * 137.0 * np.exp(1j * 2.1)).astype(np.complex64)
    claim = {"epoch_sample": 300, "cfo_hz": 0.0}

    plain = adjudicate(made["samples"], claim, sample_rate_hz=RATE)
    altered = adjudicate(turned, claim, sample_rate_hz=RATE)

    for name, block in plain["conditioned"].items():
        assert altered["conditioned"][name]["score"] == pytest.approx(
            block["score"], rel=1e-6)
        assert altered["conditioned"][name]["margin"] == pytest.approx(
            block["margin"], abs=1e-6)


def test_a_probe_holding_no_complete_frame_is_answered_rather_than_refused():
    """These run over a corpus, and one short probe must not stop a sweep.

    Zero is also the right answer — no frame means no evidence — and the verdict
    has to say which of the two it is, because a zero that means "refuted" and a
    zero that means "nothing to look at" are not the same finding.
    """
    verdict = adjudicate(_noise(5_000, seed=23), {"epoch_sample": 4_900,
                                                  "cfo_hz": 0.0},
                         sample_rate_hz=RATE)

    assert verdict["frames"] == 0
    assert verdict["conditioned"]["verify"]["score"] == 0.0
    assert any("no complete frame" in note for note in verdict["caveats"])


# --------------------------------------------------------------------------
# The split, and the reason its mode is a parameter
# --------------------------------------------------------------------------

def test_the_two_split_modes_trade_exchangeability_against_cfo_tolerance():
    """Which is why the mode is exposed rather than chosen once in here.

    An interleaved VERIFY spans the whole frame, so it decorrelates over one
    frame resolution — 750 Hz — exactly as ACQUIRE does, and the two sets are
    exchangeable.  A contiguous VERIFY spans half the frame and tolerates about
    twice the frequency error, which is worth having when the certificate's
    offset is only good to a few hundred hertz, and costs the exchangeability
    that makes the null clean.  Measured on a noiseless replica.
    """
    values = _replica(epoch=900)
    tolerance = {}
    for mode in ("interleaved", "contiguous"):
        split = symbol_split(mode)
        tolerance[mode] = [float(conditioned_scores(
            values, RATE, 900, [error], symbols=split.verify).mean_scores[0])
            for error in (0.0, 375.0, 750.0)]

    for mode, scores in tolerance.items():
        assert scores[0] == pytest.approx(1.0, abs=1e-6), mode
    assert tolerance["interleaved"][1] < 0.75            # half power by 375 Hz
    assert tolerance["contiguous"][1] > 0.85             # still nearly intact
    assert tolerance["interleaved"][2] < 0.05            # a null at 750 Hz
    assert tolerance["contiguous"][2] > 0.55
    assert FRAME_FREQUENCY_RESOLUTION_HZ == 750.0


def test_overlapping_acquire_and_verify_sets_are_refused_outright():
    """The disjointness is the entire content of the construction.

    A split that overlapped by one symbol would still look like a withheld test
    and would quietly leak selection into the verdict, so it is refused where it
    is built rather than caught downstream.
    """
    split = symbol_split()

    assert np.intersect1d(split.acquire, split.verify).size == 0
    assert split.acquire.size + split.verify.size == 300
    assert split.as_dict()["verify_count"] == 150
    with pytest.raises(ValueError, match="disjoint"):
        type(split)(acquire=np.arange(2, 200), verify=np.arange(150, 302),
                    mode="broken", verify_fraction=0.5, seed=None)
    with pytest.raises(ValueError, match="2..301"):
        symbol_split(last=400)
    for mode in ("interleaved", "contiguous", "random"):
        alternative = symbol_split(mode, 0.25, seed=3)
        assert alternative.verify.size == 75
        assert np.intersect1d(alternative.acquire, alternative.verify).size == 0


def test_a_certificate_survives_the_json_it_was_stored_as():
    """Certificates arrive from another process, through a file, as a dict.

    Only the epoch and the offset are load-bearing; everything else is
    provenance and must come back out of the verdict unchanged, so a stored
    verdict answers "who claimed this" without a join against a table that may
    no longer exist.
    """
    original = {"tuning": [1, "lower-edge"], "receiver": 0, "epoch_sample": 640,
                "cfo_hz": -1234.5, "utc": "2026-08-12T19:00:00Z",
                "method": "relative-phase.glrt-32", "score": 0.41,
                "control": 0.08, "margin": 0.33}

    claim = Certificate.from_mapping(json.loads(json.dumps(original)))
    verdict = adjudicate(_replica(epoch=640), claim, sample_rate_hz=RATE)

    assert claim.epoch_sample == 640 and claim.cfo_hz == -1234.5
    assert Certificate.from_mapping(claim) is claim
    assert verdict["certificate"]["method"] == "relative-phase.glrt-32"
    assert verdict["certificate"]["tuning"] == [1, "lower-edge"]
    with pytest.raises(ValueError, match="missing"):
        Certificate.from_mapping({"cfo_hz": 0.0})


def test_acquiring_on_one_half_and_judging_on_the_other_is_one_call_each():
    """The end-to-end construction, because it is the intended way in.

    A proposer that names its symbols is the only kind whose verdict can be
    certified withheld, and the pairing has to be usable without the caller
    reassembling it by hand every time.
    """
    made = inject(_noise(PROBE, seed=24), sample_rate_hz=RATE, epoch_sample=1234,
                  cfo_hz=0.0, snr_db=-9.0, occupancy=1.0, seed=25)
    split = symbol_split()

    proposed = acquire_certificate(made["samples"], RATE, split=split)
    verdict = adjudicate(made["samples"], proposed, sample_rate_hz=RATE,
                         split=split)

    assert proposed.epoch_sample == 1234
    assert proposed.method == "adjudicate.acquire-epoch-scan"
    assert verdict["withheld"] == "yes"
    assert verdict["conditioned"]["verify"]["score"] > 0.25
    assert verdict["conditioned"]["verify"]["control_score"] < 0.05


# --------------------------------------------------------------------------
# The exhaustive mode, which must stay hard to run by accident
# --------------------------------------------------------------------------

def test_the_exhaustive_sweep_will_not_start_until_its_cost_is_acknowledged():
    """Eighty-eight minutes a capture is not a thing to discover afterwards.

    A docstring nobody reads is not a guard, so the cost is a parameter that has
    to be passed and has to be large enough.  The estimate itself is free, which
    is the call that belongs in front of every use.
    """
    values = _noise(20_000, seed=26)
    offsets = np.arange(-700_000, 700_001, 375.0)
    estimate = exhaustive_cost_estimate(200_000, offsets)

    assert estimate["offsets"] == 3734
    assert estimate["cells"] == pytest.approx(3333 * 3734)
    assert estimate["seconds"] > 300
    assert estimate["capture_seconds"] > 60 * 60
    assert estimate["cost_ratio"] > 10_000
    assert estimate["search_penalty_db"] == pytest.approx(6.58, abs=0.02)
    with pytest.raises(ValueError, match="acknowledge_cost_seconds"):
        exhaustive_sensitivity_bound(values, RATE, frequency_offsets_hz=offsets,
                                     acknowledge_cost_seconds=1.0)


def test_the_exhaustive_bound_is_a_ceiling_and_reports_what_it_cost_to_get():
    """What is the most that could possibly be found in this probe?

    It finds the injected point without being told it, and then says what the
    peak had to clear: a maximum over epochs times offsets, not a conditioned
    number, and its control is taken **at the winning epoch** rather than
    searched, because a searched control re-finds the signal 187 samples early.
    """
    made = inject(_noise(30_000, seed=27), sample_rate_hz=RATE,
                  epoch_sample=1_500, cfo_hz=750.0, snr_db=-6.0, occupancy=1.0,
                  seed=28)
    offsets = np.arange(-1500.0, 1501.0, 375.0)
    estimate = exhaustive_cost_estimate(30_000, offsets)

    found = exhaustive_sensitivity_bound(
        made["samples"], RATE, frequency_offsets_hz=offsets,
        acknowledge_cost_seconds=estimate["seconds"] + 1.0)

    assert found["epoch_sample"] == 1_500
    assert found["frequency_offset_hz"] == pytest.approx(750.0)
    assert found["score"] > 0.4
    assert found["control_score"] < 0.05
    assert "conditioned at the winning epoch" in found["control_basis"]
    assert found["search_penalty_db"] > 4.0
    assert found["cells"] == pytest.approx(3333 * offsets.size)


# --------------------------------------------------------------------------
# Real probes
# --------------------------------------------------------------------------

def test_a_certificate_planted_in_a_real_probe_is_confirmed_where_it_was_planted():
    """Synthetic noise is what mis-calibrated the deployed threshold once.

    The background here is a real recorded probe with its real interference,
    real gain and whatever real signal it already held, and the injected claim
    still has to be confirmed at its own point and refuted a frame period away.
    The tuning's edge comes from ``sample_order``; entries without it are
    skipped rather than guessed at.
    """
    checked = 0
    for name, edge, values in _corpus_streams(limit=4, receivers=(0,)):
        made = inject(values, sample_rate_hz=RATE, edge=edge, epoch_sample=1_777,
                      cfo_hz=-5_000.0, snr_db=-9.0, occupancy=1.0, seed=29)
        verdict = adjudicate(made["samples"],
                             {"epoch_sample": 1_777, "cfo_hz": -5_000.0,
                              "edge": edge}, sample_rate_hz=RATE)
        elsewhere = adjudicate(made["samples"],
                               {"epoch_sample": 1_777 + 1_666,
                                "cfo_hz": -5_000.0, "edge": edge},
                               sample_rate_hz=RATE)

        assert verdict["conditioned"]["verify"]["score"] > 0.2, name
        assert verdict["conditioned"]["verify"]["control_score"] < 0.05, name
        assert elsewhere["conditioned"]["verify"]["score"] < 0.05, name
        checked += 1
    assert checked > 0
