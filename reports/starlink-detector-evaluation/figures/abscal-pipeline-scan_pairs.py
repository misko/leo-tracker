#!/usr/bin/env python3
"""Both receivers' offsets on the SAME check, so the differential and the two
marginals can be compared on one population instead of three.

The differential cancels Doppler within a check; a difference of marginal means
does not, because the two ports detect different subsets of the sky.  If the two
disagree, this is the file that says which effect it is.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from multiprocessing import Pool

import numpy as np

REPORTS = "/mnt/qnap01/mouse9911/leo/reports"
OUT = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/abscal/narrow-pairs.npz"


def one(path):
    try:
        report = json.load(open(path))
    except (OSError, ValueError):
        return None
    manifest = report.get("capture_manifest") or {}
    identity = manifest.get("identity") or {}
    radio = identity.get("radio_id")
    ports = identity.get("receiver_labels") or []
    when = manifest.get("created_utc_ns")
    if not radio or len(ports) < 2 or when is None:
        return None
    centres = ((report.get("lnb_calibration") or {}).get("centers_hz") or [0.0, 0.0])
    rows = []
    for check in report.get("exact_checks") or []:
        receivers = (check.get("receivers") or [])[:2]
        if len(receivers) < 2:
            continue
        offsets = []
        for receiver in receivers:
            match = (receiver.get("acquisition") or {}).get("exact_match") or {}
            offsets.append(match.get("frequency_offset_hz"))
        if None in offsets:
            continue
        flags = check.get("receiver_candidates") or [False, False]
        rows.append((float(offsets[0]), float(offsets[1]),
                     bool(check.get("candidate")),
                     bool(check.get("qualified")),
                     bool(flags[0]), bool(flags[1]),
                     float(check.get("epoch_difference_samples") or -1)))
    if not rows:
        return None
    return {"path": os.path.basename(path), "radio": radio,
            "ports": [str(p) for p in ports[:2]], "utc": float(when) / 1e9,
            "centres": [float(centres[0]), float(centres[1])], "rows": rows}


def main():
    paths = sorted(glob.glob(os.path.join(REPORTS, "*narrow*.json")))
    print(len(paths), "narrow reports", flush=True)
    files, rows = [], []
    with Pool(3) as pool:
        for k, result in enumerate(pool.imap(one, paths, chunksize=16)):
            if result is None:
                continue
            fid = len(files)
            files.append((result["path"], result["radio"], result["ports"][0],
                          result["ports"][1], result["utc"],
                          result["centres"][0], result["centres"][1]))
            for row in result["rows"]:
                rows.append((fid,) + row)
            if k % 1000 == 0:
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
        c_file=np.array([r[0] for r in rows], "i4"),
        c_off0=np.array([r[1] for r in rows], "f8"),
        c_off1=np.array([r[2] for r in rows], "f8"),
        c_dual=np.array([r[3] for r in rows], bool),
        c_qual=np.array([r[4] for r in rows], bool),
        c_cand0=np.array([r[5] for r in rows], bool),
        c_cand1=np.array([r[6] for r in rows], bool),
        c_epochd=np.array([r[7] for r in rows], "f8"),
    )
    print("files", len(files), "checks", len(rows), "wrote", OUT)


if __name__ == "__main__":
    main()
