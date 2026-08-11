import json

import pytest

from leo_tracker.radio.beacon.dashboard_shards import (
    LISTING_ROW_SCHEMA, SHARD_SCHEMA, compact_shards, listing_row,
    migrate_index, read_listing, recording_date, write_listing_row)


def _record(name, *, start_utc, **extra):
    """A full dashboard record, including the heavy fields a listing must drop."""
    return {"recording_id": name, "kind": "beacon", "start_utc": start_utc,
            "status": "confirmed", "confirmed": True, "decoded": True,
            "channel": 4, "mode": "narrow", "duration_s": 120.0,
            "_statistics": {"radio_parameters": {"x": [0] * 500}},
            "_plots": ["a.png"] * 20, "_artifacts": ["b.json"] * 20,
            "_source_signature": [1, 2, 3], **extra}


@pytest.mark.parametrize("name,expected", [
    ("ch4-lower-edge-narrow-20260808T235556Z", "2026-08-08"),
    # Dual-radio captures insert a radio identity before the stamp.
    ("ch4-lower-edge-narrow-pluto-5d4d-20260810T164205Z", "2026-08-10"),
    # Hop children append a session suffix after the stamp.
    ("hop-lower-edge-20260807T201908Z-b00-03-ch4-lower-edge", "2026-08-07"),
])
def test_recording_date_reads_every_naming_form(name, expected):
    """Sharding must never depend on reading a report to learn its date."""
    assert recording_date(name) == expected


def test_recording_date_is_none_when_no_stamp_is_present():
    assert recording_date("not-a-recording") is None


def test_listing_row_drops_the_fields_that_made_the_index_grow():
    row = listing_row(_record("ch4-a-20260810T000000Z", start_utc="2026-08-10T00:00:00Z"))
    assert row["schema"] == LISTING_ROW_SCHEMA
    assert row["recording_id"] == "ch4-a-20260810T000000Z"
    for heavy in ("_statistics", "_plots", "_artifacts", "_source_signature"):
        assert heavy not in row


def test_concurrent_producers_all_survive_compaction(tmp_path):
    """Sixteen workers write rows without a lock; none may be lost.

    The monolithic index required a read-modify-write of the whole file, which
    is why it had to be owned by a single producer.
    """
    for index in range(16):
        name = f"ch4-lower-edge-narrow-20260810T0000{index:02d}Z"
        write_listing_row(tmp_path, name,
                          _record(name, start_utc=f"2026-08-10T00:00:{index:02d}Z"))

    result = compact_shards(tmp_path)

    assert result["folded_rows"] == 16
    assert result["recording_count"] == 16
    shard = json.loads((tmp_path / "reports/dashboard-index/2026-08-10.json").read_text())
    assert shard["schema"] == SHARD_SCHEMA and shard["row_count"] == 16
    # Rows are a write-ahead buffer, removed only once the shard is in place.
    assert not list((tmp_path / "reports/dashboard-rows").glob("*.json"))


def test_compaction_merges_into_an_existing_day_and_is_idempotent(tmp_path):
    first = "ch4-lower-edge-narrow-20260810T000000Z"
    write_listing_row(tmp_path, first, _record(first, start_utc="2026-08-10T00:00:00Z"))
    compact_shards(tmp_path)
    second = "ch4-lower-edge-narrow-20260810T010000Z"
    write_listing_row(tmp_path, second, _record(second, start_utc="2026-08-10T01:00:00Z"))

    compact_shards(tmp_path)
    repeated = compact_shards(tmp_path)

    shard = json.loads((tmp_path / "reports/dashboard-index/2026-08-10.json").read_text())
    assert shard["row_count"] == 2
    assert [row["recording_id"] for row in shard["rows"]] == [first, second]
    # A second run with nothing pending must not disturb the shard.
    assert repeated["folded_rows"] == 0 and repeated["recording_count"] == 2


def test_a_republished_row_replaces_the_older_one(tmp_path):
    name = "ch4-lower-edge-narrow-20260810T000000Z"
    write_listing_row(tmp_path, name, _record(name, start_utc="2026-08-10T00:00:00Z",
                                              candidate_count=1))
    compact_shards(tmp_path)
    write_listing_row(tmp_path, name, _record(name, start_utc="2026-08-10T00:00:00Z",
                                              candidate_count=9))

    compact_shards(tmp_path)

    shard = json.loads((tmp_path / "reports/dashboard-index/2026-08-10.json").read_text())
    assert shard["row_count"] == 1
    assert shard["rows"][0]["candidate_count"] == 9


def test_listing_reads_newest_first_across_days(tmp_path):
    for day in ("08", "09", "10"):
        for minute in range(3):
            name = f"ch4-lower-edge-narrow-202608{day}T00{minute:02d}00Z"
            write_listing_row(tmp_path, name,
                              _record(name, start_utc=f"2026-08-{day}T00:{minute:02d}:00Z"))
    compact_shards(tmp_path)

    newest = read_listing(tmp_path, limit=4)

    assert len(newest) == 4
    assert newest[0]["start_utc"] == "2026-08-10T00:02:00Z"
    assert [row["start_utc"] for row in newest] == sorted(
        (row["start_utc"] for row in newest), reverse=True)


def test_listing_rejects_a_nonpositive_limit(tmp_path):
    with pytest.raises(ValueError, match="limit must be positive"):
        read_listing(tmp_path, limit=0)


def test_migration_projects_a_monolithic_index_without_rereading_reports(tmp_path):
    """Parsing the reports is the expensive half; the index is that work already done."""
    index = tmp_path / "dashboard-index.json"
    index.write_text(json.dumps({
        "schema": "leo-tracker.starlink-beacon-dashboard/v3",
        "recordings": [
            _record("ch4-lower-edge-narrow-20260808T235556Z",
                    start_utc="2026-08-08T23:55:56Z"),
            _record("ch4-lower-edge-narrow-pluto-5d4d-20260810T164205Z",
                    start_utc="2026-08-10T16:42:05Z"),
            _record("no-stamp-here", start_utc="2026-08-10T00:00:00Z")]}))

    result = migrate_index(index, tmp_path)

    assert result["migrated_rows"] == 2
    assert result["undated_rows"] == 1
    assert result["shards_written"] == 2
    shard = json.loads((tmp_path / "reports/dashboard-index/2026-08-10.json").read_text())
    assert shard["rows"][0]["recording_id"].endswith("20260810T164205Z")
    assert "_statistics" not in shard["rows"][0]


def test_migration_refuses_an_unreadable_index(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(ValueError, match="cannot read dashboard index"):
        migrate_index(broken, tmp_path)


def test_summary_counts_only_what_a_header_shows(tmp_path):
    for index, confirmed in enumerate((True, False, True)):
        name = f"ch4-lower-edge-narrow-20260810T00000{index}Z"
        write_listing_row(tmp_path, name, _record(
            name, start_utc=f"2026-08-10T00:00:0{index}Z", confirmed=confirmed,
            decoded=confirmed, qualified_tle_association_count=1 if confirmed else 0))

    compact_shards(tmp_path)

    summary = json.loads(
        (tmp_path / "reports/dashboard-index/summary.json").read_text())
    assert summary["recording_count"] == 3
    assert summary["confirmed_count"] == 2
    assert summary["qualified_association_count"] == 2
    assert summary["by_date"] == {"2026-08-10": 3}


def _association(path, *, qualified, norad=57622, name="STARLINK-30056", rms=120.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"associations": [
        {"track_id": "track-000", "qualified": qualified,
         **({"stability": {"primary": {
             "best_norad_id": norad, "best_name": name,
             "holdout_residual_rms_hz": rms}}} if qualified else {})}]}))


def test_listing_row_names_the_satellite_and_each_provider_fit(tmp_path):
    """A count of qualified associations does not say which spacecraft was found.

    Providers appear side by side because a shared identity across
    independently retrieved catalogs is stronger evidence than either alone.
    """
    from leo_tracker.radio.beacon.dashboard_shards import identity_fields
    name = "ch4-lower-edge-narrow-20260810T000000Z"
    reports = tmp_path / "reports" / "associations"
    _association(reports / f"{name}.json", qualified=True)
    _association(reports / "space-track" / f"{name}.json", qualified=True, rms=67.5)
    _association(reports / "huggingface" / f"{name}.json", qualified=True, rms=57.8)

    fields = identity_fields(tmp_path, name, ("space-track", "huggingface"))

    assert fields["satellite_name"] == "STARLINK-30056"
    assert fields["satellite_norad_id"] == 57622
    assert fields["source_fit_hz"] == {"space-track": 67.5, "huggingface": 57.8}
    assert fields["source_identity_agreement"] is True


def test_listing_row_flags_when_providers_name_different_satellites(tmp_path):
    from leo_tracker.radio.beacon.dashboard_shards import identity_fields
    name = "ch4-lower-edge-narrow-20260810T000000Z"
    reports = tmp_path / "reports" / "associations"
    _association(reports / "space-track" / f"{name}.json", qualified=True, norad=57622)
    _association(reports / "huggingface" / f"{name}.json", qualified=True,
                 norad=11111, name="STARLINK-OTHER")

    fields = identity_fields(tmp_path, name, ("space-track", "huggingface"))

    assert fields["source_identity_agreement"] is False


def test_agreement_is_absent_when_only_one_provider_qualified(tmp_path):
    """Agreement between one provider and nothing is not agreement."""
    from leo_tracker.radio.beacon.dashboard_shards import identity_fields
    name = "ch4-lower-edge-narrow-20260810T000000Z"
    reports = tmp_path / "reports" / "associations"
    _association(reports / "space-track" / f"{name}.json", qualified=True)
    _association(reports / "huggingface" / f"{name}.json", qualified=False)

    fields = identity_fields(tmp_path, name, ("space-track", "huggingface"))

    assert "source_identity_agreement" not in fields
    assert fields["source_fit_hz"] == {"space-track": 120.0}


def test_unqualified_recordings_never_read_association_artifacts(tmp_path, monkeypatch):
    """Roughly one recording in a hundred qualifies; the rest must cost nothing."""
    import leo_tracker.radio.beacon.dashboard_shards as shards
    calls = []
    monkeypatch.setattr(shards, "identity_fields",
                        lambda *a, **k: calls.append(a) or {})
    name = "ch4-lower-edge-narrow-20260810T000000Z"
    shards.write_listing_row(tmp_path, name, {
        "recording_id": name, "start_utc": "2026-08-10T00:00:00Z",
        "qualified_tle_association_count": 0}, ("space-track",))

    assert calls == []


def _activity_row(hours_ago, **extra):
    """A row that started `hours_ago` before a fixed reference moment."""
    from datetime import timedelta
    when = _ACTIVITY_NOW - timedelta(hours=hours_ago)
    return {"start_utc": when.isoformat().replace("+00:00", "Z"),
            "duration_s": 120.0, **extra}


from datetime import datetime, timezone  # noqa: E402

_ACTIVITY_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_activity_windows_are_nested_not_disjoint():
    """The 24 h window reports everything, not only what the 12 h one excluded."""
    from leo_tracker.radio.beacon.dashboard_shards import activity_summary
    rows = [_activity_row(1, radio_id="pluto-a"), _activity_row(8, radio_id="pluto-a"),
            _activity_row(20, radio_id="pluto-a")]

    summary = activity_summary(rows, now=_ACTIVITY_NOW)["by_radio"]["pluto-a"]

    assert summary["6h"]["recordings"] == 1
    assert summary["12h"]["recordings"] == 2
    assert summary["24h"]["recordings"] == 3


def test_duty_is_recorded_time_over_wall_time_for_one_radio():
    from leo_tracker.radio.beacon.dashboard_shards import activity_summary
    # Three 120 s captures in six hours is 360 s of 21600 s.
    rows = [_activity_row(hour, radio_id="pluto-a") for hour in (1, 2, 3)]

    stats = activity_summary(rows, now=_ACTIVITY_NOW)["by_radio"]["pluto-a"]["6h"]

    assert stats["rf_seconds"] == 360.0
    assert stats["duty_fraction"] == pytest.approx(360 / 21600, abs=1e-4)


def test_a_dual_receiver_capture_counts_in_full_against_both_ports():
    """Splitting one capture between its two ports would halve each port's duty.

    Both LNBs are occupied for the whole capture, so both were busy for it.
    """
    from leo_tracker.radio.beacon.dashboard_shards import activity_summary
    rows = [_activity_row(1, radio_id="pluto-a", receiver_labels=["lnb-a", "lnb-b"])]

    by_lnb = activity_summary(rows, now=_ACTIVITY_NOW)["by_lnb"]

    assert by_lnb["lnb-a"]["6h"]["rf_seconds"] == 120.0
    assert by_lnb["lnb-b"]["6h"]["rf_seconds"] == 120.0


def test_global_duty_is_normalised_by_the_radios_that_recorded():
    """Two radios recording at once would otherwise report over 100% busy."""
    from leo_tracker.radio.beacon.dashboard_shards import activity_summary
    rows = [_activity_row(1, radio_id="pluto-a"), _activity_row(1, radio_id="pluto-b")]

    overall = activity_summary(rows, now=_ACTIVITY_NOW)["overall"]["6h"]

    assert overall["radios"] == 2
    assert overall["rf_seconds"] == 240.0
    assert overall["duty_fraction"] == pytest.approx(240 / (21600 * 2), abs=1e-4)


def test_activity_ignores_rows_outside_the_widest_window_and_bad_stamps():
    from leo_tracker.radio.beacon.dashboard_shards import activity_summary
    rows = [_activity_row(1, radio_id="pluto-a"), _activity_row(30, radio_id="pluto-a"),
            {"start_utc": "not-a-time", "duration_s": 120.0, "radio_id": "pluto-a"},
            {"duration_s": 120.0, "radio_id": "pluto-a"},
            # A clock skew that puts a row in the future must not count as recent.
            _activity_row(-2, radio_id="pluto-a")]

    stats = activity_summary(rows, now=_ACTIVITY_NOW)["by_radio"]["pluto-a"]

    assert stats["24h"]["recordings"] == 1


def test_activity_counts_detections_beacons_and_distinct_satellites():
    from leo_tracker.radio.beacon.dashboard_shards import activity_summary
    rows = [_activity_row(1, radio_id="pluto-a", confirmed=True,
                          beacon_detected_count=3, satellite_norad_id=57622,
                          qualified_tle_association_count=1),
            _activity_row(2, radio_id="pluto-a", confirmed=True,
                          beacon_detected_count=2, satellite_norad_id=57622,
                          qualified_tle_association_count=1),
            _activity_row(3, radio_id="pluto-a", confirmed=False,
                          beacon_detected_count=0)]

    stats = activity_summary(rows, now=_ACTIVITY_NOW)["by_radio"]["pluto-a"]["6h"]

    assert stats["confirmed"] == 2 and stats["beacons_detected"] == 5
    assert stats["qualified_associations"] == 2
    # The same spacecraft seen twice is one satellite tracked.
    assert stats["satellites_tracked"] == 1
