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
    longitude       double precision NOT NULL,
    -- Generated rather than written, so it cannot drift from the coordinates it is
    -- derived from. Latitude and longitude stay canonical; this is only how the
    -- datastore is asked geographic questions about them.

    -- ST_SetSRID is used to set the SRID (Spatial Reference System Identifier) of the point to 4326
    -- which corresponds to the WGS 84 coordinate system (the standard for GPS coordinates). This ensures that the point is correctly interpreted in a geographic context.
    
    -- ST_MakePoint creates a point geometry from the longitude and latitude values. 
    -- The resulting geometry is then cast to geography type, which allows for accurate distance calculations on the Earth's surface.
    position        geography(Point, 4326) GENERATED ALWAYS AS
                        (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography)
                        STORED
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

-- Radius search on the sphere. Measured with EXPLAIN ANALYZE over the supplied 2,696
-- records, and the result differs from the composite index above: this one is used at
-- every radius tried - a bitmap index scan for wide circles, a plain index scan for
-- narrow ones. ST_DWithin costs enough per row that the planner reaches for the index
-- even at this volume, which is the opposite of what the row count alone would suggest.
CREATE INDEX IF NOT EXISTS position_report_position
    ON position_report USING GIST (position);

-- Every request that reached the API, including the ones it turned away: a log that
-- omits refused traffic cannot show abuse, which is most of the reason to keep one.
--
-- The client is text rather than inet because it records whatever the transport
-- reported, the same way a Position Report records what the feed said. A value that
-- does not parse as an address is a fact about the request, not a reason to lose it.
CREATE TABLE IF NOT EXISTS request_log (
    id              bigserial   PRIMARY KEY,
    received_at     timestamptz NOT NULL DEFAULT now(),
    method          text        NOT NULL,
    path            text        NOT NULL,
    query           text,
    client_ip       text        NOT NULL,
    status          smallint    NOT NULL,
    duration_ms     numeric(12,3) NOT NULL,
    response_bytes  integer     NOT NULL
);

CREATE INDEX IF NOT EXISTS request_log_received_at ON request_log (received_at DESC);
