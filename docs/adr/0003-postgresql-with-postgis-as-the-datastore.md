# PostgreSQL with PostGIS as the datastore

Position Reports are stored in PostgreSQL with the PostGIS extension. The deciding
constraint came from ADR-0001: making Report ID the identity turned idempotency into a
primary-key property, and PostgreSQL enforces that synchronously with
`ON CONFLICT (report_id) DO NOTHING`. PostGIS additionally makes a metric-correct radius
filter a forty-line feature rather than a design exercise.

## Considered Options

| Datastore | Idempotent write | Consistency model | Geospatial support | Ingest/analytical scale | Verdict |
|---|---|---|---|---|---|
| **PostgreSQL + PostGIS** | Synchronous, transactional — `INSERT ... ON CONFLICT (report_id) DO NOTHING` rejects the duplicate inside the same statement, no race window | Full ACID — a committed row is immediately and permanently visible to every subsequent read | OGC-compliant `geometry`/`geography` types, GiST spatial index, `ST_DWithin` measures across the WGS84 spheroid | Row-store; ingest ceiling far below a columnar engine's | **Chosen** |
| **ClickHouse** | Asynchronous/eventual — see below | Eventually consistent for dedup unless every read pays `FINAL`'s cost | `pointInPolygon`, H3 grid functions; less complete than PostGIS's OGC stack | Best-in-class columnar ingest throughput and compression | Rejected — strongest alternative |
| **Elasticsearch** | Synchronous at the document level — see below | Near-real-time, not immediate — see below | `geo_point`/`geo_shape`, BKD-tree indexed; strong for distance/bounding-box | Strong for relevance-ranked full-text search | Rejected |
| **TimescaleDB** | Same as PostgreSQL — it's a PostgreSQL extension, not a different engine | Same as PostgreSQL: full ACID | Full PostGIS compatibility — same engine underneath | Hypertable chunking pays off once data spans a long time range | Rejected *here*, not in principle — see below |

**ClickHouse** is the better fit for production AIS volumes and was the strongest
alternative, but it has no synchronous unique constraint. `ReplacingMergeTree` only
deduplicates rows sharing a sort key when a background merge happens to run — a `SELECT`
issued moments after an insert can legitimately return the same Report ID twice, because
the merge that would have collapsed them hasn't executed yet. `FINAL` forces
deduplication at query time instead of waiting for a background merge, but it is not a
one-time toggle: it makes the engine re-merge the rows a query touches on every single
read, which is real CPU and memory cost paid forever, not once. Choosing a store that
cannot express our identity model synchronously would have undermined the delivery
guarantee in ADR-0004, which depends on the primary key rejecting a duplicate at write
time, not eventually.

**Elasticsearch** offers idempotency via document `_id` — re-indexing the same `_id`
overwrites rather than duplicates, and that part genuinely is synchronous, unlike
ClickHouse. What it doesn't offer is *read-your-write* consistency: a document is not
guaranteed visible to search until the next index refresh (default interval ~1 second),
so "written" and "queryable" are two different moments. It also has strong geospatial
support (`geo_point`/`geo_shape`), but its query and pagination model is built around
relevance-ranked search, not the exact numeric filtering, deterministic ordering, and
streamed CSV export this API needs — making it a poor canonical store for this shape of
data even where it's otherwise idempotent.

**TimescaleDB** was rejected only on scale grounds, and the ADR is explicit that this is
a rejection of applying it *now*, not a rejection of the technology. TimescaleDB is a
PostgreSQL extension — same SQL surface, same ACID guarantees, same PostGIS
compatibility — so choosing it later is a migration, not a rewrite. Its hypertable
feature auto-partitions a logical table into time-bounded chunks and prunes chunks a
query's time filter can't match ("chunk exclusion"). At 2,696 rows spanning 18 hours,
every row lives in a single chunk regardless of chunk size, so there is nothing to
prune and nothing to exclude — the time-partitioning argument is better made in prose
than performed on a dataset too small to need it.

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
