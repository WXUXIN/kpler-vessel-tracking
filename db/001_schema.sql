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
