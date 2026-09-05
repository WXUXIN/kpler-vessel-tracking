# PostgreSQL with PostGIS as the datastore

Position Reports are stored in PostgreSQL with the PostGIS extension. The deciding
constraint came from ADR-0001: making Report ID the identity turned idempotency into a
primary-key property, and PostgreSQL enforces that synchronously with
`ON CONFLICT (report_id) DO NOTHING`. PostGIS additionally makes a metric-correct radius
filter a forty-line feature rather than a design exercise.

## Considered Options

**ClickHouse** is the better fit for production AIS volumes and was the strongest
alternative, but it has no synchronous unique constraint: `ReplacingMergeTree` deduplicates
eventually, and forcing consistency with `FINAL` on every read is a permanent cost. Choosing
a store that cannot express our identity model would have undermined the delivery guarantee
in ADR-0004.

**Elasticsearch** offers idempotency via document `_id` and strong geospatial support, but is
a poor canonical store for exact numeric filtering and CSV export.

**TimescaleDB** was rejected only on scale grounds: a hypertable over 2,696 rows is ceremony,
and the time-partitioning argument is better made in prose than performed on a dataset too
small to need it.

## Consequences

This choice does not scale to production AIS ingest, which runs to billions of positions.
The migration path is a Timescale hypertable or ClickHouse, and the crossover arrives when
the working set stops fitting in memory — around the low hundreds of millions of reports.

Indexes are designed for the query shape at scale, not for the supplied dataset. Monthly
`RANGE` partitioning on Reported Time is the intended scheme at volume, and is deliberately
not applied here.

An earlier revision of this section went further and said the planner would "correctly
sequentially scan 2,696 rows and use none of them". `EXPLAIN ANALYZE` over the real dataset
disproves that for two of the three query shapes it was measured against:

- **MMSI alone**: a sequential scan, as predicted. One vessel is 36% of the table, so a
  scan is genuinely cheaper.
- **MMSI and a time window**: a bitmap index scan on `(mmsi, reported_at)`.
- **A radius**: a bitmap index scan on the GiST index for wide circles, and a plain index
  scan for narrow ones.

The deciding factor is cost per row rather than row count. A btree equality test on a
three-valued column saves the planner nothing, while `ST_DWithin` on a geography is
expensive enough per row that the index pays for itself even over 2,696 of them. The claim
was written at the same time as the decision, before anything had been measured.
