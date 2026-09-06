# Class Reference

Every class in `src/vessel_tracking/`, its methods, and what each one does — one line
per method, no prose. For *why* a class is shaped this way, see
[architecture.md](architecture.md) (roles and how classes hand off to each other) or the
relevant [ADR](adr/). This document only answers: what can I call, and what does it do?

**23 classes total.** 12 have real behavior (tabled below, one table per class). The
other 11 are plain data holders — no logic, just fields — listed together in the last
table so they aren't repeated eleven times as "no methods."

## Ingest side (`ingest.py`, `consumer.py`)

### `Backoff` — the retry-wait schedule

| Method | Does |
|---|---|
| `wait(attempt)` | Sleeps for `min(initial × 2^(attempt-1), maximum)` seconds, then returns how long it waited. Attempt 1 → 0.5s, attempt 2 → 1s, attempt 3 → 2s, capped at 30s. |

### `DeadLetters` — the interface, not a class with a body

| Method | Does |
|---|---|
| `send(rejected)` | *(Protocol — no implementation here.)* Whatever implements this must accept one `RejectedReport` and get it somewhere it won't be lost. `KafkaDeadLetters` and a test fake both satisfy it. |

### `BatchWriter` — accumulates reports, writes them in bounded batches

| Method | Does |
|---|---|
| `written` *(property)* | How many reports have been successfully written so far. |
| `rejected` *(property)* | How many messages have been dead-lettered so far. |
| `reject(message, reason)` | Sends a message to `DeadLetters` and increments the rejected count. Used both for messages that never decoded and for rows the database refused. |
| `add(message)` | Decodes one raw message. Invalid → `reject()`. Valid → appended to the in-memory batch; if the batch just hit its size limit, calls `flush()`. |
| `flush()` | Writes the whole pending batch in one call, empties the batch, and adds to the running `written` total. No-op if the batch is empty. |
| `_write(batch)` | The actual write, with retry logic: on a transient failure, waits (via `Backoff`) and retries forever; on a permanent one-row refusal, falls back to `_write_separately`. |
| `_write_separately(batch)` | Writes a refused batch one row at a time, so one bad row doesn't cost the other 499 their place. Each row that's individually refused gets dead-lettered. |

### `KafkaDeadLetters` — publishes Rejected Reports to the dead-letter topic

| Method | Does |
|---|---|
| `send(rejected)` | Publishes one Rejected Report (message + reason) to the dead-letter Kafka topic as JSON. |
| `flush()` | Blocks until everything sent so far is confirmed delivered. Returns the number that *didn't* make it — the caller checks this before committing Kafka offsets. |

## Datastore (`store.py`)

### `ReportFilter` — one caller's request, in the datastore's own language

| Method | Does |
|---|---|
| `__post_init__()` | Validates on construction: a radius search needs a centre point *and* a radius, or neither — never a partial circle. |
| `conditions()` | Turns the filter's fields into a list of SQL fragments (`"mmsi = ANY(%s)"`, `"reported_at >= %s"`, etc.) plus their parameters, ready to be joined into a `WHERE` clause. |

### `PositionReportStore` — the only class allowed to run SQL

| Method | Does |
|---|---|
| `close()` | Closes the connection pool. |
| `_select(filters)` *(static)* | Builds the one `SELECT ... WHERE ...` statement both paging and CSV export share. |
| `insert_many(reports)` | Writes a batch of Position Reports in one transaction. Returns rows actually added; raises `TransientFailure` for an unreachable database, `UnstorableReport` for data the database permanently refuses. |
| `stream_reports(limit, filters)` | Yields matching reports one at a time from a server-side cursor, so a large export doesn't load everything into memory at once. |
| `record_request(record)` | Writes one row to `request_log`. |
| `count()` | Total rows in `position_report`. |
| `get(report_id)` | Fetches one report by its Report ID, or `None`. |
| `list_reports(limit, filters)` | Same query as `stream_reports`, but returns a plain list — used for paging one page at a time. |

## Rate limiting (`limits.py`)

### `Limits` — a fixed-window rate limit, counted in Redis

| Method | Does |
|---|---|
| `check(client)` | Increments this client's counter for the current time window and returns a `Decision`: allowed or not, how many requests remain, and how many seconds until the window resets. |
| `close()` | Closes the Redis connection. |

## HTTP layer (`api.py`)

### `RequestLog` — writes request records without a request waiting for them

| Method | Does |
|---|---|
| `record(store, record)` | Fires off a background task to write one request record. If too many writes are already pending, drops the record and logs an error instead — a log write must never block or crash a real request. |
| `_write(store, record)` *(static)* | The actual write, run in a worker thread so it doesn't block the event loop. Any failure here is only logged, never raised. |
| `drain()` | Waits for every in-flight write to finish — called during shutdown so a record already counted as "kept" isn't cancelled. |

### `Traffic` — rate limits, times, and records every request

| Method | Does |
|---|---|
| `_allowance(scope, client)` | Asks `Limits` if this client may proceed. Returns `None` (meaning "allow it") if the path is exempt, or if the limiter itself is unreachable — the limiter fails open. |
| `__call__(scope, receive, send)` | The middleware entry point: checks the allowance, either forwards the request or returns a 429, times the whole thing, and always calls `RequestLog.record` afterwards — success, failure, or refusal alike. |

### `PositionReportResource` — one Position Report, dressed for public view

| Method | Does |
|---|---|
| `of(report)` *(classmethod)* | Converts an internal `PositionReport` into the public JSON shape: normalises the timestamp to UTC, converts `Decimal` speed to `float`. |

### `PositionReportPage` — one page of results, with a cursor for the next

| Method | Does |
|---|---|
| `of(reports, limit)` *(classmethod)* | Builds a page from *one row more than the page size* — if that extra row exists, it becomes `next_cursor`; if not, `next_cursor` is `null`, meaning "this was the last page." |

### `ReportQuery` — the endpoint's query parameters, validated as one object

| Method | Does |
|---|---|
| `_a_circle_is_all_or_nothing()` *(validator)* | Rejects the request if only some of `centre_latitude` / `centre_longitude` / `radius_nautical_miles` were supplied — a partial circle is refused rather than silently ignored. |
| `filters()` | Converts this request into a `ReportFilter` — the same request, in the datastore's vocabulary instead of the web's (nautical miles → metres, etc.). |

## Plain data holders — fields only, no methods worth a table

These carry information between the classes above; none of them contain logic.

| Class | File | What it holds |
|---|---|---|
| `PositionReport` | `domain.py` | One validated observation: Report ID, MMSI, position, speed, course, heading, etc. — all in natural units. |
| `RejectedReport` | `domain.py` | A message that failed validation, plus the reason. |
| `InvalidReport` | `domain.py` | *(Exception.)* Raised when a message breaks a validation rule; carries the reason as `.reason`. |
| `ReportFilter`'s sibling, `RequestRecord` | `store.py` | One HTTP request as the log keeps it: method, path, status, duration, bytes sent. |
| `TransientFailure` | `store.py` | *(Exception.)* Raised when the database is unreachable — retry, don't give up. |
| `UnstorableReport` | `store.py` | *(Exception.)* Raised when the database permanently refuses a row. |
| `IngestResult` | `ingest.py` | The final tally of one ingest run: how many written, how many rejected. |
| `Decision` | `limits.py` | The rate limiter's verdict: allowed or not, remaining allowance, retry-after seconds. |
| `Settings` | `settings.py` | Every configuration value, read from the environment (database URL, Kafka topic, batch size, rate limit, etc.). |
| `InvalidParameter` | `api.py` | One reason a request was rejected — which parameter, and why. |
| `Problem` | `api.py` | The one error shape the API ever returns (RFC 9457): title, status, detail, and a list of `InvalidParameter`. |

## Where to find methods you don't see here

A few free functions do real work without being attached to a class — `from_message()`
(`domain.py`, turns a raw message into a `PositionReport` or raises `InvalidReport`),
`read_feed()` (`feed.py`), `create_app()` (`api.py`, wires every class above together).
They're covered in [architecture.md](architecture.md) rather than here, since this
document is specifically the class/method surface.
