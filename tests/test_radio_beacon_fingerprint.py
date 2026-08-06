import json
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

from leo_tracker.radio.cli import main
from leo_tracker.radio.beacon.fingerprint import (
    FINGERPRINT_SCHEMA, INDEX_SCHEMA, compare_fingerprints,
    fingerprint_decode, render_fingerprint_svg, update_fingerprint_store)


def _probabilities(states, confidence=.97):
    states = np.asarray(states)
    values = np.full(states.shape + (4,), (1 - confidence) / 3, np.float32)
    np.put_along_axis(values, states[..., None], confidence, axis=-1)
    return values


def _write_decode(root: Path, name: str, states: np.ndarray, *, phase_slope=.1,
                  confidence=.97):
    decoded = root / "reports" / "decoded"
    followups = root / "reports" / "followups"
    decoded.mkdir(parents=True, exist_ok=True); followups.mkdir(parents=True, exist_ok=True)
    pilot = _probabilities(states, confidence)
    sss_states = np.arange(8) % 4
    sss = np.repeat(_probabilities(sss_states, confidence)[None, ...], 6, axis=0)
    x = np.arange(8)
    channel0 = np.linspace(.7, 1.2, 8) * np.exp(1j * (phase_slope * x + .03 * x ** 2))
    channel1 = np.linspace(1.1, .8, 8) * np.exp(1j * (-phase_slope * x + .02 * x ** 2))
    symbols = decoded / f"{name}.npz"
    np.savez_compressed(symbols, combined_pilot_probabilities=pilot,
        combined_sss_probabilities=sss, rx0_channel=channel0, rx1_channel=channel1)
    report = {"schema": "leo-tracker.starlink-edge-decode/v1",
        "decoder_revision": 2, "created_utc": "2026-08-05T20:00:00Z",
        "selected_observation": {"start_s": 3.2},
        "capture_parameters": {"rf_center_hz": 11_459_687_500,
            "sample_rate_hz": 2_500_000, "observation_mode": "narrow"},
        "waveform": {"edge": "lower"},
        "receivers": [{"carrier_offset_hz": -220_000,
            "residual_cfo_refinement_hz": 20, "pss": {"peak_to_median": 2.7}},
            {"carrier_offset_hz": -218_000, "residual_cfo_refinement_hz": -15,
             "pss": {"peak_to_median": 2.5}}],
        "combined": {"soft_dual_rx": {"pilot": {"hard_symbol_accuracy": .93,
            "soft_mean_confidence": confidence, "soft_mean_entropy_bits": .1},
            "sss": {"hard_symbol_accuracy": .75,
                    "soft_mean_confidence": confidence}}},
        "symbol_archive_sha256": name + "-hash"}
    decode_path = decoded / f"{name}.json"
    decode_path.write_text(json.dumps(report))
    (followups / f"{name}.json").write_text(json.dumps({
        "confirmation": {"confirmed": True,
            "dual_receiver_links": [{"drift_hz_s": -31_000}]},
        "overlapping_passes": [{"name": "STARLINK-1", "norad_id": 12345}]}))
    return decode_path, symbols


def test_fingerprint_similarity_separates_repeated_code_from_chance(tmp_path):
    states = (np.arange(2400) % 4).reshape(300, 8)
    a_decode, a_symbols = _write_decode(tmp_path, "a", states, phase_slope=.1)
    b_decode, b_symbols = _write_decode(tmp_path, "b", states, phase_slope=.8)
    c_decode, c_symbols = _write_decode(tmp_path, "c", (states + 1) % 4)
    followups = tmp_path / "reports" / "followups"
    a = fingerprint_decode(a_decode, a_symbols, tmp_path / "a.json",
                           followup_path=followups / "a.json")
    b = fingerprint_decode(b_decode, b_symbols, tmp_path / "b.json",
                           followup_path=followups / "b.json")
    c = fingerprint_decode(c_decode, c_symbols, tmp_path / "c.json",
                           followup_path=followups / "c.json")

    same = compare_fingerprints(a, b)
    different = compare_fingerprints(a, c)
    assert same["waveform_family_similarity"] == 1
    assert same["family_link"]
    # Linear channel phase is removed before comparison.
    assert same["conditional_channel_similarity"] > .99
    assert same["shared_overlapping_norad_ids"] == [12345]
    assert same["minimum_confirmed_drift_difference_hz_s"] == 0
    assert different["waveform_family_similarity"] < .11
    assert not different["family_link"]
    assert not same["satellite_identity_claim"]


def test_fingerprint_cli_backfills_store_and_builds_clusters(tmp_path):
    states = (np.arange(2400) % 4).reshape(300, 8)
    _write_decode(tmp_path, "capture-a", states)
    _write_decode(tmp_path, "capture-b", states)

    assert main(["starlink-beacon-fingerprint", str(tmp_path)]) == 0
    fingerprints = tmp_path / "reports" / "fingerprints"
    saved = json.loads((fingerprints / "capture-a.json").read_text())
    index = json.loads((fingerprints / "index.json").read_text())
    assert saved["schema"] == FINGERPRINT_SCHEMA
    assert saved["trajectory_context"]["confirmed_drift_hz_s"] == [-31_000]
    assert not saved["interpretation"]["satellite_identity_claim"]
    assert index["schema"] == INDEX_SCHEMA
    assert index["fingerprint_count"] == 2
    assert index["clusters"][0]["member_count"] == 2
    nearest = index["nearest_matches"]["capture-a"][0]
    assert nearest["capture_name"] == "capture-b"
    assert nearest["waveform_family_similarity"] == 1

    svg = render_fingerprint_svg(index)
    assert ElementTree.fromstring(svg).tag.endswith("svg")
    assert b"Nearest-neighbor fingerprint evidence map" in svg
    assert b"family-link threshold" in svg
    assert b"capture-a" in svg
    assert b"not satellite identity" in svg

    reused = update_fingerprint_store(tmp_path)
    assert reused["written"] == []
    assert reused["reused_count"] == 2


def test_fingerprint_accepts_legacy_hard_symbol_archive(tmp_path):
    states = (np.arange(2400) % 4).reshape(300, 8)
    decode_path, symbols = _write_decode(tmp_path, "legacy", states)
    pilot_constellation = np.exp(1j * np.pi / 2 * (states + .5))
    sss_states = np.arange(8) % 4
    sss = np.repeat(np.exp(1j * np.pi / 2 * sss_states)[None, :], 6, axis=0)
    channel = np.ones(8, np.complex64)
    np.savez_compressed(symbols, rx0_pilot_equalized=pilot_constellation,
        rx1_pilot_equalized=pilot_constellation, rx0_sss_equalized=sss,
        rx1_sss_equalized=sss, rx0_channel=channel, rx1_channel=channel)

    saved = fingerprint_decode(decode_path, symbols, tmp_path / "legacy.json")

    assert saved["waveform_signature"]["extraction_mode"] == "legacy_hard_dual_rx"
    assert saved["waveform_signature"]["pilot_state_count"] == 2400
