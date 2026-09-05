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
