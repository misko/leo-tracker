"""Turn synchronised paired sweeps into survey-corpus entries.

One sweep becomes TWO entries, one per radio: each radio swept 8 tunings x 2
receivers, which is exactly one survey.  The IQ needs no reshaping -- it is
already (tuning, sample, receiver, component) int16 LE, the corpus layout.

THE ONE THING THAT MUST NOT GO WRONG: sample_order is the order THAT RADIO
actually scanned, taken from its own tunings list.  Half the sweeps are edge
order "U", scanned (1,upper),(1,lower),(2,upper),...  Importing those with the
canonical lower-first order would file every probe under the wrong frequency
while every other number still looked right.
"""
import hashlib, json, os, sys
from pathlib import Path

SURVEY_SCHEMA = "leo-tracker.pre-dwell-survey/v1"
LAYOUT = ("tuning,sample,receiver,component; sample,receiver,component; "
          "receivers=rx0,rx1; components=i,q")

def digest(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()

def import_sweep(sweep_dir: Path, corpus_root: Path, link=True):
    made = []
    try:
        sweep = json.loads((sweep_dir / "sweep.json").read_text())
    except Exception as exc:
        return [], f"unreadable sweep.json: {exc}"
    for radio_id, r in (sweep.get("radios") or {}).items():
        if r.get("error") or not r.get("iq"):
            continue                      # a radio that produced nothing
        iq = r["iq"]
        src = sweep_dir / iq["path"]
        if not src.is_file():
            continue
        tunings, per_tuning = int(iq["shape"][0]), int(iq["shape"][1])
        name = f"{sweep_dir.name}-{radio_id}"
        entry = corpus_root / name
        if (entry / "scores.json").exists() or (entry / "manifest.json").exists():
            continue                      # idempotent
        entry.mkdir(parents=True, exist_ok=True)
        dst = entry / "survey.ci16"
        if not dst.exists():
            try:
                if link: os.link(src, dst)
                else: dst.write_bytes(src.read_bytes())
            except OSError:
                dst.write_bytes(src.read_bytes())
        # sample_order: THIS radio's own scan order, with the corpus's region spelling
        order = [[int(c), f"{e}-edge"] for c, e in r["tunings"]]
        record = {
            "schema": SURVEY_SCHEMA,
            "state": "complete",
            "sample_rate_hz": float(r["arm"]["sample_rate_hz"]),
            "sample_order": order,
            "sample_order_basis": (
                "the order THIS radio scanned, from its own edge-order draw; "
                f"edge_order={r['edge_order']}"),
            "tunings": [{"channel": c, "region": reg, "receivers": []}
                        for c, reg in order],
            "profile": {"probe_s": float(r["arm"]["probe_s"]),
                        "block_size": per_tuning},
            "synchronised_scan": {
                "paired_sweep": sweep.get("utc"),
                "peer_radio": next((k for k in sweep["radios"] if k != radio_id), None),
                "edge_order": r["edge_order"],
                "arm": r["arm"],
                "matched_arm": sweep.get("matched_arm"),
                "pilot_band_fits": r["arm"].get("pilot_band_fits"),
                "skew_ms": sweep.get("skew_ms"),
                "skew_basis": ("measured at barrier release, a LOWER BOUND on the "
                               "true sample-start offset; see leo-scans/README.md"),
            },
            "note": "imported from a synchronised paired scan; no dwell followed",
        }
        manifest = {
            "schema": "leo-tracker.capture-manifest/v1",
            "state": "complete",
            "sample_rate_hz": float(r["arm"]["sample_rate_hz"]),
            "identity": {"radio_id": radio_id,
                         "receiver_labels": list(r["receiver_labels"])},
            "survey_iq": {"path": "survey.ci16", "dtype": "ci16_le", "layout": LAYOUT,
                          "tunings": tunings, "samples_per_tuning": per_tuning,
                          "bytes": dst.stat().st_size, "sha256": digest(dst)},
            "metadata": {"pre_dwell_survey": record},
        }
        (entry / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        made.append(name)
    return made, None

if __name__ == "__main__":
    src_root = Path(sys.argv[1]); corpus = Path(sys.argv[2])
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    corpus.mkdir(parents=True, exist_ok=True)
    sweeps = sorted(p for p in src_root.glob("sync-*") if (p / "sweep.json").is_file())
    if limit: sweeps = sweeps[:limit]
    total, errs = 0, 0
    for s in sweeps:
        made, err = import_sweep(s, corpus)
        total += len(made)
        if err: errs += 1; print(f"  {s.name}: {err}", file=sys.stderr)
    print(f"imported {total} corpus entries from {len(sweeps)} sweeps ({errs} errors)")
