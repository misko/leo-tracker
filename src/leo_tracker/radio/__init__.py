"""Hardware-independent RF acquisition and Doppler extraction."""

from .artifact import CaptureArtifact, capture_to_artifact
from .extract import FrequencyTrack, TrackPoint, extract_frequency_ridge
from .source import FakeSource, RadioConfig, RadioSource, ReplaySource, SampleBlock
from .synthetic import doppler_signal, linear_chirp, tone
from .paired import (FakePairedSource, PairedCI16Block, PairedSampleBlock,
                     capture_pair_to_artifacts, paired_sample_count)
from .carrier import CarrierPoint, CarrierTrack, track_carrier
from .validated_scan import TimedSamples, ValidatedScanPoint, validated_scan
from .monitor import (MonitorResult, MotionCandidate, SpectrumFrame, compact_psd,
                      estimate_spectral_shift, find_motion_candidates, promote_dual_motion)
from .starlink import (StarlinkChannel, StarlinkMetrics, analyze_starlink_block,
                       channel_plan, get_channel)

__all__ = [
    "CaptureArtifact", "FakeSource", "FrequencyTrack", "RadioConfig",
    "RadioSource", "ReplaySource", "SampleBlock", "TrackPoint",
    "capture_to_artifact", "doppler_signal", "extract_frequency_ridge",
    "linear_chirp", "tone",
    "FakePairedSource", "PairedCI16Block", "PairedSampleBlock",
    "capture_pair_to_artifacts", "paired_sample_count",
    "CarrierPoint", "CarrierTrack", "track_carrier",
    "TimedSamples", "ValidatedScanPoint", "validated_scan",
    "MonitorResult", "MotionCandidate", "SpectrumFrame", "compact_psd",
    "estimate_spectral_shift", "find_motion_candidates", "promote_dual_motion",
    "StarlinkChannel", "StarlinkMetrics", "analyze_starlink_block",
    "channel_plan", "get_channel",
]
