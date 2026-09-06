# Vessel Tracking

A streaming pipeline that replays a feed of raw AIS broadcasts through Kafka into
PostgreSQL, and an HTTP API that serves what landed there filtered by vessel, time and
area, as JSON or CSV.

The design treats a **Position Report** as an immutable record of what arrived — where a
vessel *said* it was, at a moment it *said* it was there — and never as a claim about
where the vessel truly was. That distinction drives most of what follows.

- **[CONTEXT.md](CONTEXT.md)** — the project glossary. Where this README uses a term of
  art — Position Report, Report ID, Reported Time, Conflicting Reports — it is defined
  there and used as defined.
- **[docs/adr/](docs/adr/)** — the seven architecture decision records.
- **[docs/api.md](docs/api.md)** — the endpoint reference: every parameter, the response
  shape, content negotiation and every error.
- **[docs/architecture.md](docs/architecture.md)** — the important classes in plain
  language, and how they hand work to each other, end to end.
- **[docs/class-reference.md](docs/class-reference.md)** — every class's methods, one
  table each, one line per method.
- **[docs/implementation-log.md](docs/implementation-log.md)** — what each ticket
  shipped, what the reviews caught, and what was deferred on purpose.
- **[docs/dataset-observations.md](docs/dataset-observations.md)** — the full dataset
  analysis, regenerable from the feed.
- **[docs/test-catalog.md](docs/test-catalog.md)** — every test in the suite, grouped by
  file and by what it proves.
- **[db/SCHEMA.md](db/SCHEMA.md)** — every table, column and index, and why each is
  shaped the way it is.

---

## Running it

Everything below is a command that was run to write this section.

```bash
docker compose up -d --wait          # PostgreSQL/PostGIS, Kafka, Redis, consumer, API
```

Every dependency — PostgreSQL, Kafka, Redis — declares a healthcheck, and everything
that depends on one waits for it, so the command returns when the system is usable rather
than merely started.

The **consumer** runs as a service and is already waiting on the topic. The **producer**
is a job, run on demand:

```bash
docker compose run --rm --build producer vt-producer --rate 0     # unthrottled
docker compose run --rm --build producer vt-producer --rate 200   # a live-looking feed
```

It prints `{"event": "feed_published", "published": 2696, ...}` when done. The consumer
logs its running totals, and the API answers on port 8000:

```bash
curl 'http://localhost:8000/v1/position-reports?mmsi=311486000&limit=2'
curl 'http://localhost:8000/v1/position-reports?format=csv&limit=2'
```

Once the feed has been streamed in, **[`http://localhost:8000/ui/`](http://localhost:8000/ui/)**
serves a small map-based front-end over the same API — vessel tracks, radius search, CSV
download, and the rate limiter and error responses made visible, without writing a single
`curl` command. It's a demo surface, not a new endpoint: same origin as the API, no
separate service to run.

The supplied feed contains no invalid records, so the rejection path needs messages made
for the purpose:

```bash
docker compose run --rm producer vt-producer --rate 0 --inject-invalid 3
```

The consumer then reports `{"event": "ingest_progress", "written": 2696, "rejected": 3}`,
and each rejected message is on the dead-letter topic with the reason it failed:

```bash
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic ais.position-reports.dead-letter \
  --from-beginning --max-messages 3
```

A consumer that should stop on its own — for a demonstration rather than a service —
takes an idle timeout:

```bash
docker compose run --rm -e VT_IDLE_TIMEOUT_SECONDS=5 consumer vt-consumer
```

Configuration is environment variables through one settings object; see
[.env.example](.env.example) for every value and its default. Nothing that differs
between environments is compiled in; a few operational constants — the page size bounds
and the connection pool size — are in code, and are named where they matter below.

### Tests

```bash
pip install -e '.[dev]'
pytest -m "not compose"   # the seam tests: needs Docker, starts its own containers
pytest                    # adds the end-to-end run against a real broker
```

---

## The API

One collection endpoint, `GET /v1/position-reports`. Every filter is optional and they
compose, so a specific question is a single request. Full reference in
[docs/api.md](docs/api.md); OpenAPI at `/openapi.json`.

| | |
| --- | --- |
| **Vessel** | `mmsi`, repeated for several vessels |
| **Time** | `reported_from` (inclusive), `reported_to` (exclusive) — half-open, so adjacent windows never double-count. A value without a timezone is read as UTC |
| **Box** | `min_latitude`, `max_latitude`, `min_longitude`, `max_longitude` — four separately named bounds, so a rejected one can be named back |
| **Circle** | `centre_latitude`, `centre_longitude`, `radius_nautical_miles` — all three or none |
| **Paging** | `after` (a Report ID), `limit` (default 100, maximum 1000) |
| **Format** | `Accept: text/csv`, or `format=csv` for callers who cannot set a header |

Results are ordered by **Report ID** and by nothing else, and paged by keyset on the same
column. `next_cursor` is null exactly when the collection is exhausted — the datastore is
asked for one report beyond the page, so a full last page is not mistaken for a full
page with more behind it.

There is no `sort` parameter, and the endpoint's own description says why: Reported Time
cannot order this data. See [Dataset observations](#dataset-observations).

Errors are RFC 9457 problem documents under `application/problem+json` — validation, not
found, wrong method and unhandled failures alike, because two error formats are worse
than one. Structured validation detail sits in an `errors` member, naming each rejected
parameter.

Requests are limited to ten per minute per client IP, counted in Redis so the limit means
the same thing however many API instances run. The eleventh gets a problem document with
`retry-after` and `ratelimit-*` headers. Every request is written to a `request_log`
table — the refused ones especially, since a log that omits them cannot show abuse.

---

## Datastore Design Decisions

### Why PostgreSQL with PostGIS

The deciding constraint is the identity model. Making **Report ID** the identity of a
Position Report turns idempotency into a primary-key property, and PostgreSQL enforces
that synchronously with `ON CONFLICT (report_id) DO NOTHING`. PostGIS then makes a
metrically correct radius filter a small feature rather than a project. Recorded as
[ADR-0003](docs/adr/0003-postgresql-with-postgis-as-the-datastore.md).

| Datastore | Idempotent write | Consistency model | Geospatial support | Verdict |
|---|---|---|---|---|
| **PostgreSQL + PostGIS** | Synchronous — `ON CONFLICT` rejects a duplicate inside the write itself | Full ACID — a commit is immediately visible everywhere | OGC-compliant `geometry`/`geography`, GiST index, spheroid-accurate `ST_DWithin` | **Chosen** |
| **ClickHouse** | Eventual — see below | Dedup only after a background merge, unless every read pays `FINAL` | `pointInPolygon`, H3; less complete than PostGIS | Rejected — strongest alternative |
| **Elasticsearch** | Synchronous per document `_id` | Near-real-time, not immediate — see below | `geo_point`/`geo_shape`, strong for distance/bbox | Rejected |
| **TimescaleDB** | Same as PostgreSQL (it's the same engine) | Same as PostgreSQL | Full PostGIS compatibility | Rejected *at this scale*, not in principle |

**ClickHouse** was the strongest alternative and is the better fit for production AIS
volumes, but it has no synchronous unique constraint. `ReplacingMergeTree` only collapses
rows sharing a sort key when a background merge happens to run — a query issued moments
after an insert can legitimately see the same Report ID twice, because nothing has merged
them yet. `FINAL` forces that merge at query time instead, but it's not a one-time fix:
it makes the engine redo the merge work on every single read from then on, forever.
A store that cannot express the identity model synchronously would undermine the delivery
guarantee in
[ADR-0004](docs/adr/0004-effectively-once-ingest-via-at-least-once-delivery.md), which
depends on a duplicate being rejected at write time, not eventually.

**Elasticsearch** gives idempotency through document `_id` — re-indexing the same `_id`
overwrites rather than duplicates, and unlike ClickHouse that part is genuinely
synchronous. What isn't synchronous is *read-your-write*: a document isn't guaranteed
searchable until the next index refresh (default ~1 second later), so "written" and
"queryable" are two different moments. It also has strong geospatial support, but its
query model is built for relevance-ranked search, not the exact numeric filtering and
streamed CSV export this API needs — a poor canonical store for this shape of data.

**TimescaleDB** was rejected on scale grounds only, and specifically at this dataset's
scale, not as a technology. It's a PostgreSQL extension — same SQL, same ACID
guarantees, same PostGIS support — so adopting it later is a migration, not a rewrite.
Its hypertable feature auto-partitions a table into time-bounded chunks and skips chunks
a query's time filter can't match. Over 2,696 rows spanning 18 hours, every row lands in
one chunk regardless of chunk size, so there's nothing to skip: the time-partitioning
argument is better made in prose than performed on a Feed too small to need it.

### The data model against the query patterns

One table of Position Reports, append-only, and a second holding the request log. A
Position Report is an observation; nothing updates one, and
**Conflicting Reports** — two reports sharing an MMSI and a Reported Time but disagreeing
about position — are both kept, because the pipeline's job is to record the feed
faithfully and leave adjudication to callers who know their own tolerance for ambiguity
([ADR-0001](docs/adr/0001-position-reports-are-an-append-only-observation-log.md)).

One real row, report 81, run through the whole table — the same row the test suite
uses as its running example:

| Column | Type | Example | Why |
| --- | --- | --- | --- |
| `report_id` | `bigint` primary key | `81` | The feed's `stationId`. The idempotency mechanism, not merely an access path |
| `mmsi` | `integer` | `247039300` | Nine digits fit in four bytes |
| `reported_at` | `timestamptz` | `2013-07-01 13:06:00+00` | Converted from epoch seconds, so time filtering and indexing work naturally |
| `nav_status` | `smallint` | `0` (under way, engine) | **Navigational Status**: self-declared, so only as reliable as the crew setting it |
| `speed_knots` | `numeric(4,1)` | `18.0` | Wire value divided by ten; the 1023 sentinel becomes null, not 102.3 knots |
| `course_degrees` | `smallint` | `144` | **Course**, over the ground. Whole degrees in this Feed, not decidegrees |
| `heading_degrees` | `smallint` null | `144` | **Heading**, where the bow points. 511 is the AIS sentinel for unavailable |
| `rate_of_turn` | `smallint` null | `NULL` | Empty in every supplied record; the type comes from the AIS specification, not the data |
| `latitude`, `longitude` | `double precision` | `42.75178`, `15.4415` | Canonical |
| `position` | `geography(Point,4326)` | `0101000020E6100000355EBA490CE22E40B8E4B8533A604540` | **Generated** from the coordinates, so the two cannot drift |

`position`'s example is what a client actually receives with no cast applied: raw EWKB
hex — byte order, geometry type, SRID, then longitude and latitude as two little-endian
doubles. `SELECT ST_AsText(position) ...` turns the same value into
`POINT(15.4415 42.75178)`.

Units are normalised on write ([ADR-0002](docs/adr/0002-normalise-ais-units-at-ingest.md)),
so no downstream reader has to know that Speed crosses the wire ten times too large.

Access is hand-written SQL over psycopg
([ADR-0005](docs/adr/0005-raw-sql-over-an-orm.md)). Filters compose as fragment-and-parameter
pairs: only fixed fragments defined in the filter object reach the statement text, and
every value a caller supplied travels beside it as a parameter.

### Indexing, and what the planner actually does with it

Three indexes, each matched to a query shape:

| Index | Serves |
| --- | --- |
| `report_id` primary key | Idempotent writes and keyset paging |
| btree `(mmsi, reported_at)` | One vessel over a time window — equality column first, range column last |
| GiST `(position)` | Radius search on the sphere |

**The honest part.** ADR-0003 predicted that at 2,696 rows the planner would sequentially
scan and use none of them. `EXPLAIN ANALYZE` over the real dataset says that is true of
one case and false of two:

| Query | Plan at this volume |
| --- | --- |
| MMSI alone | **Sequential scan.** One vessel is 36% of the table, so a scan is genuinely cheaper and Postgres is right to prefer it |
| MMSI + time window | **Bitmap index scan** on `(mmsi, reported_at)` |
| Radius, 115 nm (967 rows) | **Bitmap index scan** on the GiST index |
| Radius, 5 nm | **Index scan** on the GiST index |

The difference is cost per row, not row count. A btree equality test on a
three-valued column saves the planner nothing; `ST_DWithin` on a geography is expensive
enough per row that the index pays for itself even over 2,696 of them. So the blanket
claim "these indexes will not be used at this data volume" is not one this project can
make. What it can say is that the indexes are chosen for the query shapes the API
exposes, and that at this volume only the low-cardinality equality case degrades to a
scan. ADR-0003's Consequences section has been corrected accordingly.

### Partitioning

Not applied. Monthly `RANGE` partitioning on `reported_at` is the intended scheme at
volume, and 2,696 rows spanning eighteen hours would put every row in one partition —
the ceremony without the benefit. It is described here rather than performed.

### Known trade-offs and limits

- **This does not scale to production AIS ingest**, which runs to billions of positions.
  The migration path is a Timescale hypertable or ClickHouse, and the crossover arrives
  when the working set stops fitting in memory — around the low hundreds of millions of
  reports.
- **The schema is applied once to a fresh database.** There is no migration framework by
  design (ADR-0005); a deployment that must evolve a populated schema needs Alembic or
  Flyway introduced first.
- **Keying by MMSI produces three hot partitions** regardless of the topic's partition
  count. That is a property of a three-vessel sample, not of the design.
- **A CSV export holds a pooled connection** for as long as the caller reads it. The API
  pool is five connections; the consumer has its own, so an abandoned export cannot
  reach ingest.

---

## Geospatial Filtering

### Radius — implemented

A centre and a distance in nautical miles, because that is what a chart is marked in:

```
GET /v1/position-reports?centre_latitude=44.5&centre_longitude=14.0&radius_nautical_miles=60
```

All three parameters or none. Two thirds of a circle is not a smaller circle, it is a
question with no answer, so a partial circle is refused rather than silently ignored.

Distance is evaluated with `ST_DWithin` against the generated `geography` column, so it
is measured across the WGS84 spheroid and a circle is not squashed by latitude. The
difference is not academic at this dataset's latitudes: within 115 nautical miles of one
test centre lie **967** reports on the sphere, where comparing the radius as flat degrees
would return **714**.

The `geography` column is generated from the canonical coordinates rather than written,
so the two cannot drift, and the GiST index over it is used at every radius measured.

### Polygon — sketched, not built

Polygon filtering is deliberately out of scope; radius covers the geospatial requirement.
What it would take:

**Storage and indexing** need nothing new. The same generated `geography` column and its
GiST index serve `ST_Covers(polygon, position)` exactly as they serve `ST_DWithin`. The
work is in validation, not storage: a polygon arriving from a caller may be
self-intersecting, may be wound the wrong way — on a `geography`, ring order decides
which side of the boundary is "inside", so a reversed ring selects the rest of the planet
— and may cross the antimeridian, where a naive coordinate-order assumption produces a
band around the world instead of a shape near it. `ST_IsValid` and an explicit winding
normalisation would have to run before the predicate, and a vertex cap would be needed to
stop one request planning a scan over a thousand-vertex outline.

**The API contract is the real question**, and it is a request-shape question rather than
a geometry one. A polygon does not fit comfortably into a query string:

- *Repeated coordinate parameters* keep every filter uniform and individually
  nameable in a validation error, which is how the bounding box works today. But order
  carries meaning for a polygon and query parameters do not guarantee it, so the contract
  would rest on an ordering the transport does not promise.
- *One encoded parameter* — WKT or GeoJSON in a single value — preserves order and is
  unambiguous, at the cost of URLs long enough to meet proxy limits, and of an error that
  can only say "the polygon is invalid" rather than naming the bound at fault, which is
  the property the four separate box bounds exist to keep.
- *A request body on a POST* removes both problems and creates a third: the operation is
  a read, and making it a POST costs cacheability and makes it invisible to the ordinary
  HTTP machinery a caller expects to work. `GET` with a body is worse still — permitted
  by the specification, honoured by very little.

There is no clean answer, which is exactly why it is worth writing down rather than
guessing at. The shape of the decision is: a polygon is the first filter whose value is
structured, and this API's error contract is built on being able to name the single
parameter that was wrong. Whichever encoding is chosen, that property is what is being
traded away.

---

## Dataset observations

Observations about the supplied data, recorded because they shaped the design. They
describe what the file contains, not what it should. Full analysis, regenerable with
`python analysis/explore_feed.py`, in
[docs/dataset-observations.md](docs/dataset-observations.md).

**2,696 Position Reports, three vessels, 18.2 hours** — 30 June to 1 July 2013. Every
record carries all ten fields; none is null.

**`stationId` is a record identifier, not a receiving station.** Unique across all 2,696
records, ascending in file order, range 81–3396 with gaps. No value repeats even once
across eighteen hours and three vessels, which is not how a receiving station behaves.
It is treated as the **Report ID**, and it is what makes ingest idempotent.

**`(mmsi, timestamp)` is not unique, and not by a small margin.** Only 345 distinct pairs
across 2,696 records. 142 of those keys carry more than one report and none is an exact
duplicate; the busiest carries 119. Where they disagree they disagree wildly — one key
places the same vessel at the same second **515 km** apart. A last-write-wins key on that
pair would discard **2,351 records, 87% of the feed**, while the ingest counter reported
every record written. This is the single observation that most shaped the design: it is
why Report ID is the identity and why Conflicting Reports are kept.

**Reported Time is coarse and repeated.** 219 distinct values, all on exact minute
boundaries, one of them carrying 272 reports spanning the whole geographic range. This is
why there is no ordering by time and no sort parameter: a page boundary inside a group of
equal Reported Times has no defined place to resume from
([ADR-0006](docs/adr/0006-position-results-are-ordered-by-report-id.md)).

**Ordering by Report ID recovers a coherent voyage; ordering by Reported Time does not.** Median
consecutive-hop distance by Report ID is 0.51 km and 0.60 km for two of the three
vessels, against 7.30 km and 10.90 km by Reported Time. Corroborated independently: taking
each report's own Speed, the time needed to cover the distance to the next report in
Report ID order has a median of 67 and 79 seconds — landing on the same minute grid the
Reported Times use, which is what you would expect if `stationId` is the true receipt
sequence.

**The third vessel is incoherent under both orderings**, consistent with two vessels
sharing one MMSI, which is a real AIS phenomenon. Not investigated further; it changes no
decision.

**`rot` is the empty string in every record**, so its column type comes from the AIS
specification rather than from the data.

---

## Testing

Tests sit at three seams and assert only on what a caller or an operator could see: rows
in the datastore, messages on a topic, counter output, an HTTP response.

- **The ingest seam** — feed file to datastore, with the broker faked by passing the
  producer's message stream straight into the consumer's batch processor. Real
  PostgreSQL, no Kafka. Covers ordering, validation, dead-lettering, unit normalisation,
  idempotency under replay, the counters, and crash-and-replay.
- **The HTTP seam** — request to datastore to response, against real PostgreSQL. Covers
  every filter and their combinations, paging, negotiation, CSV, the error document,
  rate limiting and the request log.
- **The Compose smoke seam** — one end-to-end run against real Kafka, Redis and
  PostgreSQL. The only test that touches a real broker.

One kind of test sits below those seams by agreement: the AIS decoding cases the Feed
does not contain — the unavailable-heading and unavailable-speed sentinels, and an empty
Rate of Turn — are covered table-driven, because staging a sentinel through a full ingest
run is awkward and enumerating it is cheap.

Real PostgreSQL rather than a fake, because the behaviour most worth testing — conflict
handling on insert, and the filter predicates — is exactly what a fake would accept and a
real database would reject. CI runs everything but the Compose test against PostGIS and
Redis service containers.

Some things a seam cannot show, and those were run by hand: SIGKILL of the consumer
mid-ingest, which recovered to 2,696 rows and 2,696 distinct Report IDs on restart; and
the rate limiter under Compose, which turned out to be refusing the container's own
health probe until `/healthz` was exempted.

---

## Areas for improvement

Deliberately deferred, so that conscious scoping is not mistaken for oversight.

- **Authentication and authorisation.** Client IP is the only identity, and only for rate
  limiting.
- **TLS.** Plain HTTP under local Compose.
- **Polygon filtering.** Sketched above rather than built.
- **Resolving Conflicting Reports.** The store keeps them; no endpoint picks a winner and
  no "current position of this vessel" answer is offered.
- **Vessel attributes.** No name, type or dimensions — nothing beyond the MMSI.
- **Metrics endpoints.** Counters are logged; there is no Prometheus surface.
- **Time partitioning and a migration framework.** Both described above as the production
  approach and deliberately not applied.
- **Multi-instance deployment.** Compose runs one API worker, though the Redis limiter
  makes that a deployment choice rather than a correctness constraint.
- **Backfill and late-arriving data.** The feed is replayed from a static file.
- **The rate limiter fails open.** If Redis is unreachable the limit is not applied, and
  that is logged at error rather than turning a limiter outage into a total outage. The
  trade is real — anyone who can make Redis unreachable removes the limit — and is
  recorded in [ADR-0007](docs/adr/0007-the-rate-limiter-fails-open.md).
- **A mid-export failure cannot be reported.** CSV streams, so the status is already sent;
  everything up to the first row still gets a problem document.
- **`OperationalError` covers a bad password as well as a dead database**, so a
  misconfigured DSN retries forever rather than failing fast. Loud, but wrong-shaped.

---

## Scaling to production volumes

This system was built and tuned against the supplied 2,696-record sample. Several
sections above already flag a specific point where that sample's scale drove a decision
that production AIS volumes — billions of Position Reports, tens of thousands of
Vessels, a continuous 24/7 stream — would not: ADR-0003's storage crossover, the
unapplied monthly partitioning, the three-vessel partition skew in ADR-0004. This section
draws those together with the questions not yet touched above.

### Ingest

- **Kafka partition key.** MMSI keying (ADR-0004) produces three hot partitions on this
  sample because there are three Vessels. At production cardinality — tens of thousands
  of MMSIs — the same key distributes naturally, but partition count and consumer group
  size stop being defaults and become capacity planning: too few partitions caps
  consumer parallelism regardless of how many consumer processes are run.
- **One consumer, one batch writer.** The pipeline today is a single consumer process
  draining one `BatchWriter` serially. Production throughput needs horizontal consumer
  scaling — multiple processes sharing a consumer group, each with its own `BatchWriter`
  and dead-letter producer, coordinated by Kafka's own partition assignment.
- **Batch size is tuned for a small feed.** 500 records / 1 second bounds today's
  ingest_batch_size and ingest_batch_seconds. At volume this becomes a real trade rather
  than a comfortable default: a larger batch amortises `fsync` cost further but replays
  more on a crash; the right value depends on measured write latency and an accepted
  replay window, not on this feed's size.
- **Backfill and late data.** The pipeline assumes one forward replay of a static file.
  Continuous ingest needs a defined policy for late-arriving reports and for running
  ingest against more than one upstream feed at once, neither of which this design
  addresses.

### Storage

- **Monthly `RANGE` partitioning on `reported_at`**, already described and deliberately
  not applied, becomes necessary once data spans months rather than hours: it bounds the
  working set for indexes and `VACUUM`, and turns data retention into dropping a
  partition instead of a `DELETE` that has to be indexed around.
- **The Timescale/ClickHouse crossover** (ADR-0003) arrives around the low hundreds of
  millions of reports, when the working set stops fitting in memory. A TimescaleDB
  hypertable keeps today's SQL and PostGIS surface, including `ST_DWithin`, with the
  schema in this file mostly intact. ClickHouse trades that surface for raw ingest
  throughput and would need the radius predicate reimplemented without PostGIS —
  geohash or H3 bucketing plus a haversine filter is the usual substitute.
- **The composite index's calculus changes with cardinality.** `EXPLAIN ANALYZE` today
  shows an MMSI-only query preferring a sequential scan, because one of three Vessels is
  36% of the table. At real cardinality — one Vessel among tens of thousands — that same
  query becomes highly selective, and the planner should be expected to use the index
  unconditionally rather than fall back to a scan.
- **A migration framework**, absent by design (ADR-0005) while the schema is applied
  once to an empty database, is the first thing a populated production database needs
  before any further schema change can ship without downtime.

### Querying at scale

- **No "current position" answer exists.** Conflicting Reports are kept, not resolved,
  so a fleet-overview or live-map query has no faster path today than scanning and
  sorting a Vessel's whole history. At volume that wants a materialised view or a
  separate current-position table, maintained incrementally — e.g. a consumer-side
  upsert keyed by `mmsi` that keeps only the newest Report ID — rather than computed
  per request.
- **One database serves both the write path and the read path.** The consumer and the
  API share one PostgreSQL instance today. At volume, ingest and query contend for the
  same I/O and connection budget; a read replica (or a distributed layer's read nodes)
  separates them so a slow export cannot slow ingest and vice versa.
- **Connection pooling stops scaling linearly.** The API's own pool is five connections
  per process. Running many API instances behind a load balancer multiplies that
  directly against PostgreSQL's connection limit; a pooler such as PgBouncer in front of
  the database is the usual fix once instance count is no longer one.
- **CSV export holds a live cursor for the request's whole duration** (noted above as a
  trade-off already). At high concurrency and large export sizes, a queued or
  asynchronous export — generate to object storage, hand back a link — avoids holding
  many simultaneous server-side cursors against the same connection pool.

### Rate limiting and observability

- **Redis is already the shared source of truth** — ADR-0007 rejects a per-process
  fallback counter precisely because it would let a multi-instance fleet exceed the
  configured limit. At production scale, Redis itself needs the same treatment as
  PostgreSQL: a single container is a single point of failure, so a fleet-scale
  deployment runs it as a cluster (Redis Cluster or Sentinel) rather than one instance.
  The fail-open behaviour doesn't change; what changes is how often it's exercised.
- **Metrics are logs today, not a Prometheus surface** (also noted above). Every
  production concern in this section — batch backoff triggering, the read replica
  falling behind, the rate limiter failing open — is only visible today by reading
  structured logs by hand. A metrics endpoint is the prerequisite for noticing any of
  the above happening in real time rather than after the fact.
