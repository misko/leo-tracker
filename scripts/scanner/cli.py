"""Command line entry point: scan N frequencies at specified bandwidths.

    scripts/scanner/scan.py --uri usb:1.90.5 --dwell-ms 1 \
        --point 2401e6:1e6 --point 2402.5e6:200e3 --json out.json

    scripts/scanner/scan.py --sweep 2400e6:2450e6:1e6 --dwell-ms 0.5 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .execute import DEFAULT_FFT_SIZE, FakeScanRadio, execute_scan
from .plan import DEFAULT_USABLE_FRACTION, ScanPoint, plan_scan


def _hz(text: str) -> float:
    return float(text.strip())


def _parse_point(text: str) -> ScanPoint:
    if ":" not in text:
        raise argparse.ArgumentTypeError("--point wants CENTER_HZ:BANDWIDTH_HZ")
    center, bandwidth = text.split(":", 1)
    return ScanPoint(center_hz=_hz(center), bandwidth_hz=_hz(bandwidth))


def _parse_sweep(text: str) -> list[ScanPoint]:
    parts = text.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("--sweep wants START_HZ:STOP_HZ:BANDWIDTH_HZ")
    start, stop, bandwidth = (_hz(p) for p in parts)
    if stop <= start or bandwidth <= 0:
        raise argparse.ArgumentTypeError("--sweep needs stop > start and a positive bandwidth")
    points, centre = [], start + bandwidth / 2
    while centre - bandwidth / 2 < stop:
        points.append(ScanPoint(center_hz=centre, bandwidth_hz=bandwidth))
        centre += bandwidth
    return points


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uri", help="libiio URI, e.g. usb:1.90.5 or ip:192.168.1.165")
    parser.add_argument("--serial", help="assert the radio's serial; addresses move, serials do not")
    parser.add_argument("--point", action="append", type=_parse_point, default=[],
                        metavar="CENTER:BW", help="repeatable")
    parser.add_argument("--sweep", type=_parse_sweep, metavar="START:STOP:BW",
                        help="contiguous points covering a range")
    parser.add_argument("--sample-rate", type=_hz, default=30e6)
    parser.add_argument("--gain-db", type=float, default=41.0)
    parser.add_argument("--dwell-ms", type=float, default=0.0)
    parser.add_argument("--fft-size", type=int, default=DEFAULT_FFT_SIZE)
    parser.add_argument("--usable-fraction", type=float, default=DEFAULT_USABLE_FRACTION)
    parser.add_argument("--fastlock", action="store_true",
                        help="store up to 8 tunings as fastlock profiles; only pays off "
                             "for repeated tunings with dwell under ~2 ms")
    parser.add_argument("--calibrate-rssi", action="store_true",
                        help="measure the RSSI-to-FFT offset so both paths share a scale")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only, or run against the synthetic radio when --tones given")
    parser.add_argument("--tones", type=_hz, nargs="*", default=None,
                        help="dry-run only: synthetic tone frequencies")
    parser.add_argument("--json", type=Path, help="write the full report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    points = list(args.point) + list(args.sweep or [])
    if not points:
        print("nothing to scan: pass --point or --sweep", file=sys.stderr)
        return 2

    try:
        plan = plan_scan(points, sample_rate_hz=args.sample_rate,
                         usable_fraction=args.usable_fraction)
    except ValueError as exc:
        print(f"cannot plan this scan: {exc}", file=sys.stderr)
        return 2
    dwell_s = args.dwell_ms / 1000.0
    print(f"{len(plan.points)} points -> {plan.tunings} tunings "
          f"({plan.metadata['grouping_saving']} saved), mode {plan.metadata['measure_mode']}, "
          f"analog BW {plan.analog_bandwidth_hz/1e6:.3f} MHz set once")
    print(f"predicted {plan.estimated_seconds(dwell_s)*1e3:.1f} ms")

    if args.dry_run and not args.uri:
        radio = FakeScanRadio(args.tones or (), gain_db=args.gain_db)
    elif args.uri:
        from .pluto import PlutoScanRadio
        radio = PlutoScanRadio(args.uri, gain_db=args.gain_db, expect_serial=args.serial)
        if args.fastlock:
            stored = radio.prepare_fastlock([g.tune_hz for g in plan.groups])
            print(f"fastlock profiles stored: {len(stored)} of {plan.tunings} tunings")
        if args.calibrate_rssi:
            radio.configure(sample_rate_hz=plan.sample_rate_hz,
                            analog_bandwidth_hz=plan.analog_bandwidth_hz)
            radio.tune(plan.groups[0].tune_hz)
            print(f"rssi offset {radio.calibrate_rssi_offset():+.2f} dB")
    else:
        print("pass --uri for hardware, or --dry-run for the synthetic radio", file=sys.stderr)
        return 2

    report = execute_scan(radio, plan, dwell_s=dwell_s, fft_size=args.fft_size)
    print(f"\nmeasured {report.elapsed_s*1e3:.1f} ms  "
          f"{report.points_per_second:.0f} points/s  "
          f"per-tune p50 {report.per_tune_s[0]*1e3:.3f} ms")
    flags = report.metadata
    if flags["clipped_points"] or flags["floor_points"]:
        print(f"  {flags['clipped_points']} clipped, {flags['floor_points']} at the floor")
    print(f"\n{'center MHz':>12} {'BW kHz':>9} {'dBFS':>9} {'flags':>18}")
    for result in report.results:
        marks = ",".join(m for m, on in (("clipped", result.clipped),
                                         ("floor", result.below_floor),
                                         ("edge", result.partially_out_of_span)) if on) or "-"
        print(f"{result.center_hz/1e6:>12.4f} {result.bandwidth_hz/1e3:>9.1f} "
              f"{result.power_dbfs:>9.2f} {marks:>18}")

    if args.json:
        payload = {
            "plan": {"sample_rate_hz": plan.sample_rate_hz,
                     "analog_bandwidth_hz": plan.analog_bandwidth_hz,
                     "usable_span_hz": plan.usable_span_hz, **plan.metadata},
            "timing": {"elapsed_s": report.elapsed_s,
                       "points_per_second": report.points_per_second,
                       "per_tune_p50_s": report.per_tune_s[0],
                       "per_tune_p95_s": report.per_tune_s[1], **report.metadata},
            "radio": getattr(radio, "identity", lambda: {"synthetic": True})(),
            "results": [asdict(r) for r in report.results],
        }
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
