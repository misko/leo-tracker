from __future__ import annotations

import numpy as np
from dataclasses import replace

from scipy.optimize import linear_sum_assignment

from .controls import safe_correlation
from .models import JointTrack, TrackCandidate


def associate_tracks(candidates: list[TrackCandidate], *, minimum_time_iou: float = .4,
                     maximum_drift_difference_hz_s: float = 1_500,
                     minimum_correlation: float = .6,
                     offset_consensus_tolerance_hz: float = 100_000) -> list[JointTrack]:
    first = [(i, item) for i, item in enumerate(candidates) if item.receiver == 0]
    second = [(i, item) for i, item in enumerate(candidates) if item.receiver == 1]
    if not first or not second:
        return []
    costs = np.full((len(first), len(second)), 10.0); reports = {}
    for row, (first_index, a) in enumerate(first):
        for column, (second_index, b) in enumerate(second):
            start, stop = max(a.start_time_s, b.start_time_s), min(a.stop_time_s, b.stop_time_s)
            union = max(a.stop_time_s, b.stop_time_s)-min(a.start_time_s, b.start_time_s)
            iou = max(0.0, stop-start)/union if union > 0 else 0.0
            if iou < minimum_time_iou:
                continue
            common = np.asarray(a.time_s); common = common[(common >= start)&(common <= stop)]
            if common.size < 3:
                continue
            pa = np.interp(common, a.time_s, a.frequency_hz)
            pb = np.interp(common, b.time_s, b.frequency_hz)
            centered_a, centered_b = pa-np.mean(pa), pb-np.mean(pb)
            correlation = safe_correlation(centered_a, centered_b)
            trace_a = (a.diagnostics or {}).get("heldout_trace_db")
            trace_b = (b.diagnostics or {}).get("heldout_trace_db")
            trace_correlation = None
            if trace_a is not None and trace_b is not None:
                ta = (a.diagnostics or {}).get("heldout_time_s", [])
                tb = (b.diagnostics or {}).get("heldout_time_s", [])
                trace_start, trace_stop = max(min(ta), min(tb)), min(max(ta), max(tb))
                trace_times = np.asarray(ta, float)
                trace_times = trace_times[(trace_times >= trace_start)&(trace_times <= trace_stop)]
                if trace_times.size >= 3:
                    trace_correlation = safe_correlation(
                        np.interp(trace_times, ta, trace_a),
                        np.interp(trace_times, tb, trace_b))
            difference = abs(a.drift_hz_s-b.drift_hz_s)
            trace_score = .5 if trace_correlation is None else max(0, trace_correlation)
            confidence = .3*iou+.25*max(0, correlation)+.25*max(
                0, 1-difference/maximum_drift_difference_hz_s)+.2*trace_score
            qualified = (a.qualified and b.qualified and
                correlation >= minimum_correlation and
                difference <= maximum_drift_difference_hz_s and
                (trace_correlation is None or trace_correlation >= .25))
            warnings = []
            if not (a.qualified and b.qualified):
                warnings.append("one or both receiver-local candidates failed controls")
            if correlation < minimum_correlation: warnings.append("receiver paths disagree")
            if difference > maximum_drift_difference_hz_s: warnings.append("receiver slopes disagree")
            if trace_correlation is not None and trace_correlation < .25:
                warnings.append("receiver held-out amplitudes disagree")
            report = JointTrack(a.tracker, (first_index, second_index), correlation,
                float(np.median(pb-pa)), difference, float(confidence), qualified, tuple(warnings))
            reports[row, column] = report; costs[row, column] = 1-confidence
    rows, columns = linear_sum_assignment(costs)
    matched = [reports[row, column] for row, column in zip(rows, columns)
               if (row, column) in reports]
    # A real two-LNB observation may have an arbitrary receiver offset, but that
    # offset is common to simultaneous tracks.  Linear paths alone correlate
    # perfectly for every same-slope pair, so use the population's dominant
    # offset as a second association control when at least three pairs exist.
    qualified = [item for item in matched if item.qualified]
    if len(qualified) >= 3:
        offsets = np.asarray([item.receiver_frequency_offset_hz for item in qualified])
        support = np.asarray([np.count_nonzero(
            np.abs(offsets-offset) <= offset_consensus_tolerance_hz)
            for offset in offsets])
        center = float(np.median(offsets[
            np.abs(offsets-offsets[int(np.argmax(support))]) <=
            offset_consensus_tolerance_hz]))
        revised = []
        for item in matched:
            if item.qualified and abs(item.receiver_frequency_offset_hz-center) > offset_consensus_tolerance_hz:
                revised.append(replace(item, qualified=False,
                    confidence=item.confidence*.5,
                    warnings=item.warnings+("receiver offset outside population consensus",)))
            else:
                revised.append(item)
        matched = revised
    return sorted(matched, key=lambda item: item.confidence, reverse=True)
