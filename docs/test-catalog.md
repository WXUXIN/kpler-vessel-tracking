# Test Catalog

Every test in the suite, grouped by file and by the section comments in each file.
Regenerate by hand when tests are added or renamed — this is a map for a reader, not
generated output.

## `tests/test_ais_decoding.py`

Below the ingest seam on purpose: the unavailable sentinels and an empty Rate of Turn
are awkward to stage through a full ingest run and cheap to enumerate directly, so this
one file reaches straight into `domain.py`.

| Test | Function | What it aims to prove |
|---|---|---|
| AIS wire values decode to natural units | `test_ais_wire_values_decode_to_natural_units` | Parametrized over heading, speed, and Rate of Turn: each AIS "unavailable" sentinel (511, 1023, empty string) decodes to `None`, and ordinary wire values decode to the expected natural unit (e.g. speed 199 → 19.9 knots). |

## `tests/test_ingest_seam.py`

The broker is faked by passing the producer's message stream straight into the
consumer's batch processor. Everything asserted is externally observable: rows in the
datastore, and the counters the pipeline reports.

### Whole-feed ingest

| Test | Function | What it aims to prove |
|---|---|---|
| The whole feed reaches the datastore | `test_the_whole_feed_reaches_the_datastore` | All 2,696 supplied records are written and counted. |
| Replaying the feed writes nothing new | `test_replaying_the_feed_writes_nothing_new` | Ingesting the same feed twice writes 0 new rows the second time — at-least-once delivery must not corrupt the store (ADR-0004). |

### Normalisation and retention

| Test | Function | What it aims to prove |
|---|---|---|
| AIS wire encodings are normalised on write | `test_ais_wire_encodings_are_normalised_on_write` | A written row has speed in knots, Reported Time as a real timestamp, and an absent Rate of Turn stored as null. |
| Conflicting reports are both retained | `test_conflicting_reports_are_both_retained` | Two reports sharing an MMSI and Reported Time but disagreeing on position (~160km apart) are both stored — the store records observations rather than adjudicating between them (ADR-0001). |

### Rejection

| Test | Function | What it aims to prove |
|---|---|---|
| An invalid report is rejected rather than written | `test_an_invalid_report_is_rejected_rather_than_written` | One malformed message mixed into a valid feed is counted as rejected and never reaches the store, while the rest of the feed still writes in full. |
| A report breaking a rule is dead-lettered with its reason | `test_a_report_breaking_a_rule_is_dead_lettered_with_its_reason` | Parametrized over five broken fields (bad latitude, longitude, MMSI, speed, timestamp): each is rejected, written nowhere, and dead-lettered with a reason naming the broken field. |
| Injected malformed messages are rejected while the feed is written | `test_injected_malformed_messages_are_rejected_while_the_feed_is_written` | Five malformed messages spread through the real feed are all rejected with five distinct reasons, while all 2,696 valid records still land. |
| The supplied feed alone is rejected nowhere | `test_the_supplied_feed_alone_is_rejected_nowhere` | The feed as supplied contains no invalid records: 2,696 written, 0 rejected, nothing dead-lettered. |

### Crash recovery (ADR-0004)

| Test | Function | What it aims to prove |
|---|---|---|
| A batch lost to a crash comes back on replay | `test_a_batch_lost_to_a_crash_comes_back_on_replay` | A writer that dies holding an unflushed batch loses nothing: redelivery from the last committed offset re-ingests exactly the records that never made it to disk. |
| A batch written before a crash is not written twice | `test_a_batch_written_before_a_crash_is_not_written_twice` | Replaying records that were already written before a crash writes 0 new rows — the Report ID primary key absorbs the duplicate delivery (ADR-0004). |
| A transient failure is waited out rather than dead-lettered | `test_a_transient_failure_is_waited_out_rather_than_dead_lettered` | Against a store that fails 3 times before succeeding, the writer retries with the documented backoff schedule (0.5s, 1s, 2s) and eventually writes everything — nothing reaches the dead-letter topic over an outage. |
| A report the datastore refuses does not take its batch down | `test_a_report_the_datastore_refuses_does_not_take_its_batch_down` | One row the datastore permanently refuses (a value that overflows a column) is isolated and dead-lettered, while the other 2,696 good rows in its batch still get written. |

## `tests/test_http_seam.py`

Request → datastore → response, against real PostgreSQL.

### Basic retrieval and ordering

| Test | Function | What it aims to prove |
|---|---|---|
| The collection is empty before anything is ingested | `test_the_collection_is_empty_before_anything_is_ingested` | An empty store serves an empty page with no cursor, not an error. |
| Ingested position reports are served | `test_ingested_position_reports_are_served` | Paging to the end returns exactly the 2,696 ingested reports. |
| Position reports are served in natural units | `test_position_reports_are_served_in_natural_units` | The first served report has speed in knots, an ISO timestamp, and null Rate of Turn — no caller has to know the wire units. |
| Position reports are served in Report ID order | `test_position_reports_are_served_in_report_id_order` | Reports come back in ascending Report ID (receipt sequence), not Reported Time, which the data cannot support (ADR-0006). |

### Filtering by vessel (MMSI)

| Test | Function | What it aims to prove |
|---|---|---|
| Reports can be narrowed to one vessel | `test_reports_can_be_narrowed_to_one_vessel` | Filtering by one MMSI returns only that vessel's reports, at its exact count. |
| Several vessels can be asked about at once | `test_several_vessels_can_be_asked_about_at_once` | A repeated `mmsi` parameter (no comma-separated list) returns the union of both vessels' reports. |

### Filtering by time interval

| Test | Function | What it aims to prove |
|---|---|---|
| The time interval is half-open | `test_the_time_interval_is_half_open` | Reports sitting exactly on a boundary instant belong to the window that starts there, never the one that ends there — the two adjacent windows partition the feed exactly, with no overlap or gap. |
| A bound without a timezone is read as UTC | `test_a_bound_without_a_timezone_is_read_as_utc` | A naive ISO-8601 timestamp (no offset) is interpreted as UTC, not the server's local zone. |

### Filtering by bounding box

| Test | Function | What it aims to prove |
|---|---|---|
| Reports can be narrowed to a bounding box | `test_reports_can_be_narrowed_to_a_bounding_box` | A box clipping (not just enclosing) one vessel's track returns exactly the reports inside it. |
| Each bound of the box narrows in its own direction | `test_each_bound_of_the_box_narrows_in_its_own_direction` | Each of the four bounds (min/max latitude/longitude) is wired to the correct column and the correct direction, tested one at a time against data spanning it. |

### Combining filters

| Test | Function | What it aims to prove |
|---|---|---|
| Both bounds together make a closed window | `test_both_bounds_together_make_a_closed_window` | A `reported_from` + `reported_to` pair narrows further than either bound alone — both are applied, not just the last one. |
| Filters combine in one request | `test_filters_combine_in_one_request` | MMSI, time, and bounding-box filters together narrow the result to the intersection of all three, in a single call. |
| Combining filters narrows rather than widens | `test_combining_filters_narrows_rather_than_widens` | A box holding one vessel's track, filtered for a different vessel, returns nothing — filters intersect, they don't union. |

### Pagination (keyset)

| Test | Function | What it aims to prove |
|---|---|---|
| A page is bounded even when nothing is asked for | `test_a_page_is_bounded_even_when_nothing_is_asked_for` | An unfiltered request still returns only `DEFAULT_PAGE_SIZE` items, with a cursor for the next page. |
| The cursor seeks past the reports already seen | `test_the_cursor_seeks_past_the_reports_already_seen` | The second page's items all have a Report ID greater than the cursor, and share no IDs with the first page — keyset paging, not offset paging. |
| Paging a filtered set returns every match exactly once | `test_paging_a_filtered_set_returns_every_match_exactly_once` | Walking every page of a filtered query returns every matching Report ID exactly once, in order, with no drift or duplication. |
| Page size has a documented maximum | `test_page_size_has_a_documented_maximum` | `limit=MAX_PAGE_SIZE` succeeds; `limit=MAX_PAGE_SIZE + 1` is rejected with 422 — a caller cannot ask for the whole table. |
| No alternative ordering is offered and the reason is published | `test_no_alternative_ordering_is_offered_and_the_reason_is_published` | The OpenAPI schema offers no `sort`/`order` parameter, and its description explains why (Report ID vs. Reported Time). |
| A last page that is exactly full still ends the paging | `test_a_last_page_that_is_exactly_full_still_ends_the_paging` | When the final page divides evenly (869 reports ÷ 79), it still correctly reports no next cursor rather than sending the caller back for an empty page. |

### Error handling (RFC 9457 problem documents)

| Test | Function | What it aims to prove |
|---|---|---|
| An invalid parameter returns a problem document | `test_an_invalid_parameter_returns_a_problem_document` | An out-of-range `limit` returns a 422 problem document naming `limit` in `errors`, with prose detail separate from structure. |
| The problem document names the bound that was wrong | `test_the_problem_document_names_the_bound_that_was_wrong` | Two simultaneously invalid bounds are both named individually in `errors`, each with its own detail. |
| An unknown path returns the same problem shape | `test_an_unknown_path_returns_the_same_problem_shape` | A 404 for an unrecognised path uses the same problem-document shape as validation errors (minus the `errors` array). |
| A failure inside the API returns the same problem shape | `test_a_failure_inside_the_api_returns_the_same_problem_shape` | An unhandled exception (via a store that raises) becomes a 500 problem document that leaks nothing of the underlying error message. |
| The UTC assumption is documented | `test_the_utc_assumption_is_documented` | The OpenAPI description for every `reported_*` parameter states the UTC assumption in words, not just in behaviour. |
| A bad value in a repeated parameter names the parameter | `test_a_bad_value_in_a_repeated_parameter_names_the_parameter` | An invalid `mmsi` value is named as `"mmsi"` in the error, not by its position in the repeated list. |
| The wrong method keeps the headers the status requires | `test_the_wrong_method_keeps_the_headers_the_status_requires` | A 405 response still carries the RFC-required `Allow` header, even inside the uniform problem-document shape. |
| The published schema offers only the problem media type | `test_the_published_schema_offers_only_the_problem_media_type` | The OpenAPI schema's 422/500 responses declare only `application/problem+json`, so a generated client expects what the wire actually returns. |

### Content negotiation (JSON vs. CSV)

| Test | Function | What it aims to prove |
|---|---|---|
| An Accept header selects CSV | `test_an_accept_header_selects_csv` | `Accept: text/csv` returns a CSV body with the correct row count. |
| A query parameter overrides the header | `test_a_query_parameter_overrides_the_header` | `?format=csv` wins over a conflicting `Accept: application/json` header, so a browser address bar (which can't set headers) can still request CSV. |
| CSV and JSON carry the same fields and values | `test_csv_and_json_carry_the_same_fields_and_values` | The same query in both formats produces the same fields in the same order with matching values — switching format doesn't change the data. |
| CSV renders absent values as empty fields | `test_csv_renders_absent_values_as_empty_fields` | A null Rate of Turn renders as an empty CSV field, not the string `"None"`. |
| CSV honours the same ordering and paging | `test_csv_honours_the_same_ordering_and_paging` | CSV export respects `mmsi`, `limit`, and `after` exactly like JSON does. |
| A failure before the first row still gets a problem document | `test_a_failure_before_the_first_row_still_gets_a_problem_document` | A store failure that occurs before streaming starts still produces a proper problem document, not a truncated 200. |
| Both formats render time as UTC whatever the session says | `test_both_formats_render_time_as_utc_whatever_the_session_says` | A report constructed with a non-UTC-tagged timestamp still renders as UTC in the JSON output, regardless of the database session's timezone. |
| The schema offers both formats | `test_the_schema_offers_both_formats` | The OpenAPI 200 response declares exactly `application/json` and `text/csv`. |
| A refused format is not served | `test_a_refused_format_is_not_served` | `Accept: text/csv;q=0` (explicitly refusing CSV) is honoured — the response comes back as JSON. |
| The format the caller prefers wins | `test_the_format_the_caller_prefers_wins` | Content negotiation correctly resolves q-value ties and preferences across three different Accept headers. |
| CSV is streamed rather than buffered | `test_csv_is_streamed_rather_than_buffered` | A large CSV export has no `Content-Length` header — proof the body streams from a server-side cursor rather than being buffered and measured first. |

### Radius (geospatial) search

| Test | Function | What it aims to prove |
|---|---|---|
| Reports can be narrowed to a radius | `test_reports_can_be_narrowed_to_a_radius` | A circular search (centre + radius) returns exactly the reports inside it, all from one vessel. |
| The radius is measured on the sphere | `test_the_radius_is_measured_on_the_sphere` | A radius search returns 967 reports where a flat-degree approximation would only return 714 — the circle is geodesic (WGS84), not a rectangle wearing a circle's name. |
| A partial radius is rejected rather than ignored | `test_a_partial_radius_is_rejected_rather_than_ignored` | Supplying only `radius_nautical_miles` without a centre point is a 422 (a question with no answer), not a silently ignored parameter. |
| The radius combines with the other filters | `test_the_radius_combines_with_the_other_filters` | A radius filter narrows further when paired with a bounding box, a time window, or a different vessel — each pairing removes something the circle alone did not. |

### Rate limiting and request logging

| Test | Function | What it aims to prove |
|---|---|---|
| The eleventh request in a minute is turned away | `test_the_eleventh_request_in_a_minute_is_turned_away` | The first 10 requests in a window succeed; the 11th is refused with 429, a `retry-after` header, and `ratelimit-remaining: 0`. |
| Every request is recorded | `test_every_request_is_recorded` | A successful request is logged with method, path, query, client IP, status, duration, and response size. |
| Turned-away requests are recorded too | `test_turned_away_requests_are_recorded_too` | A 429 refusal is logged alongside the successful requests — a log that omits refusals can't show abuse. |
| A logging failure never fails a request | `test_a_logging_failure_never_fails_a_request` | A request log that raises on write does not prevent the request itself from succeeding — logging happens off the response path. |
| The health probe is not held to the allowance | `test_the_health_probe_is_not_held_to_the_allowance` | `/healthz` is exempt from the rate limit (so Compose's health checks can't trip it) but is still recorded in the request log. |
| A limiter outage does not become an API outage | `test_a_limiter_outage_does_not_become_an_api_outage` | With Redis unreachable, requests still succeed and are still logged — the rate limiter fails open by design (ADR-0007), not by accident. |

## `tests/test_compose_smoke.py`

The only test file that exercises a real Kafka broker, via `docker compose`. Marked
`compose` so the default `pytest` run excludes it.

| Test | Function | What it aims to prove |
|---|---|---|
| The feed travels end to end | `test_the_feed_travels_end_to_end` | Against the real containerised stack: the producer publishes 2,696 records, the consumer writes 2,696 rows, and the API serves 2,696 reports back out. |
| A consumer given an idle timeout stops on its own | `test_a_consumer_given_an_idle_timeout_stops_on_its_own` | With no new records arriving, the consumer exits by itself after its idle timeout (rather than needing to be killed) and reports why it stopped. |
| Request records reach container stdout | `test_request_records_reach_container_stdout` | A real API container's request-log lines are visible via `docker compose logs` — proof logging isn't silently swallowed by Uvicorn's own logger configuration, which only a real container can show. |
