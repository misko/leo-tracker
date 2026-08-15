#!/usr/bin/env python3
"""Pull per-receiver live acquisition offsets and paired differences out of the
narrow reports, epoch-tagged by capture time.

The live narrow path centres each receiver's +/-350 kHz search on that
receiver's own receiver_centers value, so unlike the survey re-scoring (a fixed
+/-700 kHz bank about raw zero) a port with a large LO error is searched
symmetrically about itself.  That makes this population the right one for both
the differential and, port by port, the absolute centre.

Uses the repository's own pair extractor, never a local reimplementation.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.lnb_calibration import _paired_differences  # noqa: E402

REPORTS = "/mnt/qnap01/mouse9911/leo/reports"
OUT = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/abscal/narrow-live.npz"


def one(path):
    try:
        report = json.load(open(path))
    except (OSError, ValueError):
        return None
    manifest = report.get("capture_manifest") or {}
    identity = manifest.get("identity") or {}
    radio = identity.get("radio_id")
    ports = identity.get("receiver_labels") or []
    if not radio or len(ports) < 2:
        return None
    when = manifest.get("created_utc_ns")
    if when is None:
        return None
    when = float(when) / 1e9
    centres = ((report.get("lnb_calibration") or {}).get("centers_hz")
               or [0.0, 0.0])
    rf = float(manifest.get("rf_center_hz") or 0.0)
    pairs = _paired_differences(report)
    singles = []          # (receiver_index, offset_hz, candidate_flag)
    for check in report.get("exact_checks") or []:
        flags = check.get("receiver_candidates") or [False, False]
        for index, receiver in enumerate((check.get("receivers") or [])[:2]):
            match = (receiver.get("acquisition") or {}).get("exact_match") or {}
            value = match.get("frequency_offset_hz")
            if value is None:
                continue
            singles.append((index, float(value), bool(flags[index])
                            if index < len(flags) else False))
    return {"path": os.path.basename(path), "radio": radio,
            "ports": [str(p) for p in ports[:2]], "utc": when,
            "centres": [float(centres[0]), float(centres[1])],
            "rf": rf, "pairs": pairs, "singles": singles}


def main():
    paths = sorted(glob.glob(os.path.join(REPORTS, "*narrow*.json")))
    paths = [p for p in paths if any(day in os.path.basename(p) for day in
                                     ("2026081", "2026080"))]
    print(len(paths), "narrow reports", flush=True)

    rows_p, rows_s = [], []
    files = []
    with Pool(3) as pool:
        for k, result in enumerate(pool.imap(one, paths, chunksize=16)):
            if result is None:
                continue
            fid = len(files)
            files.append((result["path"], result["radio"], result["ports"][0],
                          result["ports"][1], result["utc"],
                          result["centres"][0], result["centres"][1],
                          result["rf"]))
            for value in result["pairs"]:
                rows_p.append((fid, value))
            for index, value, flag in result["singles"]:
                rows_s.append((fid, index, value, flag))
            if k % 500 == 0:
                print(f"  {k}/{len(paths)}", flush=True)

    np.savez_compressed(
        OUT,
        f_name=np.array([r[0] for r in files]),
        f_radio=np.array([r[1] for r in files]),
        f_rx0=np.array([r[2] for r in files]),
        f_rx1=np.array([r[3] for r in files]),
        f_utc=np.array([r[4] for r in files], "f8"),
        f_c0=np.array([r[5] for r in files], "f8"),
        f_c1=np.array([r[6] for r in files], "f8"),
        f_rf=np.array([r[7] for r in files], "f8"),
        p_file=np.array([r[0] for r in rows_p], "i4"),
        p_diff=np.array([r[1] for r in rows_p], "f8"),
        s_file=np.array([r[0] for r in rows_s], "i4"),
        s_rx=np.array([r[1] for r in rows_s], "i1"),
        s_off=np.array([r[2] for r in rows_s], "f8"),
        s_cand=np.array([r[3] for r in rows_s], bool),
    )
    print("files", len(files), "pairs", len(rows_p), "singles", len(rows_s))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
