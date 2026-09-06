# Database Schema

Reference for [`001_schema.sql`](001_schema.sql), the single file that defines this
project's PostgreSQL schema. There is no migration history: per
[ADR-0005](../docs/adr/0005-raw-sql-over-an-orm.md), the file is applied once, in full,
to a fresh database, and later work edits it directly rather than layering migrations
on top. Docker Compose mounts it at `/docker-entrypoint-initdb.d/001_schema.sql`, which
Postgres's own entrypoint runs automatically the first time the `postgres` container
starts against an empty data volume.

Terms below — Position Report, Report ID, Reported Time, Vessel, MMSI — are used exactly
as [`CONTEXT.md`](../CONTEXT.md) defines them.

## Extension

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

[PostGIS](https://postgis.net/) adds the spatial types, functions, and index support
this schema depends on: the `geography` type, `ST_MakePoint`/`ST_SetSRID`, `ST_DWithin`,
and GiST indexing over geographic data. See
[ADR-0003](../docs/adr/0003-postgresql-with-postgis-as-the-datastore.md) for why
PostgreSQL+PostGIS was chosen over ClickHouse, Elasticsearch, and TimescaleDB.

## Table: `position_report`

One row per Position Report received from the AIS feed. Append-only: rows are inserted
and read, never updated. See
[ADR-0001](../docs/adr/0001-position-reports-are-an-append-only-observation-log.md).

Example values below are one real row from the supplied feed, Report 81 — the same row
the test suite (`tests/test_ingest_seam.py`) uses as its running example.

| Column | Type | Nullable | Example | Description |
|---|---|---|---|---|
| `report_id` | `bigint` | No (primary key) | `81` | The feed's own identifier for the report, sourced from the raw feed's `stationId` field. Unique across the dataset and ascending in receipt order — this is the idempotency mechanism, not merely an access path. |
| `mmsi` | `integer` | No | `247039300` | The Vessel's nine-digit Maritime Mobile Service Identity. Not guaranteed unique to one vessel. |
| `reported_at` | `timestamptz` | No | `2013-07-01 13:06:00+00` | Reported Time — the timestamp the Position Report claims for itself. Coarse and frequently repeated in the source data; untrusted as an ordering key (see [ADR-0006](../docs/adr/0006-position-results-are-ordered-by-report-id.md)). |
| `nav_status` | `smallint` | No | `0` (under way using engine) | Navigational Status: what the Vessel reports itself to be doing (under way, at anchor, moored, aground). Self-declared. |
| `speed_knots` | `numeric(4,1)` | Yes | `18.0` | Speed over the ground, in knots. Normalised at ingest from a wire encoding ten times this value (see [ADR-0002](../docs/adr/0002-normalise-ais-units-at-ingest.md)). |
| `course_degrees` | `smallint` | Yes | `144` | Course: the direction the Vessel is actually travelling over the ground. |
| `heading_degrees` | `smallint` | Yes | `144` | Heading: the direction the Vessel's bow is pointing. Differs from Course when wind or current pushes the vessel sideways. |
| `rate_of_turn` | `smallint` | Yes | `NULL` | How fast the Vessel is turning, in degrees per minute. Empty in every currently supplied record; the column stays nullable rather than assuming that always holds. |
| `latitude` | `double precision` | No | `42.75178` | Canonical latitude, WGS84. Source of truth for position — `position` below is derived from this column, never the reverse. |
| `longitude` | `double precision` | No | `15.4415` | Canonical longitude, WGS84. |
| `position` | `geography(Point, 4326)` | Generated | `0101000020E6100000355EBA490CE22E40B8E4B8533A604540` | Computed automatically from `latitude`/`longitude` (see below). Exists only so the datastore can be asked geographic questions; never written directly. |

### The `position` column

```sql
position geography(Point, 4326) GENERATED ALWAYS AS
    (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography)
    STORED
```

Built in three steps, evaluated automatically on every insert:

1. **`ST_MakePoint(longitude, latitude)`** — constructs a raw point. Argument order is
   x-then-y, i.e. longitude first — the opposite of how a coordinate is normally spoken
   ("42.75°N, 15.44°E").
2. **`ST_SetSRID(..., 4326)`** — tags the point with SRID 4326 (WGS84), the coordinate
   reference system GPS and AIS both use.
3. **`::geography`** — casts to PostGIS's `geography` type, which measures distances and
   containment across the curved surface of the earth (a spheroid), rather than `geometry`'s
   flat-plane Cartesian math. This is what makes a radius search a true circle at any
   latitude instead of one that distorts near the poles.

`GENERATED ALWAYS AS (...) STORED` means Postgres computes and physically stores this
value itself from `latitude`/`longitude` — the application never writes to it and cannot
make it drift from the coordinates it's derived from.

### Reading `position` back out

Querying `position` directly, with no cast, returns raw **EWKB** (Extended Well-Known
Binary) as a hex string — the value most DB clients show by default, and the format used
in this document's example row above. It decodes as:

| Bytes | Meaning | Value for the example row |
|---|---|---|
| `01` | Byte order (1 = little-endian) | little-endian |
| `01000020` | Geometry type + the EWKB "has SRID" flag, as a little-endian `uint32` | Point, SRID present |
| `E6100000` | SRID, little-endian `uint32` | `4326` |
| `355EBA490CE22E40` | X (longitude), little-endian `float64` | `15.4415` |
| `B8E4B8533A604540` | Y (latitude), little-endian `float64` | `42.75178` |

To get the human-readable form instead, cast or convert explicitly:

```sql
SELECT ST_AsText(position) FROM position_report WHERE report_id = 81;
-- POINT(15.4415 42.75178)
```

## Table: `request_log`

One row per HTTP request the API received, including requests it refused (e.g. rate
limits). A log that omitted refused traffic couldn't show abuse, which is most of the
reason to keep one.

Example values below are one representative row: a request for one vessel's reports,
paged at 50 per page.

| Column | Type | Nullable | Example | Description |
|---|---|---|---|---|
| `id` | `bigserial` | No (primary key) | `482` | Surrogate key; no external meaning. |
| `received_at` | `timestamptz` | No, defaults to `now()` | `2026-09-06 08:14:02.331+00` | When the request was received. |
| `method` | `text` | No | `GET` | HTTP method (`GET`, `POST`, etc.). |
| `path` | `text` | No | `/v1/position-reports` | Request path. |
| `query` | `text` | Yes | `mmsi=247039300&limit=50` | Raw query string, if any. |
| `client_ip` | `text` | No | `203.0.113.42` | The client address as the transport reported it. `text` rather than `inet` deliberately: this column records whatever arrived, the same way a Position Report records what the feed said — a value that doesn't parse as an address is a fact about the request, not a reason to lose the row. |
| `status` | `smallint` | No | `200` | HTTP status code returned. |
| `duration_ms` | `numeric(12,3)` | No | `8.421` | Request duration in milliseconds. |
| `response_bytes` | `integer` | No | `1024` | Size of the response body. |

## Indexes

### `position_report_mmsi_reported_at` — `(mmsi, reported_at)`

Supports filtering by Vessel and by a Reported Time window. Column order matters: MMSI
is matched by equality and Reported Time by range, and equality columns lead in a btree
index — the reverse order would force a scan of every Vessel's slice of the window.

Measured with `EXPLAIN ANALYZE` over the supplied 2,696-record dataset
([ADR-0003](../docs/adr/0003-postgresql-with-postgis-as-the-datastore.md)):

- **MMSI + time window** — bitmap index scan, as intended.
- **MMSI alone** — sequential scan. One Vessel is 36% of the table at this volume, so a
  scan is genuinely cheaper than using the index. This index is sized for the shape of
  the data at production volume, not for this sample of it.

### `position_report_position` — `GIST (position)`

A GiST (Generalized Search Tree) index over the generated `position` column — the index
type PostGIS spatial queries require. Backs radius search (`ST_DWithin`).

Measured behaviour differs from the composite index above: this one is used at every
radius tried against the sample data — a bitmap index scan for wide circles, a plain
index scan for narrow ones. `ST_DWithin` costs enough per row that the planner reaches
for the index even at low row counts, which is the opposite of what row count alone
would suggest.

### `request_log_received_at` — `(received_at DESC)`

Supports reading the request log in recency order.

## How the schema is queried

Filters are composed as SQL fragments with parameterised values —
`ReportFilter.conditions()` in [`store.py`](../src/vessel_tracking/store.py) — never by
interpolating a value into query text, so a filter value cannot become SQL however
strange it is ([ADR-0005](../docs/adr/0005-raw-sql-over-an-orm.md)). The radius filter
in particular re-derives a point from the caller's centre coordinates the same way the
`position` column derives its own:

```sql
ST_DWithin(position, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
```

with parameters `(centre_longitude, centre_latitude, radius_metres)` — same
longitude-before-latitude argument order as the generated column, same SRID.

## Conventions for changing this file

- **No migrations.** This file is applied once to a fresh database. A deployment that
  must evolve a populated schema needs a migration tool (Alembic, Flyway) introduced
  first — see [ADR-0005](../docs/adr/0005-raw-sql-over-an-orm.md).
- **`IF NOT EXISTS` throughout**, so the file can be safely re-applied against a database
  that already has the schema (e.g. container restarts without a volume reset).
- **Comments explain decisions, not syntax.** Follow the existing style: a comment above
  a column, table, or index states *why* it's shaped that way, with a reference to the
  relevant ADR where one exists.
- **Not applied at production volume.** Indexing and partitioning choices here are sized
  for the supplied 2,696-record dataset and documented as such; ADR-0003 records the
  intended production migration path (a Timescale hypertable or ClickHouse) and where
  the crossover is expected to fall.
