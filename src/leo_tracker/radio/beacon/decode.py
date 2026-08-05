"""Narrowband demodulation of published Starlink edge pilots and SSS slices.

The PSS/SSS waveform and frame geometry are from Humphreys et al., *Signal
Structure of the Starlink Ku-Band Downlink*, IEEE TAES 2023.  Edge-pilot codes
are from Qin et al., *Pilots and Other Predictable Elements of the Starlink
Ku-Band Downlink*, 2026.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np

from .artifact import BeaconCapture
from .channels import (STARLINK_EDGE_PILOT_SUBCARRIERS,
                       STARLINK_SUBCARRIER_SPACING_HZ,
                       starlink_edge_pilot_offset_hz, subcarrier_offset_hz)
from .pilots import (CYCLIC_PREFIX_DURATION_S, OFDM_SYMBOL_DURATION_S,
                     edge_pilot_symbols)
from .structure import STARLINK_FRAME_DURATION_S, STARLINK_FRAME_RATE_HZ

DECODE_SCHEMA = "leo-tracker.starlink-edge-decode/v1"

# Humphreys et al. (2023), equations 38--40.  The least-significant base-4
# digit is s_2; each subsequent two-bit digit is the next OFDM subcarrier.
SSS_HEX = (
    "BD565D5064E9B3A94958F28624DED560946199F5B40F0E4FB5EFCB473B4C24"
    "B2D1E0BD01A6A04D5017DE91A8ECC0DA09EBFE57F9F1B44C532F161C583A4249"
    "0A5C09F2A117F9A28F9B2FD547A74C44BABB4BE85DA6A62B1235E2AD084C0018"
    "0142A8F7F357DEC4F31316BC58FA404909A3FCA7F88E421902B6A2580AE80308"
    "03F65809DB347F590DBC46F010EBE3A25C060D74429FC46BDF9B63719279798D"
    "232C5ABA274122FF66AD7E449F44CB40C49C24A1E2629F5BFE82CE531FDC34F8"
    "C64A43A963F40D5B71BDE6FB2F13492D6F2E8544B21D449722C635180342CD00"
    "26A1E7F7E80E91B175E852F919767E5AF9B6E909AF362F5218E2B908DC005803"
)


def sss_phase_states(indexes: tuple[int, ...] | None = None) -> np.ndarray:
    """Return published SSS phase states s_k in {0,1,2,3}."""
    selected = tuple(range(2, 1022)) if indexes is None else tuple(indexes)
    if not selected or any(index < 2 or index > 1021 for index in selected):
        raise ValueError("SSS subcarrier indexes must lie in 2..1021")
    encoded = int(SSS_HEX, 16)
    return np.asarray([(encoded >> (2 * (index - 2))) & 3
                       for index in selected], dtype=np.int8)


def sss_edge_symbols(edge: str = "lower") -> np.ndarray:
    """Return the eight known SSS coefficients in one edge-pilot band."""
    try:
        indexes = STARLINK_EDGE_PILOT_SUBCARRIERS[edge]
    except KeyError as exc:
        raise ValueError("edge must be lower or upper") from exc
    return np.asarray(np.exp(1j * np.pi / 2 * sss_phase_states(indexes)),
                      np.complex64)


def _constellation_states(values: np.ndarray, *, rotation_quarters: float) -> np.ndarray:
    constellation = np.exp(1j * np.pi / 2 *
                           (np.arange(4, dtype=float) + rotation_quarters))
    return np.argmin(np.abs(np.asarray(values)[..., None] - constellation), axis=-1)


def _edge_frequencies(edge: str) -> tuple[np.ndarray, np.ndarray]:
    try:
        indexes = np.asarray(STARLINK_EDGE_PILOT_SUBCARRIERS[edge], dtype=int)
    except KeyError as exc:
        raise ValueError("edge must be lower or upper") from exc
    center = starlink_edge_pilot_offset_hz(edge)
    frequencies = np.asarray([subcarrier_offset_hz(int(index)) - center
                              for index in indexes], dtype=float)
    return indexes, frequencies


def _demodulate_symbol(values: np.ndarray, sample_rate_hz: float,
                       frame_start: int, symbol_index: int,
                       frequencies_hz: np.ndarray) -> np.ndarray:
    start = frame_start + round(symbol_index * sample_rate_hz * OFDM_SYMBOL_DURATION_S)
    stop = frame_start + round((symbol_index + 1) * sample_rate_hz *
                               OFDM_SYMBOL_DURATION_S)
    if start < 0 or stop > values.size or stop - start < frequencies_hz.size:
        raise ValueError("OFDM symbol lies outside the available window")
    local_time_s = ((np.arange(start, stop) - frame_start) / sample_rate_hz -
                    symbol_index * OFDM_SYMBOL_DURATION_S)
    design = np.exp(2j * np.pi *
                    (local_time_s[:, None] - CYCLIC_PREFIX_DURATION_S) *
                    frequencies_hz[None, :]) / np.sqrt(frequencies_hz.size)
    return np.asarray(np.linalg.lstsq(design, values[start:stop], rcond=None)[0],
                      np.complex64)


def _metric_summary(equalized: np.ndarray, expected: np.ndarray, *,
                    rotation_quarters: float) -> tuple[dict, np.ndarray]:
    expected_values = np.broadcast_to(expected, equalized.shape)
    decoded = _constellation_states(equalized, rotation_quarters=rotation_quarters)
    known = _constellation_states(expected_values, rotation_quarters=rotation_quarters)
    correct = decoded == known
    error = equalized - expected_values
    return ({"observation_count": int(equalized.size),
             "hard_symbol_accuracy": float(np.mean(correct)),
             "random_chance_accuracy": .25,
             "rms_evm": float(np.sqrt(np.mean(np.abs(error) ** 2))),
             "median_equalized_magnitude": float(np.median(np.abs(equalized)))},
            correct)


def _sss_decode(coefficients: np.ndarray, frame_phase: np.ndarray,
                pilot_channel: np.ndarray, expected: np.ndarray
                ) -> tuple[dict, dict[str, np.ndarray]]:
    if coefficients.shape != (frame_phase.size, 8):
        raise ValueError("SSS coefficients must have shape frame,8")
    normalized = (coefficients * np.exp(-1j * frame_phase)[:, None] /
                  np.where(np.abs(pilot_channel) > 1e-20,
                           pilot_channel, np.complex64(1))[None, :])
    equalized = np.empty_like(normalized)
    frame_indexes = np.arange(frame_phase.size)
    if frame_phase.size < 2:
        equalized[:] = normalized
    else:
        for parity in range(2):
            training = frame_indexes % 2 != parity
            testing = ~training
            scale = np.mean(normalized[training] * np.conj(expected)[None, :])
            equalized[testing] = normalized[testing] / (
                scale if abs(scale) > 1e-20 else np.complex64(1))
    metrics, correct = _metric_summary(
        equalized, expected[None, :], rotation_quarters=0)
    metrics.update({"frame_count": int(frame_phase.size),
                    "per_subcarrier_accuracy": np.mean(correct, axis=0).tolist()})
    return metrics, {"expected": expected, "equalized": equalized,
                     "correct": correct}


def demodulate_edge_window(samples: np.ndarray, sample_rate_hz: float, *,
                           epoch_sample: int, carrier_offset_hz: float,
                           edge: str = "lower") -> tuple[dict, dict[str, np.ndarray]]:
    """Demodulate all complete pilot and narrow-SSS symbols in one IQ window."""
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1:
        raise ValueError("receiver samples must be one dimensional")
    indexes, frequencies = _edge_frequencies(edge)
    minimum_rate_hz = len(indexes) * \
        STARLINK_SUBCARRIER_SPACING_HZ
    if sample_rate_hz < minimum_rate_hz:
        raise ValueError(
            f"sample rate is too low for the eight edge subcarriers; "
            f"need at least {minimum_rate_hz:.0f} Hz")
    if epoch_sample < 0:
        raise ValueError("frame epoch must be nonnegative")
    time_indexes = np.arange(values.size, dtype=float)
    corrected = values * np.exp(-2j * np.pi * carrier_offset_hz *
                                time_indexes / sample_rate_hz)
    pilot_frames, sss_frames, starts = [], [], []
    frame = 0
    frame_content_samples = round(302 * sample_rate_hz * OFDM_SYMBOL_DURATION_S)
    while True:
        frame_start = epoch_sample + round(frame * sample_rate_hz *
                                           STARLINK_FRAME_DURATION_S)
        if frame_start + frame_content_samples > corrected.size:
            break
        sss_frames.append(_demodulate_symbol(
            corrected, sample_rate_hz, frame_start, 1, frequencies))
        pilot_frames.append([_demodulate_symbol(
            corrected, sample_rate_hz, frame_start, symbol, frequencies)
                             for symbol in range(2, 302)])
        starts.append(frame_start)
        frame += 1
    if not pilot_frames:
        raise ValueError("window contains no complete Starlink frame")
    pilots = np.asarray(pilot_frames, np.complex64)
    # The phase-code matrix differs between lower and upper pilot bands.
    expected_pilots = edge_pilot_symbols(edge)
    # _pilot_decode's channel estimator needs the selected edge's matrix.
    frame_match = np.sum(pilots * np.conj(expected_pilots)[None, :, :], axis=(1, 2))
    frame_phase = np.angle(frame_match)
    aligned = pilots * np.exp(-1j * frame_phase)[:, None, None]
    stacked = np.mean(aligned, axis=0)
    equalized = np.empty_like(stacked)
    symbol_indexes = np.arange(300)
    for parity in range(2):
        training = symbol_indexes % 2 != parity
        testing = ~training
        channel = np.mean(stacked[training] *
                          np.conj(expected_pilots[training]), axis=0)
        equalized[testing] = stacked[testing] / np.where(
            np.abs(channel) > 1e-20, channel, np.complex64(1))
    full_channel = np.mean(stacked * np.conj(expected_pilots), axis=0)
    modeled = (np.exp(1j * frame_phase)[:, None, None] *
               full_channel[None, None, :] * expected_pilots[None, :, :])
    residual = pilots - modeled
    pilot_metrics, pilot_correct = _metric_summary(
        equalized, expected_pilots, rotation_quarters=.5)
    pilot_metrics.update({"frame_count": len(pilot_frames),
        "stacking_gain_db": float(10 * np.log10(len(pilot_frames))),
        "model_snr_db": float(10 * np.log10(
            max(float(np.mean(np.abs(modeled) ** 2)), 1e-30) /
            max(float(np.mean(np.abs(residual) ** 2)), 1e-30))),
        "per_subcarrier_accuracy": np.mean(pilot_correct, axis=0).tolist(),
        "channel_magnitude": np.abs(full_channel).tolist(),
        "channel_phase_deg": np.rad2deg(np.angle(full_channel)).tolist(),
        "frame_phase_deg": np.rad2deg(frame_phase).tolist()})
    sss_expected = sss_edge_symbols(edge)
    sss_metrics, sss_arrays = _sss_decode(
        np.asarray(sss_frames), frame_phase, full_channel, sss_expected)
    arrays = {"pilot_expected": expected_pilots,
              "pilot_equalized": equalized,
              "pilot_stacked": stacked,
              "pilot_correct": pilot_correct,
              "sss_expected": sss_arrays["expected"],
              "sss_equalized": sss_arrays["equalized"],
              "sss_correct": sss_arrays["correct"],
              "channel": full_channel,
              "frame_phase": frame_phase,
              "frame_starts": np.asarray(starts, dtype=np.int64),
              "subcarrier_indexes": indexes,
              "subcarrier_frequencies_hz": frequencies}
    report = {"receiver_sample_count": int(values.size),
              "sample_rate_hz": float(sample_rate_hz),
              "epoch_sample": int(epoch_sample),
              "carrier_offset_hz": float(carrier_offset_hz),
              "pilot": pilot_metrics, "sss": sss_metrics}
    return report, arrays


def _best_check(checks: list[dict], time_s: float | None) -> dict:
    usable = [check for check in checks if len(check.get("receivers", [])) == 2]
    if not usable:
        raise ValueError("follow-up contains no paired receiver checks")
    if time_s is not None:
        return min(usable, key=lambda item: abs(float(item["start_s"]) - time_s))

    def rank(check: dict) -> tuple:
        match = [float(item.get("acquisition", {}).get("match_score_margin", 0))
                 for item in check["receivers"]]
        symbol = [float(item.get("pilot", {}).get("score_margin", 0))
                  for item in check["receivers"]]
        return (bool(check.get("qualified")), bool(check.get("candidate")),
                min(match), min(symbol), sum(match) + sum(symbol))
    return max(usable, key=rank)


def _json_ready(value):
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def decode_followup(capture_path: Path, followup_path: Path, output: Path, *,
                    time_s: float | None = None,
                    symbols_output: Path | None = None) -> tuple[dict, dict[str, np.ndarray]]:
    """Decode the strongest paired check in a confirmed dense follow-up."""
    capture_path, followup_path = Path(capture_path), Path(followup_path)
    capture = BeaconCapture.open(capture_path, verify=True)
    followup = json.loads(followup_path.read_text())
    check = _best_check(followup.get("checks", []), time_s)
    rate = float(capture.manifest["sample_rate_hz"])
    start_s, duration_s = float(check["start_s"]), float(check["duration_s"])
    first_sample, sample_count = round(start_s * rate), round(duration_s * rate)
    paired = capture.read_window(first_sample, sample_count)
    edge = str(check["receivers"][0].get("pilot", {}).get("edge", "lower"))
    receivers, arrays = [], {}
    for receiver in range(2):
        evidence = check["receivers"][receiver]
        decoded, receiver_arrays = demodulate_edge_window(
            paired[:, receiver], rate,
            epoch_sample=int(evidence["acquisition"]["selected_epoch_sample"]),
            carrier_offset_hz=float(evidence["pilot"]["frequency_offset_hz"]),
            edge=edge)
        decoded.update({"receiver": receiver,
                        "pss": evidence.get("pss", {}),
                        "exact_control_match_margin": evidence.get(
                            "acquisition", {}).get("match_score_margin"),
                        "pilot_control_margin": evidence.get(
                            "pilot", {}).get("score_margin")})
        receivers.append(decoded)
        arrays.update({f"rx{receiver}_{key}": value
                       for key, value in receiver_arrays.items()})
    indexes, frequencies = _edge_frequencies(edge)
    report = {"schema": DECODE_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "capture": str(capture_path.resolve()),
        "source_followup": str(followup_path.resolve()),
        "selected_observation": {"start_s": start_s, "duration_s": duration_s,
            "candidate": bool(check.get("candidate")),
            "qualified": bool(check.get("qualified")),
            "epoch_difference_samples": check.get("epoch_difference_samples")},
        "capture_parameters": {key: capture.manifest.get(key) for key in (
            "sample_rate_hz", "bandwidth_hz", "center_frequency_hz",
            "rf_center_hz", "gain_mode", "configured_gain_db")},
        "waveform": {"edge": edge, "frame_rate_hz": STARLINK_FRAME_RATE_HZ,
            "ofdm_symbol_duration_s": OFDM_SYMBOL_DURATION_S,
            "subcarrier_spacing_hz": STARLINK_SUBCARRIER_SPACING_HZ,
            "subcarrier_indexes": indexes.tolist(),
            "subcarrier_frequencies_hz": frequencies.tolist(),
            "decoded_pilot_symbols_per_frame": 300,
            "decoded_pilot_subcarriers": 8,
            "decoded_sss_subcarriers_per_frame": 8,
            "note": "edge pilots are predictable; these are waveform states, not user payload",
            "references": ["https://arxiv.org/abs/2210.11578",
                           "https://arxiv.org/abs/2602.02627"]},
        "receivers": receivers,
        "combined": {"minimum_pilot_accuracy": min(
            item["pilot"]["hard_symbol_accuracy"] for item in receivers),
            "minimum_sss_accuracy": min(
                item["sss"]["hard_symbol_accuracy"] for item in receivers),
            "minimum_frame_count": min(item["pilot"]["frame_count"]
                                       for item in receivers)},
        "limitations": [
            "2.5 MS/s contains one 1.875 MHz edge-pilot band, not the full 240 MHz channel",
            "pilot channel estimates are cross-fitted on opposite symbol parities",
            "SSS is only the eight-subcarrier narrowband slice and is lower SNR",
            "no Starlink header or user payload is decoded"]}
    if symbols_output is not None:
        symbols_output = Path(symbols_output)
        symbols_output.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(symbols_output, **arrays)
        report["symbol_archive"] = str(symbols_output.resolve())
        report["symbol_archive_bytes"] = symbols_output.stat().st_size
        report["symbol_archive_sha256"] = hashlib.sha256(
            symbols_output.read_bytes()).hexdigest()
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(_json_ready(report), indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    return report, arrays


def plot_decode_report(report: dict, arrays: dict[str, np.ndarray], output: Path) -> None:
    """Render constellations, held-out decisions, and channel measurements."""
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(3, 2, figsize=(14, 15), constrained_layout=True)
    pilot_ideal = np.exp(1j * np.pi / 2 * (np.arange(4) + .5))
    sss_ideal = np.exp(1j * np.pi / 2 * np.arange(4))
    colors = plt.get_cmap("tab10")
    for receiver in range(2):
        equalized = arrays[f"rx{receiver}_pilot_equalized"]
        expected = arrays[f"rx{receiver}_pilot_expected"]
        states = _constellation_states(expected, rotation_quarters=.5).ravel()
        axis = axes[0, receiver]
        for state in range(4):
            selected = equalized.ravel()[states == state]
            axis.scatter(selected.real, selected.imag, s=7, alpha=.28,
                         color=colors(state), label=f"state {state}")
        axis.scatter(pilot_ideal.real, pilot_ideal.imag, marker="x", s=90,
                     linewidth=2, color="black", label="ideal")
        metric = report["receivers"][receiver]["pilot"]
        axis.set(title=(f"RX{receiver} stacked edge-pilot constellation · "
                        f"accuracy {metric['hard_symbol_accuracy']:.1%}"),
                 xlabel="I", ylabel="Q", aspect="equal")
        axis.grid(alpha=.25); axis.legend(ncol=2, fontsize=8)

        correct = arrays[f"rx{receiver}_pilot_correct"].T
        axis = axes[1, receiver]
        axis.imshow(correct, origin="lower", aspect="auto", interpolation="nearest",
                    cmap="RdYlGn", vmin=0, vmax=1,
                    extent=(2, 302, -.5, 7.5))
        axis.set(title=f"RX{receiver} held-out pilot decisions",
                 xlabel="OFDM symbol index", ylabel="edge subcarrier position")

        sss = arrays[f"rx{receiver}_sss_equalized"]
        sss_expected = arrays[f"rx{receiver}_sss_expected"]
        sss_states = _constellation_states(sss_expected, rotation_quarters=0)
        axis = axes[2, receiver]
        for state in range(4):
            selected = sss[:, sss_states == state].ravel()
            axis.scatter(selected.real, selected.imag, s=22, alpha=.55,
                         color=colors(state), label=f"state {state}")
        axis.scatter(sss_ideal.real, sss_ideal.imag, marker="x", s=90,
                     linewidth=2, color="black", label="ideal")
        metric = report["receivers"][receiver]["sss"]
        axis.set(title=(f"RX{receiver} narrow SSS slice · "
                        f"accuracy {metric['hard_symbol_accuracy']:.1%}"),
                 xlabel="I", ylabel="Q", aspect="equal")
        axis.grid(alpha=.25); axis.legend(ncol=2, fontsize=8)
    observation = report["selected_observation"]
    capture = report["capture_parameters"]
    figure.suptitle(
        "Starlink narrowband waveform decode\n"
        f"RF {capture.get('rf_center_hz', 0)/1e9:.6f} GHz · "
        f"capture t={observation['start_s']:.3f} s · "
        f"{report['combined']['minimum_frame_count']} stacked frames · "
        f"pilot is known synchronization structure, not payload",
        fontsize=15)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=150); plt.close(figure)
