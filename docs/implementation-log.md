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
