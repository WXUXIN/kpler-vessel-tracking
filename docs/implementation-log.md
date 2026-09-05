# Implementation Log

One entry per `/implement` run, newest first. Written to be reviewed rather than to
record history: **Take note** is the section that matters, and it holds the things
needing a human decision, the deliberate deviations, and the gaps a later ticket
inherits. If an entry has nothing under Take note, say so explicitly rather than
dropping the heading.

Append new entries directly below this line.

---

## #4 — Consumer hardening: batching, manual offsets, failure taxonomy, ingest counters

`aea3b93` · 2026-09-05 · 31 tests passing

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
- Batch size, batch time bound and idle timeout moved into `Settings` (with matching
  entries in `.env.example` and `docker-compose.yml`).
- Consumer exits on a configurable idle period and names the reason in its summary.

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

---

## #3 — Validation, Rejected Reports, and the dead-letter path

`d3bddc3` · 2026-09-04 · 26 tests passing

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

### Take note

- Entry reconstructed from commit messages after the fact, so it has no Review-caught or
  Verified detail. Later entries are written during the run.
