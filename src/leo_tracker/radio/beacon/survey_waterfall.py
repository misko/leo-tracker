"""A picture of what the survey saw, kept after the samples are gone.

The survey scores eight tunings on two receivers and records the numbers, but
a number cannot show that a detection sat on the edge of the search, that a
whole band was lifted by interference, or that one port was saturating. The
IQ that would show it shares the capture's lifetime and retention removes
that within days.

So the picture is rendered once, while the samples are still in hand, and
written where retention does not reach. It is a record rather than a
diagnostic rerun: after the capture directory is gone this is the only
remaining evidence of what the receiver was looking at.

Time depth is deliberately shallow. Doppler drifts at roughly one to three
kilohertz per second, so across an 80 ms probe it moves less than a single
frequency bin — the axis that matters for drift is frequency span, not time.
The window is therefore wide in frequency, wide enough to hold the Doppler
excursion *and* an LNB that disagrees with its twin, and only deep enough in
time to show whether what was there stayed there.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

#: Half-width of the window about each tuning. Ku-band LEO Doppler stays
#: inside about +/-250 kHz and the two LNBs here disagree by about 440 kHz, so
#: a pilot can legitimately appear anywhere within this span. Narrower would
#: crop exactly the cases worth looking at.
DEFAULT_SPAN_HZ = 600_000.0

#: Time bins per panel. Drift within an 80 ms probe is sub-bin, so this shows
#: persistence rather than motion; it is finer than that argument requires
#: because the picture is the only surviving evidence and detail costs only
#: bytes, which are affordable here.
TIME_BINS = 156

#: Overlap between segments, as a fraction. Buys time resolution without
#: costing frequency resolution, which is the axis that carries the Doppler.
SEGMENT_OVERLAP = 0.5

#: Written truecolour. A 48-colour palette measured a quarter of the size and
#: loses nothing a reader would notice, and is the first thing to reach for if
#: these ever need to be smaller: roughly 2.5 MB each against 0.4 MB.
FIGURE_DPI = 120


def waterfall_paths(plots_root: Path, capture_name: str) -> tuple[Path, Path]:
    """Where the picture and its numbers live, keyed by capture name.

    Under ``plots/`` because that is what the dashboard already serves and
    what retention already leaves alone; named for the capture because the
    capture directory itself will not survive to be pointed at.
    """
    root = Path(plots_root)
    return (root / f"{capture_name}-survey.png",
            root / f"{capture_name}-survey.json")


#: Fallback when a record predates the configuration being written down. Every
#: probe taken before the survey was randomised was taken here, so it is the
#: right guess — and it is only ever a guess, which is why
#: :func:`record_sample_rate_hz` prefers what the record says.
LEGACY_SAMPLE_RATE_HZ = 2_500_000.0


def record_sample_rate_hz(record: dict,
                          fallback: float = LEGACY_SAMPLE_RATE_HZ) -> float:
    """The rate these samples were taken at, from the record that owns them.

    The frequency axis of every panel is scaled by this, and the survey is now
    drawn between 2.5 and 5 MS/s, so taking it from a constant would draw a
    5 MS/s probe on a 2.5 MS/s axis and label the result in kilohertz. Nothing
    in the picture would look wrong; every frequency in it would be half what
    it should be.
    """
    for value in ((record.get("capture_config") or {}).get("sample_rate_hz"),
                  record.get("sample_rate_hz")):
        if value:
            return float(value)
    return float(fallback)


def _panel_label(entry: dict, scored: dict, labels: list | None) -> str:
    """Everything the panel is evidence for, in one line.

    ``active`` is a tri-state now: true, false, or absent because the
    configuration this probe was taken in has no measured threshold. The label
    distinguishes the third from the second, because "not marked active" and
    "no bar exists to mark it against" are different pictures.
    """
    name = (labels[scored["receiver"]] if labels
            and scored["receiver"] < len(labels) else f"rx{scored['receiver']}")
    verdict = scored.get("active")
    return (f"ch{entry['channel']} {entry['region'].replace('-edge','')} · {name} · "
            f"p/med {scored['peak_to_median']:.2f} · "
            f"anch {scored.get('anchor_agreement', '-')}/"
            f"{scored.get('anchor_count', '-')} · "
            f"ctr {scored.get('offset_contrast', float('nan')):.2f}"
            f"{'  ACTIVE' if verdict else ''}"
            f"{'  (uncalibrated)' if verdict is None else ''}")


def render(samples: np.ndarray, record: dict, output: Path, *,
           sample_rate_hz: float | None = None,
           span_hz: float = DEFAULT_SPAN_HZ,
           receiver_labels: list | None = None,
           capture_name: str = "") -> dict:
    """Draw one panel per tuning per receiver and write the image.

    ``samples`` is the raw ci16 the survey collected, shaped
    ``(tuning, sample, receiver, component)`` and ordered as it was collected,
    which is why the tuning order is taken from ``sample_order`` rather than
    from the score-sorted results.

    The sample count per tuning is read off the array and the rate off the
    record; neither is assumed. Three of the four survey configurations hold a
    sample count this function was originally written not to expect.
    """
    if sample_rate_hz is None:
        sample_rate_hz = record_sample_rate_hz(record)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy import signal as sig

    values = np.asarray(samples)
    if values.ndim != 4 or values.shape[2:] != (2, 2):
        raise ValueError("survey IQ must be (tuning, sample, receiver, component)")
    order = record.get("sample_order") or [
        (entry["channel"], entry["region"]) for entry in record["tunings"]]
    by_tuning = {(entry["channel"], entry["region"]): entry
                 for entry in record["tunings"]}

    tunings, count = values.shape[0], values.shape[1]
    # The axis cannot claim more band than was sampled. At 2.5 MS/s Nyquist is
    # ±1.25 MHz and the default window fits inside it, so this changed nothing
    # while the rate was fixed; a caller asking for a window wider than the
    # drawn rate supports would otherwise get an extent that says 1 MHz over
    # data that stops at half the rate.
    span_hz = min(float(span_hz), float(sample_rate_hz) / 2.0)
    # Frequency resolution is what carries the Doppler, so the segment is
    # sized for it and time resolution is bought back with overlap instead.
    per_segment = max(64, 1 << int(np.log2(max(count // TIME_BINS, 64))))
    overlap = int(per_segment * SEGMENT_OVERLAP)
    # Every panel is computed before any is drawn, so they can share one
    # colour scale. Autoscaling each panel separately is actively misleading
    # here: the panel holding a signal rescales around it and stops being
    # comparable with the seven quiet ones, which is the comparison the
    # picture exists to make.
    panels, span_ms = [], 1.0
    for index in range(tunings):
        for receiver in (0, 1):
            block = values[index, :, receiver, :]
            wave = (block[:, 0] + 1j * block[:, 1]).astype(np.complex64)
            freqs, times, power = sig.spectrogram(
                wave, fs=sample_rate_hz, nperseg=per_segment,
                noverlap=overlap, return_onesided=False)
            freqs = np.fft.fftshift(freqs)
            power = np.fft.fftshift(power, axes=0)
            keep = np.abs(freqs) <= span_hz
            panels.append(10 * np.log10(power[keep] + 1e-20))
            if times.size:
                span_ms = times[-1] * 1000
    stacked = np.concatenate([panel.ravel() for panel in panels])
    # Robust rather than min/max: one saturating sample would otherwise set
    # the ceiling for the whole survey and flatten everything else to nothing.
    floor, ceiling = np.percentile(stacked, [2.0, 99.95])
    if not ceiling > floor:
        floor, ceiling = float(stacked.min()), float(stacked.min()) + 1.0

    figure, axes = plt.subplots(tunings, 2, figsize=(9.0, 1.5 * tunings),
                                constrained_layout=True, squeeze=False)
    for index in range(tunings):
        key = tuple(order[index]) if index < len(order) else None
        entry = by_tuning.get(key)
        for receiver in (0, 1):
            axis = axes[index][receiver]
            image = axis.imshow(panels[index * 2 + receiver], aspect="auto",
                                origin="lower", interpolation="nearest",
                                vmin=floor, vmax=ceiling,
                                extent=[0, span_ms,
                                        -span_hz / 1e3, span_hz / 1e3])
            axis.tick_params(labelsize=5)
            if entry is not None:
                scored = next((item for item in entry["receivers"]
                               if item["receiver"] == receiver), None)
                if scored is not None:
                    axis.set_title(_panel_label(entry, scored, receiver_labels),
                                   fontsize=5.5,
                                   color=("#b00020" if scored.get("active")
                                          else "#333333"))
            if index == tunings - 1:
                axis.set_xlabel("ms", fontsize=5)
            if receiver == 0:
                axis.set_ylabel("kHz", fontsize=5)
    bar = figure.colorbar(image, ax=axes, location="right", shrink=0.35,
                          pad=0.01)
    bar.set_label("dB, one scale for the whole survey", fontsize=6)
    bar.ax.tick_params(labelsize=5)
    figure.suptitle(
        f"pre-dwell survey · {capture_name}" if capture_name else "pre-dwell survey",
        fontsize=7)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(".png.partial")
    # Format stated rather than inferred: the staged name ends in .partial, and
    # inference would refuse it.
    figure.savefig(partial, dpi=FIGURE_DPI, format="png")
    plt.close(figure)
    os.replace(partial, output)
    return {"path": output.name, "bytes": output.stat().st_size,
            "span_hz": span_hz, "segment_samples": int(per_segment),
            "sample_rate_hz": float(sample_rate_hz),
            "samples_per_tuning": int(count)}


def write(samples, record: dict, plots_root: Path, capture_name: str, *,
          sample_rate_hz: float | None = None,
          receiver_labels: list | None = None) -> dict:
    """Write the picture and the full metric record beside it.

    The numbers travel inside the capture manifest too, but that copy dies
    with the capture directory. This one is written where the picture is, so
    a panel can always be read against the statistics that produced it.

    Never raises. A capture without a picture is a capture; a capture that
    failed because of one is a recording that will not happen again.
    """
    image_path, metrics_path = waterfall_paths(plots_root, capture_name)
    outcome = {"state": "complete"}
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        payload["capture"] = capture_name
        temporary = metrics_path.with_suffix(".json.partial")
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=1))
        os.replace(temporary, metrics_path)
        outcome["metrics"] = metrics_path.name
        if samples is not None:
            outcome.update(render(
                samples, record, image_path, sample_rate_hz=sample_rate_hz,
                receiver_labels=receiver_labels, capture_name=capture_name))
    except Exception as exc:                           # noqa: BLE001 - fail open
        return {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return outcome
