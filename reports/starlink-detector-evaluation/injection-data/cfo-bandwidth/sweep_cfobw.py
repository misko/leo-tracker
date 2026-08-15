"""The CFO x sample-rate x RX-bandwidth factorial on radio `.165`.

Why it exists.  ``fast_scan`` writes ``rf_bandwidth = sampling_frequency`` on
every sky arm, so on the sky corpus the digital Nyquist window and the AD9361
analog baseband filter are the same number and no sky measurement can tell them
apart.  This sweep sets them independently and imposes the carrier offset on the
transmitted waveform, so the offset is known by construction rather than
estimated by the pipeline whose sensitivity is in question.

Three limits could clip an offset pilot block (occupied width 1,875,000 Hz,
outermost pilot centres +/-820,312.5 Hz):

  digital   |CFO| <= (Fs - 1,875,000) / 2          moves with the sample rate
  analog    |CFO| <= (B_RX - 1,875,000) / 2        moves with the RX bandwidth
  search    |CFO| <= the coarse bank's outermost offset, plus what one symbol's
                     correlation tolerates -- 300 kHz for the deployed A bank,
                     700 kHz for E.  Moves with NEITHER.

The offset is imposed digitally at the sample rate, so beyond the digital window
the block wraps rather than vanishing: |x[n]| is untouched by a phase rotation,
so transmitted power is identical at every offset in this sweep and received
power moves only when the receive chain moves it.  That is what makes the power
column able to separate "the filter attenuated it" from "the detector lost it".

Loop order is rate, then offset, then bandwidth.  Bandwidth innermost means the
four bandwidth cells of one offset share one TX push, so nothing about the DMA
can differ between them; and the TX start is proved once per offset at a wide
reference bandwidth, before any narrow filter is applied, because a dead DMA and
a filter doing its job both read as the noise floor.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import signal
import sys
import time
import traceback
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rig_cfobw import (BANDWIDTHS_HZ, PROBE_MS, REF_BW_HZ, RX_GAIN_DB,  # noqa: E402
                       SAMPLE_RATES_HZ, TX_AMPLITUDE, TX_BW_HZ, VERIFY_GAIN_DB,
                       Rig, appender, power_row, probe_samples, snap_offset_hz,
                       tx_waveform)

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import survey_scoring as ss  # noqa: E402

EDGE, NULL_EDGE = "lower", "upper"

OFFSETS_HZ = (0, 100_000, 200_000, 300_000, 350_000, 400_000, 500_000,
              600_000, 700_000, 800_000, 900_000, 1_000_000)

TX_GAIN_DB = -20.0
CAPTURES_PER_CELL = 20

#: 40 captures x 2 receivers = 80 null probes per (rate, bandwidth), 320 per
#: rate.  Sized by ``survey_comparison.MINIMUM_EXCEEDANCES = 3``: a 1% threshold
#: needs 300 draws before the order statistic stops being set by two or three
#: of them.  The point statistics reach that on ~7 candidates a probe either
#: way, but the COARSE statistic yields one draw per probe, so at the 10
#: captures this started with its 1% bar would have come back
#: ``supported: false`` -- and the coarse front ends are the two detectors this
#: experiment most needs a real threshold for, because they are what the search
#: edge in the hypothesis belongs to.
NULL_CAPTURES = 40

#: Anything above this in counts means the converter is near its rail and the
#: power column has stopped being linear.  Full scale is 2048.
RX_PEAK_CEILING = 1500.0


class RailExceeded(RuntimeError):
    """The converter is clipping, so every power reading after it is a fiction.

    Raised past the per-cell handler deliberately.  A cell that fails for its
    own reasons is one lost cell; a rail is a statement about the whole run's
    calibration, and continuing would fill the file with cells whose power
    column cannot be read.
    """

SCRATCH = Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
               "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/cfobw")
RESULTS = SCRATCH / "cfo_bandwidth-165.jsonl"


def score(samples: np.ndarray, sample_rate_hz: float, banks: dict) -> dict:
    """Every searcher's certificate and every confirmer's verdict, once."""
    observation = ss.search_observation(samples, sample_rate_hz, edge=EDGE,
                                        banks=banks)
    points = ss.distinct_points(observation["certificates"], sample_rate_hz)
    confirmed = ss.confirm_points(samples, sample_rate_hz, points, edge=EDGE,
                                  null_edge=NULL_EDGE)
    return {
        "searchers": {certificate["method"]: {
            "score": certificate["score"],
            "control_score": certificate["control_score"],
            "epoch_sample": certificate["epoch_sample"],
            "cfo_hz": certificate["cfo_hz"]}
            for certificate in observation["certificates"]},
        "coarse": {name: {"peak_to_median": row["peak_to_median"],
                          "frequency_offset_hz": row["frequency_offset_hz"],
                          "epoch_sample": row["epoch_sample"],
                          "peak_to_second": row["peak_to_second"],
                          "offset_contrast": row["offset_contrast"],
                          "anchor_agreement": row["anchor_agreement"],
                          "mean_power": row["mean_power"]}
                   for name, row in observation["coarse"].items()},
        "points": [{"point_id": point["point_id"],
                    "epoch_sample": point["epoch_sample"],
                    "cfo_hz": point["cfo_hz"],
                    "claimed_by": point["claimed_by"],
                    "methods": {name: {
                        "score": value["score"],
                        "control_score": value["control_score"],
                        "cross_edge_score": value["cross_edge_score"]}
                        for name, value in point["methods"].items()}}
                   for point in confirmed],
    }


_BANKS: dict = {}


def _banks_for(sample_rate_hz: float) -> dict:
    """Per-process bank cache.  Warming is per rate and costs ~2 s, so a worker
    that scores a thousand probes at one rate pays it once."""
    if sample_rate_hz not in _BANKS:
        ss.warm(sample_rate_hz)
        _BANKS[sample_rate_hz] = ss._banks(EDGE, sample_rate_hz)
    return _BANKS[sample_rate_hz]


def _score_task(payload: tuple) -> dict:
    """Score one probe in a worker.  Identical arithmetic to a serial call."""
    sample_rate_hz, values = payload
    return score(np.asarray(values, np.complex64), sample_rate_hz,
                 _banks_for(sample_rate_hz))


class Scorer:
    """Hand probes to a pool of workers while the radio keeps capturing.

    The detector, not the radio, is what this sweep is rate-limited by: a probe
    that finds nothing raises ~7 candidate points and every one of them is
    conditioned three ways, so an empty cell costs several times what a detected
    one does.  Capture is serial because there is one radio; scoring is not.

    Results are drained in submission order, so the raw file stays in the order
    the probes were taken, and the queue is bounded so a fast capture loop
    cannot pile up whole cells of IQ in memory.
    """

    def __init__(self, workers: int):
        self.workers = int(workers)
        self.pool = (None if self.workers <= 1 else
                     mp.get_context("fork").Pool(self.workers))
        self.pending: deque = deque()
        self.limit = 2 * self.workers + 4

    def submit(self, sample_rate_hz: float, values: np.ndarray, meta: dict) -> list:
        if self.pool is None:
            return [(meta, score(values, sample_rate_hz, _banks_for(sample_rate_hz)))]
        self.pending.append(
            (meta, self.pool.apply_async(_score_task, ((sample_rate_hz, values),))))
        out = []
        while len(self.pending) > self.limit:
            meta, result = self.pending.popleft()
            out.append((meta, result.get()))
        return out

    def drain(self) -> list:
        out = []
        while self.pending:
            meta, result = self.pending.popleft()
            out.append((meta, result.get()))
        return out

    def close(self) -> None:
        if self.pool is not None:
            self.pool.terminate()
            self.pool.join()


def completed(path: Path, expect: int) -> set:
    """Cells already finished in an earlier attempt, so a restart resumes."""
    counts: dict = {}
    if not path.exists():
        return set()
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("kind") != "observation":
                continue
            key = (record["sample_rate_hz"], record["rx_bandwidth_hz"],
                   record["offset_nominal_hz"])
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count >= expect}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", type=float, nargs="*", default=list(SAMPLE_RATES_HZ))
    parser.add_argument("--bandwidths", type=float, nargs="*", default=list(BANDWIDTHS_HZ))
    parser.add_argument("--offsets", type=float, nargs="*", default=list(OFFSETS_HZ))
    parser.add_argument("--captures", type=int, default=CAPTURES_PER_CELL)
    parser.add_argument("--nulls", type=int, default=NULL_CAPTURES)
    parser.add_argument("--tx-gain", type=float, default=TX_GAIN_DB)
    parser.add_argument("--probe-ms", type=float, default=PROBE_MS)
    parser.add_argument("--tag", default="base")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--out", default=str(RESULTS))
    arguments = parser.parse_args()

    # A SIGTERM that skips the ``finally`` leaves TX2 radiating at the sweep's
    # drive level with a cyclic buffer still running in the FPGA.  Turning it
    # into an exception is what makes "kill the run" and "turn the transmitter
    # off" the same act.
    signal.signal(signal.SIGTERM,
                  lambda number, frame: (_ for _ in ()).throw(
                      KeyboardInterrupt("SIGTERM")))

    path = Path(arguments.out)
    write = appender(path)
    done = completed(path, 2 * arguments.captures)
    if done:
        print(f"resuming: {len(done)} cells already complete", flush=True)

    rig = None
    failures = []
    scorer = Scorer(arguments.workers)
    try:
        rig = Rig(rx_gain_db=RX_GAIN_DB)
        write({"kind": "header", "tag": arguments.tag,
               "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "uri": "ip:192.168.1.165",
               "context": rig.context_attrs,
               "edge": EDGE, "null_edge": NULL_EDGE,
               "probe_ms": arguments.probe_ms,
               "rx_gain_db": rig.rx_gain_db, "rx_gain_mode": "manual",
               "tx_gain_db": arguments.tx_gain, "tx_port": 2,
               "tx_bandwidth_hz": TX_BW_HZ, "tx_amplitude_counts": TX_AMPLITUDE,
               "reference_bandwidth_hz": REF_BW_HZ,
               "sample_rates_hz": arguments.rates,
               "rx_bandwidths_hz": arguments.bandwidths,
               "offsets_hz": arguments.offsets,
               "captures_per_cell": arguments.captures,
               "null_captures": arguments.nulls,
               "scoring_workers": arguments.workers,
               "note": "CABLED LOOPBACK TX2 -> SMA splitter -> 2x 30 dB -> RX1+RX2; "
                       "carrier offset imposed on the waveform before the DAC; "
                       "rf_bandwidth set independently of sampling_frequency and "
                       "read back on every cell"})

        total_cells = len(arguments.rates) * len(arguments.bandwidths) * len(arguments.offsets)
        started, cells_done, probes_done = time.time(), 0, 0

        for rate in arguments.rates:
            rate = float(rate)
            rig.set_rate(rate, probe_ms=arguments.probe_ms)
            samples_per_probe = probe_samples(rate, arguments.probe_ms)

            # ---- TX-off floor, per bandwidth, before anything radiates ----
            floors = {}
            for bandwidth in arguments.bandwidths:
                bandwidth = float(bandwidth)
                rig.stop_tx()
                rig.set_rx_bandwidth(bandwidth)
                back = rig.readback()
                applied = rig.applied(back)
                write({"kind": "null_header", "sample_rate_hz": rate,
                       "rx_bandwidth_hz": bandwidth, "probe_samples": samples_per_probe,
                       "requested": {"sample_rate_hz": rate,
                                     "rx_bandwidth_hz": bandwidth,
                                     "tx_bandwidth_hz": TX_BW_HZ,
                                     "rx_gain_db": rig.rx_gain_db},
                       "applied": applied, "readback": back})
                observed = []
                for probe in range(arguments.nulls):
                    block = rig.capture(discard=1)
                    for receiver in (0, 1):
                        values = np.asarray(block[receiver], np.complex64)
                        row = power_row(values)
                        observed.append(row["rms_counts"])
                        meta = {"kind": "null", "sample_rate_hz": rate,
                                "rx_bandwidth_hz": bandwidth,
                                "sample_rate_applied_hz": applied["sample_rate_applied_hz"],
                                "rx_bandwidth_applied_hz": applied["rx_bandwidth_applied_hz"],
                                "probe": probe, "receiver": receiver + 1, **row}
                        for done_meta, scored in scorer.submit(rate, values, meta):
                            write({**done_meta, **scored})
                for done_meta, scored in scorer.drain():
                    write({**done_meta, **scored})
                # ``--nulls 0`` is legitimate on a re-run that is only filling
                # in failed cells: the null arm is already in the file and
                # re-measuring it would cost twenty minutes to learn the same
                # numbers.  The floor is only a record here -- what the TX check
                # is scored against is ``reference_floor`` below, measured
                # separately -- so an empty arm records None rather than the
                # nan that ``np.median([])`` would put in the file.
                floors[bandwidth] = (float(np.median(observed)) if observed
                                     else None)
                print(f"[null] Fs {rate/1e6:5.2f} B {bandwidth/1e6:5.2f}  "
                      f"floor {'n/a' if floors[bandwidth] is None else format(floors[bandwidth], '.2f')} counts",
                      flush=True)

            rig.set_rx_bandwidth(REF_BW_HZ)
            reference_floor = float(np.median(
                [power_row(np.asarray(rig.capture(discard=1)[r], np.complex64))["rms_counts"]
                 for _ in range(3) for r in (0, 1)]))

            for nominal in arguments.offsets:
                nominal = float(nominal)
                pending = [b for b in arguments.bandwidths
                           if (rate, float(b), nominal) not in done]
                if not pending:
                    cells_done += len(arguments.bandwidths)
                    print(f"[skip] Fs {rate/1e6:5.2f} CFO {nominal/1e3:7.1f} kHz "
                          f"already complete", flush=True)
                    continue
                offset = snap_offset_hz(nominal, rate)
                try:
                    wave = tx_waveform(rate, EDGE, offset)
                    status = rig.load(wave, arguments.tx_gain,
                                      floor_rms=reference_floor)
                except Exception as error:                   # noqa: BLE001
                    failures.append({"sample_rate_hz": rate,
                                     "offset_nominal_hz": nominal,
                                     "stage": "tx-load", "error": repr(error)})
                    write({"kind": "cell_error", "sample_rate_hz": rate,
                           "offset_nominal_hz": nominal, "stage": "tx-load",
                           "traceback": traceback.format_exc()})
                    continue
                write({"kind": "reference", "sample_rate_hz": rate,
                       "offset_nominal_hz": nominal, "offset_hz": offset,
                       "tx_gain_db": arguments.tx_gain,
                       "verify_gain_db": VERIFY_GAIN_DB,
                       "reference_bandwidth_hz": REF_BW_HZ,
                       "reference_floor_rms_counts": reference_floor,
                       **{key: value for key, value in status.items()
                          if key != "reference_readback"},
                       "readback": status["reference_readback"]})

                for bandwidth in arguments.bandwidths:
                    bandwidth = float(bandwidth)
                    if (rate, bandwidth, nominal) in done:
                        cells_done += 1
                        continue
                    try:
                        rig.set_rx_bandwidth(bandwidth)
                        back = rig.readback()
                        applied = rig.applied(back)
                        write({"kind": "cell_header", "sample_rate_hz": rate,
                               "rx_bandwidth_hz": bandwidth,
                               "offset_nominal_hz": nominal, "offset_hz": offset,
                               "tx_gain_db": arguments.tx_gain,
                               "probe_samples": samples_per_probe,
                               "probe_ms": arguments.probe_ms,
                               "null_floor_rms_counts": floors.get(bandwidth),
                               "requested": {"sample_rate_hz": rate,
                                             "rx_bandwidth_hz": bandwidth,
                                             "tx_bandwidth_hz": TX_BW_HZ,
                                             "rx_gain_db": rig.rx_gain_db,
                                             "offset_hz": offset},
                               "applied": applied, "readback": back})
                        for probe in range(arguments.captures):
                            block = rig.capture(discard=1)
                            for receiver in (0, 1):
                                values = np.asarray(block[receiver], np.complex64)
                                row = power_row(values)
                                meta = {"kind": "observation",
                                        "sample_rate_hz": rate,
                                        "rx_bandwidth_hz": bandwidth,
                                        "offset_nominal_hz": nominal,
                                        "offset_hz": offset,
                                        "sample_rate_applied_hz": applied["sample_rate_applied_hz"],
                                        "rx_bandwidth_applied_hz": applied["rx_bandwidth_applied_hz"],
                                        "tx_bandwidth_applied_hz": applied["tx_bandwidth_applied_hz"],
                                        "rx_gain_applied_db": applied["rx_gain_applied_db"],
                                        "rx_fir_en": applied["rx_fir_en"],
                                        "tx_gain_db": arguments.tx_gain,
                                        "probe": probe, "receiver": receiver + 1,
                                        **row}
                                for done_meta, scored in scorer.submit(rate, values, meta):
                                    write({**done_meta, **scored})
                                probes_done += 1
                                if row["peak_counts"] > RX_PEAK_CEILING:
                                    write({"kind": "abort",
                                           "reason": "rx peak ceiling",
                                           "peak_counts": row["peak_counts"]})
                                    raise RailExceeded("RX peak ceiling exceeded")
                        for done_meta, scored in scorer.drain():
                            write({**done_meta, **scored})
                    except RailExceeded:
                        raise
                    except Exception as error:               # noqa: BLE001
                        failures.append({"sample_rate_hz": rate,
                                         "rx_bandwidth_hz": bandwidth,
                                         "offset_nominal_hz": nominal,
                                         "stage": "capture", "error": repr(error)})
                        write({"kind": "cell_error", "sample_rate_hz": rate,
                               "rx_bandwidth_hz": bandwidth,
                               "offset_nominal_hz": nominal, "stage": "capture",
                               "traceback": traceback.format_exc()})
                        try:
                            for done_meta, scored in scorer.drain():
                                write({**done_meta, **scored})
                        except Exception:                    # noqa: BLE001
                            pass
                    cells_done += 1
                    elapsed = time.time() - started
                    remaining = ((elapsed / cells_done) * (total_cells - cells_done)
                                 if cells_done else 0.0)
                    print(f"Fs {rate/1e6:5.2f} B {bandwidth/1e6:5.2f} "
                          f"CFO {nominal/1e3:7.1f} kHz  cell {cells_done}/{total_cells}  "
                          f"probes {probes_done}  eta {remaining/60:.0f} min", flush=True)
                rig.stop_tx()
        write({"kind": "footer", "failures": failures,
               "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        return 0
    except Exception:                                        # noqa: BLE001
        write({"kind": "error", "traceback": traceback.format_exc()})
        traceback.print_exc()
        return 1
    finally:
        if rig is not None:
            state = rig.close()
            write({"kind": "rest", "readback": state})
        scorer.close()
        print("radio rested: manual gain, 2.5 MS/s, 2.5 MHz, TX at -89.75 dB",
              flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
