"""Standalone ``leo-radio`` command line interface.

This module intentionally depends only on :mod:`leo_tracker.radio` components.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from typing import Sequence

import numpy as np

from .artifact import CaptureArtifact, capture_to_artifact
from .extract import extract_frequency_ridge
from .pluto import PairedPlutoSource, PlutoSource
from .paired import FakePairedSource, capture_pair_to_artifacts
from .qualification import qualify_paired_comb
from .carrier import track_carrier
from .gps import read_nmea_snapshot
from .validated_scan import (PROMOTION_MIN_CONFIRMATIONS, PROMOTION_MIN_SETTLE_SECONDS,
                             confirmed_features, validated_scan_pyadi, write_validated_scan)
from .schema import RIDGE_SCHEMA_VERSION
from .moving import detect_moving_comb, detect_moving_ridge
from .monitor import (MonitorResult, find_motion_candidates, monitor_channels_pyadi,
                      read_monitor_cycles, write_monitor_report)
from .scout import scan_channels_pyadi, scan_pyadi, write_multi_scan_report, write_scan_report
from .source import FakeSource, RadioConfig, RadioSource, SampleBlock
from .synthetic import linear_chirp
from .starlink import (aggregate_gutter_search, analyze_starlink_block, channel_plan,
                       fake_blocks, get_channel, observe_blocks, read_event_iq,
                       synthetic_starlink_block, threaded_source_blocks)
from .wide_doppler import (analyze_compact_waterfall, capture_compact_waterfall,
                           plot_compact_waterfall)
from .dashboard import serve_dashboard
from .gain_experiment import run_gain_experiment
from .measurement import capture_measurement_waterfall
from .measurement_analysis import write_measurement_analysis
from .tuning_dither import compare_tuning_dither
from .rf_baseline import build_rf_baseline, write_rf_novelty
from .wide_feature import write_wide_feature_analysis
from .wide_population import write_wide_population
from .iq_evidence import (IQEvidenceSelector, gate_iq_evidence,
                          write_starlink_waveform_iq)
from .confounds import write_confound_population
from .hybrid import build_hybrid_plan
from .doppler_observations import write_doppler_observations
from .tracking.ensemble import write_tracker_ensemble
from .tracking.coherent import (write_coherent_iq_analysis,
                                write_cross_ambiguity_analysis)
from .tracking.benchmark import write_tracker_summary
from .tracking.tle_match import rematch_tracker_report
from .beacon.analysis import analyze_capture as analyze_beacon_capture
from .beacon.artifact import capture_beacon_iq, queued_paired_blocks
from .beacon.channels import starlink_edge_pilot_if_hz, starlink_if_hz
from .beacon.retention import apply_retention as apply_beacon_retention
from .beacon.plot import plot_beacon_followup, plot_beacon_report
from .beacon.recovery import recover_unanalyzed as recover_unanalyzed_beacons
from .beacon.followup import (followup_capture as followup_beacon_capture,
                              rescore_followup as rescore_beacon_followup)
from .beacon.calibration import build_calibration as build_beacon_calibration
from .beacon.null_replay import replay_null_calibration as replay_beacon_null_calibration
from .beacon.decode import decode_followup as decode_beacon_followup
from .beacon.decode import plot_decode_report as plot_beacon_decode
from .beacon.fingerprint import update_fingerprint_store
from .beacon.gain_comparison import build_gain_comparison
from .beacon.dashboard_index import update_dashboard_index
from .beacon.continuous import track_capture as track_beacon_capture
from .beacon.hopping import capture_hop_session
from .beacon.frame_tracking import track_conditioned_frames
from .beacon.template_learning import learn_bandpass_beacon
from .beacon.channel_link import link_channel_tracks
from .beacon.evidence_archive import (archive_evidence, audit_evidence,
                                      archive_evidence_v2,
                                      build_evidence_v2_shadow,
                                      compare_evidence_plan_coverage, extract_evidence,
                                      materialize_evidence_clip, plan_evidence,
                                      repair_evidence_v2_summaries,
                                      verify_evidence)
from .beacon.storage_regime import (CONFIRMATION as STORAGE_REGIME_CONFIRMATION,
                                    apply_storage_regime_plan,
                                    build_storage_regime_plan)
from .beacon.storage_audit import build_storage_regime_audit
from .beacon.legacy_normalizer import (
    CONFIRMATION as LEGACY_LAYOUT_CONFIRMATION,
    apply_legacy_layout_plan, build_legacy_layout_plan)
from .beacon.local_reclamation import (apply_reclamation_plan,
                                       build_reclamation_plan)
from .beacon.local_report_convergence import (
    apply_local_report_plan, build_local_report_plan)
from .beacon.local_artifact_convergence import (
    apply_local_artifact_plan, build_local_artifact_plan)
from .beacon.qnap_lifecycle import (apply_qnap_lifecycle_plan,
                                    build_qnap_lifecycle_plan,
                                    qnap_storage_mutation_lock)
from .beacon.shared_transient_convergence import (
    CONFIRMATION as SHARED_TRANSIENT_CONFIRMATION,
    apply_shared_transient_plan, build_shared_transient_plan)


def discover_pluto_serials(sysfs: str | Path = "/sys/bus/usb/devices") -> list[str]:
    """Discover local Pluto USB serials without loading SPF or opening hardware."""
    found: list[str] = []
    for device in Path(sysfs).glob("*"):
        try:
            vendor = (device / "idVendor").read_text().strip().lower()
            product = (device / "idProduct").read_text().strip().lower()
            serial = (device / "serial").read_text().strip()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if vendor == "0456" and product == "b673" and serial:
            found.append(serial)
    return sorted(set(found))


class _LimitedSource:
    def __init__(self, source: RadioSource, count: int): self.source, self.count = source, count
    @property
    def config(self): return self.source.config
    @property
    def identity(self): return self.source.identity
    def blocks(self):
        remaining, index = self.count, 0
        for block in self.source.blocks():
            if remaining <= 0: break
            values = block.samples[:remaining]
            if values.size:
                yield SampleBlock(values, index, block.utc_ns, block.dropped_samples)
                index += values.size
                remaining -= values.size
        if remaining: raise RuntimeError(f"radio ended with {remaining} requested samples missing")
    def close(self): self.source.close()


def _write_waterfall(samples: np.ndarray, rate: float, destination: Path, fft_size: int, hop: int) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("analysis plotting requires matplotlib") from exc
    window = np.hanning(fft_size)
    starts = range(0, samples.size - fft_size + 1, hop)
    image = np.empty((len(starts), fft_size), dtype=np.float32)
    for row, start in enumerate(starts):
        image[row] = 20 * np.log10(
            np.abs(np.fft.fftshift(np.fft.fft(samples[start:start + fft_size] * window))) + 1e-12
        )
    fig, axis = plt.subplots(figsize=(9, 4.5), constrained_layout=True)
    extent = [-rate / 2 / 1e3, rate / 2 / 1e3, samples.size / rate, 0]
    rendered = axis.imshow(image, aspect="auto", extent=extent, cmap="viridis")
    axis.set(xlabel="Baseband frequency (kHz)", ylabel="Time (s)", title="Blind RF waterfall")
    fig.colorbar(rendered, ax=axis, label="Magnitude (dB)")
    fig.savefig(destination, dpi=140)
    plt.close(fig)


def parse_channels(value: str) -> tuple[int, ...]:
    """Parse a canonical receiver selection such as ``0``, ``1``, or ``0,1``."""
    try:
        channels = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("channels must be 0, 1, or 0,1") from exc
    if not channels or len(set(channels)) != len(channels) or any(c not in (0, 1) for c in channels):
        raise argparse.ArgumentTypeError("channels must be 0, 1, or 0,1 without duplicates")
    return tuple(sorted(channels))


def _selected_channels(args: argparse.Namespace) -> tuple[int, ...]:
    legacy = getattr(args, "channel", None)
    selected = getattr(args, "channels", None)
    if legacy is not None and selected is not None:
        raise ValueError("use either --channel or --channels, not both")
    return selected or ((legacy,) if legacy is not None else (0,))


def _radio_config(args: argparse.Namespace, channel: int | None = None) -> RadioConfig:
    selected = _selected_channels(args)
    return RadioConfig(args.center_frequency_hz, args.sample_rate_hz, args.bandwidth_hz,
                       args.gain_db, selected[0] if channel is None else channel)


def command_preflight(args: argparse.Namespace) -> int:
    serials = [args.fake_serial] if args.fake_serial else discover_pluto_serials(args.sysfs)
    report = {"mode": "fake" if args.fake_serial else "hardware", "ready": bool(serials), "pluto_serials": serials}
    print(json.dumps(report, sort_keys=True))
    return 0 if serials else 2


def command_capture(args: argparse.Namespace) -> int:
    channels = _selected_channels(args)
    config = _radio_config(args)
    count = round(args.duration_s * config.sample_rate_hz)
    if channels == (0, 1):
        if args.fake:
            rx0 = linear_chirp(args.fake_start_hz, args.fake_stop_hz, config.sample_rate_hz,
                               args.duration_s, amplitude=.25, noise_std=args.fake_noise_std,
                               seed=args.seed)
            rx1 = linear_chirp(args.fake_start_hz + 100, args.fake_stop_hz + 100,
                               config.sample_rate_hz, args.duration_s, amplitude=.2,
                               noise_std=args.fake_noise_std, seed=args.seed + 1)
            paired_source = FakePairedSource(rx0, rx1, config, block_size=args.block_size)
        else:
            paired_source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                               transport=args.transport, serial=args.serial)
        rx0_artifact, rx1_artifact = capture_pair_to_artifacts(
            paired_source, args.output, sample_count=count,
            metadata={"command": "leo-radio capture", "channels": [0, 1]},
        )
        print(json.dumps({"capture": str(Path(args.output)), "channels": [0, 1],
            "pair_session_id": rx0_artifact.manifest["metadata"]["pair_session_id"],
            "rx0_capture_id": rx0_artifact.manifest["capture_id"],
            "rx1_capture_id": rx1_artifact.manifest["capture_id"],
            "samples_per_channel": count,
            "first_buffer_utc_ns": rx0_artifact.manifest["start_utc_ns"]}, sort_keys=True))
        return 0
    if args.fake:
        samples = linear_chirp(args.fake_start_hz, args.fake_stop_hz, config.sample_rate_hz,
                               args.duration_s, amplitude=.25, noise_std=args.fake_noise_std, seed=args.seed)
        source: RadioSource = FakeSource(samples, config, args.block_size,
                                         identity={"kind": "fake", "seed": args.seed})
    else:
        source = PlutoSource(config, uri=args.uri, block_size=args.block_size,
                             transport=args.transport, serial=args.serial)
    artifact = capture_to_artifact(_LimitedSource(source, count), args.output,
                                   metadata={"command": "leo-radio capture",
                                             "channels": list(channels)})
    print(json.dumps({"capture": str(artifact.path), "capture_id": artifact.manifest["capture_id"],
                      "channel": channels[0],
                      "samples": artifact.manifest["sample_count"]}, sort_keys=True))
    return 0


def command_paired_capture(args: argparse.Namespace) -> int:
    config = _radio_config(args); count = round(args.duration_s * config.sample_rate_hz)
    if args.fake:
        rx0 = linear_chirp(args.fake_start_hz, args.fake_stop_hz, config.sample_rate_hz,
                           args.duration_s, amplitude=.25, noise_std=args.fake_noise_std, seed=args.seed)
        rx1 = linear_chirp(args.fake_start_hz + 100, args.fake_stop_hz + 100, config.sample_rate_hz,
                           args.duration_s, amplitude=.2, noise_std=args.fake_noise_std, seed=args.seed + 1)
        source = FakePairedSource(rx0, rx1, config, block_size=args.block_size,
                                  start_utc_ns=args.fake_start_utc_ns)
    else:
        source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                   transport=args.transport, serial=args.serial)
    rx0, rx1 = capture_pair_to_artifacts(source, args.output, sample_count=count,
                                         metadata={"command": "leo-radio paired-capture"})
    print(json.dumps({"pair": str(Path(args.output)),
        "pair_session_id": rx0.manifest["metadata"]["pair_session_id"],
        "rx0_capture_id": rx0.manifest["capture_id"], "rx1_capture_id": rx1.manifest["capture_id"],
        "samples_per_channel": count, "first_buffer_utc_ns": rx0.manifest["start_utc_ns"]}, sort_keys=True))
    return 0


def command_starlink_beacon_capture(args: argparse.Namespace) -> int:
    if args.agc_settle_s < 0:
        raise ValueError("AGC settle time cannot be negative")
    experiment_metadata = {}
    if args.gain_experiment_id is not None:
        if args.gain_random_draw_u32 is None or args.gain_assignment_probability is None:
            raise ValueError("gain experiment metadata requires random draw and assignment probability")
        if not 0 <= args.gain_assignment_probability <= 1:
            raise ValueError("gain assignment probability must lie between zero and one")
        experiment_metadata = {"gain_experiment_id": args.gain_experiment_id,
            "gain_random_draw_u32": args.gain_random_draw_u32,
            "agc_assignment_probability": args.gain_assignment_probability,
            "assigned_gain_mode": args.gain_mode,
            "agc_settle_s": args.agc_settle_s if args.gain_mode != "manual" else 0.0}
    nominal_center_hz = (starlink_if_hz(args.channel_number, args.lnb_lo_hz)
                         if args.region == "center" else
                         starlink_edge_pilot_if_hz(
                             args.channel_number, args.region.removesuffix("-edge"),
                             args.lnb_lo_hz))
    center_hz = args.center_frequency_hz
    if center_hz is None:
        # Two LNBs on one radio have independent references, and the pair share
        # a single tuner, so neither can be centred alone.  Offsetting the
        # common tuning by half their disagreement keeps both inside the
        # acquisition search instead of leaving one outside it.
        center_hz = nominal_center_hz + args.tuning_offset_hz
    configured_gain_db = args.gain_db if args.gain_mode == "manual" else None
    config = RadioConfig(center_hz, args.sample_rate_hz, args.bandwidth_hz,
                         configured_gain_db, gain_mode=args.gain_mode)
    # Before the capture claims the radio, not while it holds it: a USB context
    # is an exclusive claim, and the two want opposite configurations. Roughly
    # half a second against a dwell of two minutes.
    survey = survey_samples = None
    if getattr(args, "survey_before_dwell", False):
        from .beacon.presurvey import run_survey
        survey, survey_samples = run_survey(
            uri=args.uri, serial=args.serial,
            sample_rate_hz=args.sample_rate_hz, lnb_lo_hz=args.lnb_lo_hz,
            dwell_channel=args.channel_number, dwell_region=args.region,
            keep_samples=args.keep_survey_iq)
    if args.fake:
        count = round(args.duration_s * args.sample_rate_hz)
        period = max(1, round(args.sample_rate_hz / 750))
        rng = np.random.default_rng(args.seed)
        template = (rng.normal(size=period) + 1j * rng.normal(size=period)) * 500
        rx0 = np.tile(template, int(np.ceil(count / period)))[:count]
        rx1 = np.roll(rx0, 2) * .8
        noise = lambda: (rng.normal(size=count) + 1j * rng.normal(size=count)) * args.fake_noise_std
        source = FakePairedSource(rx0 + noise(), rx1 + noise(), config,
                                  block_size=args.block_size, start_utc_ns=args.fake_start_utc_ns)
    else:
        source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                   transport="iio", serial=args.serial,
                                   sample_format=args.sample_format)
    identity = dict(source.identity)
    if args.serial and not identity.get("serial"):
        identity["serial"] = args.serial
    if args.radio_id:
        identity["radio_id"] = args.radio_id
    if args.receiver_labels:
        identity["receiver_labels"] = list(args.receiver_labels)
    if not args.fake and args.gain_mode != "manual" and args.agc_settle_s > 0:
        time.sleep(args.agc_settle_s)
    if args.host_temperature_c is not None:
        identity["host_temperature_c"] = args.host_temperature_c
    if args.radio_temperature_c is not None:
        identity["radio_temperature_c"] = args.radio_temperature_c
    try:
        queued = queued_paired_blocks(source, queue_blocks=args.queue_blocks)
        manifest = capture_beacon_iq(queued, args.output,
            sample_rate_hz=args.sample_rate_hz, center_frequency_hz=center_hz,
            bandwidth_hz=args.bandwidth_hz, duration_s=args.duration_s,
            lnb_lo_hz=args.lnb_lo_hz, chunk_s=args.chunk_s,
            identity=identity, gain_mode=args.gain_mode,
            configured_gain_db=configured_gain_db,
            metadata={"channel_number": args.channel_number, "region": args.region,
                      "observation_mode": args.observation_mode,
                      "nominal_if_hz": nominal_center_hz,
                      "nominal_rf_hz": nominal_center_hz + args.lnb_lo_hz,
                      "configured_tuning_offset_hz": center_hz - nominal_center_hz,
                      "tuning_basis": "published Starlink channel and edge-pilot geometry",
                      **({"pre_dwell_survey": survey} if survey else {}),
                      **experiment_metadata},
            survey_samples=survey_samples)
    finally:
        if "queued" in locals():
            queued.close()
        source.close()
    print(json.dumps({"capture": str(args.output), "state": manifest["state"],
        "samples_per_receiver": manifest["captured_samples_per_receiver"],
        "stored_bytes": manifest["stored_bytes"], "rf_center_hz": manifest["rf_center_hz"],
        **({"survey_state": survey["state"],
            "survey_active": survey.get("active_count"),
            "survey_ms": survey.get("total_ms")} if survey else {}),
        "radio_id": identity.get("radio_id"),
        "radio_serial": identity.get("serial"), "radio_uri": identity.get("uri")},
        sort_keys=True))
    return 0


def command_starlink_beacon_hop_capture(args: argparse.Namespace) -> int:
    """Capture short settled edge-band dwells while keeping one Pluto open."""
    channels = tuple(args.channels)
    initial_center = starlink_edge_pilot_if_hz(
        channels[0], args.region.removesuffix("-edge"), args.lnb_lo_hz)
    configured_gain_db = args.gain_db if args.gain_mode == "manual" else None
    config = RadioConfig(initial_center, args.sample_rate_hz, args.bandwidth_hz,
                         configured_gain_db, gain_mode=args.gain_mode)
    if args.fake:
        samples_per_dwell = round(args.dwell_s * args.sample_rate_hz)
        blocks_per_dwell = int(np.ceil(samples_per_dwell / args.block_size))
        block_count = len(channels) * (args.settle_buffers + blocks_per_dwell)
        count = block_count * args.block_size
        rng = np.random.default_rng(args.seed)
        rx0 = (rng.normal(size=count) + 1j * rng.normal(size=count)) * 100
        rx1 = (rng.normal(size=count) + 1j * rng.normal(size=count)) * 100
        source = FakePairedSource(rx0, rx1, config, block_size=args.block_size,
                                  start_utc_ns=args.fake_start_utc_ns)
    else:
        source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                   transport="iio", serial=args.serial,
                                   sample_format=args.sample_format)
    identity = dict(source.identity)
    if args.serial and not identity.get("serial"):
        identity["serial"] = args.serial
    if args.radio_id:
        identity["radio_id"] = args.radio_id
    if args.receiver_labels:
        identity["receiver_labels"] = list(args.receiver_labels)
    if not args.fake and args.gain_mode != "manual" and args.agc_settle_s > 0:
        time.sleep(args.agc_settle_s)
    report = capture_hop_session(source, args.output, channels=channels,
        region=args.region, dwell_s=args.dwell_s,
        sample_rate_hz=args.sample_rate_hz, bandwidth_hz=args.bandwidth_hz,
        lnb_lo_hz=args.lnb_lo_hz, settle_buffers=args.settle_buffers,
        chunk_s=args.chunk_s, gain_mode=args.gain_mode,
        configured_gain_db=configured_gain_db,
        identity=identity,
        metadata={"command": "leo-radio starlink-beacon-hop-capture"})
    print(json.dumps({"hop_session": str(args.output), "state": report["state"],
        "segment_count": len(report["segments"]),
        "duration_wall_s": report["duration_wall_s"],
        "radio_id": identity.get("radio_id"),
        "radio_serial": identity.get("serial"), "radio_uri": identity.get("uri")},
        sort_keys=True))
    return 0


def _calibration_status_for(args: argparse.Namespace) -> dict:
    """Why this capture was, or was not, frequency-corrected.

    Recorded in the report so an uncorrected run is visible in its own output.
    A missing artifact and a correctly-zero calibration are otherwise identical
    downstream, which is how a wrong lookup path went unnoticed in the field.
    """
    explicit = getattr(args, "receiver_center_offsets_hz", None)
    if explicit:
        return {"applied": True, "reason": "explicit override",
                "centers_hz": [float(explicit[0]), float(explicit[1])]}
    root = getattr(args, "calibration_root", None)
    if root is None:
        return {"applied": False, "reason": "no calibration root given",
                "centers_hz": [0.0, 0.0]}
    from .beacon.lnb_calibration import calibration_status
    try:
        manifest = json.loads((Path(args.capture) / "manifest.json").read_text())
    except (OSError, ValueError):
        return {"applied": False, "reason": "capture manifest unreadable",
                "centers_hz": [0.0, 0.0]}
    radio = (manifest.get("identity") or {}).get("radio_id")
    if not radio:
        return {"applied": False, "reason": "capture declares no radio",
                "centers_hz": [0.0, 0.0]}
    return {**calibration_status(root, radio), "radio_id": radio}


def _receiver_centers_for(args: argparse.Namespace) -> tuple[float, float]:
    """Acquisition search centres for this capture's two receivers.

    An explicit pair wins so a replay can test a hypothesis; otherwise the
    stored calibration is looked up by the capture's own radio, which keeps a
    re-analysis consistent with what production would do.
    """
    explicit = getattr(args, "receiver_center_offsets_hz", None)
    if explicit:
        return (float(explicit[0]), float(explicit[1]))
    root = getattr(args, "calibration_root", None)
    if root is None:
        return (0.0, 0.0)
    from .beacon.lnb_calibration import load_calibration, receiver_centers
    try:
        manifest = json.loads((Path(args.capture) / "manifest.json").read_text())
    except (OSError, ValueError):
        return (0.0, 0.0)
    radio = (manifest.get("identity") or {}).get("radio_id")
    return receiver_centers(load_calibration(root), radio) if radio else (0.0, 0.0)


def command_starlink_beacon_analyze(args: argparse.Namespace) -> int:
    report = analyze_beacon_capture(args.capture, args.output, window_s=args.window_s,
        maximum_analysis_rate_hz=args.maximum_analysis_rate_hz,
        exact_interval_s=args.exact_interval_s, exact_window_s=args.exact_window_s,
        acquisition_span_hz=args.acquisition_span_hz,
        acquisition_step_hz=args.acquisition_step_hz,
        exact_subband_rate_hz=args.exact_subband_rate_hz,
        exact_acquisition_method=args.exact_acquisition_method,
        exact_start_s=args.exact_start_s, exact_stop_s=args.exact_stop_s,
        learned_beacon_path=args.beacon_template,
        receiver_center_offsets_hz=_receiver_centers_for(args))
    report["lnb_calibration"] = _calibration_status_for(args)
    Path(args.output).write_text(json.dumps(report, sort_keys=True))
    if args.plot:
        plot_beacon_report(report, args.plot)
    print(json.dumps({"analysis": str(args.output), **report["summary"]}, sort_keys=True))
    return 0


def command_starlink_beacon_retain(args: argparse.Namespace) -> int:
    print(json.dumps(apply_beacon_retention(args.root, keep_negative=args.keep_negative,
                                            keep_confirmed=args.keep_confirmed,
                                            keep_wide=args.keep_wide,
                                            keep_oversample=args.keep_oversample,
                                            keep_hop_sessions=args.keep_hop_sessions,
                                            dry_run=args.dry_run), sort_keys=True))
    return 0


def command_starlink_storage_reconcile(args: argparse.Namespace) -> int:
    if args.interval_s <= 0 or args.minimum_age_s < 0:
        raise ValueError("interval must be positive and minimum age non-negative")
    while True:
        plan = build_reclamation_plan(args.local_root, args.shared_root,
            archive_root=args.archive_root,
            verify_sha256=args.verify_sha256,
            minimum_age_s=args.minimum_age_s, pipeline_id=args.pipeline_id)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_name(args.output.name + ".next")
            temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
            temporary.replace(args.output)
        result = (apply_reclamation_plan(plan, limit=args.limit)
                  if args.apply else {"dry_run": True, **plan["summary"]})
        print(json.dumps(result, sort_keys=True), flush=True)
        if not args.watch:
            return 0
        time.sleep(args.interval_s)


def command_starlink_local_report_converge(args: argparse.Namespace) -> int:
    plan = build_local_report_plan(
        args.local_root, args.shared_root, archive_root=args.archive_root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    result = (apply_local_report_plan(plan) if args.apply else
              {"dry_run": True, **plan["summary"]})
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("deferred") else 0


def command_starlink_local_artifact_converge(args: argparse.Namespace) -> int:
    plan = build_local_artifact_plan(
        args.local_root, args.shared_root, archive_root=args.archive_root,
        minimum_age_s=args.minimum_age_s)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    result = (apply_local_artifact_plan(plan) if args.apply else
              {"dry_run": True, **plan["summary"]})
    if args.apply:
        result = {"status": result["status"],
            "removed_count": result["removed_count"],
            "removed_bytes": result["removed_bytes"],
            "deferred_count": len(result.get("deferred", [])),
            "receipt": str(args.shared_root /
                "reports/reclamation/local-obsolete-artifacts.json")}
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("deferred") else 0


def command_starlink_shared_transient_converge(args: argparse.Namespace) -> int:
    plan = build_shared_transient_plan(
        args.shared_root, args.archive_root,
        minimum_age_s=args.minimum_age_s)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    if args.apply:
        result = apply_shared_transient_plan(plan, confirmation=args.confirm)
        output = {"status": result["status"],
            "removed_count": result["removed_count"],
            "removed_bytes": result["removed_bytes"],
            "deferred_count": len(result.get("deferred", [])),
            "receipt": str(args.shared_root /
                "reports/reclamation/shared-transients.json")}
    else:
        output = {"dry_run": True, **plan["summary"]}
    print(json.dumps(output, sort_keys=True))
    return 1 if output.get("deferred_count") else 0


def command_starlink_qnap_lifecycle(args: argparse.Namespace) -> int:
    if args.apply:
        with qnap_storage_mutation_lock(args.shared_root):
            return _command_starlink_qnap_lifecycle(args, mutation_lock_held=True)
    return _command_starlink_qnap_lifecycle(args, mutation_lock_held=False)


def _command_starlink_qnap_lifecycle(
        args: argparse.Namespace, *, mutation_lock_held: bool) -> int:
    plan = build_qnap_lifecycle_plan(args.shared_root, args.archive_root,
        minimum_age_hours=args.minimum_age_hours,
        maximum_tier=args.maximum_tier)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    result = (apply_qnap_lifecycle_plan(plan, confirmation=args.confirm,
        trigger_free_gb=args.trigger_free_gb, target_free_gb=args.target_free_gb,
        limit=args.limit, pressure_required=not args.ignore_pressure,
        mutation_lock_held=mutation_lock_held)
        if args.apply else {"dry_run": True, **plan["summary"]})
    print(json.dumps(result, sort_keys=True))
    return 0


def command_starlink_evidence_plan(args: argparse.Namespace) -> int:
    report = plan_evidence(args.capture, args.reports, args.output,
                           guard_s=args.guard_s,
                           control_duration_s=args.control_duration_s,
                           control_count=args.control_count, policy=args.policy)
    print(json.dumps({"plan": str(args.output), **report["summary"]}, sort_keys=True))
    return 0


def command_starlink_evidence_compare(args: argparse.Namespace) -> int:
    reference = json.loads(args.reference.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    report = compare_evidence_plan_coverage(reference, candidate)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["valid"] else 1


def command_starlink_evidence_v2_shadow(args: argparse.Namespace) -> int:
    report = build_evidence_v2_shadow(args.shared_root, args.archive_root,
                                      output=args.output, limit=args.limit)
    print(json.dumps(report["summary"], sort_keys=True))
    return 0 if not report["failures"] else 1


def command_starlink_evidence_extract(args: argparse.Namespace) -> int:
    report = extract_evidence(args.capture, args.plan, args.output)
    print(json.dumps({"bundle": str(args.output), **report["summary"]}, sort_keys=True))
    return 0


def command_starlink_evidence_verify(args: argparse.Namespace) -> int:
    report = verify_evidence(args.bundle, capture_path=args.source,
                             write=not args.no_write)
    print(json.dumps({"bundle": str(args.bundle), "valid": report["valid"],
                      "source_verified": report["source_verified"],
                      "clip_count": len(report["checks"])}, sort_keys=True))
    return 0 if report["valid"] else 1


def command_starlink_evidence_audit(args: argparse.Namespace) -> int:
    report = audit_evidence(args.source_root, args.evidence_root, args.output)
    print(json.dumps({"audit": None if args.output is None else str(args.output),
                      "source_capture_count": report["source_capture_count"],
                      "bundle_count": report["bundle_count"],
                      "verified_bundle_count": report["verified_bundle_count"],
                      "invalid_count": len(report["invalid"]),
                      "missing_count": len(report["missing_recordings"])}, sort_keys=True))
    return 0 if not report["invalid"] else 1


def command_starlink_evidence_archive(args: argparse.Namespace) -> int:
    receipt = archive_evidence(args.capture, args.reports, args.qnap_root,
                               guard_s=args.guard_s,
                               control_duration_s=args.control_duration_s,
                               control_count=args.control_count)
    print(json.dumps({"receipt": str(args.qnap_root / "catalog" / "receipts" /
                                     f"{args.capture.name}.json"),
                      **receipt["summary"]}, sort_keys=True))
    return 0


def command_starlink_evidence_archive_v2(args: argparse.Namespace) -> int:
    receipt = archive_evidence_v2(args.capture, args.reports, args.qnap_root)
    print(json.dumps({"receipt": str(args.qnap_root / "catalog" / "v2" / "receipts" /
                                     f"{args.capture.name}.json"),
                      **receipt["summary"]}, sort_keys=True))
    return 0


def command_starlink_storage_regime_v2(args: argparse.Namespace) -> int:
    if args.apply:
        with qnap_storage_mutation_lock(args.shared_root):
            return _command_starlink_storage_regime_v2(
                args, mutation_lock_held=True)
    return _command_starlink_storage_regime_v2(args, mutation_lock_held=False)


def _command_starlink_storage_regime_v2(
        args: argparse.Namespace, *, mutation_lock_held: bool) -> int:
    plan = build_storage_regime_plan(args.shared_root, args.archive_root,
                                     minimum_age_hours=args.minimum_age_hours,
                                     scope=args.scope,
                                     eligible_limit=args.planning_limit,
                                     auto_archive_slots=args.archive_reserved_slots)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    result = (apply_storage_regime_plan(plan, confirmation=args.confirm,
                                        limit=args.limit,
                                        workers=args.workers,
                                        mutation_lock_held=mutation_lock_held)
              if args.apply else
              {"dry_run": True, **plan["summary"]})
    print(json.dumps(result, sort_keys=True))
    return 0 if not result.get("failure_count") else 1


def command_starlink_storage_audit_v2(args: argparse.Namespace) -> int:
    with qnap_storage_mutation_lock(args.shared_root):
        audit = build_storage_regime_audit(
            args.shared_root, args.archive_root,
            minimum_age_hours=args.minimum_age_hours,
            sample_limit=args.sample_limit,
            require_producer_contract=args.require_producer_contract,
            maximum_producer_heartbeat_age_s=
                args.maximum_producer_heartbeat_age_s,
            local_root=args.local_root)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_name(args.output.name + ".next")
            temporary.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n")
            temporary.replace(args.output)
    print(json.dumps({"audit": str(args.output) if args.output else None,
                      "converged": audit["converged"],
                      "violation_counts": audit["violation_counts"]}, sort_keys=True))
    return 0 if audit["converged"] else 1


def command_starlink_storage_normalize_legacy(args: argparse.Namespace) -> int:
    if args.apply:
        with qnap_storage_mutation_lock(args.shared_root):
            plan = build_legacy_layout_plan(
                args.shared_root, args.archive_root,
                eligible_limit=args.planning_limit)
            result = apply_legacy_layout_plan(
                plan, confirmation=args.confirm, limit=args.limit,
                mutation_lock_held=True)
    else:
        plan = build_legacy_layout_plan(
            args.shared_root, args.archive_root,
            eligible_limit=args.planning_limit)
        result = {"dry_run": True, **plan["summary"]}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".next")
        temporary.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
    print(json.dumps(result, sort_keys=True))
    return 0 if not result.get("failure_count") else 1


def command_starlink_evidence_repair_v2_summaries(args: argparse.Namespace) -> int:
    result = repair_evidence_v2_summaries(args.qnap_root, limit=args.limit)
    print(json.dumps(result, sort_keys=True))
    return 0 if not result["error_count"] else 1


def command_starlink_evidence_materialize(args: argparse.Namespace) -> int:
    manifest = materialize_evidence_clip(args.bundle, args.interval_id, args.output)
    print(json.dumps({"capture": str(args.output),
                      "samples_per_receiver": manifest["captured_samples_per_receiver"],
                      "source_first_sample": manifest["metadata"]["evidence"][
                          "source_first_sample"]}, sort_keys=True))
    return 0


def command_starlink_beacon_recover(args: argparse.Namespace) -> int:
    print(json.dumps(recover_unanalyzed_beacons(args.root, passes_path=args.passes,
        exact_acquisition_method=args.exact_acquisition_method,
        narrow_exact_interval_s=args.narrow_exact_interval_s,
        wide_exact_interval_s=args.wide_exact_interval_s),
                     sort_keys=True))
    return 0


def command_starlink_beacon_followup(args: argparse.Namespace) -> int:
    report = followup_beacon_capture(args.capture, args.analysis, args.output,
        radius_s=args.radius_s, interval_s=args.interval_s, window_s=args.window_s,
        passes_path=args.passes)
    if args.confirmation_marker:
        marker = Path(args.confirmation_marker)
        if report["confirmation"]["confirmed"]:
            marker.parent.mkdir(parents=True, exist_ok=True); marker.touch()
        else:
            marker.unlink(missing_ok=True)
    print(json.dumps({"followup": str(args.output), "trigger_count": report["trigger_count"],
                      "check_count": len(report["checks"]),
                      "confirmed": report["confirmation"]["confirmed"]}, sort_keys=True))
    return 0


def command_starlink_beacon_followup_rescore(args: argparse.Namespace) -> int:
    report = rescore_beacon_followup(args.followup, passes_path=args.passes)
    if args.plot:
        plot_beacon_followup(report, args.plot, start_s=args.plot_start_s,
                             stop_s=args.plot_stop_s)
    print(json.dumps({"followup": str(args.followup),
                      "confirmed": report["confirmation"]["confirmed"],
                      "dual_receiver_confirmed": report["confirmation"].get(
                          "dual_receiver_confirmed", False),
                      "overlapping_pass_count": len(report["overlapping_passes"])},
                     sort_keys=True))
    return 0


def command_starlink_beacon_track(args: argparse.Namespace) -> int:
    report = track_beacon_capture(
        args.capture, args.followup, args.output,
        output_rate_hz=args.output_rate_hz,
        search_span_hz=args.search_span_hz,
        maximum_reacquisition_span_hz=args.maximum_reacquisition_span_hz,
        search_step_hz=args.search_step_hz,
        minimum_margin=args.minimum_margin,
        tracking_margin=args.tracking_margin,
        maximum_relative_error_hz=args.maximum_relative_error_hz,
        maximum_gap_s=args.maximum_gap_s,
        maximum_drift_hz_s=args.maximum_drift_hz_s,
        measurement_source=args.measurement_source,
        frame_track_path=args.frame_track)
    print(json.dumps({"track": str(args.output), **report["summary"]}, sort_keys=True))
    return 0


def command_starlink_beacon_frame_track(args: argparse.Namespace) -> int:
    report = track_conditioned_frames(args.capture, args.followup, args.output,
        args.samples, window_s=args.window_s, minimum_margin=args.minimum_margin,
        maximum_relative_error_hz=args.maximum_relative_error_hz,
        beacon_template_path=args.beacon_template,
        maximum_extension_s=args.maximum_extension_s,
        maximum_missed_windows=args.maximum_missed_windows,
        minimum_extension_window_margin=args.minimum_extension_window_margin,
        minimum_sparse_frame_margin=args.minimum_sparse_frame_margin)
    print(json.dumps({"frame_track": str(args.output), "samples": str(args.samples),
        **report["summary"]}, sort_keys=True))
    return 0


def command_starlink_beacon_template_learn(args: argparse.Namespace) -> int:
    report = learn_bandpass_beacon(
        args.capture, args.followup, args.output, args.samples,
        window_s=args.window_s, maximum_frames=args.maximum_frames,
        iterations=args.iterations)
    print(json.dumps({"learned_beacon": str(args.output),
        "samples": str(args.samples), **report["summary"]}, sort_keys=True))
    return 0


def command_starlink_beacon_channel_link(args: argparse.Namespace) -> int:
    report = link_channel_tracks(args.tracks, args.output,
        maximum_gap_s=args.maximum_gap_s,
        maximum_acceleration_difference_m_s2=
            args.maximum_acceleration_difference_m_s2,
        minimum_ambiguity_margin_m_s2=args.minimum_ambiguity_margin_m_s2,
        maximum_same_tuning_quadratic_rms_hz=
            args.maximum_same_tuning_quadratic_rms_hz,
        maximum_same_tuning_range_jerk_m_s3=
            args.maximum_same_tuning_range_jerk_m_s3,
        minimum_segment_epochs=args.minimum_segment_epochs,
        minimum_segment_duration_s=args.minimum_segment_duration_s)
    print(json.dumps({"channel_link": str(args.output), **report["summary"]},
                     sort_keys=True))
    return 0


def command_starlink_beacon_calibrate(args: argparse.Namespace) -> int:
    report = build_beacon_calibration(args.reports, args.output)
    print(json.dumps({"calibration": str(args.output),
                      "narrow_checks": report["modes"]["narrow"]["check_count"],
                      "wide_checks": report["modes"]["wide"]["check_count"],
                      "acquisition_methods": {method: {
                          "narrow_checks": modes["narrow"]["check_count"],
                          "wide_checks": modes["wide"]["check_count"]}
                          for method, modes in report.get(
                              "acquisition_methods", {}).items()}}, sort_keys=True))
    return 0


def command_starlink_beacon_null_replay(args: argparse.Namespace) -> int:
    def progress(item: dict) -> None:
        print(json.dumps({"null_replay_progress": item}, sort_keys=True), flush=True)
    report = replay_beacon_null_calibration(args.root, args.output,
        acquisition_method=args.exact_acquisition_method,
        capture_limit=args.capture_limit, checks_per_capture=args.checks_per_capture,
        window_s=args.exact_window_s,
        maximum_host_temperature_c=args.maximum_host_temperature_c,
        resume_host_temperature_c=args.resume_host_temperature_c,
        progress=progress)
    print(json.dumps({"null_replay": str(args.output / "replay-summary.json"),
                      "selected_capture_count": report["selected_capture_count"],
                      "completed_report_count": len(report["completed_reports"]),
                      "reused_report_count": len(report["reused_reports"])}, sort_keys=True))
    return 0


def command_starlink_beacon_decode(args: argparse.Namespace) -> int:
    report, arrays = decode_beacon_followup(
        args.capture, args.followup, args.output, time_s=args.time_s,
        symbols_output=args.symbols)
    if args.plot:
        plot_beacon_decode(report, arrays, args.plot)
    print(json.dumps({"decode": str(args.output),
        "selected_time_s": report["selected_observation"]["start_s"],
        "frame_count": report["combined"]["minimum_frame_count"],
        "minimum_pilot_accuracy": report["combined"]["minimum_pilot_accuracy"],
        "minimum_sss_accuracy": report["combined"]["minimum_sss_accuracy"],
        "soft_dual_rx_pilot_accuracy": report["combined"]["soft_dual_rx"][
            "pilot"]["hard_symbol_accuracy"],
        "soft_dual_rx_mean_confidence": report["combined"]["soft_dual_rx"][
            "pilot"]["soft_mean_confidence"],
        "plot": str(args.plot) if args.plot else None,
        "symbols": str(args.symbols) if args.symbols else None}, sort_keys=True))
    return 0


def command_starlink_beacon_fingerprint(args: argparse.Namespace) -> int:
    report = update_fingerprint_store(args.root, capture_name=args.capture_name)
    print(json.dumps(report, sort_keys=True))
    return 1 if report["errors"] else 0


def command_starlink_beacon_gain_summary(args: argparse.Namespace) -> int:
    report = build_gain_comparison(args.root, args.output)
    print(json.dumps({"gain_summary": str(args.output),
        "randomized_capture_count": report["randomized_capture_count"],
        "groups": {mode: {"capture_count": value["capture_count"],
                           "analyzed_count": value["analyzed_count"]}
                   for mode, value in report["groups"].items()},
        "decision_ready": report["decision_guidance"]["ready"]}, sort_keys=True))
    return 0


def command_starlink_beacon_dashboard_row(args: argparse.Namespace) -> int:
    # Imported here, not at module scope: on-demand detail records are a newer
    # capability than this command, and a missing one must degrade to a failed
    # listing row rather than breaking every leo-radio entry point, which
    # includes the analysis server itself.
    from leo_tracker.radio.beacon.dashboard_index import capture_dashboard_record
    from leo_tracker.radio.beacon.dashboard_shards import write_listing_row
    record = capture_dashboard_record(args.root, args.name)
    if record is None:
        raise ValueError(f"no dashboard record for {args.name}")
    path = write_listing_row(args.root, args.name, record,
                             tuple(args.sources or ()))
    print(json.dumps({"dashboard_row": str(path), "recording_id": args.name},
                     sort_keys=True))
    return 0


def command_starlink_probe_index(args: argparse.Namespace) -> int:
    """Build, inspect or query the per-probe projection."""
    from .beacon.probe_index import (ProbeIndexUnavailable, build,
                                     partition_status, query)
    try:
        if args.action == "build":
            print(json.dumps(build(args.root, pattern=args.pattern,
                                   rebuild=args.rebuild, limit=args.limit),
                             indent=2, sort_keys=True))
        elif args.action == "status":
            print(json.dumps(partition_status(args.root, pattern=args.pattern),
                             indent=2, sort_keys=True))
        else:
            if not args.sql:
                print("--sql is required for query", file=sys.stderr)
                return 2
            for row in query(args.root, args.sql):
                print(json.dumps(list(row), default=str))
    except ProbeIndexUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0


def command_starlink_analysis_index(args: argparse.Namespace) -> int:
    """Build, inspect or query the partitioned analysis projection."""
    from .analysis_store.partition import (build, partition_status, query)
    from .analysis_store.ingest import AnalysisStoreUnavailable
    root = args.root if args.index_root is None else args.index_root
    try:
        if args.action == "build":
            print(json.dumps(build(root, args.root, rebuild=args.rebuild,
                                   limit=args.limit, work_dir=args.work_dir,
                                   memory_limit=args.memory_limit),
                             indent=2, sort_keys=True, default=str))
        elif args.action == "status":
            print(json.dumps(partition_status(root, args.root),
                             indent=2, sort_keys=True, default=str))
        else:
            if not args.sql:
                print("--sql is required for query", file=sys.stderr)
                return 2
            for row in query(root, args.sql):
                print(json.dumps(list(row), default=str))
    except AnalysisStoreUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0


def command_starlink_analysis_store(args: argparse.Namespace) -> int:
    """Operate the Kalman-owned transactional analysis store."""
    from .analysis_store.queue import StoreQueue, enqueue_backfill, owner_lock
    from .analysis_store.service import run_service, service_status
    from .analysis_store.snapshot import publish_snapshot

    database = (args.database if args.database is not None else
                args.store_root / "live" / "analysis.duckdb")
    create_queue = args.action not in {"status", "query"} and not (
        args.action == "backfill" and args.dry_run)
    queue = StoreQueue(args.store_root, args.shared_root, database,
                       create=create_queue)
    if args.action == "init":
        with owner_lock(args.store_root):
            result = queue.store.initialize()
    elif args.action == "enqueue":
        if not args.recording_id or not args.pipeline_id:
            raise ValueError("enqueue requires --recording-id and --pipeline-id")
        result = queue.enqueue(args.recording_id, args.pipeline_id)
    elif args.action == "drain":
        with owner_lock(args.store_root):
            queue.store.initialize(); queue.recover()
            result = queue.drain(limit=args.limit,
                                 continue_on_error=args.continue_on_error)
    elif args.action == "backfill":
        if args.dry_run and not database.is_file():
            result = enqueue_backfill(queue, limit=args.limit, dry_run=True)
        else:
            # Backfill reads the live run-ID set. It may enqueue concurrently
            # with workers, but it must never open the live database alongside
            # the daemon owner.
            with owner_lock(args.store_root):
                result = enqueue_backfill(queue, limit=args.limit,
                                          dry_run=args.dry_run)
    elif args.action == "status":
        if args.runtime_output is not None and args.runtime_output.is_file():
            result = json.loads(args.runtime_output.read_text())
        else:
            # Offline inspection only. A running daemon publishes its cheap
            # status document via --runtime-output.
            with owner_lock(args.store_root):
                result = service_status(queue)
    elif args.action == "query":
        if not args.sql:
            raise ValueError("query requires --sql")
        if args.database is None:
            raise ValueError("query requires --database pointing to a published snapshot")
        columns, rows = queue.store.query(args.sql)
        result = {"columns": columns, "rows": rows}
    elif args.action == "publish":
        if args.publication_root is None:
            raise ValueError("publish requires --publication-root")
        with owner_lock(args.store_root):
            result = publish_snapshot(queue.store, args.store_root,
                                      args.publication_root)
    else:
        result = run_service(
            args.store_root, args.shared_root, database=database,
            publication_root=args.publication_root,
            runtime_output=args.runtime_output, poll_s=args.poll_s,
            snapshot_interval_s=args.snapshot_interval_s,
            reconciliation_interval_s=args.reconciliation_interval_s,
            reconciliation_limit=args.reconciliation_limit, once=args.once)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 1 if result.get("errors") or result.get("failures") else 0


def command_starlink_lnb_calibration(args: argparse.Namespace) -> int:
    """Measure LNB mismatch, and report anything that moved since last time."""
    from .beacon.lnb_calibration import (compare_calibration, load_calibration,
                                         measure_mismatch, receiver_centers,
                                         write_calibration)
    previous = load_calibration(args.root)
    current = measure_mismatch(args.root / "reports", limit=args.limit)
    alerts = compare_calibration(previous, current)
    current["alerts"] = alerts
    current["receiver_centers_hz"] = {
        radio: list(receiver_centers(current, radio))
        for radio, entry in current["radios"].items() if entry.get("measured")}
    if args.apply:
        current["stored"] = str(write_calibration(args.root, current))
    print(json.dumps(current, indent=2, sort_keys=True))
    # A moved cable earns a non-zero exit so a timer surfaces it.
    return 1 if alerts else 0


def command_starlink_dashboard_shards(args: argparse.Namespace) -> int:
    from leo_tracker.radio.beacon.dashboard_shards import (compact_shards,
                                                           migrate_index)
    if args.action == "migrate":
        if args.index is None:
            raise ValueError("migrate needs --index")
        result = migrate_index(args.index, args.root,
                               tuple(args.sources or ()))
    else:
        result = compact_shards(args.root)
    print(json.dumps({"action": args.action, "root": str(args.root), **result},
                     sort_keys=True))
    return 0


def command_starlink_beacon_dashboard_index(args: argparse.Namespace) -> int:
    report = update_dashboard_index(args.root, args.output,
                                    capture_name=args.capture_name)
    print(json.dumps({"dashboard_index": str(args.output),
        "recording_count": len(report["recordings"]),
        "summary": report["summary"]}, sort_keys=True))
    return 0


def command_analyze(args: argparse.Namespace) -> int:
    artifact = CaptureArtifact.open(args.capture, verify=True)
    samples, config = artifact.load_samples(mmap=True), RadioConfig(**artifact.manifest["radio_config"])
    hop = args.hop_size or args.fft_size // 2
    track = extract_frequency_ridge(samples, config.sample_rate_hz, fft_size=args.fft_size,
                                    hop_size=hop, search_hz=tuple(args.search_hz) if args.search_hz else None,
                                    min_snr_db=args.min_snr_db, max_step_hz=args.max_step_hz)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    waterfall = output / "waterfall.png"
    ridge_path = output / "ridge.json"
    _write_waterfall(samples, config.sample_rate_hz, waterfall, args.fft_size, hop)
    ridge = {"schema_version": RIDGE_SCHEMA_VERSION, "capture_id": artifact.manifest["capture_id"],
             "sample_rate_hz": config.sample_rate_hz, "fft_size": track.fft_size,
             "hop_size": track.hop_size, "blind": True,
             "points": [asdict(point) for point in track.points]}
    ridge_path.write_text(json.dumps(ridge, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"ridge": str(ridge_path), "waterfall": str(waterfall),
                      "points": len(track.points)}, sort_keys=True))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    artifact = CaptureArtifact.open(args.capture, verify=True)
    print(json.dumps({"capture_id": artifact.manifest["capture_id"], "verified": True}, sort_keys=True))
    return 0


def command_moving(args: argparse.Namespace) -> int:
    artifact = CaptureArtifact.open(args.capture, verify=not args.skip_checksum)
    config = RadioConfig(**artifact.manifest["radio_config"])
    result = detect_moving_ridge(artifact.path / "iq.c64", config.sample_rate_hz,
        fft_size=args.fft_size, hop_size=args.hop_size,
        search_hz=tuple(args.search_hz) if args.search_hz else None,
        candidates_per_frame=args.candidates, max_step_hz=args.max_step_hz)
    payload = asdict(result); payload["capture_id"] = artifact.manifest["capture_id"]
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: payload[k] for k in ("moving_score", "median_excess_db", "frequency_span_hz", "fitted_drift_hz_s")}, sort_keys=True))
    return 0


def command_comb(args: argparse.Namespace) -> int:
    artifact = CaptureArtifact.open(args.capture, verify=not args.skip_checksum)
    config = RadioConfig(**artifact.manifest["radio_config"])
    result = detect_moving_comb(artifact.path / "iq.c64", config.sample_rate_hz,
        fft_size=args.fft_size, integration_s=args.integration_s,
        spectra_per_integration=args.spectra_per_integration, tone_count=args.tone_count,
        tone_spacing_hz=args.tone_spacing_hz, search_hz=tuple(args.search_hz),
        max_drift_hz_s=args.max_drift_hz_s)
    payload = asdict(result); payload["capture_id"] = artifact.manifest["capture_id"]
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as exc: raise RuntimeError("comb plotting requires matplotlib") from exc
        times = [point.time_s for point in result.points]
        fig, axes = plt.subplots(3, 1, figsize=(9, 7), sharex=True, constrained_layout=True)
        axes[0].plot(times, [point.center_frequency_hz / 1e3 for point in result.points], lw=1)
        axes[0].set(ylabel="Comb center (kHz)", title="Blind stationary-suppressed comb track")
        axes[1].plot(times, [point.comb_z_score for point in result.points], lw=1)
        axes[1].axhline(0, color="black", lw=.5); axes[1].set(ylabel="Comb score (z)")
        axes[2].plot(times, [point.positive_tone_fraction for point in result.points], lw=1)
        axes[2].set(xlabel="Capture time (s)", ylabel="Tone fraction", ylim=(-.02, 1.02))
        fig.savefig(args.plot, dpi=140); plt.close(fig)
    print(json.dumps({k: payload[k] for k in ("median_comb_z_score", "median_positive_tone_fraction",
                                               "frequency_span_hz", "fitted_drift_hz_s", "score_percentile")}, sort_keys=True))
    return 0


def command_qualify_pair(args: argparse.Namespace) -> int:
    summary, results = qualify_paired_comb(args.session, true_spacing_hz=args.true_spacing_hz,
        wrong_spacings_hz=args.wrong_spacings_hz, score_margin=args.score_margin,
        rx_margin=args.rx_margin, min_positive_tone_fraction=args.min_positive_tone_fraction,
        skip_checksum=args.skip_checksum, fft_size=args.fft_size, integration_s=args.integration_s,
        spectra_per_integration=args.spectra_per_integration, tone_count=args.tone_count,
        search_hz=tuple(args.search_hz), max_drift_hz_s=args.max_drift_hz_s)
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    if args.plot_dir:
        try:
            import matplotlib
            matplotlib.use("Agg"); import matplotlib.pyplot as plt
        except ImportError as exc: raise RuntimeError("qualification plotting requires matplotlib") from exc
        plot_dir = Path(args.plot_dir); plot_dir.mkdir(parents=True, exist_ok=True); plots = {}
        for channel in ("rx0", "rx1"):
            fig, axis = plt.subplots(figsize=(9, 4), constrained_layout=True)
            for spacing, result in results[channel].items():
                axis.plot([p.time_s for p in result.points], [p.comb_z_score for p in result.points],
                          lw=1, label=f"{spacing:g} Hz")
            axis.set(xlabel="Capture time (s)", ylabel="Comb score (z)", title=f"{channel.upper()} true/control comb scores")
            axis.legend(); path = plot_dir / f"{channel}_comb_controls.png"; fig.savefig(path, dpi=140); plt.close(fig)
            plots[channel] = str(path)
        summary["diagnostic_plots"] = plots
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"radio_qualified": summary["radio_qualified"], "reasons": summary["reasons"],
                      "summary": str(output)}, sort_keys=True))
    return 0 if summary["radio_qualified"] else 3


def command_carrier(args: argparse.Namespace) -> int:
    artifact = CaptureArtifact.open(args.capture, verify=not args.skip_checksum)
    config = RadioConfig(**artifact.manifest["radio_config"])
    if args.search_hz:
        low, high = args.search_hz
    else:
        if args.search_center_hz is None or args.search_span_hz is None:
            raise ValueError("provide --search-hz LOW HIGH or both --search-center-hz and --search-span-hz")
        low, high = args.search_center_hz-args.search_span_hz/2, args.search_center_hz+args.search_span_hz/2
    result = track_carrier(artifact.path / "iq.c64", sample_rate_hz=config.sample_rate_hz,
        center_frequency_hz=config.center_frequency_hz, search_low_hz=low, search_high_hz=high,
        integration_s=args.integration_s, fft_size=args.fft_size,
        spectra_per_integration=args.spectra_per_integration)
    payload = asdict(result)
    payload.update({"schema_version": 1, "workflow": "narrow-carrier-track",
        "capture_id": artifact.manifest["capture_id"],
        "capture_iq_sha256": artifact.manifest["files"]["iq.c64"]["sha256"],
        "capture_start_utc_ns": artifact.manifest.get("start_utc_ns"),
        "radio_config": artifact.manifest["radio_config"],
        "analysis_claim": "measurement only; not a detector claim"})
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True)+"\n")
    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as exc: raise RuntimeError("carrier plotting requires matplotlib") from exc
        times = np.array([point.time_s for point in result.points])
        frequencies = np.array([point.frequency_hz for point in result.points])
        prominence = np.array([point.prominence_db for point in result.points])
        fit = result.fitted_intercept_hz + result.fitted_linear_drift_hz_s * times
        fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True, constrained_layout=True)
        axes[0].plot(times, frequencies, ".", ms=3, label="measured")
        axes[0].plot(times, fit, lw=1, label="linear fit")
        axes[0].ticklabel_format(axis="y", style="plain", useOffset=False)
        axes[0].set(ylabel="Absolute frequency (Hz)", title="Narrow-carrier measurement track")
        axes[0].legend()
        axes[1].plot(times, prominence, lw=1)
        axes[1].set(xlabel="Capture time (s)", ylabel="Prominence (dB)")
        note = (f"drift {result.fitted_linear_drift_hz_s:+.3f} Hz/s   "
                f"span {result.frequency_span_hz:.1f} Hz   residual RMS {result.residual_rms_hz:.2f} Hz\n"
                "Measurement only — not a detector claim")
        fig.text(.5, .005, note, ha="center", va="bottom", fontsize=9)
        plot_path = Path(args.plot); plot_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(plot_path, dpi=140); plt.close(fig)
    print(json.dumps({"points": len(result.points), "fitted_linear_drift_hz_s": result.fitted_linear_drift_hz_s,
        "frequency_span_hz": result.frequency_span_hz, "residual_rms_hz": result.residual_rms_hz,
        "output": str(output)}, sort_keys=True))
    return 0


def command_scan(args: argparse.Namespace) -> int:
    frequencies = np.arange(args.start_hz, args.stop_hz + args.step_hz / 2,
                            args.step_hz).tolist()
    channels = _selected_channels(args)
    results = scan_channels_pyadi(
        uri=args.uri, frequencies_hz=frequencies,
        sample_rate_hz=args.sample_rate_hz, bandwidth_hz=args.bandwidth_hz,
        gain_db=args.gain_db, samples_per_frequency=args.samples_per_frequency,
        settle_seconds=args.settle_seconds, channels=channels,
    )
    metadata = {
        "uri": args.uri, "serial": args.serial, "gain_db": args.gain_db,
        "sample_rate_hz": args.sample_rate_hz, "bandwidth_hz": args.bandwidth_hz,
        "channels": list(channels),
    }
    if len(channels) == 1:
        write_scan_report(results[channels[0]], args.output_dir,
                          {**metadata, "channel": channels[0], "simultaneous": False})
    else:
        write_multi_scan_report(results, args.output_dir, metadata)
    print(json.dumps({"points_per_channel": len(results[channels[0]]),
                      "channels": list(channels), "simultaneous": len(channels) > 1,
                      "scan": str(args.output_dir / "scan.json"),
                      "plot": str(args.output_dir / "scan.png")}, sort_keys=True))
    return 0


def command_monitor(args: argparse.Namespace) -> int:
    if args.centers_file:
        from .regions import read_centers
        centers = read_centers(args.centers_file)
    else:
        if args.stop_hz is None or args.step_hz is None or args.step_hz <= 0:
            raise ValueError("range monitoring requires --stop-hz and a positive --step-hz")
        centers = np.arange(args.start_hz, args.stop_hz + args.step_hz / 2,
                            args.step_hz).tolist()
    channels = _selected_channels(args)
    cycles = monitor_channels_pyadi(
        uri=args.uri, centers_hz=centers, cycles=args.cycles,
        sample_rate_hz=args.sample_rate_hz, bandwidth_hz=args.bandwidth_hz,
        gain_db=args.gain_db, samples_per_tuning=args.samples_per_tuning,
        settle_seconds=args.settle_seconds, channels=channels,
        output_bins=args.psd_bins, fft_size=args.fft_size,
        discard_buffers=args.discard_buffers)
    candidates = find_motion_candidates(
        cycles, min_shift_hz=args.min_shift_hz, max_shift_hz=args.max_shift_hz,
        min_correlation=args.min_correlation,
        channel_tolerance_hz=args.channel_tolerance_hz,
        max_cycle_lag=args.max_cycle_lag)
    result = MonitorResult(tuple(channels), tuple(centers), cycles, candidates)
    metadata = {key: getattr(args, key) for key in (
        "uri", "sample_rate_hz", "bandwidth_hz", "gain_db", "samples_per_tuning",
        "settle_seconds", "discard_buffers", "fft_size", "psd_bins", "min_shift_hz",
        "max_shift_hz", "min_correlation", "channel_tolerance_hz", "max_cycle_lag")}
    write_monitor_report(result, args.output_dir, metadata)
    print(json.dumps({"cycles": len(cycles), "centers": len(centers),
                      "channels": list(channels), "candidates": len(candidates),
                      "strongest_candidates": [asdict(item) for item in candidates[:5]],
                      "report": str(args.output_dir / "monitor.json"),
                      "spectra": str(args.output_dir / "spectra.npz")}, sort_keys=True))
    return 0


def command_rank_regions(args: argparse.Namespace) -> int:
    from .regions import write_region_plan
    value = write_region_plan(args.monitor_report, args.output, count=args.count,
                              offset=args.offset)
    print(json.dumps({"regions": len(value["regions"]), "centers_hz": value["centers_hz"],
                      "output": str(args.output)}, sort_keys=True))
    return 0


def command_reanalyze_monitor(args: argparse.Namespace) -> int:
    cycles = read_monitor_cycles(args.monitor_report)
    channels = tuple(sorted({frame.channel for frame in cycles[0]}))
    centers = tuple(sorted({frame.center_frequency_hz for frame in cycles[0]}))
    candidates = find_motion_candidates(cycles, min_shift_hz=args.min_shift_hz,
        max_shift_hz=args.max_shift_hz, min_correlation=args.min_correlation,
        channel_tolerance_hz=args.channel_tolerance_hz,
        max_cycle_lag=args.max_cycle_lag)
    result = MonitorResult(channels, centers, cycles, candidates)
    metadata = {"reprocessed_from": str(args.monitor_report),
        "min_shift_hz": args.min_shift_hz, "max_shift_hz": args.max_shift_hz,
        "min_correlation": args.min_correlation,
        "channel_tolerance_hz": args.channel_tolerance_hz,
        "max_cycle_lag": args.max_cycle_lag}
    write_monitor_report(result, args.output_dir, metadata)
    print(json.dumps({"candidates": len(candidates),
        "strongest_candidates": [asdict(item) for item in candidates[:5]],
        "report": str(args.output_dir / "monitor.json")}, sort_keys=True))
    return 0


def command_starlink_channels(args: argparse.Namespace) -> int:
    values = [asdict(item) for item in channel_plan()]
    print(json.dumps({"schema": "leo-tracker.starlink-channels/v1", "channels": values},
                     indent=2, sort_keys=True))
    return 0


def command_starlink_hybrid_plan(args: argparse.Namespace) -> int:
    plan = build_hybrid_plan(dwell_seconds=args.dwell_seconds)
    # ``monitor --centers-file`` consumes this compatibility field directly.
    plan["centers_hz"] = plan["survey"]["centers_hz"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2, sort_keys=True)+"\n")
    print(json.dumps({"output": str(args.output),
        "survey_tiles": plan["survey"]["tile_count"],
        "dwell_sample_rate_hz": plan["dwell"]["sample_rate_hz"],
        "fallback_sample_rate_hz": plan["fallback"]["sample_rate_hz"]}, sort_keys=True))
    return 0


def command_starlink_observe(args: argparse.Namespace) -> int:
    channel = get_channel(args.channel_number)
    if channel.lnb_band == "high" and not args.high_band_selected:
        raise ValueError("channels 5-8 require the LNB 22 kHz high-band tone; pass --high-band-selected only after it is active")
    channels = _selected_channels(args)
    if args.if_offset_hz:
        channel = replace(channel, if_center_hz=channel.if_center_hz + args.if_offset_hz)
    thresholds = {"gutter_threshold_db": args.gutter_threshold_db,
                  "periodicity_threshold": args.periodicity_threshold,
                  "comb_threshold_db": args.comb_threshold_db,
                  "search_hz": args.search_hz, "fft_size": args.fft_size}
    if args.fake:
        blocks = fake_blocks(sample_rate_hz=args.sample_rate_hz, block_size=args.block_size,
            duration_s=args.duration_s, receiver_count=len(channels), signal=not args.fake_noise_only,
            seed=args.seed)
        identity = {"kind": "fake-starlink", "seed": args.seed,
                    "signal": not args.fake_noise_only, "channels": list(channels)}
        report = observe_blocks(blocks, args.output_dir, sample_rate_hz=args.sample_rate_hz,
            channel=channel, duration_s=args.duration_s, ring_seconds=args.ring_seconds,
            thresholds=thresholds, identity=identity)
    else:
        config = RadioConfig(channel.if_center_hz, args.sample_rate_hz, args.bandwidth_hz,
                             args.gain_db, channels[0])
        if len(channels) == 2:
            source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                       transport="iio", serial=args.serial)
            blocks = threaded_source_blocks(source, paired=True, queue_blocks=args.queue_blocks)
        else:
            source = PlutoSource(config, uri=args.uri, block_size=args.block_size,
                                 transport=args.transport, serial=args.serial)
            blocks = threaded_source_blocks(source, paired=False, queue_blocks=args.queue_blocks)
        try:
            report = observe_blocks(blocks, args.output_dir,
                sample_rate_hz=args.sample_rate_hz, channel=channel,
                duration_s=args.duration_s, ring_seconds=args.ring_seconds,
                thresholds=thresholds, identity=dict(source.identity))
        finally:
            source.close()
    print(json.dumps({"report": str(args.output_dir / "observation.json"),
        "channel_number": channel.number, "if_center_hz": channel.if_center_hz,
        "blocks": report["block_count"], "promoted_blocks": report["promoted_blocks"],
        "event_iq": report["event_iq"]}, sort_keys=True))
    return 0


def command_starlink_analyze(args: argparse.Namespace) -> int:
    channels, sample_rate = read_event_iq(args.event_iq)
    metrics = [asdict(analyze_starlink_block(values, sample_rate,
        search_hz=args.search_hz, gutter_threshold_db=args.gutter_threshold_db,
        periodicity_threshold=args.periodicity_threshold,
        comb_threshold_db=args.comb_threshold_db, fft_size=args.fft_size))
        for values in channels]
    value = {"schema": "leo-tracker.starlink-analysis/v1",
             "source": str(args.event_iq), "sample_rate_hz": sample_rate,
             "receivers": metrics, "promoted": any(item["promoted"] for item in metrics)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "promoted": value["promoted"]}, sort_keys=True))
    return 0


def command_starlink_offset_search(args: argparse.Namespace) -> int:
    channel = get_channel(args.channel_number)
    if channel.lnb_band == "high" and not args.high_band_selected:
        raise ValueError("channels 5-8 require the LNB 22 kHz high-band tone; pass --high-band-selected only after it is active")
    channels = _selected_channels(args)
    nominal_if_center_hz = channel.if_center_hz
    if args.if_offset_hz:
        channel = replace(channel, if_center_hz=channel.if_center_hz + args.if_offset_hz)
    if args.fake:
        blocks = fake_blocks(sample_rate_hz=args.sample_rate_hz,
            block_size=args.block_size, duration_s=(args.snapshots * args.block_size /
            args.sample_rate_hz), receiver_count=len(channels), signal=not args.fake_noise_only,
            seed=args.seed)
        identity = {"kind": "fake-starlink-offset", "channels": list(channels)}
        report = aggregate_gutter_search(blocks, sample_rate_hz=args.sample_rate_hz,
            snapshots=args.snapshots, search_hz=args.search_hz, fft_size=args.fft_size,
            top_count=args.top_count, bin_hz=args.bin_hz)
    else:
        config = RadioConfig(channel.if_center_hz, args.sample_rate_hz,
                             args.bandwidth_hz, args.gain_db, channels[0])
        if len(channels) == 2:
            source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                       transport="iio", serial=args.serial)
            blocks = threaded_source_blocks(source, paired=True, queue_blocks=args.queue_blocks)
        else:
            source = PlutoSource(config, uri=args.uri, block_size=args.block_size,
                                 transport=args.transport, serial=args.serial)
            blocks = threaded_source_blocks(source, paired=False, queue_blocks=args.queue_blocks)
        identity = dict(source.identity)
        try:
            report = aggregate_gutter_search(blocks, sample_rate_hz=args.sample_rate_hz,
                snapshots=args.snapshots, search_hz=args.search_hz, fft_size=args.fft_size,
                top_count=args.top_count, bin_hz=args.bin_hz)
        finally:
            source.close()
    report["channel"] = asdict(channel); report["identity"] = identity
    report["nominal_if_center_hz"] = nominal_if_center_hz
    report["tuning_offset_hz"] = args.if_offset_hz
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "channel_number": channel.number,
        "snapshots": report["snapshots"], "top_candidates": report["ranked_candidates"][:5]},
        sort_keys=True))
    return 0


def command_starlink_waterfall_capture(args: argparse.Namespace) -> int:
    channel = get_channel(args.channel_number)
    if channel.lnb_band == "high" and not args.high_band_selected:
        raise ValueError("channels 5-8 require the LNB 22 kHz high-band tone; pass --high-band-selected only after it is active")
    channel = replace(channel, if_center_hz=channel.if_center_hz + args.if_offset_hz)
    channels = _selected_channels(args)
    if args.fake:
        start = 1_700_000_000_000_000_000
        def moving_blocks():
            for index in range(args.snapshots):
                offset = args.fake_start_offset_hz + (index * args.block_size /
                    args.sample_rate_hz) * args.fake_drift_hz_s
                values = [synthetic_starlink_block(args.sample_rate_hz, args.block_size,
                    seed=args.seed + index*17 + receiver, gutter_offset_hz=offset)
                    for receiver in range(len(channels))]
                yield (start + round(index*args.block_size*1e9/args.sample_rate_hz), values)
        blocks = moving_blocks(); identity = {"kind": "fake-moving-starlink",
            "channels": list(channels), "drift_hz_s": args.fake_drift_hz_s}
        report = capture_compact_waterfall(blocks, args.output,
            sample_rate_hz=args.sample_rate_hz, center_frequency_hz=channel.if_center_hz,
            snapshots=args.snapshots, fft_size=args.fft_size, output_bins=args.output_bins,
            identity=identity, lnb_lo_hz=channel.lnb_lo_hz)
    else:
        config = RadioConfig(channel.if_center_hz, args.sample_rate_hz,
                             args.bandwidth_hz, args.gain_db, channels[0])
        if len(channels) == 2:
            source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                       transport="iio", serial=args.serial)
            blocks = threaded_source_blocks(source, paired=True, queue_blocks=args.queue_blocks)
        else:
            source = PlutoSource(config, uri=args.uri, block_size=args.block_size,
                                 transport=args.transport, serial=args.serial)
            blocks = threaded_source_blocks(source, paired=False, queue_blocks=args.queue_blocks)
        try:
            report = capture_compact_waterfall(blocks, args.output,
                sample_rate_hz=args.sample_rate_hz, center_frequency_hz=channel.if_center_hz,
                snapshots=args.snapshots, fft_size=args.fft_size, output_bins=args.output_bins,
                identity=dict(source.identity), lnb_lo_hz=channel.lnb_lo_hz)
        finally:
            source.close()
    print(json.dumps(report, sort_keys=True)); return 0


def command_starlink_waterfall_analyze(args: argparse.Namespace) -> int:
    report = analyze_compact_waterfall(args.waterfall,
        integration_s=args.integration_s, max_drift_hz_s=args.max_drift_hz_s,
        permutations=args.permutations, seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.plot: plot_compact_waterfall(args.waterfall, report, args.plot)
    print(json.dumps({"output": str(args.output), "tracks": report["tracks"],
                      "plot": None if args.plot is None else str(args.plot),
                      "receiver_agreement": report["receiver_agreement"]}, sort_keys=True))
    return 0


def command_starlink_dashboard(args: argparse.Namespace) -> int:
    serve_dashboard(args.observation_dir, host=args.host, port=args.port,
                    passes_path=args.passes,
                    beacon_root=args.beacon_root,
                    analysis_store_pointer=args.analysis_store_pointer,
                    analysis_store_cache=args.analysis_store_cache,
                    samples_per_snapshot=args.samples_per_snapshot,
                    sample_rate_hz=args.sample_rate_hz,
                    snapshots_per_chunk=args.snapshots_per_chunk)
    return 0


def command_starlink_gain_experiment(args: argparse.Namespace) -> int:
    report = run_gain_experiment(args.output_dir,
        center_frequency_hz=args.center_frequency_hz,
        sample_rate_hz=args.sample_rate_hz, bandwidth_hz=args.bandwidth_hz,
        manual_gains_db=args.manual_gains_db, snapshots=args.snapshots,
        block_size=args.block_size, fft_size=args.fft_size, output_bins=args.output_bins,
        lnb_lo_hz=args.lnb_lo_hz, uri=args.uri, settle_seconds=args.settle_seconds,
        discard_buffers=args.discard_buffers, adc_full_scale=args.adc_full_scale,
        fake=args.fake, seed=args.seed)
    print(json.dumps({"report": str(args.output_dir / "report.json"),
                      "profiles": len(report["profiles"])}, sort_keys=True))
    return 0


def command_starlink_measurement_capture(args: argparse.Namespace) -> int:
    gain_db = 40.0 if args.gain_mode == "manual" and args.gain_db is None else args.gain_db
    if args.gain_mode != "manual" and args.gain_db is not None:
        raise ValueError("AGC measurement capture cannot use --gain-db")
    if args.interleaved_dither_hz is not None and args.tuning_dither_hz is not None:
        raise ValueError("constant and interleaved tuning dither are mutually exclusive")
    if args.interleaved_dither_hz is not None and args.iq_evidence_output is not None:
        raise ValueError("interleaved dither IQ evidence needs per-block center provenance")
    gps_identity = {}
    if args.gps_device is not None:
        try:
            gps_identity["gps_fix"] = read_nmea_snapshot(
                args.gps_device, timeout_s=args.gps_timeout_s)
        except (OSError, ValueError, TimeoutError) as exc:
            if args.require_gps_fix:
                raise RuntimeError(f"required GPS fix unavailable: {exc}") from exc
            gps_identity["gps_error"] = str(exc)
    if args.fake:
        rng = np.random.default_rng(args.seed)
        start = 1_700_000_000_000_000_000
        def blocks():
            extra = (args.snapshots*args.dither_discard_buffers
                     if args.interleaved_dither_hz is not None else 0)
            for index in range(args.snapshots+args.discard_buffers+extra):
                values = []
                for receiver in range(2):
                    noise = rng.normal(0, .1, args.block_size) + 1j*rng.normal(0, .1, args.block_size)
                    if args.snapshots//3 <= index < 2*args.snapshots//3:
                        offset = .08 + receiver*.01 + index*.0001
                        noise += np.exp(2j*np.pi*offset*np.arange(args.block_size))
                    values.append(np.asarray(noise, np.complex64))
                yield start + index*100_000_000, values
        source = None; incoming = blocks(); identity = {"kind": "fake-measurement"}
        hardware_gain = (gain_db or 35, gain_db or 36)
        gain_reader = lambda: hardware_gain
    else:
        config = RadioConfig(args.center_frequency_hz, args.sample_rate_hz,
                             args.bandwidth_hz, gain_db, 0, args.gain_mode)
        source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size)
        iterator = iter(source.blocks())
        incoming = ((block.utc_ns, [block.rx0, block.rx1], block.read_duration_ns)
                    for block in iterator)
        identity = dict(source.identity); gain_reader = source.gain_snapshot
    incoming = iter(incoming)
    for _ in range(args.discard_buffers):
        try:
            next(incoming)
        except StopIteration as exc:
            raise RuntimeError("radio ended while discarding settling buffers") from exc
    if args.interleaved_dither_hz is not None:
        if args.interleaved_dither_hz == 0 or args.dither_segment_s <= 0:
            raise ValueError("interleaved dither and segment duration must be positive")
        segment_snapshots = max(1, round(
            args.dither_segment_s*args.sample_rate_hz/args.block_size))
        base_incoming = incoming
        def interleaved_blocks():
            current_center = float(args.center_frequency_hz)
            for output_index in range(args.snapshots):
                desired = float(args.center_frequency_hz + args.interleaved_dither_hz*(
                    (output_index//segment_snapshots) % 2))
                if desired != current_center:
                    if source is not None:
                        source.retune(desired)
                    for _ in range(args.dither_discard_buffers):
                        next(base_incoming)
                    current_center = desired
                block = next(base_incoming)
                if len(block) == 2:
                    yield block[0], block[1], None, desired
                else:
                    yield block[0], block[1], block[2], desired
        incoming = interleaved_blocks()
    identity.update(gps_identity)
    identity["discarded_settling_buffers"] = int(args.discard_buffers)
    if args.host_temperature_c is not None:
        identity["host_temperature_c"] = float(args.host_temperature_c)
    if args.radio_temperature_c is not None:
        identity["radio_temperature_c"] = float(args.radio_temperature_c)
    if args.experiment_tag is not None:
        identity["experiment_tag"] = args.experiment_tag
    if args.observation_mode is not None:
        identity["observation_mode"] = args.observation_mode
    if args.tuning_dither_hz is not None:
        identity["tuning_dither_hz"] = float(args.tuning_dither_hz)
    if args.interleaved_dither_hz is not None:
        identity.update({"interleaved_dither_hz": float(args.interleaved_dither_hz),
                         "dither_segment_s": float(args.dither_segment_s),
                         "dither_discard_buffers": int(args.dither_discard_buffers)})
    selector = (None if args.iq_evidence_output is None else IQEvidenceSelector(
        maximum_blocks=args.iq_evidence_blocks, warmup_blocks=args.iq_trigger_warmup_blocks,
        threshold_db=args.iq_trigger_threshold_db,
        minimum_separation_blocks=args.iq_trigger_separation_blocks,
        stratum_blocks=args.iq_trigger_stratum_blocks))
    try:
        report = capture_measurement_waterfall(incoming, args.output,
            sample_rate_hz=args.sample_rate_hz, center_frequency_hz=args.center_frequency_hz,
            bandwidth_hz=args.bandwidth_hz, snapshots=args.snapshots,
            fft_size=args.fft_size, output_bins=args.output_bins,
            samples_per_snapshot=args.block_size, lnb_lo_hz=args.lnb_lo_hz,
            gain_mode=args.gain_mode, configured_gain_db=gain_db,
            gain_reader=gain_reader, adc_full_scale=args.adc_full_scale,
            psd_quantization_db=args.psd_quantization_db,
            identity=identity, snapshot_observer=(None if selector is None else selector.observe))
    finally:
        if source is not None: source.close()
    if selector is not None:
        report["iq_evidence"] = selector.write(args.iq_evidence_output,
            sample_rate_hz=args.sample_rate_hz,
            center_frequency_hz=args.center_frequency_hz, lnb_lo_hz=args.lnb_lo_hz,
            identity=identity)
    print(json.dumps(report, sort_keys=True)); return 0


def command_starlink_waveform_iq(args: argparse.Namespace) -> int:
    report = write_starlink_waveform_iq(args.iq_evidence, args.output,
        beacon_rate_hz=args.beacon_rate_hz, tone_spacing_hz=args.tone_spacing_hz,
        period_search_fraction=args.period_search_fraction,
        tone_search_hz=args.tone_search_hz)
    print(json.dumps({"output": str(args.output), "blocks": report["blocks"]}, sort_keys=True))
    return 0


def command_starlink_iq_evidence_gate(args: argparse.Namespace) -> int:
    report = gate_iq_evidence(args.iq_evidence, args.wide_report, args.output,
                              margin_s=args.margin_s,
                              frequency_margin_hz=args.frequency_margin_hz)
    print(json.dumps(report, sort_keys=True)); return 0


def command_starlink_confound_analysis(args: argparse.Namespace) -> int:
    report = write_confound_population(args.observation_root, args.output,
                                       settling_window_s=args.settling_window_s)
    print(json.dumps({"output": str(args.output), "capture_count": report["capture_count"],
        "qualified_event_count": report["qualified_event_count"]}, sort_keys=True)); return 0


def command_starlink_measurement_analyze(args: argparse.Namespace) -> int:
    report = write_measurement_analysis(args.measurement, args.output,
        plot=args.plot, pass_catalog_path=args.passes,
        threshold_db=args.threshold_db, minimum_time_bins=args.minimum_time_bins,
        minimum_frequency_bins=args.minimum_frequency_bins,
        event_frequency_bins=args.event_frequency_bins,
        carrier_hz=args.carrier_hz,
        broadband_fraction=args.broadband_fraction,
        tle_dwell_window_s=args.tle_dwell_window_s,
        tle_dwell_step_s=args.tle_dwell_step_s,
        tle_minimum_window_s=args.tle_minimum_window_s,
        tle_comb_spacing_hz=args.tle_comb_spacing_hz,
        tle_minimum_comb_spacing_improvement=args.tle_minimum_comb_spacing_improvement,
        tle_broadband_frequency_fraction=args.tle_broadband_frequency_fraction,
        tle_maximum_broadband_row_fraction=args.tle_maximum_broadband_row_fraction,
        blind_comb=not args.no_blind_comb,
        blind_comb_search_half_width_hz=args.blind_comb_search_half_width_hz)
    qualified_tle = ([] if report["tle_guided_search"] is None else
                     [item for item in report["tle_guided_search"]["candidates"]
                      if item.get("qualified")])
    models = {model: sum(item.get("signal_model") == model for item in qualified_tle)
              for model in sorted({item.get("signal_model") for item in qualified_tle})}
    print(json.dumps({"output": str(args.output), "plot": None if args.plot is None else str(args.plot),
        "events": [len(item) for item in report["events"]],
        "joint_events": len(report["joint_events"]),
        "qualified_events": sum(bool((item.get("qualification") or {}).get("qualified"))
                                for item in report["joint_events"]),
        "doppler_observations": sum(bool((item.get("doppler_observation") or {}).get(
            "qualified")) for item in report["joint_events"]),
        "tle_guided_candidates": (None if report["tle_guided_search"] is None else
                                  report["tle_guided_search"]["qualified_count"]),
        "tle_guided_models": models,
        "blind_comb_candidates": (None if report["blind_comb_search"] is None else
                                  report["blind_comb_search"]["qualified_count"]),
        "blind_carrier_candidates": (None if report["blind_carrier_search"] is None else
                                     report["blind_carrier_search"]["qualified_count"]),
        "tle_hopping_candidates": (None if report["tle_carrier_hopping_search"] is None else
                                   report["tle_carrier_hopping_search"]["qualified_count"])},
        sort_keys=True)); return 0


def command_doppler_observations(args: argparse.Namespace) -> int:
    report = write_doppler_observations(args.measurement, args.output,
        threshold_db=args.threshold_db,
        event_frequency_bins=args.event_frequency_bins,
        stable_guard_s=args.stable_guard_s,
        minimum_track_duration_s=args.minimum_track_duration_s,
        assume_all_shifts_doppler=args.assume_all_shifts_doppler)
    print(json.dumps({"output": str(args.output), **report["summary"]}, sort_keys=True))
    return 0


def command_doppler_tracker_ensemble(args: argparse.Namespace) -> int:
    windows = None
    if args.window:
        windows = []
        for value in args.window:
            try:
                start, stop = (float(item) for item in value.split(":", 1))
            except (ValueError, TypeError) as error:
                raise ValueError("--window must use START:STOP elapsed seconds") from error
            if stop <= start:
                raise ValueError("--window stop must be after start")
            windows.append((start, stop))
    report = write_tracker_ensemble(args.measurement, args.output, windows=windows,
        passes=args.passes, plot=args.plot,
        integration_s=args.integration_s,
        dedoppler_window_s=args.dedoppler_window_s,
        dedoppler_step_s=args.dedoppler_step_s,
        minimum_drift_hz_s=args.minimum_drift_hz_s,
        maximum_drift_hz_s=args.maximum_drift_hz_s,
        drift_step_hz_s=args.drift_step_hz_s)
    print(json.dumps({"output": str(args.output),
        "candidates": len(report["candidates"]),
        "joint_tracks": len(report["joint_tracks"]),
        "tle_identifications": len(report["identifications"]),
        "qualified_by_tracker": report["metrics"]["qualified_count_by_tracker"]},
        sort_keys=True))
    return 0


def command_doppler_iq_track(args: argparse.Namespace) -> int:
    report = write_coherent_iq_analysis(args.iq, args.output,
        estimates_per_block=args.estimates_per_block,
        repetition_minimum_lag=args.repetition_minimum_lag,
        repetition_maximum_lag=args.repetition_maximum_lag)
    print(json.dumps({"output": str(args.output), "blocks": len(report["blocks"]),
        "receiver_count": 0 if not report["blocks"] else
            len(report["blocks"][0]["receivers"])}, sort_keys=True))
    return 0


def command_doppler_ambiguity(args: argparse.Namespace) -> int:
    report = write_cross_ambiguity_analysis(args.iq, args.template, args.output,
        block=args.block, receiver=args.receiver,
        maximum_delay_samples=args.maximum_delay_samples,
        minimum_doppler_hz=args.minimum_doppler_hz,
        maximum_doppler_hz=args.maximum_doppler_hz,
        doppler_step_hz=args.doppler_step_hz)
    print(json.dumps({"output": str(args.output),
        "best_delay_samples": report["best_delay_samples"],
        "best_doppler_hz": report["best_doppler_hz"],
        "best_score": report["best_score"]}, sort_keys=True))
    return 0


def command_doppler_tracker_summary(args: argparse.Namespace) -> int:
    report = write_tracker_summary(args.inputs, args.output)
    print(json.dumps({"output": str(args.output),
        "report_count": report["report_count"],
        "tracker_count": len(report["trackers"])}, sort_keys=True))
    return 0


def command_doppler_tle_match(args: argparse.Namespace) -> int:
    report = rematch_tracker_report(args.trackers, args.passes, args.output)
    identifications = report["identifications"]
    print(json.dumps({"output": str(args.output),
        "compatible": sum(bool(item.get("compatible")) for item in identifications),
        "specific_identifications": sum(bool(item.get("qualified"))
                                         for item in identifications)}, sort_keys=True))
    return 0


def command_starlink_dither_compare(args: argparse.Namespace) -> int:
    report = compare_tuning_dither(args.first, args.second,
        smoothing_bins=args.smoothing_bins, edge_fraction=args.edge_fraction,
        exclude_dc_hz=args.exclude_dc_hz)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    print(json.dumps({"output": str(args.output), "classification": report["classification"],
                      "tuning_dither_hz": report["tuning_dither_hz"]}, sort_keys=True))
    return 0


def command_starlink_rf_baseline(args: argparse.Namespace) -> int:
    report = build_rf_baseline(args.measurements, args.output)
    print(json.dumps(report, sort_keys=True)); return 0


def command_starlink_rf_novelty(args: argparse.Namespace) -> int:
    report = write_rf_novelty(args.measurement, args.baseline, args.output,
                              plot=args.plot, threshold_db=args.threshold_db,
                              trend_smoothing_bins=args.trend_smoothing_bins,
                              integration_s=args.integration_s)
    print(json.dumps({"output": str(args.output),
        "plot": None if args.plot is None else str(args.plot),
        "positive_novel_fraction": [item["positive_novel_fraction"]
                                    for item in report["receivers"]]}, sort_keys=True))
    return 0


def command_starlink_wide_feature(args: argparse.Namespace) -> int:
    report = write_wide_feature_analysis(args.measurement, args.baseline, args.output,
        pass_catalog_path=args.passes, plot=args.plot, threshold_db=args.threshold_db,
        integration_s=args.integration_s,
        minimum_boundary_margin_s=args.minimum_boundary_margin_s)
    print(json.dumps({"output": str(args.output),
        "plot": None if args.plot is None else str(args.plot),
        "candidate_count": report["candidate_count"]}, sort_keys=True))
    return 0


def command_starlink_wide_population(args: argparse.Namespace) -> int:
    report = write_wide_population(args.inputs, args.output,
                                   narrow_width_hz=args.narrow_width_hz)
    print(json.dumps({"output": str(args.output),
        "report_count": report["report_count"],
        "qualified_feature_count": report["qualified_feature_count"],
        "families": {item["family"]: item["count"] for item in report["families"]}},
        sort_keys=True))
    return 0


def _wide_detection(report: dict, args: argparse.Namespace) -> tuple[bool, list[str]]:
    reasons = []
    for receiver, (track, significance) in enumerate(zip(
            report["tracks"], report["significance"], strict=True)):
        probability = significance["false_alarm_probability"]
        if probability is None or probability > args.max_false_alarm_probability:
            reasons.append(f"rx{receiver}_false_alarm")
        if track["frequency_span_hz"] < args.min_span_hz:
            reasons.append(f"rx{receiver}_span")
        if track["median_depression_db"] < args.min_depression_db:
            reasons.append(f"rx{receiver}_depth")
    agreement = report["receiver_agreement"]
    if agreement is not None:
        if agreement["correlation"] < args.min_receiver_correlation:
            reasons.append("receiver_correlation")
        if agreement["median_absolute_difference_hz"] > args.max_receiver_difference_hz:
            reasons.append("receiver_difference")
    return not reasons, reasons


def command_starlink_waterfall_watch(args: argparse.Namespace) -> int:
    channel = get_channel(args.channel_number)
    if channel.lnb_band == "high" and not args.high_band_selected:
        raise ValueError("channels 5-8 require the LNB 22 kHz high-band tone; pass --high-band-selected only after it is active")
    channel = replace(channel, if_center_hz=channel.if_center_hz + args.if_offset_hz)
    channels = _selected_channels(args)
    root = args.output_dir; chunks = root / "chunks"; plots = root / "plots"
    detections = root / "detections"
    for path in (root, chunks, plots, detections): path.mkdir(parents=True, exist_ok=True)
    index_path, summary_path = root / "index.jsonl", root / "summary.json"
    existing = []
    if index_path.exists():
        existing = [json.loads(row) for row in index_path.read_text().splitlines() if row.strip()]
    chunk_index = len(existing)
    now_utc = datetime.now(timezone.utc)
    started_utc = now_utc
    if existing and summary_path.exists():
        previous_summary = json.loads(summary_path.read_text())
        previous_hours = float(previous_summary.get("requested_hours", args.hours))
        if previous_hours != args.hours:
            raise ValueError(
                f"cannot resume {previous_hours:g}-hour watch with --hours {args.hours:g}")
        started_utc = datetime.fromisoformat(previous_summary["started_utc"])
        if started_utc.tzinfo is None:
            raise ValueError("existing watcher start time must include a UTC offset")
        started_utc = started_utc.astimezone(timezone.utc)
    deadline_utc = started_utc + timedelta(hours=args.hours)

    if args.fake:
        fake_index = 0
        def fake_watch_blocks():
            nonlocal fake_index
            origin = 1_700_000_000_000_000_000
            while True:
                offset = args.fake_start_offset_hz + (fake_index * args.block_size /
                    args.sample_rate_hz) * args.fake_drift_hz_s
                values = [synthetic_starlink_block(args.sample_rate_hz, args.block_size,
                    seed=args.seed + fake_index*17 + receiver, gutter_offset_hz=offset)
                    for receiver in range(len(channels))]
                yield origin + round(fake_index*args.block_size*1e9/args.sample_rate_hz), values
                fake_index += 1
        blocks = fake_watch_blocks(); identity = {"kind": "fake-starlink-watch",
            "channels": list(channels), "drift_hz_s": args.fake_drift_hz_s}
        source = None
    else:
        config = RadioConfig(channel.if_center_hz, args.sample_rate_hz,
                             args.bandwidth_hz, args.gain_db, channels[0])
        if len(channels) == 2:
            source = PairedPlutoSource(config, uri=args.uri, block_size=args.block_size,
                                       transport="iio", serial=args.serial)
            blocks = threaded_source_blocks(source, paired=True, queue_blocks=args.queue_blocks)
        else:
            source = PlutoSource(config, uri=args.uri, block_size=args.block_size,
                                 transport=args.transport, serial=args.serial)
            blocks = threaded_source_blocks(source, paired=False, queue_blocks=args.queue_blocks)
        identity = dict(source.identity)

    records = list(existing); state = "running"
    try:
        while datetime.now(timezone.utc) < deadline_utc and (args.max_chunks is None or
                                                chunk_index < len(existing)+args.max_chunks):
            stem = f"chunk-{chunk_index:05d}"
            waterfall = chunks / f"{stem}.npz"; analysis_path = chunks / f"{stem}.json"
            plot_path = plots / f"{stem}.png"
            capture = capture_compact_waterfall(blocks, waterfall,
                sample_rate_hz=args.sample_rate_hz, center_frequency_hz=channel.if_center_hz,
                snapshots=args.chunk_snapshots, fft_size=args.fft_size,
                output_bins=args.output_bins, identity=identity,
                lnb_lo_hz=channel.lnb_lo_hz)
            analysis = analyze_compact_waterfall(waterfall,
                integration_s=args.integration_s, max_drift_hz_s=args.max_drift_hz_s,
                permutations=args.permutations, seed=args.seed+chunk_index)
            detected, reasons = _wide_detection(analysis, args)
            render = args.plot_mode == "all" or (args.plot_mode == "detections" and detected)
            if render:
                plot_compact_waterfall(waterfall, analysis, plot_path,
                                       lnb_lo_hz=channel.lnb_lo_hz)
            analysis["detected"] = detected; analysis["rejection_reasons"] = reasons
            analysis_path.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n")
            record = {"chunk": chunk_index, "waterfall": str(waterfall),
                "analysis": str(analysis_path), "plot": str(plot_path) if render else None,
                "detected": detected, "rejection_reasons": reasons,
                "first_utc_ns": capture["first_utc_ns"], "last_utc_ns": capture["last_utc_ns"],
                "wall_duration_s": capture["wall_duration_s"], "tracks": analysis["tracks"],
                "significance": analysis["significance"],
                "receiver_agreement": analysis["receiver_agreement"]}
            if detected:
                (detections / f"{stem}.json").write_text(
                    json.dumps(record, indent=2, sort_keys=True) + "\n")
            with index_path.open("a") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n"); stream.flush()
            records.append(record); chunk_index += 1
            summary = {"schema": "leo-tracker.starlink-waterfall-watch/v1",
                "state": state, "started_utc": started_utc.isoformat(),
                "updated_utc": datetime.now(timezone.utc).isoformat(),
                "requested_hours": args.hours, "channel": asdict(channel),
                "identity": identity, "completed_chunks": len(records),
                "detection_count": sum(item["detected"] for item in records),
                "last_chunk": record}
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
            print(json.dumps({"chunk": record["chunk"], "detected": detected,
                "plot": record["plot"], "detection_count": summary["detection_count"]},
                sort_keys=True), flush=True)
        state = "complete"
    except KeyboardInterrupt:
        state = "interrupted"
        raise
    finally:
        if source is not None: source.close()
        final = {"schema": "leo-tracker.starlink-waterfall-watch/v1", "state": state,
            "started_utc": started_utc.isoformat(),
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "requested_hours": args.hours, "channel": asdict(channel), "identity": identity,
            "completed_chunks": len(records),
            "detection_count": sum(item["detected"] for item in records),
            "last_chunk": records[-1] if records else None}
        summary_path.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
    return 0


def command_validated_scan(args: argparse.Namespace) -> int:
    centers = np.arange(args.start_hz, args.stop_hz+args.step_hz/2, args.step_hz).tolist()
    if args.repeats < 1 or args.confirmations < 1:
        raise ValueError("--repeats and --confirmations must be at least 1")
    centers = [center for center in centers for _ in range(args.confirmations)] * args.repeats
    points = validated_scan_pyadi(uri=args.uri, nominal_centers_hz=centers,
        validation_offset_hz=args.validation_offset_hz, sample_rate_hz=args.sample_rate_hz,
        bandwidth_hz=args.bandwidth_hz, gain_db=args.gain_db,
        samples_per_tuning=args.samples_per_tuning, settle_seconds=args.settle_seconds,
        channel=args.channel, fft_size=args.fft_size, min_prominence_db=args.min_prominence_db,
        frequency_tolerance_hz=args.frequency_tolerance_hz, max_features=args.max_features)
    metadata = {key: getattr(args, key) for key in ("uri","serial","validation_offset_hz","sample_rate_hz",
        "bandwidth_hz","gain_db","channel","fft_size","min_prominence_db","frequency_tolerance_hz","settle_seconds",
        "repeats","confirmations")}
    write_validated_scan(points, args.output_dir, metadata)
    count = sum(len(point.validated_features) for point in points)
    confirmed = confirmed_features(points, confirmations=args.confirmations,
                                   frequency_tolerance_hz=args.frequency_tolerance_hz)
    strongest = confirmed[:5]
    promotion_grade = (args.settle_seconds >= PROMOTION_MIN_SETTLE_SECONDS and
                       args.confirmations >= PROMOTION_MIN_CONFIRMATIONS)
    warnings = []
    if args.settle_seconds < PROMOTION_MIN_SETTLE_SECONDS:
        warnings.append(f"settle_seconds={args.settle_seconds:g} is below promotion minimum {PROMOTION_MIN_SETTLE_SECONDS:g}")
    if args.confirmations < PROMOTION_MIN_CONFIRMATIONS:
        warnings.append(f"confirmations={args.confirmations} is below promotion minimum {PROMOTION_MIN_CONFIRMATIONS}")
    warning = None if promotion_grade else "diagnostic only: " + "; ".join(warnings)
    print(json.dumps({"centers": len(points), "validated_features": count,
        "confirmed_features": len(confirmed),
        "promotion_grade": promotion_grade, "warning": warning,
        "strongest_validated_features": strongest,
        "json": str(args.output_dir/"validated_scan.json"), "plot": str(args.output_dir/"validated_scan.png")}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="leo-radio", description="Independent LEO RF capture and analysis")
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="discover attached Pluto+ receivers")
    preflight.add_argument("--fake-serial"); preflight.add_argument("--sysfs", default="/sys/bus/usb/devices")
    preflight.set_defaults(handler=command_preflight)
    capture = commands.add_parser("capture", help="record an atomic IQ artifact")
    capture.add_argument("output"); capture.add_argument("--duration-s", type=float, required=True)
    capture.add_argument("--center-frequency-hz", type=float, required=True)
    capture.add_argument("--sample-rate-hz", type=float, required=True)
    capture.add_argument("--bandwidth-hz", type=float, required=True)
    capture.add_argument("--gain-db", type=float); capture.add_argument("--block-size", type=int, default=65536)
    capture.add_argument("--channel", type=int, choices=(0, 1), default=None,
                         help="deprecated single-channel alias for --channels")
    capture.add_argument("--channels", type=parse_channels, metavar="0|1|0,1",
                         help="receiver selection; dual mode uses one synchronized hardware read")
    capture.add_argument("--uri", default="pluto://ip:192.168.2.1"); capture.add_argument("--serial")
    capture.add_argument("--transport", choices=("iio", "direct_usb"), default="iio")
    capture.add_argument("--fake", action="store_true"); capture.add_argument("--fake-start-hz", type=float, default=-2000)
    capture.add_argument("--fake-stop-hz", type=float, default=2000)
    capture.add_argument("--fake-noise-std", type=float, default=.02); capture.add_argument("--seed", type=int, default=0)
    capture.set_defaults(handler=command_capture)
    paired = commands.add_parser("paired-capture", help="synchronously capture Pluto RX0 and RX1")
    paired.add_argument("output"); paired.add_argument("--duration-s", type=float, required=True)
    paired.add_argument("--center-frequency-hz", type=float, required=True)
    paired.add_argument("--sample-rate-hz", type=float, required=True)
    paired.add_argument("--bandwidth-hz", type=float, required=True)
    paired.add_argument("--gain-db", type=float); paired.add_argument("--block-size", type=int, default=65536)
    paired.add_argument("--uri", default="pluto://usb:"); paired.add_argument("--serial")
    paired.add_argument("--transport", choices=("iio",), default="iio")
    paired.add_argument("--fake", action="store_true"); paired.add_argument("--fake-start-hz", type=float, default=-2000)
    paired.add_argument("--fake-stop-hz", type=float, default=2000); paired.add_argument("--fake-noise-std", type=float, default=.02)
    paired.add_argument("--fake-start-utc-ns", type=int, default=1_700_000_000_000_000_000)
    paired.add_argument("--seed", type=int, default=0); paired.set_defaults(handler=command_paired_capture)
    beacon_capture = commands.add_parser("starlink-beacon-capture",
        help="continuously record crash-safe dual-RX IQ for Starlink beacon acquisition")
    beacon_capture.add_argument("output", type=Path)
    beacon_capture.add_argument("--duration-s", type=float, required=True)
    beacon_capture.add_argument("--channel-number", type=int, choices=range(1, 9), default=3)
    beacon_capture.add_argument("--tuning-offset-hz", type=float, default=0.0,
                                help="shift the published centre, to sit between "
                                     "two LNB local oscillators")
    beacon_capture.add_argument("--region", choices=("lower-edge", "upper-edge", "center"),
        default="lower-edge", help="published pilot band to capture; center is the pilot-free gutter control")
    beacon_capture.add_argument("--center-frequency-hz", type=float,
        help="explicit L-band IF; defaults to the published channel center minus the LNB LO")
    beacon_capture.add_argument("--lnb-lo-hz", type=float, default=9_750_000_000)
    beacon_capture.add_argument("--sample-rate-hz", type=float, default=2_500_000)
    beacon_capture.add_argument("--bandwidth-hz", type=float, default=2_500_000)
    beacon_capture.add_argument("--observation-mode",
        choices=("narrow", "oversample", "wide"), default="narrow")
    beacon_capture.add_argument("--gain-mode", choices=("manual", "slow_attack", "fast_attack"), default="manual")
    beacon_capture.add_argument("--gain-db", type=float, default=50)
    beacon_capture.add_argument("--agc-settle-s", type=float, default=2.0)
    beacon_capture.add_argument("--gain-experiment-id")
    beacon_capture.add_argument("--gain-random-draw-u32", type=int)
    beacon_capture.add_argument("--gain-assignment-probability", type=float)
    beacon_capture.add_argument("--block-size", type=int, default=262_144)
    beacon_capture.add_argument("--chunk-s", type=float, default=5)
    beacon_capture.add_argument("--queue-blocks", type=int, default=16,
        help="bounded lossless reader queue overlapping Pluto refills with NVMe writes")
    beacon_capture.add_argument("--sample-format", choices=("native-ci16", "complex64"),
        default="native-ci16", help="native CI16 avoids pyadi's costly complex64 round trip")
    beacon_capture.add_argument("--uri", default="pluto://ip:192.168.2.1")
    beacon_capture.add_argument("--serial")
    beacon_capture.add_argument("--radio-id",
        help="stable operator label for this physical Pluto")
    beacon_capture.add_argument("--receiver-labels", nargs=2, metavar=("RX0", "RX1"),
        help="physical LNB/feed labels connected to RX0 and RX1")
    beacon_capture.add_argument("--survey-before-dwell", action="store_true",
        help="survey the eight low-band edge tunings once before recording, and "
             "file the verdict in the manifest; observational, never gating")
    beacon_capture.add_argument("--keep-survey-iq", action="store_true",
        help="also store the survey's raw ci16 beside the capture, so a later "
             "analysis can re-decide from the signal rather than the verdict; "
             "about 12.8 MB per capture at an 80 ms probe")
    beacon_capture.add_argument("--host-temperature-c", type=float)
    beacon_capture.add_argument("--radio-temperature-c", type=float)
    beacon_capture.add_argument("--fake", action="store_true")
    beacon_capture.add_argument("--fake-noise-std", type=float, default=50)
    beacon_capture.add_argument("--fake-start-utc-ns", type=int, default=1_700_000_000_000_000_000)
    beacon_capture.add_argument("--seed", type=int, default=0)
    beacon_capture.set_defaults(handler=command_starlink_beacon_capture)
    beacon_hop = commands.add_parser("starlink-beacon-hop-capture",
        help="retune one open dual-RX stream across settled Starlink edge dwells")
    beacon_hop.add_argument("output", type=Path)
    beacon_hop.add_argument("--channels", type=int, nargs="+", choices=range(1, 9),
        default=list(range(1, 9)))
    beacon_hop.add_argument("--region", choices=("lower-edge", "upper-edge"),
        default="lower-edge")
    beacon_hop.add_argument("--dwell-s", type=float, default=1.0)
    beacon_hop.add_argument("--lnb-lo-hz", type=float, default=9_750_000_000)
    beacon_hop.add_argument("--sample-rate-hz", type=float, default=2_500_000)
    beacon_hop.add_argument("--bandwidth-hz", type=float, default=2_500_000)
    beacon_hop.add_argument("--gain-mode",
        choices=("manual", "slow_attack", "fast_attack"), default="manual")
    beacon_hop.add_argument("--gain-db", type=float, default=50)
    beacon_hop.add_argument("--agc-settle-s", type=float, default=2.0)
    beacon_hop.add_argument("--settle-buffers", type=int, default=2,
        help="discard this many complete IIO refills after every LO retune")
    beacon_hop.add_argument("--sample-format", choices=("native-ci16", "complex64"),
        default="native-ci16", help="native CI16 avoids pyadi's costly complex64 round trip")
    beacon_hop.add_argument("--block-size", type=int, default=262_144)
    beacon_hop.add_argument("--chunk-s", type=float, default=1.0)
    beacon_hop.add_argument("--uri", default="pluto://ip:192.168.2.1")
    beacon_hop.add_argument("--serial")
    beacon_hop.add_argument("--radio-id",
        help="stable operator label for this physical Pluto")
    beacon_hop.add_argument("--receiver-labels", nargs=2, metavar=("RX0", "RX1"),
        help="physical LNB/feed labels connected to RX0 and RX1")
    beacon_hop.add_argument("--fake", action="store_true")
    beacon_hop.add_argument("--fake-start-utc-ns", type=int,
        default=1_700_000_000_000_000_000)
    beacon_hop.add_argument("--seed", type=int, default=0)
    beacon_hop.set_defaults(handler=command_starlink_beacon_hop_capture)
    beacon_analyze = commands.add_parser("starlink-beacon-analyze",
        help="verify and score a beacon IQ artifact for the published 750 Hz frame cadence")
    beacon_analyze.add_argument("capture", type=Path)
    beacon_analyze.add_argument("output", type=Path)
    beacon_analyze.add_argument("--window-s", type=float, default=1)
    beacon_analyze.add_argument("--maximum-analysis-rate-hz", type=float, default=250_000)
    beacon_analyze.add_argument("--exact-interval-s", type=float, default=60)
    beacon_analyze.add_argument("--exact-window-s", type=float, default=.1)
    beacon_analyze.add_argument("--acquisition-span-hz", type=float, default=0,
        help="search this much independent-LNB frequency error on either side")
    beacon_analyze.add_argument("--acquisition-step-hz", type=float, default=500_000)
    beacon_analyze.add_argument("--exact-subband-rate-hz", type=float, default=2_500_000)
    beacon_analyze.add_argument("--exact-acquisition-method",
        choices=("coherent_grid_v1", "pss_symbolwise_v2", "pilot_symbolwise_v3"),
        default="coherent_grid_v1")
    beacon_analyze.add_argument("--exact-start-s", type=float, default=0,
        help="begin exact-code replay at this capture offset")
    beacon_analyze.add_argument("--exact-stop-s", type=float,
        help="stop exact-code replay at this capture offset")
    beacon_analyze.add_argument("--receiver-center-offsets-hz", nargs=2, type=float,
        metavar=("RX0", "RX1"),
        help="acquisition search centre per receiver; overrides the calibration")
    beacon_analyze.add_argument("--calibration-root", type=Path,
        help="storage root holding reports/lnb-calibration.json")
    beacon_analyze.add_argument("--beacon-template", type=Path,
        help="qualified learned full-frame template used as an independent dual-RX gate")
    beacon_analyze.add_argument("--plot", type=Path,
        help="write an exact-PSS, pilot-control, and CFO evidence PNG")
    beacon_analyze.set_defaults(handler=command_starlink_beacon_analyze)
    beacon_retain = commands.add_parser("starlink-beacon-retain",
        help="pin qualified evidence and bound fully-derived raw IQ rings")
    beacon_retain.add_argument("root", type=Path)
    beacon_retain.add_argument("--keep-negative", type=int, default=6)
    beacon_retain.add_argument("--keep-confirmed", type=int, default=8,
        help="retain this many newest fully-derived confirmed raw IQ captures")
    beacon_retain.add_argument("--keep-wide", type=int, default=2,
        help="retain this many newest fully-derived wide raw IQ captures")
    beacon_retain.add_argument("--keep-oversample", type=int, default=4,
        help="retain this many newest fully-derived oversampled raw IQ captures")
    beacon_retain.add_argument("--keep-hop-sessions", type=int, default=6,
        help="retain this many newest fully-derived, non-qualified hop sessions")
    beacon_retain.add_argument("--dry-run", action="store_true")
    beacon_retain.set_defaults(handler=command_starlink_beacon_retain)
    reconcile = commands.add_parser("starlink-storage-reconcile",
        help="remove local raw IQ only after verified QNAP copy and Kalman success")
    reconcile.add_argument("local_root", type=Path)
    reconcile.add_argument("shared_root", type=Path)
    reconcile.add_argument("--archive-root", type=Path,
        help="accept matching replay-verified production-v2 evidence as durable")
    reconcile.add_argument("--apply", action="store_true",
        help="apply eligible removals; without this flag only print a plan summary")
    reconcile.add_argument("--limit", type=int,
        help="remove at most this many eligible recordings per reconciliation")
    reconcile.add_argument("--minimum-age-s", type=float, default=300)
    reconcile.add_argument("--pipeline-id",
        help="optionally require this exact successful analysis pipeline")
    reconcile.add_argument("--verify-sha256", action="store_true",
        help="read and hash every QNAP IQ chunk in addition to manifest/size checks")
    reconcile.add_argument("--output", type=Path,
        help="atomically publish the complete eligibility plan as JSON")
    reconcile.add_argument("--watch", action="store_true")
    reconcile.add_argument("--interval-s", type=float, default=60)
    reconcile.set_defaults(handler=command_starlink_storage_reconcile)
    report_converge = commands.add_parser("starlink-local-report-converge",
        help="atomically move legacy local reports under QNAP authority")
    report_converge.add_argument("local_root", type=Path)
    report_converge.add_argument("shared_root", type=Path)
    report_converge.add_argument("--archive-root", type=Path,
        help="retire legacy evidence reports only behind verified V2 receipts")
    report_converge.add_argument("--apply", action="store_true")
    report_converge.add_argument("--output", type=Path)
    report_converge.set_defaults(handler=command_starlink_local_report_converge)
    artifact_converge = commands.add_parser("starlink-local-artifact-converge",
        help="receipt-gate removal of obsolete local markers, scratch and checkpoints")
    artifact_converge.add_argument("local_root", type=Path)
    artifact_converge.add_argument("shared_root", type=Path)
    artifact_converge.add_argument("--archive-root", type=Path)
    artifact_converge.add_argument("--minimum-age-s", type=float, default=6 * 3600)
    artifact_converge.add_argument("--apply", action="store_true")
    artifact_converge.add_argument("--output", type=Path)
    artifact_converge.set_defaults(handler=command_starlink_local_artifact_converge)
    shared_transients = commands.add_parser("starlink-shared-transient-converge",
        help="audit and retire stale atomic files and superseded QNAP partials")
    shared_transients.add_argument("shared_root", type=Path)
    shared_transients.add_argument("archive_root", type=Path)
    shared_transients.add_argument("--minimum-age-s", type=float, default=6 * 3600)
    shared_transients.add_argument("--output", type=Path)
    shared_transients.add_argument("--apply", action="store_true")
    shared_transients.add_argument("--confirm", default="",
        help=f"required literal {SHARED_TRANSIENT_CONFIRMATION} with --apply")
    shared_transients.set_defaults(handler=command_starlink_shared_transient_converge)
    qnap = commands.add_parser("starlink-qnap-lifecycle",
        help="rank QNAP raw IQ for pressure-based retention; dry-run by default")
    qnap.add_argument("shared_root", type=Path)
    qnap.add_argument("archive_root", type=Path)
    qnap.add_argument("--minimum-age-hours", type=float, default=24)
    qnap.add_argument("--maximum-tier", type=int, choices=range(5), default=0,
        help="highest deletable verified-v2 tier: 0 negative through 4 identity")
    qnap.add_argument("--trigger-free-gb", type=float, default=500,
        help="apply only below this QNAP free-space level")
    qnap.add_argument("--target-free-gb", type=float, default=750,
        help="stop after recovering this much QNAP free space")
    qnap.add_argument("--limit", type=int)
    qnap.add_argument("--output", type=Path)
    qnap.add_argument("--apply", action="store_true")
    qnap.add_argument("--ignore-pressure", action="store_true",
        help="remove every age-eligible verified-v2 raw capture regardless of free space")
    qnap.add_argument("--confirm", default="",
        help="required literal DELETE-QNAP-RAW-IQ when --apply is used")
    qnap.set_defaults(handler=command_starlink_qnap_lifecycle)
    evidence_plan = commands.add_parser("starlink-evidence-plan",
        help="plan conservative signal and control clips from one immutable capture")
    evidence_plan.add_argument("capture", type=Path)
    evidence_plan.add_argument("reports", type=Path)
    evidence_plan.add_argument("output", type=Path)
    evidence_plan.add_argument("--policy", choices=("conservative-v1", "tiered-v2"),
                               default="conservative-v1")
    evidence_plan.add_argument("--guard-s", type=float)
    evidence_plan.add_argument("--control-duration-s", type=float)
    evidence_plan.add_argument("--control-count", type=int)
    evidence_plan.set_defaults(handler=command_starlink_evidence_plan)
    evidence_compare = commands.add_parser("starlink-evidence-compare",
        help="prove that a candidate plan retains every required detector event")
    evidence_compare.add_argument("reference", type=Path)
    evidence_compare.add_argument("candidate", type=Path)
    evidence_compare.add_argument("--output", type=Path)
    evidence_compare.set_defaults(handler=command_starlink_evidence_compare)
    evidence_shadow = commands.add_parser("starlink-evidence-v2-shadow",
        help="build replay-gated tiered plans and project savings without extracting IQ")
    evidence_shadow.add_argument("shared_root", type=Path)
    evidence_shadow.add_argument("archive_root", type=Path)
    evidence_shadow.add_argument("--output", type=Path)
    evidence_shadow.add_argument("--limit", type=int)
    evidence_shadow.set_defaults(handler=command_starlink_evidence_v2_shadow)
    evidence_extract = commands.add_parser("starlink-evidence-extract",
        help="losslessly extract a planned dual-RX evidence bundle")
    evidence_extract.add_argument("capture", type=Path)
    evidence_extract.add_argument("plan", type=Path)
    evidence_extract.add_argument("output", type=Path)
    evidence_extract.set_defaults(handler=command_starlink_evidence_extract)
    evidence_verify = commands.add_parser("starlink-evidence-verify",
        help="read back evidence hashes and optionally compare exact source samples")
    evidence_verify.add_argument("bundle", type=Path)
    evidence_verify.add_argument("--source", type=Path)
    evidence_verify.add_argument("--no-write", action="store_true")
    evidence_verify.set_defaults(handler=command_starlink_evidence_verify)
    evidence_audit = commands.add_parser("starlink-evidence-audit",
        help="inventory local captures and verify corresponding evidence bundles")
    evidence_audit.add_argument("source_root", type=Path)
    evidence_audit.add_argument("evidence_root", type=Path)
    evidence_audit.add_argument("--output", type=Path)
    evidence_audit.set_defaults(handler=command_starlink_evidence_audit)
    evidence_archive = commands.add_parser("starlink-evidence-archive",
        help="publish one verified clip bundle, derived artifacts, and receipt")
    evidence_archive.add_argument("capture", type=Path)
    evidence_archive.add_argument("reports", type=Path)
    evidence_archive.add_argument("qnap_root", type=Path)
    evidence_archive.add_argument("--guard-s", type=float, default=10)
    evidence_archive.add_argument("--control-duration-s", type=float, default=1)
    evidence_archive.add_argument("--control-count", type=int, default=3)
    evidence_archive.set_defaults(handler=command_starlink_evidence_archive)
    evidence_archive_v2 = commands.add_parser("starlink-evidence-archive-v2",
        help="publish production tiered evidence without duplicate report artifacts")
    evidence_archive_v2.add_argument("capture", type=Path)
    evidence_archive_v2.add_argument("reports", type=Path)
    evidence_archive_v2.add_argument("qnap_root", type=Path)
    evidence_archive_v2.set_defaults(handler=command_starlink_evidence_archive_v2)
    storage_regime = commands.add_parser("starlink-storage-regime-v2",
        help="transactionally replace old raw/v1 storage with verified tiered evidence")
    storage_regime.add_argument("shared_root", type=Path)
    storage_regime.add_argument("archive_root", type=Path)
    storage_regime.add_argument("--minimum-age-hours", type=float, default=6)
    storage_regime.add_argument("--scope", choices=("all", "auto", "raw", "archive"),
                                default="all")
    storage_regime.add_argument("--limit", type=int)
    storage_regime.add_argument("--planning-limit", type=int,
        help="stop inventory after this many eligible records (phased scopes only)")
    storage_regime.add_argument("--archive-reserved-slots", type=int, default=1,
        help="archive-only records placed first in each bounded automatic plan")
    storage_regime.add_argument("--workers", type=int, default=1,
        help="run this many independent deletion-last transactions concurrently")
    storage_regime.add_argument("--output", type=Path)
    storage_regime.add_argument("--apply", action="store_true")
    storage_regime.add_argument("--confirm", default="",
        help=f"required literal {STORAGE_REGIME_CONFIRMATION} with --apply")
    storage_regime.set_defaults(handler=command_starlink_storage_regime_v2)
    storage_audit = commands.add_parser("starlink-storage-audit-v2",
        help="prove that QNAP storage has converged to the production v2 layout")
    storage_audit.add_argument("shared_root", type=Path)
    storage_audit.add_argument("archive_root", type=Path)
    storage_audit.add_argument("--minimum-age-hours", type=float, default=6)
    storage_audit.add_argument("--sample-limit", type=int, default=20)
    storage_audit.add_argument("--require-producer-contract", action="store_true",
        help="also require a fresh running Kalman tiered-v2 producer heartbeat")
    storage_audit.add_argument("--maximum-producer-heartbeat-age-s", type=float,
        default=180)
    storage_audit.add_argument("--local-root", type=Path,
        help="also prove acquisition-host raw, quarantine and reports converged")
    storage_audit.add_argument("--output", type=Path)
    storage_audit.set_defaults(handler=command_starlink_storage_audit_v2)
    legacy_normalize = commands.add_parser("starlink-storage-normalize-legacy",
        help="transactionally normalize derived and versioned-output legacy layouts")
    legacy_normalize.add_argument("shared_root", type=Path)
    legacy_normalize.add_argument("archive_root", type=Path)
    legacy_normalize.add_argument("--planning-limit", type=int, default=64)
    legacy_normalize.add_argument("--limit", type=int, default=16)
    legacy_normalize.add_argument("--output", type=Path)
    legacy_normalize.add_argument("--apply", action="store_true")
    legacy_normalize.add_argument("--confirm", default="",
        help=f"required literal {LEGACY_LAYOUT_CONFIRMATION} with --apply")
    legacy_normalize.set_defaults(handler=command_starlink_storage_normalize_legacy)
    repair_v2 = commands.add_parser("starlink-evidence-repair-v2-summaries",
        help="repair legacy v2 source-byte accounting without changing IQ clips")
    repair_v2.add_argument("qnap_root", type=Path)
    repair_v2.add_argument("--limit", type=int)
    repair_v2.set_defaults(handler=command_starlink_evidence_repair_v2_summaries)
    evidence_materialize = commands.add_parser("starlink-evidence-materialize",
        help="create a standard replayable BeaconCapture view of one exact clip")
    evidence_materialize.add_argument("bundle", type=Path)
    evidence_materialize.add_argument("interval_id")
    evidence_materialize.add_argument("output", type=Path)
    evidence_materialize.set_defaults(handler=command_starlink_evidence_materialize)
    beacon_recover = commands.add_parser("starlink-beacon-recover",
        help="idempotently analyze complete captures left unreported by a restart")
    beacon_recover.add_argument("root", type=Path)
    beacon_recover.add_argument("--passes", type=Path,
        help="archived pass predictions used to annotate recovered confirmations")
    beacon_recover.add_argument("--exact-acquisition-method",
        choices=("coherent_grid_v1", "pss_symbolwise_v2", "pilot_symbolwise_v3"),
        default="coherent_grid_v1")
    beacon_recover.add_argument("--narrow-exact-interval-s", type=float, default=1)
    beacon_recover.add_argument("--wide-exact-interval-s", type=float, default=5)
    beacon_recover.set_defaults(handler=command_starlink_beacon_recover)
    beacon_followup = commands.add_parser("starlink-beacon-followup",
        help="densely replay neighborhoods around exact-code triggers")
    beacon_followup.add_argument("capture", type=Path)
    beacon_followup.add_argument("analysis", type=Path)
    beacon_followup.add_argument("output", type=Path)
    beacon_followup.add_argument("--radius-s", type=float, default=.5)
    beacon_followup.add_argument("--interval-s", type=float, default=.1)
    beacon_followup.add_argument("--window-s", type=float, default=.01)
    beacon_followup.add_argument("--passes", type=Path)
    beacon_followup.add_argument("--confirmation-marker", type=Path)
    beacon_followup.set_defaults(handler=command_starlink_beacon_followup)
    beacon_rescore = commands.add_parser("starlink-beacon-followup-rescore",
        help="reapply current confirmation and TLE annotation to saved replay checks")
    beacon_rescore.add_argument("followup", type=Path)
    beacon_rescore.add_argument("--passes", type=Path)
    beacon_rescore.add_argument("--plot", type=Path)
    beacon_rescore.add_argument("--plot-start-s", type=float)
    beacon_rescore.add_argument("--plot-stop-s", type=float)
    beacon_rescore.set_defaults(handler=command_starlink_beacon_followup_rescore)
    beacon_frame_track = commands.add_parser("starlink-beacon-frame-track",
        help="condition dense acquisitions into checksummed 750 Hz frame observations")
    beacon_frame_track.add_argument("capture", type=Path)
    beacon_frame_track.add_argument("followup", type=Path)
    beacon_frame_track.add_argument("output", type=Path)
    beacon_frame_track.add_argument("--samples", type=Path, required=True)
    beacon_frame_track.add_argument("--window-s", type=float, default=.1)
    beacon_frame_track.add_argument("--minimum-margin", type=float, default=.005)
    beacon_frame_track.add_argument("--maximum-relative-error-hz", type=float, default=500)
    beacon_frame_track.add_argument("--maximum-extension-s", type=float, default=60,
        help="propagate each acquired lock through this much unsearched IQ")
    beacon_frame_track.add_argument("--maximum-missed-windows", type=int, default=3,
        help="coast through this many consecutive invalid 100-ms windows")
    beacon_frame_track.add_argument("--minimum-extension-window-margin", type=float,
        default=.02, help="median dual exact-minus-control gate for propagated lock")
    beacon_frame_track.add_argument("--minimum-sparse-frame-margin", type=float,
        default=.05, help="dual-RX gate allowing one predicted low-PRF frame to update lock")
    beacon_frame_track.add_argument("--beacon-template", type=Path,
        help="qualified learned bandpass beacon artifact; defaults to published edge pilots")
    beacon_frame_track.set_defaults(handler=command_starlink_beacon_frame_track)
    beacon_template = commands.add_parser("starlink-beacon-template-learn",
        help="learn a checksummed full-duration bandpass beacon from aligned frames")
    beacon_template.add_argument("capture", type=Path)
    beacon_template.add_argument("followup", type=Path)
    beacon_template.add_argument("output", type=Path)
    beacon_template.add_argument("--samples", type=Path, required=True)
    beacon_template.add_argument("--window-s", type=float, default=.1)
    beacon_template.add_argument("--maximum-frames", type=int, default=600)
    beacon_template.add_argument("--iterations", type=int, default=5)
    beacon_template.set_defaults(handler=command_starlink_beacon_template_learn)
    beacon_channel_link = commands.add_parser("starlink-beacon-channel-link",
        help="build conservative cross-channel Doppler continuation hypotheses")
    beacon_channel_link.add_argument("output", type=Path)
    beacon_channel_link.add_argument("tracks", type=Path, nargs="+")
    beacon_channel_link.add_argument("--maximum-gap-s", type=float, default=30)
    beacon_channel_link.add_argument(
        "--maximum-acceleration-difference-m-s2", type=float, default=35)
    beacon_channel_link.add_argument(
        "--minimum-ambiguity-margin-m-s2", type=float, default=5)
    beacon_channel_link.add_argument(
        "--maximum-same-tuning-quadratic-rms-hz", type=float, default=2000)
    beacon_channel_link.add_argument(
        "--maximum-same-tuning-range-jerk-m-s3", type=float, default=5)
    beacon_channel_link.add_argument("--minimum-segment-epochs", type=int, default=5,
        help="exclude tiny fragments that cannot establish motion continuity")
    beacon_channel_link.add_argument("--minimum-segment-duration-s", type=float,
        default=.5, help="minimum measured span of a continuation fragment")
    beacon_channel_link.set_defaults(handler=command_starlink_beacon_channel_link)
    beacon_track = commands.add_parser("starlink-beacon-track",
        help="follow acquired full-frame edge pilots and emit calibrated 10 Hz Doppler")
    beacon_track.add_argument("capture", type=Path)
    beacon_track.add_argument("followup", type=Path)
    beacon_track.add_argument("output", type=Path)
    beacon_track.add_argument("--output-rate-hz", type=float, default=10)
    beacon_track.add_argument("--search-span-hz", type=float, default=2_000)
    beacon_track.add_argument("--maximum-reacquisition-span-hz", type=float, default=15_000,
        help="bounded adaptive search after intermittent beacon outages")
    beacon_track.add_argument("--search-step-hz", type=float, default=200,
        help="coarse CFO bank spacing; the reported peak is parabolically refined")
    beacon_track.add_argument("--minimum-margin", type=float, default=.015)
    beacon_track.add_argument("--tracking-margin", type=float, default=.005,
        help="lower exact/control gate allowed only with dual-RX frequency continuity")
    beacon_track.add_argument("--maximum-relative-error-hz", type=float, default=500)
    beacon_track.add_argument("--maximum-gap-s", type=float, default=5,
        help="propagate CFO without inventing observations through intermittent beacon gaps")
    beacon_track.add_argument("--maximum-drift-hz-s", type=float, default=15_000)
    beacon_track.add_argument("--measurement-source",
        choices=("auto", "conditioned_frames", "dense_followup", "periodic_epoch"),
        default="auto",
        help="use independent dense full-frame epochs when available")
    beacon_track.add_argument("--frame-track", type=Path,
        help="checksummed 750 Hz conditioned frame artifact to aggregate at 10 Hz")
    beacon_track.set_defaults(handler=command_starlink_beacon_track)
    beacon_calibrate = commands.add_parser("starlink-beacon-calibrate",
        help="publish empirical exact/control null distributions and gate exceedances")
    beacon_calibrate.add_argument("reports", type=Path)
    beacon_calibrate.add_argument("output", type=Path)
    beacon_calibrate.set_defaults(handler=command_starlink_beacon_calibrate)
    beacon_null = commands.add_parser("starlink-beacon-null-replay",
        help="replay stratified retained field negatives for a detector-specific null")
    beacon_null.add_argument("root", type=Path)
    beacon_null.add_argument("output", type=Path)
    beacon_null.add_argument("--exact-acquisition-method",
        choices=("coherent_grid_v1", "pss_symbolwise_v2", "pilot_symbolwise_v3"),
        default="pilot_symbolwise_v3")
    beacon_null.add_argument("--capture-limit", type=int, default=6)
    beacon_null.add_argument("--checks-per-capture", type=int, default=12)
    beacon_null.add_argument("--exact-window-s", type=float, default=.01)
    beacon_null.add_argument("--maximum-host-temperature-c", type=float, default=75)
    beacon_null.add_argument("--resume-host-temperature-c", type=float, default=70)
    beacon_null.set_defaults(handler=command_starlink_beacon_null_replay)
    beacon_decode = commands.add_parser("starlink-beacon-decode",
        help="demodulate and render edge-pilot and narrow SSS constellations")
    beacon_decode.add_argument("capture", type=Path)
    beacon_decode.add_argument("followup", type=Path)
    beacon_decode.add_argument("output", type=Path)
    beacon_decode.add_argument("--time-s", type=float,
        help="decode the dense follow-up check nearest this capture time")
    beacon_decode.add_argument("--plot", type=Path,
        help="write pilot/SSS constellations and held-out decision maps")
    beacon_decode.add_argument("--symbols", type=Path,
        help="write equalized complex symbols and channel estimates as NPZ")
    beacon_decode.set_defaults(handler=command_starlink_beacon_decode)
    beacon_fingerprint = commands.add_parser("starlink-beacon-fingerprint",
        help="compare decoded PSS/SSS/pilot signatures across beacon observations")
    beacon_fingerprint.add_argument("root", type=Path,
        help="beacon storage root containing reports/decoded")
    beacon_fingerprint.add_argument("--capture-name",
        help="update one decoded capture before rebuilding the comparison index")
    beacon_fingerprint.set_defaults(handler=command_starlink_beacon_fingerprint)
    beacon_gain_summary = commands.add_parser("starlink-beacon-gain-summary",
        help="compare randomized manual and slow-attack beacon captures")
    beacon_gain_summary.add_argument("root", type=Path)
    beacon_gain_summary.add_argument("output", type=Path)
    beacon_gain_summary.set_defaults(handler=command_starlink_beacon_gain_summary)
    dashboard_row = commands.add_parser("starlink-beacon-dashboard-row",
        help="publish one recording's compact dashboard listing row")
    dashboard_row.add_argument("root", type=Path)
    dashboard_row.add_argument("name")
    dashboard_row.add_argument("--sources", nargs="*",
        default=["space-track", "huggingface"],
        help="catalog providers whose per-source fit to record")
    dashboard_row.set_defaults(handler=command_starlink_beacon_dashboard_row)
    probe_index = commands.add_parser("starlink-probe-index",
        help="project per-probe facts into day-partitioned parquet")
    probe_index.add_argument("action", choices=("build", "status", "query"))
    probe_index.add_argument("root", type=Path)
    probe_index.add_argument("--sql", help="query to run against the probes view")
    probe_index.add_argument("--pattern", default="*narrow*")
    probe_index.add_argument("--rebuild", action="store_true")
    probe_index.add_argument("--limit", type=int)
    probe_index.set_defaults(handler=command_starlink_probe_index)
    analysis_index = commands.add_parser("starlink-analysis-index",
        help="project authenticated analysis runs into partitioned parquet")
    analysis_index.add_argument("action", choices=("build", "status", "query"))
    analysis_index.add_argument("root", type=Path,
        help="shared analysis root holding reports and completion receipts")
    analysis_index.add_argument("--index-root", type=Path,
        help="where partitions are written; defaults to ROOT")
    analysis_index.add_argument("--sql", help="query to run against the projection")
    analysis_index.add_argument("--rebuild", action="store_true")
    analysis_index.add_argument("--limit", type=int,
        help="stop after this many partitions are rebuilt")
    analysis_index.add_argument("--work-dir", type=Path,
        help="local scratch for the per-partition build database")
    analysis_index.add_argument("--memory-limit", default=None,
        help="DuckDB memory limit for a partition build; the export peaks, "
             "not the ingest (default 16GB)")
    analysis_index.set_defaults(handler=command_starlink_analysis_index)
    analysis_store = commands.add_parser("starlink-analysis-store",
        help="ingest authenticated Kalman outputs into a single-owner DuckDB store")
    analysis_store.add_argument("action", choices=(
        "init", "enqueue", "drain", "backfill", "status", "query", "publish", "service"))
    analysis_store.add_argument("store_root", type=Path,
        help="Kalman-local store root containing live, inbox and snapshot state")
    analysis_store.add_argument("--shared-root", type=Path, required=True,
        help="shared analysis root containing reports and completion receipts")
    analysis_store.add_argument("--database", type=Path,
        help="live or read-only DuckDB path; defaults below STORE_ROOT/live")
    analysis_store.add_argument("--recording-id")
    analysis_store.add_argument("--pipeline-id")
    analysis_store.add_argument("--limit", type=int)
    analysis_store.add_argument("--dry-run", action="store_true")
    analysis_store.add_argument("--continue-on-error", action="store_true")
    analysis_store.add_argument("--sql")
    analysis_store.add_argument("--publication-root", type=Path,
        help="shared directory for immutable snapshots and current.json")
    analysis_store.add_argument("--runtime-output", type=Path)
    analysis_store.add_argument("--poll-s", type=float, default=2.0)
    analysis_store.add_argument("--snapshot-interval-s", type=float, default=300.0)
    analysis_store.add_argument("--reconciliation-interval-s", type=float, default=600.0)
    analysis_store.add_argument("--reconciliation-limit", type=int, default=100)
    analysis_store.add_argument("--once", action="store_true")
    analysis_store.set_defaults(handler=command_starlink_analysis_store)
    lnb_calibration = commands.add_parser("starlink-lnb-calibration",
        help="measure the LNB local-oscillator mismatch on each radio")
    lnb_calibration.add_argument("root", type=Path)
    lnb_calibration.add_argument("--limit", type=int, default=900,
        help="most recent narrow reports to examine")
    lnb_calibration.add_argument("--apply", action="store_true",
        help="store the measurement, replacing the previous one")
    lnb_calibration.set_defaults(handler=command_starlink_lnb_calibration)
    dashboard_shards = commands.add_parser("starlink-dashboard-shards",
        help="publish and compact the date-sharded dashboard listing")
    dashboard_shards.add_argument("action", choices=("migrate", "compact"))
    dashboard_shards.add_argument("root", type=Path)
    dashboard_shards.add_argument("--index", type=Path,
        help="existing monolithic index to project, for migrate")
    dashboard_shards.add_argument("--sources", nargs="*",
        default=["space-track", "huggingface"],
        help="catalog providers whose per-source fit to record")
    dashboard_shards.set_defaults(handler=command_starlink_dashboard_shards)
    beacon_dashboard_index = commands.add_parser("starlink-beacon-dashboard-index",
        help="incrementally build the lightweight beacon dashboard index")
    beacon_dashboard_index.add_argument("root", type=Path)
    beacon_dashboard_index.add_argument("output", type=Path)
    beacon_dashboard_index.add_argument("--capture-name")
    beacon_dashboard_index.set_defaults(handler=command_starlink_beacon_dashboard_index)
    analyze = commands.add_parser("analyze", help="create blind ridge data and waterfall")
    analyze.add_argument("capture"); analyze.add_argument("output_dir")
    analyze.add_argument("--fft-size", type=int, default=4096); analyze.add_argument("--hop-size", type=int)
    analyze.add_argument("--search-hz", type=float, nargs=2); analyze.add_argument("--min-snr-db", type=float, default=6)
    analyze.add_argument("--max-step-hz", type=float); analyze.set_defaults(handler=command_analyze)
    verify = commands.add_parser("verify", help="validate manifest and IQ checksum")
    verify.add_argument("capture"); verify.set_defaults(handler=command_verify)
    moving = commands.add_parser("moving", help="blindly score moving energy after stationary-spur suppression")
    moving.add_argument("capture"); moving.add_argument("output")
    moving.add_argument("--fft-size", type=int, default=8192)
    moving.add_argument("--hop-size", type=int, default=262144)
    moving.add_argument("--search-hz", type=float, nargs=2)
    moving.add_argument("--candidates", type=int, default=24)
    moving.add_argument("--max-step-hz", type=float, default=12000)
    moving.add_argument("--skip-checksum", action="store_true")
    moving.set_defaults(handler=command_moving)
    comb = commands.add_parser("comb", help="blindly track common Doppler of a stationary-suppressed tone comb")
    comb.add_argument("capture"); comb.add_argument("output")
    comb.add_argument("--fft-size", type=int, default=8192); comb.add_argument("--integration-s", type=float, default=1)
    comb.add_argument("--spectra-per-integration", type=int, default=24)
    comb.add_argument("--tone-count", type=int, default=9); comb.add_argument("--tone-spacing-hz", type=float, default=43949.5)
    comb.add_argument("--search-hz", type=float, nargs=2, default=(-500000, 500000))
    comb.add_argument("--max-drift-hz-s", type=float, default=12000); comb.add_argument("--skip-checksum", action="store_true")
    comb.add_argument("--plot", help="write a diagnostic PNG of track strength and tone support")
    comb.set_defaults(handler=command_comb)
    qualify = commands.add_parser("qualify-pair", help="TLE-blind true/control comb qualification of paired RX")
    qualify.add_argument("session"); qualify.add_argument("output")
    qualify.add_argument("--true-spacing-hz", type=float, default=43949.5)
    qualify.add_argument("--wrong-spacings-hz", type=float, nargs="+", default=(37000, 50000))
    qualify.add_argument("--score-margin", type=float, default=1.0); qualify.add_argument("--rx-margin", type=float, default=1.0)
    qualify.add_argument("--min-positive-tone-fraction", type=float, default=.7)
    qualify.add_argument("--fft-size", type=int, default=8192); qualify.add_argument("--integration-s", type=float, default=1)
    qualify.add_argument("--spectra-per-integration", type=int, default=24); qualify.add_argument("--tone-count", type=int, default=9)
    qualify.add_argument("--search-hz", type=float, nargs=2, default=(-500000, 500000))
    qualify.add_argument("--max-drift-hz-s", type=float, default=12000); qualify.add_argument("--skip-checksum", action="store_true")
    qualify.add_argument("--plot-dir"); qualify.set_defaults(handler=command_qualify_pair)
    carrier = commands.add_parser("carrier", help="measure a narrow carrier track without making a detection claim")
    carrier.add_argument("capture"); carrier.add_argument("output")
    carrier.add_argument("--search-hz", type=float, nargs=2, metavar=("LOW", "HIGH"))
    carrier.add_argument("--search-center-hz", type=float); carrier.add_argument("--search-span-hz", type=float)
    carrier.add_argument("--integration-s", type=float, default=1)
    carrier.add_argument("--fft-size", type=int, default=65536)
    carrier.add_argument("--spectra-per-integration", type=int, default=16)
    carrier.add_argument("--skip-checksum", action="store_true")
    carrier.add_argument("--plot", help="write a measurement-only diagnostic PNG")
    carrier.set_defaults(handler=command_carrier)
    scan = commands.add_parser("scan", help="sweep receive-only IF power and spectral excess")
    scan.add_argument("output_dir", type=Path)
    scan.add_argument("--start-hz", type=float, required=True)
    scan.add_argument("--stop-hz", type=float, required=True)
    scan.add_argument("--step-hz", type=float, required=True)
    scan.add_argument("--sample-rate-hz", type=float, default=4_000_000)
    scan.add_argument("--bandwidth-hz", type=float, default=3_000_000)
    scan.add_argument("--gain-db", type=float, default=40.0)
    scan.add_argument("--channel", type=int, choices=(0, 1), default=None,
                      help="deprecated single-channel alias for --channels")
    scan.add_argument("--channels", type=parse_channels, metavar="0|1|0,1",
                      help="receiver selection; dual mode samples both channels together")
    scan.add_argument("--samples-per-frequency", type=int, default=65_536)
    scan.add_argument("--settle-seconds", type=float, default=.05)
    scan.add_argument("--uri", default="pluto://ip:192.168.2.1")
    scan.add_argument("--serial")
    scan.set_defaults(handler=command_scan)
    monitor = commands.add_parser("monitor", help="compact dual-RX broadband motion monitor")
    monitor.add_argument("output_dir", type=Path)
    source = monitor.add_mutually_exclusive_group(required=True)
    source.add_argument("--centers-file", type=Path,
                        help="JSON region plan from rank-regions")
    source.add_argument("--start-hz", type=float)
    monitor.add_argument("--stop-hz", type=float)
    monitor.add_argument("--step-hz", type=float)
    monitor.add_argument("--cycles", type=int, default=2)
    monitor.add_argument("--sample-rate-hz", type=float, default=30_720_000)
    monitor.add_argument("--bandwidth-hz", type=float, default=24_576_000)
    monitor.add_argument("--gain-db", type=float, default=40)
    monitor.add_argument("--samples-per-tuning", type=int, default=65_536)
    monitor.add_argument("--settle-seconds", type=float, default=3)
    monitor.add_argument("--discard-buffers", type=int, default=1)
    monitor.add_argument("--fft-size", type=int, default=8192)
    monitor.add_argument("--psd-bins", type=int, default=2048)
    monitor.add_argument("--min-shift-hz", type=float, default=30_000)
    monitor.add_argument("--max-shift-hz", type=float, default=600_000)
    monitor.add_argument("--min-correlation", type=float, default=.55)
    monitor.add_argument("--channel-tolerance-hz", type=float, default=45_000)
    monitor.add_argument("--max-cycle-lag", type=int, default=1,
                         help="compare spectra up to this many revisit cycles apart")
    monitor.add_argument("--channels", type=parse_channels, default=(0, 1), metavar="0,1")
    monitor.add_argument("--uri", default="pluto://ip:192.168.2.1")
    monitor.set_defaults(handler=command_monitor)
    rank = commands.add_parser("rank-regions", help="rank discovery tiles for pass-time revisiting")
    rank.add_argument("monitor_report", type=Path); rank.add_argument("output", type=Path)
    rank.add_argument("--count", type=int, default=6)
    rank.add_argument("--offset", type=int, default=0,
                      help="skip this many higher-ranked centers")
    rank.set_defaults(handler=command_rank_regions)
    reanalyze = commands.add_parser("reanalyze-monitor", help="rerun motion detection on compact spectra")
    reanalyze.add_argument("monitor_report", type=Path); reanalyze.add_argument("output_dir", type=Path)
    reanalyze.add_argument("--min-shift-hz", type=float, default=5_000)
    reanalyze.add_argument("--max-shift-hz", type=float, default=600_000)
    reanalyze.add_argument("--min-correlation", type=float, default=.55)
    reanalyze.add_argument("--channel-tolerance-hz", type=float, default=45_000)
    reanalyze.add_argument("--max-cycle-lag", type=int, default=20)
    reanalyze.set_defaults(handler=command_reanalyze_monitor)
    sl_channels = commands.add_parser("starlink-channels", help="print exact Starlink RF/IF channel mappings")
    sl_channels.set_defaults(handler=command_starlink_channels)
    hybrid_plan = commands.add_parser("starlink-hybrid-plan",
        help="write a complete channel-3/4 survey and continuous-dwell plan")
    hybrid_plan.add_argument("output", type=Path)
    hybrid_plan.add_argument("--dwell-seconds", type=float, default=600)
    hybrid_plan.set_defaults(handler=command_starlink_hybrid_plan)
    sl_observe = commands.add_parser("starlink-observe", help="continuously observe one exact Starlink channel")
    sl_observe.add_argument("output_dir", type=Path)
    sl_observe.add_argument("--channel-number", type=int, required=True)
    sl_observe.add_argument("--if-offset-hz", type=float, default=0,
                            help="measured LNB IF correction added to the nominal channel center")
    sl_observe.add_argument("--duration-s", type=float, default=120)
    sl_observe.add_argument("--sample-rate-hz", type=float, default=2_500_000)
    sl_observe.add_argument("--bandwidth-hz", type=float, default=2_500_000)
    sl_observe.add_argument("--gain-db", type=float, default=40)
    sl_observe.add_argument("--block-size", type=int, default=262_144)
    sl_observe.add_argument("--ring-seconds", type=float, default=2)
    sl_observe.add_argument("--queue-blocks", type=int, default=8,
                            help="bounded hardware-reader queue used to overlap acquisition and analysis")
    sl_observe.add_argument("--fft-size", type=int, default=16_384)
    sl_observe.add_argument("--search-hz", type=float, default=400_000)
    sl_observe.add_argument("--gutter-threshold-db", type=float, default=3)
    sl_observe.add_argument("--periodicity-threshold", type=float, default=.05)
    sl_observe.add_argument("--comb-threshold-db", type=float, default=4)
    sl_observe.add_argument("--channels", type=parse_channels, default=(0,), metavar="0|1|0,1")
    sl_observe.add_argument("--uri", default="pluto://ip:192.168.2.1")
    sl_observe.add_argument("--serial"); sl_observe.add_argument("--transport", choices=("iio", "direct_usb"), default="iio")
    sl_observe.add_argument("--high-band-selected", action="store_true",
                            help="acknowledge that the external 22 kHz LNB high-band tone is active")
    sl_observe.add_argument("--fake", action="store_true")
    sl_observe.add_argument("--fake-noise-only", action="store_true")
    sl_observe.add_argument("--seed", type=int, default=0)
    sl_observe.set_defaults(handler=command_starlink_observe)
    sl_analyze = commands.add_parser("starlink-analyze", help="reanalyze event-triggered Starlink IQ")
    sl_analyze.add_argument("event_iq", type=Path); sl_analyze.add_argument("output", type=Path)
    sl_analyze.add_argument("--fft-size", type=int, default=16_384)
    sl_analyze.add_argument("--search-hz", type=float, default=400_000)
    sl_analyze.add_argument("--gutter-threshold-db", type=float, default=3)
    sl_analyze.add_argument("--periodicity-threshold", type=float, default=.05)
    sl_analyze.add_argument("--comb-threshold-db", type=float, default=4)
    sl_analyze.set_defaults(handler=command_starlink_analyze)
    sl_offset = commands.add_parser("starlink-offset-search",
                                    help="rank repeatable Starlink gutter offsets in wide snapshots")
    sl_offset.add_argument("output", type=Path)
    sl_offset.add_argument("--channel-number", type=int, required=True)
    sl_offset.add_argument("--if-offset-hz", type=float, default=0,
                           help="shift tuner center to test whether a feature follows absolute RF")
    sl_offset.add_argument("--sample-rate-hz", type=float, default=30_720_000)
    sl_offset.add_argument("--bandwidth-hz", type=float, default=24_576_000)
    sl_offset.add_argument("--gain-db", type=float, default=40)
    sl_offset.add_argument("--block-size", type=int, default=262_144)
    sl_offset.add_argument("--snapshots", type=int, default=256)
    sl_offset.add_argument("--queue-blocks", type=int, default=8)
    sl_offset.add_argument("--fft-size", type=int, default=16_384)
    sl_offset.add_argument("--search-hz", type=float, default=14_000_000)
    sl_offset.add_argument("--top-count", type=int, default=5)
    sl_offset.add_argument("--bin-hz", type=float, default=100_000)
    sl_offset.add_argument("--channels", type=parse_channels, default=(0, 1), metavar="0|1|0,1")
    sl_offset.add_argument("--uri", default="pluto://ip:192.168.2.1")
    sl_offset.add_argument("--serial")
    sl_offset.add_argument("--transport", choices=("iio", "direct_usb"), default="iio")
    sl_offset.add_argument("--high-band-selected", action="store_true")
    sl_offset.add_argument("--fake", action="store_true")
    sl_offset.add_argument("--fake-noise-only", action="store_true")
    sl_offset.add_argument("--seed", type=int, default=0)
    sl_offset.set_defaults(handler=command_starlink_offset_search)
    sl_wc = commands.add_parser("starlink-waterfall-capture",
        help="record compact fixed-tuning wideband spectra without routine raw IQ")
    sl_wc.add_argument("output", type=Path)
    sl_wc.add_argument("--channel-number", type=int, required=True)
    sl_wc.add_argument("--if-offset-hz", type=float, default=0)
    sl_wc.add_argument("--sample-rate-hz", type=float, default=30_720_000)
    sl_wc.add_argument("--bandwidth-hz", type=float, default=24_576_000)
    sl_wc.add_argument("--gain-db", type=float, default=50)
    sl_wc.add_argument("--block-size", type=int, default=262_144)
    sl_wc.add_argument("--snapshots", type=int, default=4096)
    sl_wc.add_argument("--queue-blocks", type=int, default=8)
    sl_wc.add_argument("--fft-size", type=int, default=16_384)
    sl_wc.add_argument("--output-bins", type=int, default=4096)
    sl_wc.add_argument("--channels", type=parse_channels, default=(0, 1), metavar="0|1|0,1")
    sl_wc.add_argument("--uri", default="pluto://ip:192.168.2.1")
    sl_wc.add_argument("--serial")
    sl_wc.add_argument("--transport", choices=("iio", "direct_usb"), default="iio")
    sl_wc.add_argument("--high-band-selected", action="store_true")
    sl_wc.add_argument("--fake", action="store_true")
    sl_wc.add_argument("--fake-start-offset-hz", type=float, default=-2_000_000)
    sl_wc.add_argument("--fake-drift-hz-s", type=float, default=5000)
    sl_wc.add_argument("--seed", type=int, default=0)
    sl_wc.set_defaults(handler=command_starlink_waterfall_capture)
    sl_wa = commands.add_parser("starlink-waterfall-analyze",
        help="extract stationary-suppressed moving depressions from a compact waterfall")
    sl_wa.add_argument("waterfall", type=Path); sl_wa.add_argument("output", type=Path)
    sl_wa.add_argument("--integration-s", type=float, default=1)
    sl_wa.add_argument("--max-drift-hz-s", type=float, default=10_000)
    sl_wa.add_argument("--permutations", type=int, default=0,
                       help="time-scrambled empirical false-alarm controls")
    sl_wa.add_argument("--seed", type=int, default=0)
    sl_wa.add_argument("--plot", type=Path)
    sl_wa.set_defaults(handler=command_starlink_waterfall_analyze)
    sl_watch = commands.add_parser("starlink-waterfall-watch",
        help="run chunked long-duration wideband tracking with PNGs and detections")
    sl_watch.add_argument("output_dir", type=Path)
    sl_watch.add_argument("--hours", type=float, default=12)
    sl_watch.add_argument("--max-chunks", type=int,
                          help="finite acceptance/test limit; hours remains the production deadline")
    sl_watch.add_argument("--channel-number", type=int, default=4)
    sl_watch.add_argument("--if-offset-hz", type=float, default=5_000_000)
    sl_watch.add_argument("--sample-rate-hz", type=float, default=30_720_000)
    sl_watch.add_argument("--bandwidth-hz", type=float, default=24_576_000)
    sl_watch.add_argument("--gain-db", type=float, default=50)
    sl_watch.add_argument("--block-size", type=int, default=262_144)
    sl_watch.add_argument("--chunk-snapshots", type=int, default=4096)
    sl_watch.add_argument("--queue-blocks", type=int, default=16)
    sl_watch.add_argument("--fft-size", type=int, default=16_384)
    sl_watch.add_argument("--output-bins", type=int, default=4096)
    sl_watch.add_argument("--integration-s", type=float, default=1)
    sl_watch.add_argument("--max-drift-hz-s", type=float, default=10_000)
    sl_watch.add_argument("--permutations", type=int, default=32)
    sl_watch.add_argument("--max-false-alarm-probability", type=float, default=.05)
    sl_watch.add_argument("--min-span-hz", type=float, default=100_000)
    sl_watch.add_argument("--min-depression-db", type=float, default=.3)
    sl_watch.add_argument("--min-receiver-correlation", type=float, default=.5)
    sl_watch.add_argument("--max-receiver-difference-hz", type=float, default=500_000)
    sl_watch.add_argument("--plot-mode", choices=("all", "detections", "none"), default="all")
    sl_watch.add_argument("--channels", type=parse_channels, default=(0, 1), metavar="0|1|0,1")
    sl_watch.add_argument("--uri", default="pluto://ip:192.168.2.1")
    sl_watch.add_argument("--serial")
    sl_watch.add_argument("--transport", choices=("iio", "direct_usb"), default="iio")
    sl_watch.add_argument("--high-band-selected", action="store_true")
    sl_watch.add_argument("--fake", action="store_true")
    sl_watch.add_argument("--fake-start-offset-hz", type=float, default=-200_000)
    sl_watch.add_argument("--fake-drift-hz-s", type=float, default=50_000)
    sl_watch.add_argument("--seed", type=int, default=0)
    sl_watch.set_defaults(handler=command_starlink_waterfall_watch)
    dashboard = commands.add_parser("starlink-dashboard",
        help="serve a live read-only web dashboard for a waterfall watch")
    dashboard.add_argument("observation_dir", type=Path)
    dashboard.add_argument("--passes", type=Path,
                           help="expected-pass catalog; defaults to OBSERVATION_DIR/passes.json")
    dashboard.add_argument("--beacon-root", type=Path,
                           help="continuous exact-beacon storage root")
    dashboard.add_argument("--analysis-store-pointer", type=Path,
        help="verified shared analysis-store current.json pointer")
    dashboard.add_argument("--analysis-store-cache", type=Path,
        help="dashboard-local directory for verified read-only snapshots")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--samples-per-snapshot", type=int, default=262_144)
    dashboard.add_argument("--sample-rate-hz", type=float, default=30_720_000)
    dashboard.add_argument("--snapshots-per-chunk", type=int, default=4096)
    dashboard.set_defaults(handler=command_starlink_dashboard)
    gain_test = commands.add_parser("starlink-gain-experiment",
        help="compare manual gains with slow/fast AGC using v2 measurement artifacts")
    gain_test.add_argument("output_dir", type=Path)
    gain_test.add_argument("--center-frequency-hz", type=float, default=1_830_117_187.5)
    gain_test.add_argument("--lnb-lo-hz", type=float, default=9_750_000_000)
    gain_test.add_argument("--sample-rate-hz", type=float, default=30_720_000)
    gain_test.add_argument("--bandwidth-hz", type=float, default=24_576_000)
    gain_test.add_argument("--manual-gains-db", type=float, nargs="+", default=(20, 30, 40, 50))
    gain_test.add_argument("--snapshots", type=int, default=64)
    gain_test.add_argument("--block-size", type=int, default=262_144)
    gain_test.add_argument("--fft-size", type=int, default=16_384)
    gain_test.add_argument("--output-bins", type=int, default=4096)
    gain_test.add_argument("--settle-seconds", type=float, default=2)
    gain_test.add_argument("--discard-buffers", type=int, default=1)
    gain_test.add_argument("--adc-full-scale", type=float)
    gain_test.add_argument("--uri", default="pluto://ip:192.168.2.1")
    gain_test.add_argument("--fake", action="store_true")
    gain_test.add_argument("--seed", type=int, default=0)
    gain_test.set_defaults(handler=command_starlink_gain_experiment)
    measurement = commands.add_parser("starlink-measurement-capture",
        help="capture absolute-power v2 spectra with timing, clipping, and gain provenance")
    measurement.add_argument("output", type=Path)
    measurement.add_argument("--center-frequency-hz", type=float, default=1_830_117_187.5)
    measurement.add_argument("--lnb-lo-hz", type=float, default=9_750_000_000)
    measurement.add_argument("--sample-rate-hz", type=float, default=30_720_000)
    measurement.add_argument("--bandwidth-hz", type=float, default=24_576_000)
    measurement.add_argument("--gain-mode", choices=("manual", "slow_attack", "fast_attack"), default="manual")
    measurement.add_argument("--gain-db", type=float)
    measurement.add_argument("--snapshots", type=int, default=256)
    measurement.add_argument("--block-size", type=int, default=262_144)
    measurement.add_argument("--fft-size", type=int, default=16_384)
    measurement.add_argument("--output-bins", type=int, default=4096)
    measurement.add_argument("--psd-quantization-db", type=float,
        help="store PSD as int16 codes at this dB step (for example 0.01)")
    measurement.add_argument("--adc-full-scale", type=float)
    measurement.add_argument("--gps-device", type=Path,
                             help="optional NMEA serial device recorded as provenance")
    measurement.add_argument("--gps-timeout-s", type=float, default=3)
    measurement.add_argument("--require-gps-fix", action="store_true")
    measurement.add_argument("--experiment-tag")
    measurement.add_argument("--observation-mode", choices=("fixed", "retune-validation"),
                             help="record the scheduled observing strategy in capture provenance")
    measurement.add_argument("--tuning-dither-hz", type=float)
    measurement.add_argument("--interleaved-dither-hz", type=float,
        help="alternate nominal and offset centers within one continuous artifact")
    measurement.add_argument("--dither-segment-s", type=float, default=3.0)
    measurement.add_argument("--dither-discard-buffers", type=int, default=2,
        help="discard this many buffers after every in-capture retune")
    measurement.add_argument("--discard-buffers", type=int, default=0,
        help="discard initial buffers after configuring/retuning the receiver")
    measurement.add_argument("--host-temperature-c", type=float,
        help="optional host temperature recorded as capture provenance")
    measurement.add_argument("--radio-temperature-c", type=float,
        help="optional radio/AD9361 temperature recorded as capture provenance")
    measurement.add_argument("--iq-evidence-output", type=Path,
        help="bounded dual-RX raw-IQ artifact for waveform-specific follow-up")
    measurement.add_argument("--iq-evidence-blocks", type=int, default=4)
    measurement.add_argument("--iq-trigger-warmup-blocks", type=int, default=16)
    measurement.add_argument("--iq-trigger-threshold-db", type=float, default=.5)
    measurement.add_argument("--iq-trigger-separation-blocks", type=int, default=8)
    measurement.add_argument("--iq-trigger-stratum-blocks", type=int,
        help="retain the best trigger per fixed snapshot stratum for time coverage")
    measurement.add_argument("--uri", default="pluto://ip:192.168.2.1")
    measurement.add_argument("--fake", action="store_true"); measurement.add_argument("--seed", type=int, default=0)
    measurement.set_defaults(handler=command_starlink_measurement_capture)
    waveform_iq = commands.add_parser("starlink-waveform-iq-analyze",
        help="test triggered raw IQ for Starlink beacon period and tone spacing")
    waveform_iq.add_argument("iq_evidence", type=Path)
    waveform_iq.add_argument("output", type=Path)
    waveform_iq.add_argument("--beacon-rate-hz", type=float, default=750.0)
    waveform_iq.add_argument("--tone-spacing-hz", type=float, default=43_949.5)
    waveform_iq.add_argument("--period-search-fraction", type=float, default=.02)
    waveform_iq.add_argument("--tone-search-hz", type=float, default=2_000)
    waveform_iq.set_defaults(handler=command_starlink_waveform_iq)
    iq_gate = commands.add_parser("starlink-iq-evidence-gate",
        help="retain staged IQ blocks only when they overlap a qualified moving feature")
    iq_gate.add_argument("iq_evidence", type=Path)
    iq_gate.add_argument("wide_report", type=Path)
    iq_gate.add_argument("output", type=Path)
    iq_gate.add_argument("--margin-s", type=float, default=1.0)
    iq_gate.add_argument("--frequency-margin-hz", type=float, default=15_000)
    iq_gate.set_defaults(handler=command_starlink_iq_evidence_gate)
    confounds = commands.add_parser("starlink-confound-analyze",
        help="test detections against tuning, order, settling, telemetry, and clustering controls")
    confounds.add_argument("observation_root", type=Path)
    confounds.add_argument("output", type=Path)
    confounds.add_argument("--settling-window-s", type=float, default=10)
    confounds.set_defaults(handler=command_starlink_confound_analysis)
    measurement_analysis = commands.add_parser("starlink-measurement-analyze",
        help="segment finite events, pair LNBs, fit Doppler, and render v2 diagnostics")
    measurement_analysis.add_argument("measurement", type=Path)
    measurement_analysis.add_argument("output", type=Path)
    measurement_analysis.add_argument("--plot", type=Path)
    measurement_analysis.add_argument("--passes", type=Path)
    measurement_analysis.add_argument("--carrier-hz", type=float,
        help="original RF carrier used for Doppler physics, not LNB IF or Pluto tuning")
    measurement_analysis.add_argument("--threshold-db", type=float, default=.35)
    measurement_analysis.add_argument("--minimum-time-bins", type=int, default=3)
    measurement_analysis.add_argument("--minimum-frequency-bins", type=int, default=3)
    measurement_analysis.add_argument("--event-frequency-bins", type=int, default=1024,
        help="power-average to this bin count for generic events; TLE search retains all bins")
    measurement_analysis.add_argument("--broadband-fraction", type=float, default=.65)
    measurement_analysis.add_argument("--tle-dwell-window-s", type=float, default=30)
    measurement_analysis.add_argument("--tle-dwell-step-s", type=float, default=10)
    measurement_analysis.add_argument("--tle-minimum-window-s", type=float, default=8)
    measurement_analysis.add_argument("--tle-comb-spacing-hz", type=float, default=43_900)
    measurement_analysis.add_argument("--tle-minimum-comb-spacing-improvement", type=float,
                                      default=.03)
    measurement_analysis.add_argument("--tle-broadband-frequency-fraction", type=float, default=.2)
    measurement_analysis.add_argument("--tle-maximum-broadband-row-fraction", type=float,
                                      default=.2)
    measurement_analysis.add_argument("--blind-comb-search-half-width-hz", type=float,
                                      default=1_000_000)
    measurement_analysis.add_argument("--no-blind-comb", action="store_true")
    measurement_analysis.set_defaults(handler=command_starlink_measurement_analyze)
    observations = commands.add_parser("doppler-observations",
        help="write settled raw-segment tracks and cross-retune origin tests")
    observations.add_argument("measurement", type=Path)
    observations.add_argument("output", type=Path)
    observations.add_argument("--threshold-db", type=float, default=.35)
    observations.add_argument("--event-frequency-bins", type=int, default=1024)
    observations.add_argument("--stable-guard-s", type=float, default=.75)
    observations.add_argument("--minimum-track-duration-s", type=float, default=3.0)
    observations.add_argument("--assume-all-shifts-doppler", action="store_true",
        help="process every detected dual-RX frequency shift as Doppler while retaining validation metadata")
    observations.set_defaults(handler=command_doppler_observations)
    tracker_ensemble = commands.add_parser("doppler-trackers",
        help="run the waveform-agnostic narrow and broadband tracker ensemble")
    tracker_ensemble.add_argument("measurement", type=Path)
    tracker_ensemble.add_argument("output", type=Path)
    tracker_ensemble.add_argument("--passes", type=Path,
        help="stored pass catalog for post-detection trajectory identification")
    tracker_ensemble.add_argument("--plot", type=Path,
        help="render qualified receiver-local and dual-RX tracks over bounded waterfalls")
    tracker_ensemble.add_argument("--window", action="append",
        help="restrict broadband/Viterbi analysis to START:STOP elapsed seconds; repeatable")
    tracker_ensemble.add_argument("--integration-s", type=float, default=.5)
    tracker_ensemble.add_argument("--dedoppler-window-s", type=float, default=10)
    tracker_ensemble.add_argument("--dedoppler-step-s", type=float, default=5)
    tracker_ensemble.add_argument("--minimum-drift-hz-s", type=float, default=-15_000)
    tracker_ensemble.add_argument("--maximum-drift-hz-s", type=float, default=15_000)
    tracker_ensemble.add_argument("--drift-step-hz-s", type=float, default=500)
    tracker_ensemble.set_defaults(handler=command_doppler_tracker_ensemble)
    iq_track = commands.add_parser("doppler-iq-track",
        help="run coherent FLL, polynomial-phase, and repetition trackers on triggered IQ")
    iq_track.add_argument("iq", type=Path)
    iq_track.add_argument("output", type=Path)
    iq_track.add_argument("--estimates-per-block", type=int, default=16)
    iq_track.add_argument("--repetition-minimum-lag", type=int, default=32)
    iq_track.add_argument("--repetition-maximum-lag", type=int, default=4096)
    iq_track.set_defaults(handler=command_doppler_iq_track)
    ambiguity = commands.add_parser("doppler-ambiguity",
        help="run a known-template delay/Doppler cross-ambiguity search")
    ambiguity.add_argument("iq", type=Path)
    ambiguity.add_argument("template", type=Path)
    ambiguity.add_argument("output", type=Path)
    ambiguity.add_argument("--block", type=int, default=0)
    ambiguity.add_argument("--receiver", type=int, default=0)
    ambiguity.add_argument("--maximum-delay-samples", type=int, default=64)
    ambiguity.add_argument("--minimum-doppler-hz", type=float, default=-5_000)
    ambiguity.add_argument("--maximum-doppler-hz", type=float, default=5_000)
    ambiguity.add_argument("--doppler-step-hz", type=float, default=250)
    ambiguity.set_defaults(handler=command_doppler_ambiguity)
    tracker_summary = commands.add_parser("doppler-tracker-summary",
        help="aggregate tracker rates, runtime, and drift distributions over captures")
    tracker_summary.add_argument("output", type=Path)
    tracker_summary.add_argument("inputs", type=Path, nargs="+")
    tracker_summary.set_defaults(handler=command_doppler_tracker_summary)
    tle_match = commands.add_parser("doppler-tle-match",
        help="retrospectively compare a blind tracker report with an archived pass catalog")
    tle_match.add_argument("trackers", type=Path)
    tle_match.add_argument("passes", type=Path)
    tle_match.add_argument("output", type=Path)
    tle_match.set_defaults(handler=command_doppler_tle_match)
    dither_compare = commands.add_parser("starlink-dither-compare",
        help="classify paired tuning-center captures as sky- or baseband-fixed")
    dither_compare.add_argument("first", type=Path)
    dither_compare.add_argument("second", type=Path)
    dither_compare.add_argument("output", type=Path)
    dither_compare.add_argument("--smoothing-bins", type=float, default=20)
    dither_compare.add_argument("--edge-fraction", type=float, default=.1)
    dither_compare.add_argument("--exclude-dc-hz", type=float, default=300_000)
    dither_compare.set_defaults(handler=command_starlink_dither_compare)
    rf_baseline = commands.add_parser("starlink-rf-baseline",
        help="build a persistent absolute-RF spectrum from dithered captures")
    rf_baseline.add_argument("output", type=Path)
    rf_baseline.add_argument("measurements", type=Path, nargs="+")
    rf_baseline.set_defaults(handler=command_starlink_rf_baseline)
    rf_novelty = commands.add_parser("starlink-rf-novelty",
        help="subtract a persistent RF baseline and render novel activity")
    rf_novelty.add_argument("measurement", type=Path)
    rf_novelty.add_argument("baseline", type=Path)
    rf_novelty.add_argument("output", type=Path)
    rf_novelty.add_argument("--plot", type=Path)
    rf_novelty.add_argument("--threshold-db", type=float, default=.35)
    rf_novelty.add_argument("--trend-smoothing-bins", type=float, default=100)
    rf_novelty.add_argument("--integration-s", type=float, default=1)
    rf_novelty.set_defaults(handler=command_starlink_rf_novelty)
    wide_feature = commands.add_parser("starlink-wide-feature-analyze",
        help="track wide dual-RX channel features and compare their motion with TLEs")
    wide_feature.add_argument("measurement", type=Path)
    wide_feature.add_argument("baseline", type=Path)
    wide_feature.add_argument("output", type=Path)
    wide_feature.add_argument("--passes", type=Path)
    wide_feature.add_argument("--plot", type=Path)
    wide_feature.add_argument("--threshold-db", type=float, default=.35)
    wide_feature.add_argument("--integration-s", type=float, default=1)
    wide_feature.add_argument("--minimum-boundary-margin-s", type=float, default=3,
        help="reject finite-event claims censored by capture start or stop")
    wide_feature.set_defaults(handler=command_starlink_wide_feature)
    wide_population = commands.add_parser("starlink-wide-feature-summary",
        help="summarize qualified wide-feature reports into morphological populations")
    wide_population.add_argument("output", type=Path)
    wide_population.add_argument("inputs", type=Path, nargs="+",
        help="wide-feature JSON reports or directories containing them")
    wide_population.add_argument("--narrow-width-hz", type=float, default=45_000,
        help="maximum median instantaneous width for the narrow-swept family")
    wide_population.set_defaults(handler=command_starlink_wide_population)
    validated = commands.add_parser("validated-scan", help="center-shift validated receive-only IF scan")
    validated.add_argument("output_dir", type=Path); validated.add_argument("--start-hz", type=float, required=True)
    validated.add_argument("--stop-hz", type=float, required=True); validated.add_argument("--step-hz", type=float, required=True)
    validated.add_argument("--validation-offset-hz", type=float, default=200000)
    validated.add_argument("--sample-rate-hz", type=float, default=4000000); validated.add_argument("--bandwidth-hz", type=float, default=3000000)
    validated.add_argument("--gain-db", type=float, default=40); validated.add_argument("--channel", type=int, choices=(0,1), default=0)
    validated.add_argument("--samples-per-tuning", type=int, default=262144); validated.add_argument("--fft-size", type=int, default=16384)
    validated.add_argument("--settle-seconds", type=float, default=PROMOTION_MIN_SETTLE_SECONDS); validated.add_argument("--min-prominence-db", type=float, default=12)
    validated.add_argument("--repeats", type=int, default=1,
                           help="repeat the complete center list in acquisition order")
    validated.add_argument("--confirmations", type=int, default=PROMOTION_MIN_CONFIRMATIONS,
                           help="consecutive center-shift pairs required at each center")
    validated.add_argument("--frequency-tolerance-hz", type=float, default=2000); validated.add_argument("--max-features", type=int, default=16)
    validated.add_argument("--uri", default="pluto://ip:192.168.2.1"); validated.add_argument("--serial")
    validated.set_defaults(handler=command_validated_scan)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.handler(args))
    except (ValueError, RuntimeError, FileNotFoundError, ImportError) as exc:
        print(f"leo-radio: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__": raise SystemExit(main())
