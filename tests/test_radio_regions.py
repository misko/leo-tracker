import json
from pathlib import Path

import numpy as np
import pytest

from leo_tracker.radio.regions import rank_regions, read_centers, write_region_plan


def _monitor(tmp_path: Path) -> Path:
    directory = tmp_path / "monitor"; directory.mkdir()
    rng = np.random.default_rng(4)
    psd = rng.normal(-60, .2, (3, 2, 3, 128)).astype(np.float32)
    psd[:, :, 1, 50:54] += 12  # repeatable dual-RX structure at center 200
    np.savez_compressed(directory / "spectra.npz", psd_db=psd,
                        centers_hz=np.array([100., 200., 300.]))
    report = {"schema": "leo-tracker.radio-monitor/v1", "spectra": "spectra.npz"}
    path = directory / "monitor.json"; path.write_text(json.dumps(report))
    return path


def test_rank_regions_prefers_repeatable_dual_rx_structure(tmp_path):
    report = _monitor(tmp_path)
    ranked = rank_regions(report, count=2)
    assert ranked[0]["center_frequency_hz"] == 200
    output = tmp_path / "regions.json"
    value = write_region_plan(report, output, count=1)
    assert read_centers(output) == [200]
    assert value["schema"] == "leo-tracker.radio-regions/v1"
    assert rank_regions(report, count=1, offset=1)[0]["center_frequency_hz"] != 200


def test_region_plan_validation(tmp_path):
    path = tmp_path / "bad.json"; path.write_text('{"centers_hz": []}')
    with pytest.raises(ValueError, match="non-empty"):
        read_centers(path)
