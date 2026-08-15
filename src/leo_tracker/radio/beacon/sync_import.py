"""File the interim synchronised sweeps into the survey corpus, unchanged.

An interim collector has been writing paired sweeps to the capture host's NVMe
for days.  Each sweep points both radios at the same eight edge-pilot tunings at
the same instant and keeps the IQ; nothing has ever read them.  They are not in
the offload queue, not on the share and not in the survey corpus, so not one of
the eight candidate detectors has ever been run over them.

The IQ is already ``(tuning, sample, receiver, component)`` int16 -- the exact
layout :func:`~.survey_scoring.read_probe` takes -- so the whole job is
bookkeeping: one radio's eight tunings on two receivers *is* one survey, and a
sweep is therefore two corpus entries that happen to have been collected
simultaneously.  Nothing here resamples, rescales or rewrites a sample.

``sample_order`` is the only thing here that can destroy the experiment
-------------------------------------------------------------------------

The collector randomises which edge of a channel it visits first.  Half the
sweeps are ``edge_order`` "U" -- the radio scanned (1, upper), (1, lower),
(2, upper), ... -- and the two radios of a single sweep disagree about that
order in **632 of 1,205** sweeps, so the order is a property of the radio and
never of the sweep, of the peer, or of a constant.

Filing a "U" sweep with the canonical lower-first list would attribute every
probe to the neighbouring tuning.  Nothing would raise: the eight labels are all
real tunings, ``read_probe`` would reshape cleanly, every score would be a
finite number, and the corpus would hold 8 x 2 wrong sky attributions per entry
with no symptom whatsoever.  That is the failure ``sample_order`` was introduced
for in the first place -- position agreed with the record list only by luck --
and it is why this module takes the order out of *that radio's own* ``tunings``
list in ``sweep.json``, character for character, and translates only the region
spelling.  The tuning records are written sorted, the way the capture host
writes them, precisely so that position and order disagree on a "U" entry and
anything reading position instead of ``sample_order`` is wrong immediately
rather than subtly.

What is carried across, and where it is put
-------------------------------------------

The sweep knows six things no single survey can know: which paired sweep this
is, which radio was on the other end, how far apart in time the two radios
actually were per tuning, which edge order this radio drew, which arm it drew,
and whether the two radios drew the same arm.  All six go in one named block,
``metadata.synchronised_sweep``, and none of them go anywhere else.  There is a
real temptation to put the peer's id in ``identity`` or the skew in
``stream_timing``, and every reader of those fields would then be wrong about
what it was reading.

The arm's ``pilot_band_fits`` flag is the exception that also travels inside
``capture_config``, because it is a property of the capture configuration and
because ``capture_config`` is the block :func:`~.survey_scoring.score_entry`
copies into every score sidecar.  Three of the twelve arms sample at 1.25 MS/s,
where the 1.875 MHz pilot band does not fit at all -- the guard is
**-312.5 kHz**, so subcarriers fall off the end of the sampled spectrum and no
search recovers them.  Pooling those with arms that fit would average a
measurement over probes that never sampled the band being measured, so the flag
has to arrive with the entry rather than be recomputed by whoever remembers to.

What is *not* claimed
---------------------

The interim collector scored nothing, so there is no bank, no threshold and no
verdict.  Every one of those is written as ``None`` with a basis that says so,
rather than filled in from this host's constants: ``capture_bank`` reads
``known: False``, the reproduction check excludes itself, and no ``active``
boolean exists to be mistaken for "looked and found nothing".  The tuning
centres are the one derived quantity, computed from the published channel
geometry and the standard 9.75 GHz LNB local oscillator because the collector
recorded no centres, and they say so in ``tuning_center_basis``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from .artifact import LAYOUT, SURVEY_IQ_FILENAME
from .channels import starlink_edge_pilot_if_hz
from .fast_scan import PILOT_BANDWIDTH_HZ, pilot_guard_hz

#: What the collector's own record calls itself.  Refused rather than guessed
#: at: a v2 collector may reorder or rename the very fields this reads.
SOURCE_SCHEMA = "leo-tracker.interim-synchronised-scan/v1"

#: What the imported manifest calls itself.  Deliberately *not* the beacon-IQ
#: schema: there is no dwell recording here, no chunks and no gain telemetry,
#: and a reader that treated it as one would enumerate an empty capture.
MANIFEST_SCHEMA = "leo-tracker.synchronised-sweep-survey/v1"

#: Schemas an entry may carry and still be a finished import.
#:
#: The corpus was built by the interim importer in ``deploy/interim/``, which
#: is what actually ran, and it stamps the capture-manifest schema instead of
#: the one above.  Reading completeness as "schema == MANIFEST_SCHEMA" therefore
#: calls every one of those entries unfinished -- and the caller that skips
#: finished work then skips nothing, so a bounded pass silently re-imports the
#: whole corpus.  Both names mean the same thing here: a manifest that parses,
#: names a sha256, and names the byte count the IQ actually has.
COMPLETE_MANIFEST_SCHEMAS = (MANIFEST_SCHEMA, "leo-tracker.capture-manifest/v1")

#: What the survey record calls itself.  It is a real pre-dwell survey record
#: -- ``tuning_plan``, ``read_probe`` and ``survey_sample_rate_hz`` all read it
#: through their ordinary path -- so it carries the schema they were written
#: against rather than a private one they have never seen.
SURVEY_SCHEMA = "leo-tracker.pre-dwell-survey/v2"

#: What the synchronisation block calls itself.
SYNC_SCHEMA = "leo-tracker.synchronised-sweep-provenance/v1"

#: The name of the experiment these sweeps belong to.  Named rather than
#: implied so a later analysis can select exactly these entries, and so that
#: they never pool with the randomised pre-dwell survey's four arms, which are
#: a different draw over a different arm set on a different collector.
EXPERIMENT_ID = "interim-synchronised-scan-v1"

#: The LNB low-band local oscillator every port on this site runs.  Used only to
#: derive the tuning centres the collector did not record, and recorded as an
#: assumption beside them.
LNB_LO_HZ = 9_750_000_000.0

#: How the collector spells an edge, and how the corpus spells it.  The corpus
#: keys ``tuning_plan`` on (channel, region) with the suffix present, so the
#: translation is a lookup rather than a format string: an unknown edge must
#: fail here, where it is one sweep, and not downstream where it is a miss.
REGIONS = {"lower": "lower-edge", "upper": "upper-edge"}


class SweepUnusable(ValueError):
    """A sweep directory cannot be imported at all, with the reason."""


def entry_name(sweep_id: str, radio_id: str) -> str:
    """What one radio's half of one sweep is called in the corpus.

    The sweep id is kept whole and the radio appended, so the paired entries
    sort together, the pairing is legible without opening a file, and the score
    sidecar's own ``capture`` field -- which is just this name -- already names
    the sweep it belongs to.
    """
    return f"{sweep_id}-{radio_id}"


def _read_sweep(sweep: Path) -> dict:
    path = Path(sweep) / "sweep.json"
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        raise SweepUnusable(f"no readable sweep.json in {sweep}: {exc}") from exc
    except ValueError as exc:
        raise SweepUnusable(f"sweep.json in {sweep} is not JSON: {exc}") from exc
    schema = payload.get("schema")
    if schema != SOURCE_SCHEMA:
        raise SweepUnusable(
            f"{sweep} declares schema {schema!r}, not {SOURCE_SCHEMA!r}")
    if not isinstance(payload.get("radios"), dict) or not payload["radios"]:
        raise SweepUnusable(f"{sweep} names no radios")
    return payload


def _started_utc_ns(utc: str) -> int:
    """The instant the collector stamped the sweep with, to the second.

    The stamp has one-second resolution and the measured file times sit 0.2 to
    0.8 s past ``utc + wall_s`` on every sweep checked, so ``utc`` is at or just
    before the true start.  It is therefore used as a *lower* bound and the
    matching upper bound is built in :func:`_manifest`, which is exactly the
    bracket ``survey_truth.probe_window`` already knows how to carry.
    """
    try:
        moment = datetime.strptime(utc, "%Y%m%dT%H%M%SZ")
    except (TypeError, ValueError) as exc:
        raise SweepUnusable(f"sweep utc {utc!r} is not a compact UTC stamp"
                            ) from exc
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1e9)


def sample_order(radio: dict) -> list[list]:
    """The order **this radio** scanned, from its own tunings list.

    The single line in this module that the whole import rests on.  Taken from
    ``radio["tunings"]`` in the order the collector wrote it and translated only
    in spelling; never sorted, never defaulted, never taken from the peer.
    """
    listed = radio.get("tunings")
    if not listed:
        raise SweepUnusable("radio record carries no tunings list, so the "
                            "order it scanned in is unknown and cannot be "
                            "guessed")
    order = []
    for pair in listed:
        try:
            channel, edge = int(pair[0]), str(pair[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise SweepUnusable(f"tuning {pair!r} is not (channel, edge)"
                                ) from exc
        if edge not in REGIONS:
            raise SweepUnusable(
                f"tuning {pair!r} names edge {edge!r}; the corpus knows "
                f"{sorted(REGIONS)}")
        order.append([channel, REGIONS[edge]])
    return order


def _tuning_records(order: list[list]) -> list[dict]:
    """One record per tuning, sorted, the way the capture host writes them.

    Sorted on purpose.  The capture host sorts its tuning records by score and
    keeps the collection order in ``sample_order`` alone, and reproducing that
    separation here means a "U" entry's record list and its collection order
    genuinely disagree -- so any reader that indexes the IQ by position instead
    of by ``sample_order`` is wrong on the first entry it touches rather than on
    some later one nobody checks.

    ``receivers`` is empty because the collector scored nothing.  It is a real
    empty list rather than a missing key: the tuning was visited, both receivers
    were kept, and no detector was run.
    """
    records = []
    for channel, region in sorted({(int(c), str(r)) for c, r in order}):
        edge = region.split("-")[0]
        centre = starlink_edge_pilot_if_hz(channel, edge, LNB_LO_HZ)
        records.append({"channel": channel, "region": region,
                        "if_center_hz": centre,
                        "rf_center_hz": centre + LNB_LO_HZ,
                        "receivers": []})
    return records


def _digest(path: Path, chunk: int = 1 << 22) -> tuple[str, int]:
    """SHA-256 of the bytes that are on disk now, and how many there are.

    Taken from the destination after it is in place rather than from the source
    as it goes past, so the digest describes the file a later reader will
    actually open -- including when the file got there by hard link and no byte
    was copied at all.
    """
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while True:
            piece = stream.read(chunk)
            if not piece:
                break
            digest.update(piece)
            size += len(piece)
    return digest.hexdigest(), size


def _place(source: Path, destination: Path, *, link: bool) -> str:
    """Put the radio's IQ where the corpus expects it, in one step.

    Hard-linked by preference.  The sweeps are 180 GB and growing; a link costs
    nothing, cannot drift from the original, and keeps the bytes alive if the
    sweep directory is later reclaimed.  A link that cannot be made -- different
    filesystem, or a share that does not support them -- falls back to a copy
    rather than failing, and which happened is recorded.

    Either way the file appears at its final name by rename, so an interrupted
    import leaves no half-file that the next pass would mistake for a probe.
    """
    partial = destination.with_name(destination.name + ".partial")
    partial.unlink(missing_ok=True)
    how = "hardlink"
    if link:
        try:
            os.link(source, partial)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK,
                                 errno.EOPNOTSUPP, errno.EACCES):
                raise
            how = "copy"
    else:
        how = "copy"
    if how == "copy":
        with open(source, "rb") as reader, partial.open("wb") as writer:
            shutil.copyfileobj(reader, writer, 1 << 22)
            writer.flush()
            os.fsync(writer.fileno())
    os.replace(partial, destination)
    return how


def _capture_config(arm: dict, per_tuning: int, iq_bytes: int) -> dict:
    """The arm, restated as the block every downstream reader already knows.

    ``pilot_band_fits`` is carried from the collector's own record rather than
    recomputed, and the recomputation is written beside it under its own name so
    that a disagreement is visible instead of resolved in silence by whichever
    of the two this module happened to trust.
    """
    rate = float(arm["sample_rate_hz"])
    guard = pilot_guard_hz(rate)
    return {
        "name": str(arm.get("name") or ""),
        "probe_s": float(arm["probe_s"]),
        "sample_rate_hz": rate,
        "samples_per_tuning": int(per_tuning),
        "pilot_bandwidth_hz": PILOT_BANDWIDTH_HZ,
        "pilot_guard_hz": guard,
        # Three of the twelve arms sample at 1.25 MS/s, where this is -312,500:
        # the pilot band is wider than the sampled spectrum and subcarriers are
        # lost before any detector runs. Nothing may pool those with the arms
        # that fit, which is why the flag travels in the block the score sidecar
        # copies rather than being left to be re-derived from the rate.
        "pilot_band_fits": bool(arm.get("pilot_band_fits")),
        "pilot_band_fits_recomputed": bool(guard >= 0.0),
        "iq_bytes": int(iq_bytes),
        "scored_samples": 0,
        "scored_probe_s": 0.0,
        "scored_on": "not scored anywhere yet; the interim collector recorded "
                     "and never scored, which is why these sweeps exist",
        "scoring_note": (
            "no bank was run on the capture host, so there is no deployed "
            "number to reproduce and no threshold to re-read. Every detector "
            "these probes have ever seen was run on the analysis host over the "
            "whole preserved probe"),
    }


def _survey_record(radio: dict, sweep: dict, order: list[list],
                   per_tuning: int, iq_bytes: int) -> dict:
    arm = radio.get("arm") or {}
    wall_ms = float(sweep.get("wall_s") or 0.0) * 1000.0
    return {
        "schema": SURVEY_SCHEMA,
        "state": "complete",
        "started_utc_ns": _started_utc_ns(sweep["utc"]),
        # Nothing was warmed between the stamp and the first probe: the
        # collector stamps the sweep and starts. Zero here is a measurement,
        # not a placeholder, and it is what makes ``started_utc_ns`` the lower
        # bound of the bracket rather than the midpoint of one.
        "warm_ms": 0.0,
        "total_ms": wall_ms,
        "per_tuning_ms": wall_ms / max(len(order), 1),
        "sample_rate_hz": float(arm["sample_rate_hz"]),
        "probe_s": float(arm["probe_s"]),
        "samples_per_tuning": int(per_tuning),
        # The order this radio actually scanned in. Everything else in this
        # record can be wrong and produce a visibly wrong answer; this can be
        # wrong and produce a perfectly plausible one about the wrong sky.
        "sample_order": order,
        "sample_order_basis": (
            "recorded, not inferred: taken from this radio's own tunings list "
            "in sweep.json, which the collector writes in collection order and "
            "randomises per radio (edge_order L or U). The two radios of one "
            "sweep disagree about it in 632 of 1,205 sweeps, so it is never "
            "the sweep's order and never the canonical lower-first list"),
        "tunings": _tuning_records(order),
        "tuning_center_basis": (
            "derived, not recorded: the collector writes no centres, so these "
            "are the published edge-pilot geometry of channels 1-4 at the "
            f"standard {LNB_LO_HZ / 1e9:.2f} GHz LNB low-band local "
            "oscillator every port on this site runs. A site with a different "
            "LNB must not read these as measurements"),
        "capture_config": _capture_config(arm, per_tuning, iq_bytes),
        "experiment": {
            "experiment_id": EXPERIMENT_ID,
            "assigned_config": str(arm.get("name") or ""),
            "random_draw_u32": sweep.get("arm_draw_u32"),
            "randomised": True,
            "assignment_rule": None,
            "basis": (
                "the collector records the draw and the arm it produced but "
                "not the mapping between them, so the split can be audited by "
                "counting arms across sweeps and not by replaying the draw. "
                "Twelve arms do not divide 2**32, so a modulo assignment "
                "would carry a bias of about one part in 350 million"),
        },
        # No bank, no span, no threshold and no verdict: the collector ran no
        # detector at all. Written as absences so ``capture_bank`` reads
        # ``known: False`` and the reproduction check excludes itself, rather
        # than inheriting this host's current constants and claiming a capture
        # searched something it never did.
        "profile": None,
        "offset_span_hz": None,
        "threshold": None,
        "threshold_calibrated": False,
        "threshold_basis": (
            "none: the interim collector applied no threshold and wrote no "
            "verdict. Any bar these probes are held to belongs to the analysis "
            "that applies it and must be recorded there"),
        "scored_on_capture_host": False,
        "active": None,
        "active_count": None,
        "dwell": None,
        "note": ("interim synchronised sweep, imported into the corpus. "
                 "Observational only: it chose no channel, gated no capture "
                 "and preceded no dwell"),
    }


def _skew_by_tuning(sweep: dict, order: list[list]) -> list[dict]:
    """The per-tuning skew, keyed to the sky *this* radio saw in each slot.

    The skew is a property of the slot -- the two radios probe slot ``i`` at the
    same instant -- and when their edge orders differ, slot ``i`` is a different
    tuning on each radio.  So the same number belongs to (1, upper-edge) on one
    entry and to (1, lower-edge) on the other, and a consumer keying on
    (channel, region) has to be handed the mapping rather than left to assume
    the two agree.
    """
    values = ((sweep.get("skew_ms") or {}).get("per_tuning")) or []
    rows = []
    for index, (channel, region) in enumerate(order):
        rows.append({"iq_index": index, "channel": int(channel),
                     "region": str(region),
                     "skew_ms": (float(values[index])
                                 if index < len(values) else None)})
    return rows


def _sync_facts(radio_id: str, radio: dict, sweep: dict, sweep_id: str,
                order: list[list], peers: dict) -> dict:
    """Everything a pair knows that neither half knows alone.

    ``matched_arm`` is carried exactly as the collector declared it and is
    never re-derived from the two arm names, because the two are not the same
    fact: on 12 of 1,205 real sweeps the collector declares ``matched_arm``
    false while both radios happen to have drawn the same arm.  The declaration
    is what the pairing draw did; the equality is what came out.  Both are
    recorded, under their own names.
    """
    peer_id = next((name for name in sweep["radios"] if name != radio_id), None)
    peer = (sweep["radios"].get(peer_id) or {}) if peer_id else {}
    peer_arm = peer.get("arm") or {}
    arm = radio.get("arm") or {}
    return {
        "schema": SYNC_SCHEMA,
        "source_schema": sweep.get("schema"),
        "sweep_id": sweep_id,
        "sweep_utc": sweep.get("utc"),
        "sweep_index": sweep.get("sweep"),
        "sweep_wall_s": sweep.get("wall_s"),
        "radio_id": radio_id,
        "peer_radio_id": peer_id,
        "peer_entry": entry_name(sweep_id, peer_id) if peer_id else None,
        "peer_imported": bool(peers.get(peer_id)),
        "peer_error": peer.get("error"),
        "peer_edge_order": peer.get("edge_order"),
        "peer_arm_name": peer_arm.get("name"),
        "peer_receiver_labels": peer.get("receiver_labels"),
        "matched_arm": sweep.get("matched_arm"),
        "arm_names_equal": (bool(arm.get("name") == peer_arm.get("name"))
                            if peer_id else None),
        "arm": {"name": arm.get("name"), "probe_s": arm.get("probe_s"),
                "sample_rate_hz": arm.get("sample_rate_hz"),
                "pilot_band_fits": arm.get("pilot_band_fits")},
        "arm_draw_u32": sweep.get("arm_draw_u32"),
        "pairing_draw_u32": sweep.get("pairing_draw_u32"),
        "order_draw_u32": radio.get("order_draw_u32"),
        "edge_order": radio.get("edge_order"),
        "skew_ms": sweep.get("skew_ms"),
        "skew_ms_by_tuning": _skew_by_tuning(sweep, order),
        "collector_note": sweep.get("note"),
        "note": (
            "the two entries of one sweep were collected simultaneously on two "
            "radios; slot i on each was probed at the same instant, to within "
            "skew_ms.per_tuning[i]. When the two edge orders differ, slot i is "
            "a different tuning on each radio, so pair by iq_index and read "
            "the sky off each entry's own sample_order"),
    }


def _manifest(radio_id: str, radio: dict, sweep: dict, sweep_id: str,
              order: list[list], per_tuning: int, iq: dict, digest: str,
              written: int, placement: str, peers: dict) -> dict:
    record = _survey_record(radio, sweep, order, per_tuning, written)
    started = record["started_utc_ns"]
    wall_ns = int(float(sweep.get("wall_s") or 0.0) * 1e9)
    return {
        "schema": MANIFEST_SCHEMA,
        "state": "complete",
        "dtype": "ci16_le",
        "layout": "tuning,sample,receiver,component; " + LAYOUT,
        "receiver_count": 2,
        # An upper bound on when the sweep finished, which is what
        # ``survey_truth.probe_window`` uses it as: the collector's stamp has
        # one-second resolution and the sweep took ``wall_s``, so it cannot have
        # finished later than a second past their sum. Pairing that with
        # ``warm_ms: 0`` brackets the scan start to exactly the second the
        # stamp does not resolve, instead of pretending it is known exactly.
        "created_utc_ns": started + wall_ns + 1_000_000_000,
        "created_utc_ns_basis": (
            "upper bound: sweep.json's utc is truncated to the second, so the "
            "true start lies in [utc, utc+1) and the finish no later than "
            "utc + 1 + wall_s"),
        # There is no dwell. An empty list rather than a missing key, so a
        # reader that enumerates chunks finds nothing rather than raising, and
        # the note says why there is nothing to find.
        "chunks": [],
        "chunks_note": ("the interim collector records probes only; no dwell "
                        "followed this survey and none is missing"),
        "identity": {
            "radio_id": radio_id,
            "receiver_labels": list(radio.get("receiver_labels") or []),
            "kind": "plutoplus-paired",
            "identity_basis": ("the collector records the radio id and the "
                               "receiver labels only; serial, firmware and "
                               "temperatures were not written down"),
        },
        "survey_iq": {
            "path": SURVEY_IQ_FILENAME,
            "dtype": "ci16_le",
            "layout": "tuning,sample,receiver,component; " + LAYOUT,
            "tunings": len(order),
            "samples_per_tuning": int(per_tuning),
            "sample_rate_hz": float((radio.get("arm") or {})["sample_rate_hz"]),
            "probe_s": float((radio.get("arm") or {})["probe_s"]),
            "capture_config": (radio.get("arm") or {}).get("name"),
            "shape_note": (
                "shape and rate are declared, never assumed: this collector "
                "draws probe length from {80, 160, 640} ms and rate from "
                "{1.25, 2.5, 5, 10} MS/s, so this file holds between 100,000 "
                "and 6,400,000 samples per tuning"),
            "sha256": digest,
            "bytes": int(written),
            "source_path": str(iq.get("path")),
            "placement": placement,
        },
        "metadata": {"pre_dwell_survey": record,
                     "synchronised_sweep": _sync_facts(radio_id, radio, sweep,
                                                       sweep_id, order, peers)},
    }


def _importable(radio_id: str, radio: dict, sweep_dir: Path) -> tuple[dict, int]:
    """The radio's declared IQ block, checked against the file on disk."""
    if radio.get("error"):
        raise SweepUnusable(f"{radio_id} recorded an error: {radio['error']}")
    arm = radio.get("arm") or {}
    if not arm.get("probe_s") or not arm.get("sample_rate_hz"):
        raise SweepUnusable(f"{radio_id} declares no arm probe length or rate")
    iq = radio.get("iq") or {}
    shape = iq.get("shape")
    if not shape or len(shape) != 4 or tuple(shape[2:]) != (2, 2):
        raise SweepUnusable(
            f"{radio_id} declares shape {shape!r}, not (tuning, sample, 2, 2)")
    path = sweep_dir / str(iq.get("path") or "")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SweepUnusable(f"{radio_id} IQ is unreadable: {exc}") from exc
    expected = int(shape[0]) * int(shape[1]) * 2 * 2 * 2
    if size != expected:
        raise SweepUnusable(
            f"{radio_id} IQ holds {size} bytes, its declared shape "
            f"{list(shape)} needs {expected}")
    if iq.get("bytes") is not None and int(iq["bytes"]) != size:
        raise SweepUnusable(
            f"{radio_id} declares {int(iq['bytes'])} bytes and holds {size}")
    return iq, int(shape[1])


def entry_complete(entry: Path) -> bool:
    """Whether this entry is finished, from the two facts that make it so.

    The manifest is written last and names the size it wrote, so an entry whose
    manifest parses and whose IQ is that long was finished; anything else was
    interrupted and is redone.  Nothing is read past the manifest -- on a
    network share the corpus is thousands of entries and reopening every probe
    would cost more than the import.
    """
    try:
        manifest = json.loads((Path(entry) / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    block = manifest.get("survey_iq") or {}
    if (manifest.get("schema") not in COMPLETE_MANIFEST_SCHEMAS
            or not block.get("sha256")):
        return False
    try:
        return (Path(entry) / SURVEY_IQ_FILENAME).stat().st_size == int(
            block.get("bytes") or -1)
    except OSError:
        return False


def import_sweep(sweep: Path, corpus_root: Path, *, link: bool = True,
                 rebuild: bool = False) -> dict:
    """Turn one paired sweep into up to two corpus entries.

    A radio that errored, or whose IQ contradicts its own declared shape, is
    skipped with its reason and **the other radio is still imported**: each
    radio's eight tunings on two receivers is a complete survey on its own, and
    throwing away the good half of a half-failed sweep would discard a probe
    nothing else on this site holds.
    """
    sweep = Path(sweep)
    corpus_root = Path(corpus_root)
    payload = _read_sweep(sweep)
    sweep_id = sweep.name
    outcome = {"sweep": sweep_id, "imported": 0, "already_imported": 0,
               "skipped": 0, "bytes": 0, "entries": [], "reasons": []}

    # Which radios are importable is settled for the whole sweep before any
    # manifest is written, because each entry states whether its peer made it
    # into the corpus and that cannot be known halfway through.
    usable: dict[str, tuple[dict, int]] = {}
    for radio_id, radio in payload["radios"].items():
        try:
            usable[radio_id] = _importable(radio_id, radio, sweep)
        except SweepUnusable as exc:
            outcome["skipped"] += 1
            outcome["reasons"].append({"radio_id": radio_id,
                                       "reason": str(exc)})
    peers = {radio_id: True for radio_id in usable}

    for radio_id, (iq, per_tuning) in usable.items():
        radio = payload["radios"][radio_id]
        entry = corpus_root / entry_name(sweep_id, radio_id)
        if not rebuild and entry_complete(entry):
            outcome["already_imported"] += 1
            outcome["entries"].append(entry.name)
            continue
        try:
            order = sample_order(radio)
        except SweepUnusable as exc:
            outcome["skipped"] += 1
            outcome["reasons"].append({"radio_id": radio_id,
                                       "reason": str(exc)})
            continue
        if len(order) != int(iq["shape"][0]):
            outcome["skipped"] += 1
            outcome["reasons"].append({
                "radio_id": radio_id,
                "reason": (f"{radio_id} scanned {len(order)} tunings and its "
                           f"IQ holds {int(iq['shape'][0])}")})
            continue
        entry.mkdir(parents=True, exist_ok=True)
        placement = _place(sweep / str(iq["path"]), entry / SURVEY_IQ_FILENAME,
                           link=link)
        digest, written = _digest(entry / SURVEY_IQ_FILENAME)
        manifest = _manifest(radio_id, radio, payload, sweep_id, order,
                             per_tuning, iq, digest, written, placement, peers)
        _write_json(entry / "manifest.json", manifest)
        outcome["imported"] += 1
        outcome["bytes"] += written
        outcome["entries"].append(entry.name)
    return outcome


def _write_json(path: Path, payload: dict) -> None:
    """Publish the manifest in one step, so no reader sees half of one.

    Written last of everything in the entry and renamed into place, which is
    what makes :func:`entry_complete` a safe resume point: a manifest that is
    there was written after the IQ it describes.
    """
    partial = Path(path).with_suffix(".json.partial")
    with partial.open("w") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def sweep_directories(sweep_root: Path) -> tuple[list[Path], int]:
    """Every sweep the collector has finished, oldest first, and how many are not.

    The collector is live and writes the IQ before the record, so the newest
    directory routinely has no ``sweep.json``.  That is neither an error nor
    nothing: reporting it as broken would put a failure in every pass forever,
    and dropping it silently would hide a collector that had stopped halfway
    through a sweep.  So it is excluded from the work and returned as a count.
    The names are UTC stamps, so sorting them is chronological.
    """
    root = Path(sweep_root)
    if not root.is_dir():
        return [], 0
    finished, in_progress = [], 0
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if (path / "sweep.json").is_file():
            finished.append(path)
        else:
            in_progress += 1
    return finished, in_progress


def run(sweep_root: Path, corpus_root: Path, *, limit: int | None = None,
        rebuild: bool = False, link: bool = True,
        sweeps: list[Path] | None = None) -> dict:
    """Import every sweep that is not in the corpus yet.

    ``limit`` counts *sweeps*, not entries, because a sweep is the unit the
    operator can reason about: half a pair in the corpus and half not is the one
    state that makes the synchronisation facts unusable.

    Never raises on one bad sweep.  The corpus is the point and a run that
    stopped on the first truncated file would leave the other thousand sweeps
    unanalysed; reasons are counted and the first few kept.
    """
    corpus_root = Path(corpus_root)
    corpus_root.mkdir(parents=True, exist_ok=True)
    outcome = {"sweeps": 0, "imported": 0, "already_imported": 0, "skipped": 0,
               "unusable": 0, "in_progress": 0, "bytes": 0, "elapsed_s": 0.0,
               "errors": []}
    started = time.perf_counter()
    if sweeps is not None:
        listed = [Path(item) for item in sweeps]
    else:
        listed, outcome["in_progress"] = sweep_directories(sweep_root)
    for sweep in listed:
        if limit is not None and outcome["sweeps"] >= limit:
            break
        outcome["sweeps"] += 1
        try:
            result = import_sweep(sweep, corpus_root, link=link,
                                  rebuild=rebuild)
        except SweepUnusable as exc:
            outcome["unusable"] += 1
            _note(outcome, Path(sweep).name, exc)
            continue
        except Exception as exc:                      # noqa: BLE001 - fail open
            outcome["unusable"] += 1
            _note(outcome, Path(sweep).name, exc)
            continue
        for key in ("imported", "already_imported", "skipped", "bytes"):
            outcome[key] += result[key]
        for reason in result["reasons"]:
            _note(outcome, f"{result['sweep']}/{reason['radio_id']}",
                  reason["reason"])
    outcome["elapsed_s"] = time.perf_counter() - started
    return outcome


def _note(outcome: dict, name: str, exc: object) -> None:
    if len(outcome["errors"]) < 8:
        detail = (f"{type(exc).__name__}: {exc}"
                  if isinstance(exc, BaseException) else str(exc))
        outcome["errors"].append({"sweep": name, "error": detail})


def import_status(sweep_root: Path, corpus_root: Path) -> dict:
    """What the collector holds against what the corpus has taken."""
    listed, in_progress = sweep_directories(sweep_root)
    corpus = Path(corpus_root)
    imported = pending = 0
    if corpus.is_dir():
        held = {path.name for path in corpus.iterdir()
                if path.is_dir() and entry_complete(path)}
    else:
        held = set()
    for sweep in listed:
        try:
            radios = json.loads((sweep / "sweep.json").read_text())["radios"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        names = {entry_name(sweep.name, radio_id) for radio_id in radios}
        taken = len(names & held)
        imported += taken
        if taken < len(names):
            pending += 1
    return {"sweeps": len(listed), "imported_entries": imported,
            "pending_sweeps": pending, "in_progress": in_progress,
            "corpus_entries": len(held), "schema": MANIFEST_SCHEMA}
