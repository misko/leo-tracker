import json
import os
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from leo_tracker.radio import (FakeSource, RadioConfig, ReplaySource, capture_to_artifact,
                               extract_frequency_ridge, linear_chirp, tone)
from leo_tracker.radio.pluto import PlutoSource


class RadioTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.config = RadioConfig(1.5e9, 20_000, 15_000, gain_db=10)

    def tearDown(self): self.temp.cleanup()

    def test_atomic_capture_checksum_and_replay(self):
        values = tone(1234, 20_000, .1)
        source = FakeSource(values, self.config, 333, start_utc_ns=10)
        artifact = capture_to_artifact(source, Path(self.temp.name) / "capture", metadata={"site": "test"})
        self.assertTrue(source.closed)
        self.assertTrue(np.array_equal(artifact.load_samples(), values))
        mapped = artifact.load_samples(mmap=True)
        self.assertIsInstance(mapped, np.memmap)
        self.assertTrue(np.array_equal(mapped, values))
        self.assertEqual(artifact.manifest["sample_count"], len(values))
        replayed = np.concatenate([b.samples for b in ReplaySource(artifact.path, 127).blocks()])
        self.assertTrue(np.array_equal(replayed, values))
        iq_path = artifact.path / "iq.c64"
        self.assertEqual(iq_path.stat().st_mode & 0o222, 0)
        os.chmod(iq_path, 0o644)  # Simulate external corruption despite publication permissions.
        with iq_path.open("r+b") as stream:
            stream.seek(0); stream.write(b"bad!")
        with self.assertRaisesRegex(ValueError, "checksum"):
            ReplaySource(artifact.path)

    def test_failed_capture_is_not_published(self):
        class BrokenSource(FakeSource):
            def blocks(self):
                yield from super().blocks()
                raise RuntimeError("receiver disconnected")
        destination = Path(self.temp.name) / "capture"
        source = BrokenSource(tone(100, 20_000, .01), self.config)
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            capture_to_artifact(source, destination)
        self.assertFalse(destination.exists())
        self.assertTrue(source.closed)
        self.assertEqual(list(Path(self.temp.name).glob("*.incomplete-*")), [])

    def test_chirp_ridge_accuracy_and_quality(self):
        rate, duration = 20_000, 1.0
        values = linear_chirp(-2000, 2500, rate, duration, amplitude=.3, noise_std=.04, seed=4)
        track = extract_frequency_ridge(values, rate, fft_size=1024, hop_size=256,
                                        search_hz=(-4000, 4000), max_step_hz=200)
        error = []
        for point in track.points:
            expected = -2000 + 4500 * point.time_s / duration
            error.append(abs(point.frequency_hz - expected))
            self.assertGreater(point.snr_db, 6)
            self.assertGreater(point.uncertainty_hz, 0)
        self.assertLess(np.median(error), 12)

    def test_low_snr_flag(self):
        rng = np.random.default_rng(2)
        noise = (rng.standard_normal(4096) + 1j * rng.standard_normal(4096)).astype(np.complex64)
        track = extract_frequency_ridge(noise, 20_000, fft_size=1024, min_snr_db=30)
        self.assertTrue(all("low_snr" in point.flags for point in track.points))

    def test_pluto_factory_without_spf_or_hardware(self):
        class Device:
            def __init__(self): self.closed = False
            def rx(self): return np.ones(8, dtype=np.complex64)
            def close(self): self.closed = True
        device = Device()
        source = PlutoSource(self.config, uri="pluto://test", block_size=8,
                             device_factory=lambda **kwargs: device)
        block = next(source.blocks())
        self.assertEqual(block.samples.size, 8)
        source.close()
        self.assertTrue(device.closed)

    def test_config_rejects_invalid_units(self):
        with self.assertRaises(ValueError): RadioConfig(1, 10, 11)


if __name__ == "__main__": unittest.main()
