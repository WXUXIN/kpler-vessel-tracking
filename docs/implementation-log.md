# Implementation Log

One entry per `/implement` run, newest first. Written to be reviewed rather than to
record history: **Take note** is the section that matters, and it holds the things
needing a human decision, the deliberate deviations, and the gaps a later ticket
inherits. If an entry has nothing under Take note, say so explicitly rather than
dropping the heading.

**Files changed** lists every path in the commit with what its change was for, so a
review knows where to look without reading the diff first. Anything unintended that
slipped in belongs there too, named as such.

Append new entries directly below this line.

---

## Postgres published on 5433, not 5432

`feat/demo-frontend` · 2026-09-06 · not a ticket, asked for directly

Asked for directly: Postgres reachable on the host at 5432. It already was, on paper —
`docker-compose.yml` has said `5432:5432` since the tracer bullet. It wasn't reachable in
practice, because this machine runs a native Homebrew PostgreSQL 17 bound to
`127.0.0.1:5432`/`::1:5432`, which macOS resolves ahead of Docker's `0.0.0.0:5432` for
anything connecting via `localhost`. `docker port` reported the container bound and
healthy the whole time; `psql -h localhost -p 5432` was silently reaching the *other*
Postgres instead (confirmed: it answered `role "vessel" does not exist`, which belongs to
neither database).

Given the choice between stopping the native service, reconfiguring it, or moving this
project's container instead, the container moved: lowest blast radius, and every other
project relying on the native install keeps working untouched.

### Done

- `docker-compose.yml`: Postgres published on `5433:5432`. The internal Docker-network
  address (`postgres:5432`, used by every container-to-container connection) is
  unchanged — only the host-side publish moved.
- `settings.py`'s `database_url` default (used only when `VT_DATABASE_URL` is unset,
  i.e. running a component directly on the host rather than via Compose) updated to
  `localhost:5433` to match, so it can't silently resolve to the native Postgres instead.
- `.github/workflows/tests.yml` deliberately **not** changed: CI runs on an isolated
  GitHub-hosted runner with no native Postgres to conflict with, so it has no reason to
  move off 5432.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `docker-compose.yml` | 1 line | Publish Postgres on host port 5433 instead of 5432 |
| `src/vessel_tracking/settings.py` | 1 line | Host-side fallback DSN updated to match |

### Verified

- `psql "postgresql://vessel:vessel@localhost:5433/vessel_tracking"` reaches the
  container (confirmed against the `vessel` role and schema); `localhost:5432` still
  reaches the native install, as expected, and is no longer this project's concern.
- Full `pytest` suite (76 tests) passes locally against the moved port.

### Take note

- **This is a per-machine port conflict, not a project decision.** On a machine without
  a native Postgres already on 5432, the compose file would work unmodified at 5432. If
  that ever changes here (the native install is stopped or moved), the value in
  `docker-compose.yml` is the only thing to revert.
- **First attempt at this also changed `.github/workflows/tests.yml`'s service-container
  port to 5433, but left its `VT_TEST_DATABASE_URL` env var pointing at 5432** — CI failed
  with `connection refused` on every seam test as a result (61 errors). The local suite
  didn't catch this because it uses Testcontainers, which starts its own ephemeral
  container on a random port regardless of anything in this repo's compose file — a
  different code path from what CI actually exercises. Reverted the CI file to its
  original, untouched state rather than fixing the mismatched env var, since CI never
  needed the change in the first place.

## Demo front-end at `/ui`

`feat/demo-frontend` · 2026-09-05 · not a ticket, asked for directly

The API's capabilities — radius search, keyset pagination, content negotiation, RFC
9457 errors, a fail-open rate limiter — were provable only via `curl`. Asked for: a
simple browser page, built from the existing API as-is, that puts them on a map.

### Done

- `static/index.html` + `app.js` + `styles.css`: vanilla JS, no build step, no
  framework. Leaflet (CDN, pinned version) on standard OpenStreetMap tiles.
- Mounted same-origin at `/ui` (`StaticFiles` in `create_app()`) instead of adding
  CORS — a plain `fetch()` from the same origin needs neither.
- Filter sidebar mirrors `ReportQuery` field-for-field: vessel checkboxes (all three
  known MMSIs), time range with two presets, radius search (click the map, drag a
  slider, live circle overlay), bounding box, page size.
- "Load next page" is wired to `after`/`next_cursor`, not a page-number scheme — the
  one control that specifically exercises keyset pagination.
- "Download CSV" issues the same query with `Accept: text/csv`, a real browser
  download.
- A telemetry strip shows status, client-measured duration, and the response's
  `ratelimit-*` headers — which only appear on a 429, not on success (see Take note).
- "Track coherence" panel: fetch one vessel's full history, redraw its path ordered
  by Report ID vs. by Reported Time, to make the finding in
  [Dataset observations](../README.md#dataset-observations) something to look at
  rather than take on faith.
- "Send an invalid query" and "Simulate the rate limit" trigger real 422/429s and
  render the RFC 9457 problem document that comes back.
- `Traffic`'s `UNLIMITED_PATHS` exact-match check couldn't exempt a directory of
  static assets, so it grew a prefix check (`_is_unlimited`) covering `/ui` — loading
  the demo page itself doesn't spend the 10-req/min budget the demo exists to show.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `static/index.html` | +143 | The page structure |
| `static/app.js` | +458 | All interaction: fetch calls, map rendering, telemetry |
| `static/styles.css` | +326 | Dark, map-friendly layout |
| `src/vessel_tracking/api.py` | +14 −2 | `StaticFiles` mount at `/ui`; `_is_unlimited` prefix check replacing the exact-match `UNLIMITED_PATHS` lookup |
| `Dockerfile` | +1 | `COPY static ./static`, alongside the existing `ship_positions.json` copy |
| `README.md` | +16 | "Demo front-end" section, linked from "Running it" and "The API" |

### Verified

- Full flow driven against the real Compose stack with a headless, scripted browser
  (not just `curl`): page load, every filter, radius click-and-drag, bounding box,
  "Load next page", CSV download (a real file landed on disk with the expected rows),
  the invalid-query and rate-limit demos, and the track-coherence toggle — each
  checked against the actual DOM state and, for the map, a rendered screenshot.
- `mypy src/` clean; full `pytest` suite (76 tests) still passes unchanged.

### Take note

- **Two basemap providers turned out not to work before a third did.** The plan
  called for CARTO's dark tiles; the first screenshot showed them watermarked
  "API KEY REQUIRED". Standard OpenStreetMap tiles worked in that session, but
  started returning their "Access denied" placeholder tile once this session's own
  repeated automated screenshotting looked like the bulk/scripted use their
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/)
  prohibits — reported by the user as "a huge black chunk" where the map should be,
  which was `#map`'s dark CSS background showing through failed tile loads. Settled
  on Esri's ArcGIS Online `World_Dark_Gray_Base` — no key, no policy issue at this
  volume, and closer to the original dark control-room look than the OSM fallback
  was anyway.
- **Rate-limit headers only exist on the 429 response**, not on a 200 — confirmed by
  reading `src/vessel_tracking/api.py` (`_problem_response` sets them; the success
  path never does). The telemetry strip reads them opportunistically and says "not
  hit yet" until the first refusal, rather than implying a live countdown the API
  doesn't expose.
- **The Track coherence panel's default vessel mattered more than expected.** It
  defaulted to `247039300` for its own anomaly (looks like two vessels sharing one
  MMSI), but that vessel is noisy in *both* orderings — 45 km/79 km median hop,
  confirmed by computing it directly from the running API's own response. Vessel
  `311486000` is the one the README's "0.5 km vs. 7 km" finding is actually about,
  and only it renders as the intended clean-line-vs.-scribble contrast. Caught by
  screenshotting both toggle states and noticing they looked identical, not by
  reasoning about it in advance — worth remembering that a documented aggregate
  number can be true of the dataset as a whole while being the wrong example for a
  demo of one specific vessel.
- **Search results were briefly invisible on the map** — a filtered query for a
  single vessel returned points already covered by that vessel's own overview track,
  in the same color. Fixed by dimming the overview once a real search or track load
  runs, and outlining result markers in a dark stroke so they read as distinct dots
  regardless of what's underneath. Also caught by screenshot, not by reading the
  code.
- `.github/workflows/tests.yml` and `docker-compose.yml` carry an uncommitted
  `5432→5433` port remap from before this session — not touched, not part of this
  commit; flagging so it isn't mistaken for something this change introduced.

## Endpoint reference in `docs/api.md`

`feat/documentation` · 2026-09-05 · docs only

Not a ticket. Asked for directly: endpoint documentation in the style of the Stripe
reference kept at `example_api.md`.

### Done

- `docs/api.md` follows that reference's structure - title, prose, prerequisites,
  request, response, returns, parameters - and adds the sections this endpoint needs that
  a Stripe charge does not: content negotiation, and every error shape.
- Linked from the README's reference list and from its API section.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `docs/api.md` | +285 | The endpoint reference |
| `README.md` | +4 −2 | Links to it from the two places a reader would look |

### Verified

- **Every response in it was captured from the running system**, not written by hand: the
  JSON page, the CSV rows, both 422 documents, and the 429 with its headers. The stack
  was brought up, the feed replayed, and each request made.
- Every link resolves.

### Take note

- **One captured response documents a rough edge.** Asking with both an out-of-range
  bound and an incomplete circle returns only the bound: rules about individual
  parameters are checked before rules about the whole request, so a caller breaking both
  is told about one, fixes it, and only then learns about the other. That is pydantic's
  ordering rather than a decision, and the documentation says so plainly rather than
  showing a tidier example that hides it.
- The file is `docs/api.md`, chosen rather than asked for. `example_api.md` is the style
  reference and is left untouched.
- No ADR, no code change, no test. The endpoint's behaviour is unchanged; this describes
  what the HTTP seam already asserts.

---

## #12 — Documentation: README, design decisions, and dataset observations

`feat/documentation` · 2026-09-05 · 73 tests passing · 4 files, +420 −5

### Done

- A README with the two sections the exercise names by title, the reasoning behind them,
  the dataset observations that shaped the design, and what was deferred on purpose.
- Every command in it was run before it was written down.
- ADR-0007 records the rate limiter failing open, closing an item #9 flagged for
  "before #12".
- The glossary, the ADRs, the log and the dataset analysis are all linked.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `README.md` | +386 −1 | The document a reviewer reads |
| `docs/adr/0007-the-rate-limiter-fails-open.md` | +38 | The trade the README names, recorded where decisions live |
| `docs/adr/0003-…postgis….md` | +18 −4 | Correcting a prediction the query planner disproves |

### Verified

- 73 tests passing, mypy strict clean, `git diff --cached --check` clean.
- Every documented command was executed: `up --wait`, both producer invocations, the
  curl calls, the dead-letter console consumer, and the idle-timeout consumer run.
- Every figure was checked against `docs/dataset-observations.md`, the schema or the
  tests. The review re-checked all of them independently and found none wrong.

### The acceptance criterion I did not meet as written

One criterion asked the README to state "that the indexes are justified by the query
shape at scale **and will not be used at this data volume**". The second half is a
factual prediction, and `EXPLAIN ANALYZE` disproves it for two of the three query shapes:
MMSI alone is a sequential scan as predicted, but MMSI with a time window uses the
composite index, and every radius tried uses the GiST index. The deciding factor is cost
per row, not row count - a btree equality test on a three-valued column saves nothing,
while `ST_DWithin` on a geography is expensive enough per row to pay for the index over
2,696 of them.

Writing the sentence would have satisfied this criterion while breaking the parent's
story 60: "the limits of that indexing at this data volume stated honestly, so that I am
not shown claims a query plan would disprove". So the README keeps the first half plainly,
gives the measured breakdown, and names the one case that does degrade to a scan.
**ADR-0003 made the same unmeasured claim and has been corrected**, with a note saying it
was written before anything was measured. Both review axes agreed this was the right call.

### Review caught

- **"Each service declares a healthcheck and each dependent waits on it" was false.** The
  consumer and the producer declare none - they are dependents, not dependencies. Fixed
  to say what is true.
- The column table dropped `nav_status` and `course_degrees`, so two terms the glossary
  defines - Navigational Status and Course - appeared nowhere in the README.
- "One table, append-only" while the same README names `request_log` two sections earlier.
- **Glossary drift in my own prose**: "by timestamp" and "the timestamps use", where
  CONTEXT.md puts that word on the avoid list for Reported Time. The third time this
  project has caught that exact drift, and this time in the document that tells everyone
  else to use the glossary.
- "Nothing is hard-coded" was too strong: the page size bounds and the pool size are.
- The seams section claimed three seams and omitted the one agreed exception below them.

### Take note

- **The README documents a system on `main`.** Everything it describes is merged, and CI
  has passed there.
- **Not everything in the README was asked for by #12.** The API section and the testing
  section were not; they serve the parent's stories about OpenAPI and CI, and they are
  what makes it "the README a reviewer actually reads". Cut them if the brief is read
  more narrowly.
- The 967-versus-714 radius figures are the only numbers not regenerable from
  `analysis/explore_feed.py`; they come from the test that asserts them.
- `docs/dataset-observations.md` keeps its name, and the README keeps one heading using
  the word "dataset", though CONTEXT.md avoids it for Feed. The ticket names the section
  that way and the file predates the glossary entry; every other use now says Feed.

---

## #11 — CI: run the test suite on every push

`feat/keyset-pagination` · 2026-09-05 · 76 tests passing · 4 files, +112 −7

### Done

- A workflow runs mypy and the suite on every push and every pull request.
- PostGIS and Redis run as service containers, and the fixtures use them when the
  environment names them, falling back to testcontainers for a developer with Docker.
- The Compose smoke test is excluded: it builds images and drives a whole stack, which
  is a different question from whether the code is correct.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `.github/workflows/tests.yml` | +75 | The workflow, its services, and their health commands |
| `tests/conftest.py` | +25 −4 | Use a provided database and Redis when the environment names them |
| `docker-compose.yml` | +8 −1 | The same health command fix, found here |
| `AGENTS.md` | +4 −2 | Stage before checking whitespace, or new files are never checked |

### Verified

- Full suite 76 passed, mypy strict clean.
- **The CI path was run locally, not assumed**: with `VT_TEST_DATABASE_URL` and
  `VT_TEST_REDIS_URL` pointed at standalone containers, 73 tests pass and no fixture
  starts a container of its own.
- Compose still comes up with the new health command.

### Review caught

- **My healthcheck fix did not fix what its comment claimed, and I had written the claim
  without measuring it.** Both axes flagged it; reproduced directly with a slow init
  script: `pg_isready` *and* `psql` over the unix socket both report success from about
  four seconds, while initdb's temporary server is still running, TCP is refused, and
  `001_schema.sql` has not been applied. Twelve seconds of false confidence, during
  which a dependent would start against a database with no `position_report` in it. Only
  a TCP connection waits for the real server, and it becomes available at the same
  instant the tables do. Both copies now force TCP, and both comments say what was
  measured.
- **The whitespace gate never saw the workflow.** `git diff --check` cannot see
  untracked files, so an entirely new file passes it vacuously. `AGENTS.md` now says to
  stage first.
- `_with_schema(url) -> str` returned its own argument and read as a pure function while
  performing DDL. Now `apply_schema(url) -> None`.
- The fixture docstring claimed both paths "run against the same database". They do not:
  one is fresh per session, the other is whatever was there before.

### Take note

- **The last acceptance criterion cannot be met from here.** "The workflow passes on the
  default branch" needs this merged: `origin/main` contains no `.github/` at all, and
  nothing has ever run. Everything else is done and verified locally; that line needs a
  push, a pull request and a merge.
  **Since closed**: merged as pull request #16, and the workflow's first run on `main`
  passed - service containers initialised, typecheck and suite both green.
- **A same-repo pull request runs the suite twice**, once for `push` and once for
  `pull_request`. Honouring both triggers is what the ticket asks for, so the duplicate
  is accepted rather than removed; a `concurrency` group at least cancels superseded
  runs on the same ref.
- **The `mypy` step was not asked for.** The criterion says "runs the test suite". Kept,
  because a repo configured mypy-strict whose CI does not typecheck is a gate with a
  hole in it, but it can red the workflow for something no criterion covers.
- **The Compose healthcheck change belongs to no ticket.** #11 excludes Compose
  explicitly. It rode along because this is where the defect was found, and leaving a
  healthcheck that lies once it is known to lie seemed worse than the scope.
- Pointing `VT_TEST_DATABASE_URL` at a long-lived database is a trap: every statement in
  the schema is IF NOT EXISTS, so an older schema is silently left alone and the tests
  run against it. Documented in the fixture rather than guarded.

---

## #9 — Rate limiting and request logging

`feat/keyset-pagination` · 2026-09-05 · 76 tests passing · 10 files, +484 −16

### Done

- Redis as a Compose service with a healthcheck; the API waits on it.
- Ten requests per client per minute, counted in Redis so the limit holds across
  however many API instances run.
- The eleventh gets an RFC 9457 document with `retry-after` and the `ratelimit-*` headers.
- Every request recorded with method, path, query, client, status, duration and size -
  refused ones included, since a log that omits them cannot show abuse.
- Records are written off the response path and also printed for container collection.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `src/vessel_tracking/api.py` | +194 −12 | `Traffic` middleware, `RequestLog`, the 429 document, logging configuration |
| `tests/test_http_seam.py` | +145 | The limit, the headers, the log, the probe exemption, a limiter outage |
| `src/vessel_tracking/store.py` | +36 | `RequestRecord` and the insert |
| `tests/conftest.py` | +36 −3 | A real Redis container; separate clients for limited and unlimited tests |
| `tests/test_compose_smoke.py` | +25 | Records on the container's stdout |
| `db/001_schema.sql` | +20 | The `request_log` table |
| `docker-compose.yml` | +13 | The Redis service and its settings |
| `src/vessel_tracking/settings.py`, `.env.example`, `pyproject.toml` | +15 −1 | Redis URL, allowance, window; the `redis` dependency |

### Verified

- Full suite 76 passed, mypy strict clean, `git diff --check` clean.
- By hand against the real stack: ten 200s then a 429 carrying `retry-after: 48`,
  `ratelimit-limit: 10`, `ratelimit-remaining: 0`, and the problem document. The
  `request_log` table held both the served and the refused requests.

### Review caught

- **Records never reached stdout, which is the whole of one acceptance criterion.**
  Uvicorn configures its own loggers and leaves the root without handlers, so the
  effective level for this module was WARNING and every record was built, formatted and
  discarded. Confirmed by applying uvicorn's own logging config and asking. **The test
  suite hid it**: pytest installs a root handler, so it worked everywhere except
  production. Now configured in `create_app`, and asserted in the Compose seam, which is
  the only place a real container's stdout can be read.
- **A Redis outage took the whole API down.** The limiter call sat outside the
  `try/finally`, so a connection error escaped as a bare 500 - not even a problem
  document - and the request was never recorded, blinding the log exactly when it is
  most wanted. `/healthz` is exempt from the limit, so Compose would have gone on
  reporting the container healthy while it failed everything. It now fails open.
- **An unreachable datastore would have taken the API down too.** Each log write parks a
  worker thread until the pool gives up; unbounded, enough of them exhaust the thread
  limiter that ordinary requests stop being served. Records are now dropped, loudly,
  beyond 32 in flight.
- `drain` awaited one snapshot of the pending writes, so a record started during the
  drain was lost. It loops now.
- `recent_requests` and a public `dsn` were production code existing only for tests.
  Removed; the tests read `request_log` with SQL, as an operator would.

### Take note

- **Found by hand, not by any test: the health probe was rate-limiting itself.** Compose
  checks every five seconds - twelve a minute against an allowance of ten - so the API
  would have started refusing its own liveness probe and been marked unhealthy for
  enforcing its own limit. `/healthz` is exempt from the allowance and still logged.
  Nothing in the seam could have caught this; it needed the real stack.
- **Failing open is a decision with a cost**, and it is recorded only here and in a
  docstring. Anyone who can make Redis unreachable removes the rate limit. The
  alternative makes a limiter outage a total outage, which is worse for everyone rather
  than better for anyone - but it is a trade, and it may deserve an ADR before #12.
- The window is keyed on each process's own clock, so instances with skewed clocks can
  count into different windows. Inherent to a fixed window keyed this way.
- `/healthz` is exempt and unauthenticated, so it can be called without limit, and each
  call writes a log row.

---

## #10 — Radius filter

`feat/keyset-pagination` · 2026-09-05 · 69 tests passing · 4 files, +179 −12

### Done

- `position` is a `geography(Point,4326)` column generated from latitude and longitude,
  so the two cannot drift; a GiST index covers it.
- Centre latitude, centre longitude and a radius in nautical miles are accepted together
  and refused unless all three arrive.
- `ST_DWithin` on the geography type measures across the spheroid, so a circle stays a
  circle at any latitude.
- Combines with MMSI, time interval and bounding box.
- **Closes the item entry #5 handed to this ticket**: ADR-0003's geography column and
  GiST index were described but absent from the schema. They exist now.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `tests/test_http_seam.py` | +78 | The radius, its sphere-correctness, the all-or-none rule, the combinations |
| `src/vessel_tracking/api.py` | +59 −11 | Three parameters, the all-or-nothing validator, nautical miles to metres |
| `src/vessel_tracking/store.py` | +27 | The `ST_DWithin` fragment and the circle invariant |
| `db/001_schema.sql` | +15 −1 | The generated column, the GiST index, and what the planner actually does with it |

### Verified

- Full suite 69 passed, mypy strict clean, `git diff --check` clean.
- The expected counts come from a haversine distance computed in Python, not from asking
  the database twice. 967 reports lie within 115 nautical miles of the test centre; a
  flat comparison in degrees would return 714, so the number distinguishes a circle on
  the sphere from a rectangle wearing its name. The nearest report is 8 nautical miles
  from the edge, far outside the ~0.6 nm that sphere and spheroid can disagree by.
- **Measured, and it contradicted what I had written.** I first commented that this index,
  like the composite one, would not earn its place at 2,696 rows. `EXPLAIN ANALYZE` says
  otherwise: the GiST index is used at every radius tried - bitmap index scan for wide
  circles, plain index scan for narrow ones - because `ST_DWithin` costs enough per row
  that the planner reaches for it even at this volume. The comment now says that.

### Review caught

- **`ReportFilter` could be built as a partial circle, and both broken states failed
  silently.** A radius with no centre becomes `ST_MakePoint(NULL, NULL)`, so the
  predicate is NULL and every row is filtered out - an empty page with a 200. A centre
  with no radius emits no fragment at all and is quietly ignored. That is exactly what
  the API validator's own docstring calls worse than refusing, one layer down. The
  dataclass now refuses it too.
- **The combination test was largely vacuous.** The circle already holds one Vessel's
  reports and no other's, so pairing it with that same Vessel removed nothing while
  looking like a combination; the narrowing came entirely from the time bound. And no
  bounding-box leg was tested at all, though the acceptance criterion names it. Each leg
  now narrows something the circle did not.

### Take note

- **The circle invariant is deliberately untested.** A test would have to construct a
  `ReportFilter` directly, and issue #1's testing decisions permit exactly one kind of
  below-seam test - table-driven AIS decoding - and say that reaching beneath a seam is
  otherwise not wanted. The guard exists to stop a future caller failing silently; it is
  not reachable through the seam because the API refuses a partial circle first.
- **Two changes to #8's error contract, made here for this ticket's rule.**
  `InvalidParameter.parameter` is now optional, because a rule about the whole request
  has no single parameter at fault and naming one `"query"` was a lie. And pydantic's
  `"Value error, "` prefix is stripped from every message. Both alter the published
  schema for every route, not just this one.
- The column is named `position`, which `CONTEXT.md` puts on the avoid list for Position
  Report. Kept: here it means the coordinate rather than the record, it is schema-internal
  and never served, and issue #1's schema table names it. Flagged so the choice is on the
  record rather than an oversight.

---

## #7 — Content negotiation and CSV output

`feat/keyset-pagination` · 2026-09-05 · 65 tests passing · 3 files, +364 −25

### Done

- Accept selects JSON or CSV, with a `format` parameter overriding it for callers who
  cannot set a header.
- CSV columns are `PositionReportResource.model_fields`, and every row is that
  resource's own JSON form, so the two formats cannot drift in fields or rendering.
- Streamed from a psycopg named cursor through `StreamingResponse`; nothing buffers.
- Absent values are empty fields; Speed is knots; timestamps are ISO-8601 UTC in both.
- The status is chosen before the first byte: the first report is pulled while a problem
  document is still possible, which is the #8 carry-forward decided here.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `tests/test_http_seam.py` | +171 −1 | Negotiation, CSV shape, streaming, UTC rendering |
| `src/vessel_tracking/api.py` | +155 −20 | Negotiation, CSV rendering, streaming response, UTC normalisation |
| `src/vessel_tracking/store.py` | +38 −4 | `stream_reports` on a server-side cursor; the shared `_select` |

### Verified

- Full suite 65 passed, mypy strict clean, `git diff --check` clean.
- Streaming confirmed as observable at the seam: a `limit=1000` CSV response carries no
  `content-length`, which a buffered body would have to.

### Review caught

- **Accept was a substring search, not negotiation.** `text/csv;q=0` — an explicit
  refusal — was served CSV, and `application/json, text/csv;q=0.1` preferred CSV over
  the caller's stated preference. Now reads q-values: CSV has to be both wanted and
  preferred, and a tie goes to JSON.
- **The export could hold a pooled connection until the collector noticed.** The rows
  were handed to `itertools.chain`, which has no `close()`, so closing the response's
  generator could not reach the store's. The remaining reports are now passed as the
  generator itself and closed in a `finally`, and a failure during priming closes it
  before raising.
- `content-disposition: attachment` was not asked for by the ticket and forces a
  download on the very address-bar caller the override parameter exists for. Removed.
- `stream_reports` had duplicated `list_reports`' whole statement; both now build it
  from one `_select`.
- The streaming property itself was untested, though issue #1 puts "CSV shape and
  streaming" on the HTTP seam.

### Take note

- **One review claim was wrong and worth recording as wrong**: that an abandoned export
  could stall ingest into backoff via pool exhaustion. The API and the consumer are
  separate processes with separate pools, so an export cannot reach ingest. It can still
  exhaust the API's own five connections, which is the real and narrower risk.
- **CSV has no exhaustion signal.** JSON's `next_cursor` is null exactly when done; a
  CSV caller gets a full page and cannot tell whether more exists without asking again.
  A `Link: rel="next"` header would need the extra row known before the body starts, and
  it is not. The AC asks that CSV honour the same ordering and paging, which it does -
  but the two formats are not equally self-describing, and that is a real asymmetry.
- **A mid-export failure still truncates a 200.** Decided rather than left open: the
  status is chosen after the datastore has been reached and the first report pulled, so
  everything except a failure part way through an export gets a problem document. There
  is no way to retract a status already sent.
- `STREAM_CHUNK` is 100 against a page cap of 1,000, so an export is a handful of
  fetches. The cursor is there for the shape of the design, not for this volume.

---

## #8 — RFC 9457 error contract and UTC input handling

`feat/keyset-pagination` · 2026-09-05 · 54 tests passing · 2 files, +293 −8

### Done

- One problem document for every failure: validation, not found, wrong method, and
  anything unhandled. `application/problem+json` on the wire.
- Structured validation detail inside the document as an `errors` extension member, with
  `detail` left as the RFC's human-readable prose rather than a competing structure.
- Each rejected parameter names itself, which is what the four separately named bounds
  were for.
- A bound without a timezone is read as UTC, and the assumption is stated on both
  interval parameters, where a caller passes them.
- The 500 document says nothing about the cause; the cause goes to the log.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `src/vessel_tracking/api.py` | +186 −7 | `Problem`, three exception handlers, the OpenAPI prune, header pass-through |
| `tests/test_http_seam.py` | +107 −1 | Each failure mode, the repeated-parameter case, the published schema |

### Verified

- Full suite 54 passed, mypy strict clean, `git diff --check` clean.
- Probed by hand: unknown path, wrong method, malformed query syntax, out-of-range
  bound, bad value inside a repeated parameter, and an unhandled store failure. All
  return the same document.

### Review caught

- **The document named the wrong thing for repeated parameters.** Pydantic locates a bad
  list item as `("query", "mmsi", 0)`, and taking the last element told the caller to fix
  a parameter called `"0"`. My tests only ever passed bad scalars, so the bug survived
  them. Found by the reviewer running the app rather than reading it.
- **The published schema advertised two formats — inside the ticket whose whole purpose
  is to have one.** Declaring a response `model` makes FastAPI add an `application/json`
  entry, and it carried the `Problem` schema while the media type actually returned
  carried none. A client generated from that document would have expected the wrong one.
- **405 lost its `Allow` header.** The handler dropped `failure.headers`, which Starlette's
  default forwards. One shape everywhere is not worth breaking RFC 9110 for.
- `HTTPStatus(code).phrase` raises on any non-IANA status, so a handler bug would have
  surfaced as a 500 from inside the error handler.
- `instance` was the path, identical on every rejected request to the collection. It now
  carries the query string — the part that actually varied.
- **A tautological assertion**, `"detail" not in document or isinstance(...)`, which could
  not fail. The #5 entry records the review catching this same class of thing; that is
  twice now, and both times in an assertion I wrote to look thorough.
- A stale docstring on `_as_utc` still said this work belonged to a later ticket.

### Take note

- **Two failure modes still escape the contract, both latent.** A failure raised in the
  lifespan produces no ASGI response at all, so no document is possible - unavoidable. A
  failure *part way through a streaming response* sends 200 with a truncated body and no
  document, because the status has already gone. There is no streaming endpoint today,
  but **#7 adds one**: CSV streamed from a server-side cursor. Worth deciding there what
  a mid-stream failure should look like.
- **Unhandled failures are logged twice** under uvicorn: once by `log.exception` with the
  request path, once by Starlette re-raising afterwards. Kept the contextual one.
- No ADR. The spec already records RFC 9457 as the decision, and a second copy would only
  be a second thing to keep in step - the same reasoning as the glossary entry.
- `_as_utc` is now properly this ticket's, closing the item #5 and #6 both carried.

---

## Glossary — the six "missing" terms, and the one that was actually missing

`feat/keyset-pagination` · 2026-09-05 · docs only

Not an `/implement` run. This closes the Take note item that the #4, #5 and #6 entries
each carried forward, and corrects it: the item was wrong.

### Done

- Added **Feed** to `CONTEXT.md`. It was used three times inside the glossary's own
  definitions — "the source feed", "the raw feed's `stationId`", "the source feed
  transmits it" — and defined nowhere. 39 uses across the code and ADRs.
- Added a scope sentence to the glossary header saying what it deliberately excludes,
  so this false gap stops being rediscovered.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `CONTEXT.md` | +11 −1 | The Feed entry, and a header sentence bounding what the glossary is for |
| `docs/implementation-log.md` | this entry | Correcting a claim the log made three times |

### What the earlier entries got wrong

Three entries flagged six terms as glossary gaps: poison message, transient failure,
bounding box, time interval, cursor, page size. Tested against the format's own rule —
*only include terms specific to this project's context; general programming concepts do
not belong even if the project uses them extensively* — **none of the six qualifies**.
All six are general engineering vocabulary that this project happens to use.

Two of them were already defined anyway. `poison message` and `transient failure` appear
in bold in ADR-0004, which is where the decision that gives them meaning lives; copying
them into the glossary would have created a second definition to keep in step with the
first. `bounding box` and `time interval` turned out not to appear in `src/` or the ADRs
at all — they are specification and test language, which is why they felt absent.

Counting undefined terms is not the same as finding gaps. The measure that found the real
one was different: which words do the glossary's own definitions lean on without
defining. That test found exactly one, and it was not on the list.

### Take note

- **The recurring item is closed, not deferred.** If a future entry reports "glossary
  gaps widening" over general vocabulary, this is the answer.
- `Feed` is the only addition. `replay`, `redelivery`, `seam` and `offset` were
  considered and left out on the same rule.
- No ADR. Nothing here was hard to reverse or the result of a real trade-off.

---

## #6 — Ordering and keyset pagination

`feat/keyset-pagination` · 2026-09-05 · 46 tests passing · 4 files, +277 −95

### Done

- Results ordered by Report ID, and by nothing else.
- `after` seeks on Report ID rather than offsetting, so rows arriving behind a cursor
  cannot shift a caller's place.
- Page size defaults to 100 and is capped at 1,000, both published in OpenAPI.
- `next_cursor` is null exactly when the collection is exhausted, because the datastore
  is asked for one report beyond the page rather than guessing from a full-looking one.
- The reason no sort parameter exists is on the endpoint description, so a caller
  looking for one finds the explanation instead.
- Query parameters gathered into a `ReportQuery` model; the endpoint signature went from
  nine parameters to two, and FastAPI still publishes each field separately.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `tests/test_http_seam.py` | +147 −48 | Paging tests; a guarded `pages()` helper; existing filter tests now page for their whole set |
| `src/vessel_tracking/api.py` | +104 −45 | `ReportQuery`, `PositionReportPage.of`, the `ORDERING` text, page-size bounds |
| `tests/test_compose_smoke.py` | +19 −2 | The end-to-end count now pages, over real HTTP |
| `src/vessel_tracking/store.py` | +7 | `after_report_id` as another filter fragment |

### Verified

- Full suite 46 passed, mypy strict clean, `git diff --check` clean.
- The exactly-full final page is tested directly: 869 reports in pages of 79 divides
  exactly, so every page including the last comes back full and the cursor still ends.

### Review caught

- **Avoided vocabulary in caller-facing documentation.** The ordering text said "a group
  of equal timestamps"; CONTEXT.md puts "timestamp" on the avoid list for Reported Time.
  The #3 entry records fixing this same drift in rejection reasons — this one was worse,
  being published in OpenAPI rather than sitting in a comment.
- **A trigger from #5's Take note walked past.** That entry said to revisit the endpoint
  signature "at #6 (paging) or #10 (radius), whichever adds parameters first". This is
  #6, it added two parameters, and the revisit did not happen until the review pointed
  at the entry. Done now rather than deferred again.
- The bounded page silently broke the Compose smoke test, which counted one response and
  called it the whole collection. It pages now, which also puts the cursor through a
  real HTTP round trip rather than only the test client.
- `if cursor` in two page loops: a Report ID of 0 is legal and falsy, so it would have
  restarted paging from the beginning and spun. `is not None` now.
- `fetch_all` had dropped two `status_code == 200` assertions and looped on `while True`.
  Both fixed by the `pages()` helper, which asserts each response and fails rather than
  hangs if a cursor stops advancing.
- Dead condition in the cursor derivation (`not page` could never decide the outcome).

### Take note

- **The empty-collection contract was untested until the review asked.** `next_cursor is
  None` on an empty result is now asserted, but it is worth noticing that the original
  test only checked `items == []` — the half of the contract a caller actually loops on
  was unverified.
- **`ReportQuery` is the third place a filter has to be named**, after `ReportFilter` and
  its `conditions()`. Adding a filter is still two edits rather than four, which is the
  improvement; it is not one edit, and pretending otherwise would be overselling it.
- Glossary gaps still widening: "cursor" and "page size" join "bounding box", "time
  interval", "poison message" and "transient failure" as vocabulary in use but absent
  from `CONTEXT.md`. Six terms now. One `/domain-modeling` pass before #12.
- `_as_utc` still belongs to #8 by the letter of the tickets; unchanged from #5.

---

## #5 — Position Report filters: MMSI, time interval, bounding box

`feat/position-report-filters` · 2026-09-05 · 40 tests passing · 6 files, +328 −40

### Done

- MMSI as a repeated query parameter, one Vessel or many, via `mmsi = ANY(%s)`.
- Half-open time interval: lower bound inclusive, upper exclusive.
- Bounding box as four separately named bounds, each individually validated.
- Every filter optional, and all of them compose with AND in one request.
- Composite `(mmsi, reported_at)` index, equality column leading.
- Filter values reach the database only as parameters; the statement text is assembled
  from fixed fragments defined inside `ReportFilter` and nothing else.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `tests/test_http_seam.py` | +178 −32 | Every filter and their combinations; existing tests moved onto the shared fixture |
| `src/vessel_tracking/store.py` | +63 −3 | `ReportFilter` and `conditions()`, the fragment-and-parameter composition |
| `src/vessel_tracking/api.py` | +53 −5 | Seven query parameters, `_as_utc`, filter construction |
| `db/001_schema.sql` | +12 | The composite index, and an honest note on what the planner does with it |
| `tests/conftest.py` | +17 | `ingested_client`, the feed already loaded behind the API |
| `AGENTS.md` | +5 | `git diff --check` before committing, after whitespace slipped in twice |

### Verified

- Full suite 40 passed, mypy strict clean, `git diff --check` clean.
- **Measured rather than assumed**: `EXPLAIN ANALYZE` over the real 2,696 records shows
  the composite index is used for an MMSI-and-window query (bitmap index scan) and is
  *not* used for MMSI alone — one Vessel is 36% of the table, so a sequential scan wins.
  Recorded beside the index in `db/001_schema.sql`.

### Review caught

- **The index comment claimed a plan the measurement disproves.** It said the index
  "seeks straight to one Vessel and then walks the time window inside it"; the actual
  plan is a bitmap index scan, and for MMSI alone there is no index scan at all. Parent
  requirement 60 asks for exactly the opposite of that: the limits stated honestly, not
  claims a query plan would disprove. Rewritten to say what was measured.
- **The bounding box test proved almost nothing.** The box enclosed one Vessel's whole
  track, and `min_longitude` alone isolates that Vessel, so three of the four bounds
  could have been dropped and the test would still have passed. Its coordinate
  assertions restated the filter over data wholly inside the box, so they could not fail
  independently. Replaced with a box that clips the track, plus a test that exercises
  each bound alone against data spanning it in both directions.
- No closed-interval test: both bounds were only ever sent separately, though together
  is the ordinary case and the one the index serves. Added.
- **Invented Vessel names.** Test constants were `ANCONA`, `LEVANT`, `TUNIS` — made up
  from the geography. CONTEXT.md is explicit that the system holds no Vessel attribute
  beyond the MMSI, so those names asserted knowledge that does not exist. Renamed to
  `NORTHERN_VESSEL` / `EASTERN_VESSEL` / `WESTERN_VESSEL`, which is true of the data.
- The trailing-whitespace artifact struck again, in `producer.py`, a file this ticket
  does not touch — exactly what #4's Take note predicted. Reverted, and prevented rather
  than noted a second time: `AGENTS.md` now requires `git diff --check`.

### Take note

- **The endpoint signature is at seven query parameters and will roughly double.** The
  review argues it is already the wrong shape and wants a FastAPI query-parameter model.
  Deferred on purpose: collapsing it now still needs the mapping into `ReportFilter`, so
  it trades one indirection for another before the need is real. Revisit at #6 (paging)
  or #10 (radius), whichever adds parameters first.
- **`_as_utc` is #8's acceptance criterion, shipped here.** A naive bound compared in the
  server's zone answers a different question, and this is the ticket that first makes
  time filtering possible. It now has a seam test, but #8 still owns documenting the
  assumption and the error contract around bad input. Revert it into #8 if you would
  rather keep the tickets clean.
- The `AGENTS.md` change is unrelated to filters and was flagged as scope creep. Kept
  deliberately: the problem it prevents occurred inside this ticket.
- **Glossary gaps widening.** "Bounding box" and "time interval" are now user-facing
  vocabulary absent from `CONTEXT.md`, alongside "poison message" and "transient
  failure" from #4. Worth one `/domain-modeling` pass before #12.
- ADR-0003's geography column and GiST index are still absent from the schema; the
  radius filter (#10) owns them. Surfaced by the review, not a divergence.

---

## #4 — Consumer hardening: batching, manual offsets, failure taxonomy, ingest counters

`aea3b93` · 2026-09-05 · 31 tests passing · 9 files, +399 −47

### Done

- Auto-commit disabled. A checkpoint writes the batch, flushes dead letters, then
  commits offsets — a crash anywhere before the last step replays messages the Report
  ID primary key discards.
- Offsets held back while any Rejected Report is undelivered, so the record that
  produced it comes back rather than vanishing with it.
- Transient failures retried with capped doubling backoff, never dead-lettered. The
  write blocks the poll loop while it waits, which is the intended trade.
- Shutdown abandons the pending batch instead of retrying forever, so SIGTERM during a
  datastore outage now terminates. The batch kept its place on the topic.
- Batch size, batch time bound and idle timeout moved into `Settings`.
- Consumer exits on a configurable idle period and names the reason in its summary.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `src/vessel_tracking/consumer.py` | +99 −33 | Manual offset commits, `checkpoint()` ordering, progress reporting off totals, idle exit, guarded shutdown, JSON default for decoded reports |
| `src/vessel_tracking/ingest.py` | +101 −4 | `Backoff`, the retry loop, the report-by-report fallback, the `stopping` predicate |
| `src/vessel_tracking/store.py` | +37 −4 | `TransientFailure` vs `UnstorableReport` classification of psycopg errors |
| `src/vessel_tracking/settings.py` | +10 | `ingest_batch_size`, `ingest_batch_seconds`, `idle_timeout_seconds` |
| `tests/test_ingest_seam.py` | +122 −5 | Crash-and-replay pair, transient retry, the refused-row case, `FlakyStore` |
| `tests/test_compose_smoke.py` | +17 | Idle-exit test against the real stack |
| `.env.example` | +9 | The three new settings, documented |
| `docker-compose.yml` | +3 | Same three passed through to the services |
| `src/vessel_tracking/feed.py` | +1 −1 | **Unintended.** A trailing space an editor added, swept in by `git add -A`. Reverted. |

### Verified

- Full suite 31 passed, mypy strict clean, both Compose tests included.
- **By hand, because the seam can only model a crash**: SIGKILL of the consumer
  mid-ingest under Compose, then restart, recovered to 2,696 rows / 2,696 distinct
  Report IDs. Visible in the counters — the restarted consumer's first flush added
  exactly 1 row (the tail of the batch that died before its offset commit), then 1,255
  more. 1,441 + 1,255 = 2,696.

### Review caught

- **A third failure class with no branch.** A report that passes validation but the
  datastore refuses (speed 99999 → 9999.9 knots → overflows `numeric(4,1)`) was
  neither retried nor dead-lettered: it escaped the write leaving the batch uncleared,
  so the shutdown flush hit the same batch and raised again — a crash-loop that never
  printed its summary. Exactly the "retrying a malformed record forever wedges ingest"
  outcome ADR-0004 exists to prevent. Fixed: a refused batch is rewritten report by
  report and only the offending rows are set aside. Now covered by a test.
- **Progress logging went dead on the fast path.** Reporting keyed off the checkpoint's
  own flush, which returns zero when the batch already filled on the size bound — an
  unthrottled run would have logged no progress at all. My manual test missed it
  because I ran the producer throttled, which only exercises the time bound.
- Weak crash test rewritten: the original could not fail for the reason it named.

### Take note

- **`OperationalError` covers a bad password as well as a dead database**, so a
  misconfigured DSN retries forever rather than failing fast. Left deliberately — the
  two are hard to tell apart during startup and every retry logs loudly. Revisit if it
  ever bites.
- **`max.poll.interval.ms` is unset (5 min default).** A datastore outage longer than
  that evicts the consumer from the group; it rejoins and replays from the last commit,
  so correctness holds but throughput does not. Raised in review, deliberately not
  fixed here.
- **`CONTEXT.md` has no entries for "poison message" or "transient failure"**, though
  ADR-0004 and three modules now use both. A real glossary gap — worth `/domain-modeling`
  before #12 (documentation) lands.
- Two crash-and-replay seam tests passed the moment they were written; the idempotency
  they assert arrived with the primary key in #2. They are here because this ticket is
  what makes the replay actually happen.
- **`git add -A` let a stray whitespace edit into the commit.** Caught only when this
  log was written. Worth `git diff --stat` before committing.

---

## #3 — Validation, Rejected Reports, and the dead-letter path

`d3bddc3` · 2026-09-04 · 26 tests passing · 12 files, +583 −72

### Done

- Validation rules live in `domain.py` and nowhere else: out-of-range latitude or
  longitude, malformed MMSI, negative Speed, unparseable Reported Time.
- The producer builds its injected malformed messages from the same constants, so the
  injections cannot drift from the rules the consumer enforces.
- Rejected Reports dead-lettered with a reason and counted apart from written reports.
- Producer gained `--inject-invalid N`, spread through the feed rather than appended.
- AIS unavailable sentinels decode to null (heading 511, speed 1023), covered
  table-driven alongside empty Rate of Turn.
- Rejection reasons name the domain concept, not the wire field — "reported time", not
  "timestamp", per CONTEXT.md's avoid list.

### Files changed

| File | Lines | What changed and why |
| --- | --- | --- |
| `src/vessel_tracking/domain.py` | +171 −14 | The validation rules, `InvalidReport`, `RejectedReport`, the AIS sentinels, `malformed_messages` and the violation table |
| `src/vessel_tracking/consumer.py` | +115 −25 | `KafkaDeadLetters`, both counters in the logs, undecodable-payload routing |
| `src/vessel_tracking/ingest.py` | +50 −7 | `DeadLetters` protocol, `reject()`, the rejected counter |
| `src/vessel_tracking/producer.py` | +41 −3 | `--inject-invalid`, `messages_to_publish`, split summary counters |
| `src/vessel_tracking/settings.py` | +1 | `kafka_dead_letter_topic` |
| `tests/test_ingest_seam.py` | +99 −10 | Rejection through the seam, the per-rule table, injection |
| `tests/test_ais_decoding.py` | +51 | New: the table-driven AIS cases absent from the sample |
| `tests/conftest.py` | +31 −6 | `RecordingDeadLetters`, the stream fixture now mirrors the producer |
| `tests/test_http_seam.py` | +17 −7 | Call sites updated for the required sink |
| `tests/test_compose_smoke.py` | +3 | Build the profiled producer so the smoke test cannot run a stale image |
| `.env.example` | +3 | Dead-letter topic documented |
| `docker-compose.yml` | +1 | Dead-letter topic passed through |

### Verified

- Full suite 26 passed, mypy strict clean.
- By hand against real Kafka: 2,696 written, 5 rejected, each dead letter carrying its
  message and its reason.

### Review caught

- The undecodable-payload branch skipped the time-bound flush, so a run of junk could
  leave a valid batch unwritten. Fixed.
- Dead-letter delivery could fail silently (async produce, no callback, discarded flush
  return). Fixed.
- Vocabulary drift in the reason strings, and a stale ADR-0004 comment. Fixed.

### Take note

- **Three "scope creep" flags I pushed back on rather than reverted**: routing
  undecodable payloads (leaving a known crash in the dead-letter ticket would be a
  knowing gap), the producer's split counters (necessary once injection exists), and
  the Compose stale-image fix below. Reverse any of these if you disagree.
- **The Compose smoke test could previously pass against a stale producer image** — the
  producer is a profiled job, so `up --build` never rebuilt it. Predates this ticket and
  would have masked producer regressions in #2. Fixed here.
- The review argued "validation rules used by both producer and consumer" was only
  half-met, since the producer imports the rules to *construct* violations rather than
  to validate. Judged correct-by-design — a producer that validated could not inject —
  and made observable instead via a distinct-reasons assertion.

---

## #2 — Tracer bullet: a Position Report from feed to API

`8a57a50`, `76f86d4` · 2026-09-02 · reconstructed from git, not from a logged run

### Done

- Feed to producer to Kafka to consumer to PostgreSQL to API, end to end.
- The three test seams every later ticket builds on: ingest, HTTP, Compose smoke.
- Schema shipped deliberately incomplete; later tickets edit it directly (ADR-0005).

### Files changed

Whole-repo scaffolding: `src/vessel_tracking/` (domain, feed, ingest, store, producer,
consumer, api, settings), `db/001_schema.sql`, `tests/` (conftest and the three seams),
`Dockerfile`, `docker-compose.yml`, `.env.example`, `pyproject.toml`. Run
`git show --stat 8a57a50 76f86d4` for the exact list.

### Take note

- Entry reconstructed from commit messages after the fact, so it has no Verified or
  Review-caught detail and no per-file reasons. Later entries are written during the
  run, which is the point of the convention in `AGENTS.md`.
