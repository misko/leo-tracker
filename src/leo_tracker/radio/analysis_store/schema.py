"""Versioned schema for the Kalman-owned analysis database."""
from __future__ import annotations

from datetime import datetime, timezone

SCHEMA_VERSION = 1
SCHEMA_NAME = "leo-tracker.analysis-store/v1"

DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    schema_name VARCHAR NOT NULL,
    applied_utc TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS store_metadata (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    commit_sequence BIGINT NOT NULL,
    last_run_id VARCHAR,
    updated_utc TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS recordings (
    recording_id VARCHAR PRIMARY KEY,
    capture_manifest_sha256 VARCHAR NOT NULL,
    created_utc TIMESTAMPTZ,
    radio_id VARCHAR,
    radio_serial VARCHAR,
    receiver_labels_json JSON NOT NULL,
    receiver_count INTEGER NOT NULL,
    channel INTEGER,
    region VARCHAR,
    mode VARCHAR,
    gain_mode VARCHAR,
    configured_gain_db DOUBLE,
    if_center_hz DOUBLE,
    rf_center_hz DOUBLE,
    sample_rate_hz DOUBLE,
    bandwidth_hz DOUBLE,
    duration_s DOUBLE,
    manifest_json JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id VARCHAR PRIMARY KEY,
    recording_id VARCHAR NOT NULL REFERENCES recordings(recording_id),
    pipeline_id VARCHAR NOT NULL,
    mode VARCHAR NOT NULL,
    completed_utc TIMESTAMPTZ NOT NULL,
    completion_sha256 VARCHAR NOT NULL,
    context_path VARCHAR,
    confirmed BOOLEAN NOT NULL,
    full_coverage BOOLEAN NOT NULL,
    input_manifest_json JSON NOT NULL,
    ingested_utc TIMESTAMPTZ NOT NULL,
    commit_sequence BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_parameters (
    run_id VARCHAR PRIMARY KEY REFERENCES analysis_runs(run_id),
    parameters_json JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_summary (
    run_id VARCHAR PRIMARY KEY REFERENCES analysis_runs(run_id),
    window_count BIGINT,
    qualified_window_count BIGINT,
    exact_check_count BIGINT,
    exact_candidate_count BIGINT,
    exact_qualified_count BIGINT,
    single_receiver_candidate_count BIGINT,
    single_receiver_qualified_count BIGINT,
    followup_trigger_count BIGINT,
    exact_sampled_time_s DOUBLE,
    exact_temporal_coverage_fraction DOUBLE,
    summary_json JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_windows (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    window_index INTEGER NOT NULL,
    start_s DOUBLE,
    duration_s DOUBLE,
    qualified BOOLEAN,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, window_index)
);
CREATE TABLE IF NOT EXISTS probe_checks (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    check_index INTEGER NOT NULL,
    start_s DOUBLE,
    duration_s DOUBLE,
    candidate BOOLEAN,
    qualified BOOLEAN,
    followup_trigger BOOLEAN,
    cfo_difference_hz DOUBLE,
    epoch_difference_samples BIGINT,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, check_index)
);
CREATE TABLE IF NOT EXISTS receiver_probes (
    run_id VARCHAR NOT NULL,
    check_index INTEGER NOT NULL,
    receiver_index INTEGER NOT NULL,
    receiver_label VARCHAR,
    candidate BOOLEAN,
    qualified BOOLEAN,
    frequency_offset_hz DOUBLE,
    epoch_sample BIGINT,
    score DOUBLE,
    match_score_margin DOUBLE,
    rms_magnitude DOUBLE,
    near_full_scale_fraction DOUBLE,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, check_index, receiver_index),
    FOREIGN KEY (run_id, check_index) REFERENCES probe_checks(run_id, check_index)
);
CREATE TABLE IF NOT EXISTS followup_checks (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    check_index INTEGER NOT NULL,
    start_s DOUBLE,
    duration_s DOUBLE,
    candidate BOOLEAN,
    qualified BOOLEAN,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, check_index)
);
CREATE TABLE IF NOT EXISTS confirmed_events (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    event_index INTEGER NOT NULL,
    start_s DOUBLE,
    stop_s DOUBLE,
    link_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, event_index)
);
CREATE TABLE IF NOT EXISTS tracks (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    kind VARCHAR NOT NULL,
    track_index INTEGER NOT NULL,
    track_id VARCHAR,
    qualified BOOLEAN,
    observation_count BIGINT,
    valid_duration_s DOUBLE,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, kind, track_index)
);
CREATE TABLE IF NOT EXISTS track_points (
    run_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    track_index INTEGER NOT NULL,
    point_index INTEGER NOT NULL,
    time_s DOUBLE,
    utc TIMESTAMPTZ,
    lock_valid BOOLEAN,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, kind, track_index, point_index),
    FOREIGN KEY (run_id, kind, track_index) REFERENCES tracks(run_id, kind, track_index)
);
CREATE TABLE IF NOT EXISTS decodes (
    run_id VARCHAR PRIMARY KEY REFERENCES analysis_runs(run_id),
    frame_count BIGINT,
    pilot_accuracy DOUBLE,
    sss_accuracy DOUBLE,
    pilot_confidence DOUBLE,
    pilot_evm DOUBLE,
    payload_json JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS associations (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    kind VARCHAR NOT NULL,
    association_index INTEGER NOT NULL,
    track_id VARCHAR,
    qualified BOOLEAN NOT NULL,
    best_norad_id BIGINT,
    best_name VARCHAR,
    holdout_residual_rms_hz DOUBLE,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, kind, association_index)
);
CREATE TABLE IF NOT EXISTS association_candidates (
    run_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    association_index INTEGER NOT NULL,
    candidate_index INTEGER NOT NULL,
    rank INTEGER,
    norad_id BIGINT,
    name VARCHAR,
    train_residual_rms_hz DOUBLE,
    holdout_residual_rms_hz DOUBLE,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, kind, association_index, candidate_index),
    FOREIGN KEY (run_id, kind, association_index)
        REFERENCES associations(run_id, kind, association_index)
);
CREATE TABLE IF NOT EXISTS structured_documents (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    kind VARCHAR NOT NULL,
    schema VARCHAR,
    payload_json JSON NOT NULL,
    PRIMARY KEY (run_id, kind)
);
CREATE TABLE IF NOT EXISTS source_documents (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    kind VARCHAR NOT NULL,
    schema VARCHAR,
    path VARCHAR NOT NULL,
    bytes BIGINT NOT NULL,
    sha256 VARCHAR NOT NULL,
    PRIMARY KEY (run_id, kind)
);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id VARCHAR NOT NULL REFERENCES analysis_runs(run_id),
    kind VARCHAR NOT NULL,
    path VARCHAR NOT NULL,
    media_type VARCHAR,
    bytes BIGINT NOT NULL,
    sha256 VARCHAR NOT NULL,
    PRIMARY KEY (run_id, kind)
);
CREATE TABLE IF NOT EXISTS dashboard_records (
    run_id VARCHAR PRIMARY KEY REFERENCES analysis_runs(run_id),
    recording_id VARCHAR NOT NULL,
    start_utc TIMESTAMPTZ,
    confirmed BOOLEAN NOT NULL,
    decoded BOOLEAN NOT NULL,
    associated BOOLEAN NOT NULL,
    listing_json JSON NOT NULL,
    detail_json JSON NOT NULL
);
CREATE OR REPLACE VIEW current_runs AS
SELECT * EXCLUDE (_rank) FROM (
    SELECT analysis_runs.*,
           row_number() OVER (
               PARTITION BY recording_id
               ORDER BY completed_utc DESC, commit_sequence DESC, run_id DESC
           ) AS _rank
    FROM analysis_runs
) WHERE _rank = 1;
CREATE OR REPLACE VIEW current_dashboard_records AS
SELECT dashboard_records.*
FROM dashboard_records
JOIN current_runs USING (run_id);
CREATE OR REPLACE VIEW probes AS
SELECT r.recording_id AS report,
       rec.radio_id AS radio,
       rec.radio_serial AS serial,
       rec.channel,
       rec.region,
       rec.mode,
       rec.gain_mode,
       epoch(rec.created_utc) AS capture_utc,
       p.start_s,
       p.candidate AS dual_candidate,
       p.qualified AS dual_qualified,
       p.cfo_difference_hz,
       rp.receiver_index AS rx,
       rp.receiver_label AS lnb,
       rp.candidate,
       rp.qualified,
       rp.frequency_offset_hz AS offset_hz,
       rp.epoch_sample AS epoch,
       rp.match_score_margin AS margin,
       rp.rms_magnitude AS rms,
       rp.near_full_scale_fraction AS near_full_scale
FROM receiver_probes rp
JOIN probe_checks p USING (run_id, check_index)
JOIN current_runs r USING (run_id)
JOIN recordings rec USING (recording_id);
"""


def initialize(connection) -> None:
    """Create or validate the current schema on one owner connection."""
    connection.execute(DDL)
    found = connection.execute(
        "SELECT schema_name FROM schema_migrations WHERE version = ?",
        [SCHEMA_VERSION]).fetchone()
    if found is None:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            [SCHEMA_VERSION, SCHEMA_NAME, datetime.now(timezone.utc)])
    elif found[0] != SCHEMA_NAME:
        raise ValueError(f"analysis store schema collision at version {SCHEMA_VERSION}")
    connection.execute(
        "INSERT INTO store_metadata(singleton, commit_sequence) VALUES (TRUE, 0) "
        "ON CONFLICT DO NOTHING")


def validate(connection) -> None:
    found = connection.execute(
        "SELECT schema_name FROM schema_migrations WHERE version = ?",
        [SCHEMA_VERSION]).fetchone()
    if found is None or found[0] != SCHEMA_NAME:
        raise ValueError(f"unsupported analysis store schema: {found!r}")
