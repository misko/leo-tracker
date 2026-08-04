from __future__ import annotations

import math
import numpy as np


def safe_correlation(first, second) -> float:
    first, second = np.asarray(first, float), np.asarray(second, float)
    if first.size != second.size or first.size < 3 or np.std(first) == 0 or np.std(second) == 0:
        return 0.0
    value = float(np.corrcoef(first, second)[0, 1])
    return value if math.isfinite(value) else 0.0


def empirical_false_alarm(observed: float, null_scores) -> float | None:
    null = np.asarray(null_scores, float)
    if not null.size:
        return None
    return float((1+np.sum(null >= observed))/(null.size+1))


def gain_transition_times(time_s, hardware_gain_db, minimum_change_db: float = .5):
    if hardware_gain_db is None:
        return []
    time = np.asarray(time_s, float); gain = np.asarray(hardware_gain_db, float)
    rows = []
    for receiver in range(gain.shape[0]):
        finite = np.isfinite(gain[receiver, 1:]) & np.isfinite(gain[receiver, :-1])
        indexes = np.flatnonzero(finite &
            (np.abs(np.diff(gain[receiver])) >= minimum_change_db))+1
        rows.extend({"receiver": receiver, "time_s": float(time[index]),
                     "change_db": float(gain[receiver, index]-gain[receiver, index-1])}
                    for index in indexes)
    return rows
