"""Reproducible measured-versus-predicted Doppler field report."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np

from leo_tracker.fusion import fit_doppler_track
from leo_tracker.orbit.artifacts import TLECatalogArtifact, parse_catalog
from leo_tracker.orbit.doppler import predicted_doppler_hz
from leo_tracker.orbit.propagation import propagate_ecef
from leo_tracker.orbit.topocentric import Observer, look_angle
from leo_tracker.radio.artifact import CaptureArtifact


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("capture start must carry a UTC offset")
    return result.astimezone(timezone.utc)


def _expected(tle, observer, times, carrier_hz):
    values = []
    geometry = []
    for timestamp in times:
        look = look_angle(observer, propagate_ecef(tle, timestamp))
        values.append(predicted_doppler_hz(carrier_hz, look.range_rate_km_s))
        geometry.append(look)
    return np.asarray(values), geometry


def _waterfall(samples, rate, fft_size, hop):
    window = np.hanning(fft_size)
    starts = range(0, samples.size - fft_size + 1, hop)
    image = np.empty((len(starts), fft_size), dtype=np.float32)
    for row, index in enumerate(starts):
        image[row] = 20 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(
            samples[index:index + fft_size] * window))) + 1e-12)
    # Fixed receiver/LNB features dominate an absolute Ku-band IF waterfall.
    # Per-bin temporal centering preserves movers and bursts while removing
    # stationary lines; the report labels this transform explicitly.
    image -= np.median(image, axis=0, keepdims=True)
    return image


def build_report(args) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    capture = CaptureArtifact.open(args.capture, verify=True)
    ridge = json.loads(Path(args.ridge).read_text())
    catalog = TLECatalogArtifact.read(args.catalog)
    tles = parse_catalog(catalog.content, source=catalog.source_url,
                         retrieved_at=catalog.retrieved_at)
    tle = next((item for item in tles if item.norad_id == args.norad_id), None)
    if tle is None:
        raise ValueError(f"NORAD {args.norad_id} is absent from the frozen catalog")
    observer = Observer(args.lat, args.lon, args.alt_m)
    start = _utc(args.capture_start)
    points = ridge["points"]
    seconds = np.asarray([item["time_s"] for item in points])
    timestamps = [start + timedelta(seconds=float(value)) for value in seconds]
    is_comb = "center_frequency_hz" in points[0]
    frequency_key = "center_frequency_hz" if is_comb else "frequency_hz"
    observed = np.asarray([item[frequency_key] for item in points])
    default_uncertainty = capture.manifest["radio_config"]["sample_rate_hz"] / ridge.get("fft_size", 8192)
    uncertainty = np.asarray([max(item.get("uncertainty_hz", default_uncertainty), 1e-6)
                              for item in points])
    expected, geometry = _expected(tle, observer, timestamps, args.carrier_hz)
    shifted, _ = _expected(tle, observer,
                           [item + timedelta(seconds=args.control_shift_s) for item in timestamps],
                           args.carrier_hz)
    fit = fit_doppler_track(seconds, observed, expected, uncertainty)
    control = fit_doppler_track(seconds, observed, shifted, uncertainty)
    centered = seconds - np.mean(seconds)
    fitted_expected = expected + fit.frequency_offset_hz + fit.frequency_drift_hz_s * centered

    snr_key = "comb_z_score" if is_comb else "snr_db"
    snr = np.asarray([item[snr_key] for item in points])
    low_snr_fraction = float(np.mean(
        snr < args.detection_snr_db if is_comb else
        ["low_snr" in item.get("flags", []) for item in points]
    ))
    median_snr = float(np.median(snr))
    tone_support = (float(np.median([item["positive_tone_fraction"] for item in points]))
                    if is_comb else None)
    detected = bool(median_snr >= args.detection_snr_db
                    and low_snr_fraction <= 0.2
                    and fit.residual_rms_hz < 0.5 * control.residual_rms_hz
                    and (tone_support is None or tone_support >= args.minimum_tone_fraction))

    samples = capture.load_samples(mmap=True)
    image = _waterfall(samples, capture.manifest["radio_config"]["sample_rate_hz"],
                       args.fft_size, args.hop_size)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    plot_path = output / "waterfall_expected_overlay.png"
    extent = [-capture.manifest["radio_config"]["sample_rate_hz"] / 2 / 1e3,
              capture.manifest["radio_config"]["sample_rate_hz"] / 2 / 1e3,
              samples.size / capture.manifest["radio_config"]["sample_rate_hz"], 0]
    figure, axis = plt.subplots(figsize=(11, 5.5), constrained_layout=True)
    rendered = axis.imshow(image, aspect="auto", extent=extent, cmap="viridis",
                           vmin=np.percentile(image, 5), vmax=np.percentile(image, 99.5))
    axis.plot(observed / 1e3, seconds, color="white", linewidth=.8, alpha=.75,
              label="blind comb center" if is_comb else "blind strongest ridge")
    axis.plot(fitted_expected / 1e3, seconds, color="red", linewidth=1.5,
              label="SGP4 Doppler + fitted offset/drift")
    axis.set(xlabel="Baseband frequency (kHz)", ylabel="Seconds from capture start",
             title=f"Pluto+ capture vs expected Doppler: {tle.name.strip()} / {tle.norad_id}")
    axis.legend(loc="upper right")
    figure.colorbar(rendered, ax=axis, label="Magnitude relative to each bin's time median (dB)")
    figure.savefig(plot_path, dpi=160)
    plt.close(figure)

    result = {
        "detected": detected,
        "median_snr_db": median_snr,
        "low_snr_fraction": low_snr_fraction,
        "correct_rms_hz": fit.residual_rms_hz,
        "shifted_control_rms_hz": control.residual_rms_hz,
        "frequency_offset_hz": fit.frequency_offset_hz,
        "frequency_drift_hz_s": fit.frequency_drift_hz_s,
        "track_kind": "nine-tone comb" if is_comb else "strongest-bin ridge",
        "median_positive_tone_fraction": tone_support,
        "peak_elevation_deg": max(item.elevation_deg for item in geometry),
        "catalog_sha256": catalog.sha256,
        "capture_sha256": capture.manifest["files"]["iq.c64"]["sha256"],
    }
    metrics_path = output / "metrics.json"
    metrics_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    verdict = "PASS — candidate Starlink Doppler detected" if detected else (
        "FAIL — this capture does not establish a Starlink Doppler detection")
    report_path = output / "REPORT.md"
    extraction_description = (
        f"blind {ridge.get('tone_count', 9)}-tone comb, "
        f"spacing {ridge.get('tone_spacing_hz', 43949.5)} Hz, "
        f"integration {ridge.get('integration_s', 1)} s"
        if is_comb else
        f"blind ridge, FFT {ridge['fft_size']}, hop {ridge['hop_size']}"
    )
    report_path.write_text(f"""# Starlink Doppler field attempt

## Verdict

**{verdict}.** The detector gate requires median ridge SNR ≥ {args.detection_snr_db:.1f} dB,
no more than 20% low-SNR points, and correct-geometry RMS less than half the
{args.control_shift_s:.0f}-second shifted control RMS.

![Measured waterfall with expected Doppler overlay](waterfall_expected_overlay.png)

## Observation

- Site: `{args.lat:.7f}, {args.lon:.7f}`, altitude `{args.alt_m:.1f} m`
- Capture start: `{start.isoformat()}`
- Satellite hypothesis: `{tle.name.strip()}` / NORAD `{tle.norad_id}`
- TLE epoch: `{tle.epoch.isoformat()}`
- Predicted peak elevation during capture: `{result['peak_elevation_deg']:.2f}°`
- Geometric carrier: `{args.carrier_hz:.0f} Hz`
- Pluto center frequency: `{capture.manifest['radio_config']['center_frequency_hz']:.0f} Hz`
- Sample rate: `{capture.manifest['radio_config']['sample_rate_hz']:.0f} samples/s`
- Manual gain: `{capture.manifest['radio_config']['gain_db']} dB`
- Pluto serial: `{capture.manifest['radio_identity'].get('serial')}`

## Results

- Median blind-ridge SNR: `{median_snr:.2f} dB`
- Track kind: `{result['track_kind']}`
- Median positive-tone fraction: `{tone_support if tone_support is not None else 'not applicable'}`
- Low-SNR fraction: `{low_snr_fraction:.3f}`
- Correct-geometry residual RMS: `{fit.residual_rms_hz:.2f} Hz`
- Shifted-control residual RMS: `{control.residual_rms_hz:.2f} Hz`
- Fitted receiver/LNB offset: `{fit.frequency_offset_hz:.2f} Hz`
- Fitted linear drift: `{fit.frequency_drift_hz_s:.4f} Hz/s`

The frequency offset and drift are nuisance parameters. They cannot by
themselves identify a satellite; the pass/fail decision depends on ridge
quality and discrimination against the shifted control.

## Provenance

- TLE catalog SHA-256: `{catalog.sha256}`
- TLE source: `{catalog.source_url}`
- TLE retrieval: `{catalog.retrieved_at.isoformat()}`
- IQ SHA-256: `{result['capture_sha256']}`
- Capture ID: `{capture.manifest['capture_id']}`
- Measurement extraction: {extraction_description}
- Waterfall display: each frequency bin's temporal median subtracted to suppress stationary spurs

## Limitations

- The LNB/downconverter LO and RF connection were not electronically
  discoverable; the selected IF is therefore a stated hardware hypothesis.
- A single receiver and obstructed view cannot prove satellite identity
  without repeat passes and negative controls.
- `ITRF_APPROX` uses UTC as UT1 and omits polar motion.
- A failed gate is retained as a useful null experiment and is not relabelled
  as a Starlink observation.

## Reproduction

```bash
uv run --active --no-sync leo-radio verify {args.capture}
uv run --active --no-sync leo-report \\
  --catalog {args.catalog} --norad-id {args.norad_id} \\
  --capture {args.capture} --ridge {args.ridge} \\
  --capture-start {args.capture_start} --lat {args.lat} --lon {args.lon} \\
  --alt-m {args.alt_m} --carrier-hz {args.carrier_hz} \\
  --output-dir {args.output_dir}
```
""", encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leo-report")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--norad-id", type=int, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--ridge", type=Path, required=True)
    parser.add_argument("--capture-start", required=True)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--alt-m", type=float, default=0.0)
    parser.add_argument("--carrier-hz", type=float, required=True)
    parser.add_argument("--control-shift-s", type=float, default=60.0)
    parser.add_argument("--detection-snr-db", type=float, default=10.0)
    parser.add_argument("--minimum-tone-fraction", type=float, default=0.7)
    parser.add_argument("--fft-size", type=int, default=32768)
    parser.add_argument("--hop-size", type=int, default=262144)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_report(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
