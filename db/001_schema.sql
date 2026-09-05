-- Position Report store. Applied once to a fresh database; there is no migration
-- history by design (ADR-0005), so later work edits this file directly.

CREATE EXTENSION IF NOT EXISTS postgis;

-- A Position Report is an immutable observation of what arrived from AIS, never a claim
-- about where a Vessel truly was (ADR-0001). Report ID is the feed's own identifier and
-- is the idempotency mechanism, not merely an access path.
CREATE TABLE IF NOT EXISTS position_report (
    report_id       bigint           PRIMARY KEY,
    mmsi            integer          NOT NULL,
    reported_at     timestamptz      NOT NULL,
    nav_status      smallint         NOT NULL,
    speed_knots     numeric(4,1),
    course_degrees  smallint,
    heading_degrees smallint,
    rate_of_turn    smallint,
    latitude        double precision NOT NULL,
    longitude       double precision NOT NULL
);

-- MMSI is matched by equality and Reported Time by range, so the equality column leads;
-- the reverse order would force a scan of every Vessel's slice of the window.
--
-- What the planner actually does at this volume, measured with EXPLAIN ANALYZE over the
-- supplied 2,696 records: an MMSI-and-window query uses the index, via a bitmap index
-- scan rather than the seek-and-walk the column order suggests. An MMSI-only query does
-- not use it at all - one Vessel is 36% of the table, so a sequential scan is cheaper
-- and Postgres is right to prefer it. The index is sized for the shape of the data
-- rather than for this sample of it, and at production volume both plans change.
CREATE INDEX IF NOT EXISTS position_report_mmsi_reported_at
    ON position_report (mmsi, reported_at);
