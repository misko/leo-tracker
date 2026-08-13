"""Crash-safe, chunked, dual-receiver complex-IQ capture artifacts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Iterable, Iterator

import numpy as np

from ..paired import PairedCI16Block, PairedSampleBlock, paired_sample_count

SCHEMA = "leo-tracker.beacon-iq/v1"
LAYOUT = "sample,receiver,component; receivers=rx0,rx1; components=i,q"


def queued_paired_blocks(source, *, queue_blocks: int = 16
                         ) -> Iterator[PairedSampleBlock | PairedCI16Block]:
    """Overlap blocking radio refills with conversion, hashing, and NVMe writes."""
    if queue_blocks < 1:
        raise ValueError("queue-blocks must be at least one")
    pending: queue.Queue = queue.Queue(maxsize=queue_blocks)
    sentinel = object(); stop = threading.Event(); failure: list[BaseException] = []

    def produce() -> None:
        try:
            for block in source.blocks():
                if stop.is_set():
                    break
                while not stop.is_set():
                    try:
                        pending.put(block, timeout=.1); break
                    except queue.Full:
                        pass
        except BaseException as exc:
            failure.append(exc)
        finally:
            while not stop.is_set():
                try:
                    pending.put(sentinel, timeout=.1); break
                except queue.Full:
                    pass

    worker = threading.Thread(target=produce, name="beacon-radio-reader", daemon=True)
    worker.start()
    try:
        while True:
            item = pending.get()
            if item is sentinel:
                if failure:
                    raise failure[0]
                return
            yield item
    finally:
        stop.set()
        worker.join(timeout=2)


@dataclass(frozen=True)
class BeaconChunk:
    path: str
    first_sample_index: int
    sample_count: int
    first_utc_ns: int
    last_utc_ns: int
    read_count: int
    sha256: str
    bytes: int


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _complex_to_ci16(rx0: np.ndarray, rx1: np.ndarray) -> np.ndarray:
    first, second = np.asarray(rx0), np.asarray(rx1)
    if first.ndim != 1 or first.shape != second.shape:
        raise ValueError("dual receiver blocks must be equal one-dimensional arrays")
    output = np.empty((first.size, 2, 2), dtype="<i2")
    for receiver, values in enumerate((first, second)):
        output[:, receiver, 0] = np.clip(np.rint(values.real), -32768, 32767).astype("<i2")
        output[:, receiver, 1] = np.clip(np.rint(values.imag), -32768, 32767).astype("<i2")
    return output


def _components(block: PairedSampleBlock | PairedCI16Block, count: int
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if isinstance(block, PairedCI16Block):
        return tuple(item[:count] for item in block.components)
    return (block.rx0[:count].real, block.rx0[:count].imag,
            block.rx1[:count].real, block.rx1[:count].imag)


def _block_to_ci16(block: PairedSampleBlock | PairedCI16Block, count: int) -> np.ndarray:
    if not isinstance(block, PairedCI16Block):
        return _complex_to_ci16(block.rx0[:count], block.rx1[:count])
    output = np.empty((count, 2, 2), dtype="<i2")
    i0, q0, i1, q1 = _components(block, count)
    output[:, 0, 0], output[:, 0, 1] = i0, q0
    output[:, 1, 0], output[:, 1, 1] = i1, q1
    return output


#: The survey's IQ, one file per capture, named so it can never be mistaken
#: for a dwell chunk: readers enumerate ``manifest["chunks"]`` and would
#: otherwise fold a probe into the recording it merely preceded.
SURVEY_IQ_FILENAME = "survey.ci16"


def _write_survey_iq(destination: Path, samples: np.ndarray, *,
                     sample_rate_hz: float | None = None,
                     probe_s: float | None = None,
                     config_name: str | None = None) -> dict:
    """Commit the probes the survey collected, with a digest, before the dwell.

    Written here rather than after the capture because this is the moment the
    directory exists and nothing else is competing for the disk; a capture
    that is later interrupted still keeps the survey that preceded it.

    Same ci16 layout as a chunk with a tuning axis in front, so existing
    tooling reads it with a reshape rather than a new decoder.

    **The rate and probe length are declared here, not inferred.**  The survey
    is drawn from four configurations and three of them hold a different sample
    count from the fourth, so nothing about this file's shape is a constant any
    more.  A reader that took ``sample_rate_hz`` from the enclosing manifest
    would take the *dwell's* rate, which is a different number by construction
    and would silently mis-scale every frequency it derived.
    """
    values = np.ascontiguousarray(samples, dtype="<i2")
    if values.ndim != 4 or values.shape[2:] != (2, 2):
        raise ValueError(
            "survey IQ must be (tuning, sample, receiver, component), "
            f"got {values.shape}")
    partial = destination / (SURVEY_IQ_FILENAME + ".partial")
    final = destination / SURVEY_IQ_FILENAME
    digest = hashlib.sha256()
    with partial.open("wb", buffering=0) as stream:
        payload = memoryview(values).cast("B")
        stream.write(payload); digest.update(payload); os.fsync(stream.fileno())
    os.replace(partial, final)
    return {"path": SURVEY_IQ_FILENAME, "dtype": "ci16_le",
            "layout": "tuning,sample,receiver,component; " + LAYOUT,
            "tunings": int(values.shape[0]),
            "samples_per_tuning": int(values.shape[1]),
            "sample_rate_hz": (None if sample_rate_hz is None
                               else float(sample_rate_hz)),
            "probe_s": None if probe_s is None else float(probe_s),
            "capture_config": config_name,
            "shape_note": ("shape and rate are declared, never assumed: the "
                           "pre-dwell survey draws its configuration, so this "
                           "file holds 200,000, 400,000 or 800,000 samples per "
                           "tuning and its rate is the survey's, not the "
                           "dwell's"),
            "sha256": digest.hexdigest(),
            "bytes": final.stat().st_size}


def capture_beacon_iq(blocks: Iterable[PairedSampleBlock | PairedCI16Block],
                      destination: Path, *,
                      sample_rate_hz: float, center_frequency_hz: float,
                      bandwidth_hz: float, duration_s: float,
                      lnb_lo_hz: float | None = None, chunk_s: float = 5.0,
                      identity: dict | None = None, gain_mode: str = "manual",
                      configured_gain_db: float | None = None,
                      metadata: dict | None = None,
                      survey_samples: np.ndarray | None = None,
                      survey_sample_rate_hz: float | None = None,
                      survey_probe_s: float | None = None,
                      survey_config_name: str | None = None) -> dict:
    """Write raw ci16 chunks, committing each chunk atomically with a checksum."""
    if min(sample_rate_hz, center_frequency_hz, bandwidth_hz, duration_s, chunk_s) <= 0:
        raise ValueError("capture frequencies, rates, duration, and chunk length must be positive")
    if bandwidth_hz > sample_rate_hz:
        raise ValueError("bandwidth cannot exceed sample rate")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    manifest_path = destination / "manifest.json"
    started_ns = time.time_ns()
    requested_samples = round(duration_s * sample_rate_hz)
    chunk_samples = max(1, round(chunk_s * sample_rate_hz))
    manifest = {"schema": SCHEMA, "state": "capturing", "dtype": "ci16_le",
        "layout": LAYOUT, "sample_rate_hz": sample_rate_hz,
        "bandwidth_hz": bandwidth_hz, "center_frequency_hz": center_frequency_hz,
        "lnb_lo_hz": lnb_lo_hz, "rf_center_hz": center_frequency_hz + (lnb_lo_hz or 0),
        "receiver_count": 2, "requested_duration_s": duration_s,
        "requested_samples_per_receiver": requested_samples,
        "chunk_samples": chunk_samples, "gain_mode": gain_mode,
        "configured_gain_db": configured_gain_db, "identity": identity or {},
        "gain_telemetry": {"target_interval_s": 1.0, "entries": [],
            "note": "gain readback is sampled after a blocking IQ refill; it is diagnostic, not sample-synchronous"},
        "metadata": metadata or {},
        "created_utc_ns": started_ns, "chunks": []}
    if survey_samples is not None:
        manifest["survey_iq"] = _write_survey_iq(
            destination, survey_samples, sample_rate_hz=survey_sample_rate_hz,
            probe_s=survey_probe_s, config_name=survey_config_name)
    _atomic_json(manifest_path, manifest)
    pending: list[np.ndarray] = []
    pending_count = total = 0
    pending_reads: list[PairedSampleBlock | PairedCI16Block] = []
    chunk_index = committed_samples = 0
    expected_source_index: int | None = None
    read_count = total_read_duration_ns = maximum_read_duration_ns = 0
    total_host_gap_ns = maximum_host_gap_ns = 0
    clock_samples: list[dict] = []
    power_sum = np.zeros(2, dtype=np.float64)
    peak_abs_component = np.zeros(2, dtype=np.float64)
    near_adc_full_scale_count = np.zeros(2, dtype=np.int64)
    first_read_start_ns: int | None = None
    previous_read_stop_ns: int | None = None

    def stream_timing() -> dict:
        span_ns = (0 if first_read_start_ns is None or previous_read_stop_ns is None else
                   previous_read_stop_ns - first_read_start_ns)
        return {"read_count": read_count, "first_read_start_utc_ns": first_read_start_ns,
            "last_read_stop_utc_ns": previous_read_stop_ns, "wall_span_s": span_ns / 1e9,
            "sample_time_s": total / sample_rate_hz,
            "total_read_duration_s": total_read_duration_ns / 1e9,
            "maximum_read_duration_s": maximum_read_duration_ns / 1e9,
            "total_positive_host_gap_s": total_host_gap_ns / 1e9,
            "maximum_positive_host_gap_s": maximum_host_gap_ns / 1e9,
            "host_read_duty_fraction": (total_read_duration_ns / span_ns if span_ns > 0 else None),
            "clock_samples": clock_samples,
            "clock_sample_semantics": ("sample range plus midpoint of the host UTC "
                "bracket around each blocking IIO refill; diagnostic, not an RF "
                "hardware timestamp"),
            "note": "host syscall timing diagnoses writer stalls but is not an RF hardware timestamp"}

    def add_measurement_diagnostics() -> None:
        manifest["sample_statistics"] = {"adc_nominal_full_scale": 2048.0,
            "near_full_scale_threshold": 2040.0,
            "note": "near-full-scale assumes raw AD9361 12-bit samples delivered by pyadi",
            "receivers": [{"receiver": receiver,
                "rms_magnitude": (float(np.sqrt(power_sum[receiver] / total))
                                  if total else None),
                "peak_abs_component": float(peak_abs_component[receiver]),
                "near_full_scale_fraction": (float(
                    near_adc_full_scale_count[receiver] / total) if total else None)}
                for receiver in range(2)]}

    def commit(count: int) -> None:
        nonlocal pending, pending_count, pending_reads, chunk_index, committed_samples
        values = np.concatenate(pending, axis=0)
        written, remainder = values[:count], values[count:]
        filename = f"chunk-{chunk_index:06d}.ci16"
        partial, final = destination / (filename + ".partial"), destination / filename
        digest = hashlib.sha256()
        with partial.open("wb", buffering=0) as stream:
            payload = memoryview(written).cast("B")
            stream.write(payload); digest.update(payload); os.fsync(stream.fileno())
        os.replace(partial, final)
        relative = str(final.relative_to(destination))
        first, last = pending_reads[0], pending_reads[-1]
        record = BeaconChunk(relative, int(committed_samples), int(count),
            int(first.utc_ns), int(last.utc_ns), len(pending_reads), digest.hexdigest(),
            final.stat().st_size)
        manifest["chunks"].append(asdict(record)); _atomic_json(manifest_path, manifest)
        pending = ([] if not remainder.size else [remainder.copy()])
        pending_count = int(remainder.shape[0])
        pending_reads = ([] if not remainder.size else [last])
        committed_samples += count
        chunk_index += 1

    try:
        for block in blocks:
            if total >= requested_samples:
                break
            if expected_source_index is None:
                expected_source_index = block.sample_index
            if block.sample_index != expected_source_index or block.dropped_samples:
                raise RuntimeError(
                    f"non-contiguous radio stream: expected sample {expected_source_index}, "
                    f"received {block.sample_index}, dropped={block.dropped_samples}"
                )
            block_samples = paired_sample_count(block)
            expected_source_index += block_samples
            read_count += 1
            if block.read_duration_ns is not None:
                duration_ns = int(block.read_duration_ns)
                read_start_ns = int(block.utc_ns - duration_ns // 2)
                read_stop_ns = int(block.utc_ns + duration_ns // 2)
                if first_read_start_ns is None:
                    first_read_start_ns = read_start_ns
                if previous_read_stop_ns is not None:
                    gap_ns = max(0, read_start_ns - previous_read_stop_ns)
                    total_host_gap_ns += gap_ns
                    maximum_host_gap_ns = max(maximum_host_gap_ns, gap_ns)
                previous_read_stop_ns = read_stop_ns
                total_read_duration_ns += duration_ns
                maximum_read_duration_ns = max(maximum_read_duration_ns, duration_ns)
            available = min(block_samples, requested_samples - total)
            if available <= 0:
                break
            if block.read_duration_ns is not None:
                clock_samples.append({"first_sample_index": int(total),
                    "sample_count": int(available), "utc_ns": int(block.utc_ns),
                    "read_duration_ns": int(block.read_duration_ns)})
            if block.gain_db is not None:
                gains = [None if not np.isfinite(value) else float(value)
                         for value in block.gain_db]
                manifest["gain_telemetry"]["entries"].append({
                    "sample_index": int(total), "utc_ns": int(block.utc_ns),
                    "rx_gain_db": gains})
            raw_components = _components(block, available)
            for receiver, (i_values, q_values) in enumerate(
                    ((raw_components[0], raw_components[1]),
                     (raw_components[2], raw_components[3]))):
                i_values = np.asarray(i_values); q_values = np.asarray(q_values)
                power_sum[receiver] += float(
                    np.sum(np.square(i_values, dtype=np.float64)) +
                    np.sum(np.square(q_values, dtype=np.float64)))
                components = np.maximum(np.abs(i_values.astype(np.int32)),
                                        np.abs(q_values.astype(np.int32)))
                peak_abs_component[receiver] = max(
                    peak_abs_component[receiver], float(np.max(components, initial=0)))
                near_adc_full_scale_count[receiver] += int(np.count_nonzero(components >= 2040))
            pending.append(_block_to_ci16(block, available))
            pending_reads.append(block); pending_count += available; total += available
            while pending_count >= chunk_samples:
                commit(chunk_samples)
            # Do not request and silently discard the next source block.  A
            # hop session deliberately reuses this iterator after retuning.
            if total >= requested_samples:
                break
        if pending_count:
            commit(pending_count)
        if total != requested_samples:
            raise RuntimeError(f"radio ended after {total} of {requested_samples} samples")
    except BaseException:
        manifest["state"] = "interrupted"; manifest["captured_samples_per_receiver"] = total
        manifest["stream_timing"] = stream_timing()
        add_measurement_diagnostics()
        _atomic_json(manifest_path, manifest)
        raise
    manifest["state"] = "complete"; manifest["captured_samples_per_receiver"] = total
    manifest["stream_timing"] = stream_timing()
    add_measurement_diagnostics()
    manifest["completed_utc_ns"] = time.time_ns()
    manifest["stored_bytes"] = sum(item["bytes"] for item in manifest["chunks"])
    _atomic_json(manifest_path, manifest)
    return manifest


class BeaconCapture:
    def __init__(self, root: Path, manifest: dict): self.root, self.manifest = root, manifest

    @classmethod
    def open(cls, root: Path, *, verify: bool = False) -> "BeaconCapture":
        root = Path(root); manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("schema") != SCHEMA:
            raise ValueError("unsupported beacon capture schema")
        capture = cls(root, manifest)
        capture._reconcile_interrupted_total()
        if verify: capture.verify()
        return capture

    def _reconcile_interrupted_total(self) -> None:
        """Report an interrupted capture's real extent, without touching its source.

        A capture killed mid-write can count more samples read from the radio
        than it durably wrote, leaving the declared total ahead of the chunks on
        disk. Those chunks are still a contiguous, checksummed prefix, so expose
        what is actually present and keep the declared target for provenance.
        A complete capture must still match exactly.
        """
        if self.manifest.get("state") != "interrupted":
            return
        written = sum(int(item["sample_count"])
                      for item in self.manifest.get("chunks", ()))
        declared = int(self.manifest.get("captured_samples_per_receiver", 0))
        if 0 < written < declared:
            self.manifest["declared_samples_per_receiver"] = declared
            self.manifest["captured_samples_per_receiver"] = written

    def verify(self) -> None:
        total = expected_index = 0
        previous_utc_ns: int | None = None
        for item in self.manifest["chunks"]:
            path = self.root / item["path"]
            if path.stat().st_size != item["bytes"]:
                raise ValueError(f"size mismatch for {path}")
            digest_builder = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest_builder.update(block)
            digest = digest_builder.hexdigest()
            if digest != item["sha256"]:
                raise ValueError(f"checksum mismatch for {path}")
            if item["bytes"] != item["sample_count"] * 2 * 2 * 2:
                raise ValueError(f"layout size mismatch for {path}")
            if item["first_sample_index"] != expected_index:
                raise ValueError("chunk sample indexes are not contiguous")
            if previous_utc_ns is not None and item["first_utc_ns"] < previous_utc_ns:
                raise ValueError("chunk timestamps are not monotonic")
            total += item["sample_count"]
            expected_index = total
            previous_utc_ns = item["last_utc_ns"]
        if total != self.manifest.get("captured_samples_per_receiver"):
            raise ValueError("manifest sample total is inconsistent")

    def chunks(self) -> Iterator[tuple[BeaconChunk, np.ndarray]]:
        for item in self.manifest["chunks"]:
            record = BeaconChunk(**item)
            raw = np.memmap(self.root / record.path, mode="r", dtype="<i2",
                            shape=(record.sample_count, 2, 2))
            values = raw[..., 0].astype(np.float32) + 1j * raw[..., 1].astype(np.float32)
            yield record, values.astype(np.complex64)

    def read_window(self, first_sample: int, sample_count: int) -> np.ndarray:
        """Read a paired window without materializing complete large chunks."""
        total = int(self.manifest.get("captured_samples_per_receiver", 0))
        if first_sample < 0 or sample_count <= 0 or first_sample + sample_count > total:
            raise ValueError("requested window lies outside the capture")
        pieces, stop = [], first_sample + sample_count
        for item in self.manifest["chunks"]:
            chunk_start = item["first_sample_index"]
            chunk_stop = chunk_start + item["sample_count"]
            if chunk_stop <= first_sample or chunk_start >= stop:
                continue
            local_start = max(first_sample, chunk_start) - chunk_start
            local_stop = min(stop, chunk_stop) - chunk_start
            raw = np.memmap(self.root / item["path"], mode="r", dtype="<i2",
                            shape=(item["sample_count"], 2, 2))
            selected = raw[local_start:local_stop]
            values = selected[..., 0].astype(np.float32) + 1j * selected[..., 1].astype(np.float32)
            pieces.append(values.astype(np.complex64))
        result = np.concatenate(pieces, axis=0) if pieces else np.empty((0, 2), np.complex64)
        if result.shape != (sample_count, 2):
            raise ValueError("capture window is incomplete")
        return result
