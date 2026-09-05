# List Position Reports

Returns Position Reports observed from AIS, narrowed by any combination of vessel, time
interval, bounding box and radius. Every filter is optional; a request with none returns
the whole collection, one page at a time.

A Position Report records what arrived from AIS — where a vessel *said* it was, at a
moment it *said* it was there. It is never a claim about where the vessel truly was, and
two reports may disagree about the same vessel at the same moment. Both are returned.

## Prerequisites

Before the endpoint returns anything, the feed has to have been ingested. Start the
system and replay the feed once:

1. Start the datastore, broker, limiter, consumer and API
   `docker compose up -d --wait`

2. Publish the feed for the consumer to ingest
   `docker compose run --rm --build producer vt-producer --rate 0`

The producer prints `{"event": "feed_published", "published": 2696, ...}` when it has
finished. The consumer writes what it receives and logs its running totals.

## Request

```bash
curl -G 'http://localhost:8000/v1/position-reports' \
  --data-urlencode 'mmsi=311486000' \
  --data-urlencode 'reported_from=2013-07-01T17:00:00Z' \
  --data-urlencode 'limit=2'
```

### Response

```json
{
  "items": [
    {
      "report_id": 1916,
      "mmsi": 311486000,
      "reported_at": "2013-07-01T17:29:00Z",
      "nav_status": 0,
      "speed_knots": 15.3,
      "course_degrees": 101,
      "heading_degrees": 102,
      "rate_of_turn": null,
      "latitude": 38.2366,
      "longitude": 10.82863
    },
    {
      "report_id": 1931,
      "mmsi": 311486000,
      "reported_at": "2013-07-01T17:43:00Z",
      "nav_status": 0,
      "speed_knots": 15.3,
      "course_degrees": 101,
      "heading_degrees": 102,
      "rate_of_turn": null,
      "latitude": 38.20821,
      "longitude": 11.00047
    }
  ],
  "next_cursor": 1931
}
```

## Returns

Returns a page object with an `items` array of Position Reports and a `next_cursor`.

Results are ordered by `report_id`, which is the sequence in which the feed received
them, and by nothing else. No alternative ordering is offered: Reported Time is coarse —
every value in the supplied feed falls on a minute boundary, and one value is shared by
272 reports spanning the whole map — so a page boundary inside a group of equal Reported
Times has no defined place to resume from.

`next_cursor` is the `report_id` to pass as `after` for the following page. It is `null`
exactly when the collection is exhausted, so a caller pages until it disappears rather
than until a page comes back short. A full final page is not mistaken for a page with
more behind it.

Speed is reported in knots and timestamps as ISO-8601 UTC, in both JSON and CSV. A value
the feed did not supply is `null` in JSON and an empty field in CSV — `rate_of_turn` is
absent from every record in the supplied feed.

This call returns [an error](#errors) if any parameter is out of range, if a circle is
given without all three of its parts, or if the client has exceeded its rate limit.

## Parameters

- `mmsi` (integer, optional)
  The nine-digit identifier a vessel broadcasts. Repeat the parameter to ask about
  several vessels in one request: `?mmsi=311486000&mmsi=247039300`. Not a guaranteed
  unique key — transponders can be misconfigured, so two vessels may share one.

- `reported_from` (string, optional)
  Start of the time interval, **inclusive**. ISO-8601. A value without a timezone is read
  as UTC.

- `reported_to` (string, optional)
  End of the time interval, **exclusive**. ISO-8601, read as UTC when naive. The interval
  is half-open so that adjacent windows neither overlap nor double-count: a report
  standing exactly on a bound belongs to the window that starts there.

- `min_latitude` (number, optional)
  Southern bound of a bounding box, between -90 and 90.

- `max_latitude` (number, optional)
  Northern bound of a bounding box, between -90 and 90.

- `min_longitude` (number, optional)
  Western bound of a bounding box, between -180 and 180.

- `max_longitude` (number, optional)
  Eastern bound of a bounding box, between -180 and 180. The box is four separately named
  bounds rather than one packed value, so that a validation error can name the bound that
  was wrong. All four are inclusive; a box is an area a caller drew, not a pair of windows
  to tile, so there is no double-counting to avoid.

- `centre_latitude` (number, optional)
  Latitude of the centre of a circular area of interest. Required if `centre_longitude`
  or `radius_nautical_miles` is given.

- `centre_longitude` (number, optional)
  Longitude of the centre. Required if `centre_latitude` or `radius_nautical_miles` is
  given.

- `radius_nautical_miles` (number, optional)
  Radius of the circle, greater than zero. Required if either centre parameter is given —
  two thirds of a circle is not a smaller circle, so a partial one is refused rather than
  silently ignored. Distance is measured across the WGS84 spheroid, so a circle is not
  squashed by latitude: within 115 nautical miles of one point in this feed lie 967
  reports, where comparing the radius as flat degrees would return 714.

- `after` (integer, optional)
  Resume after this `report_id`. Take it from the previous page's `next_cursor`. This is
  a keyset seek rather than an offset, so reports arriving behind a cursor cannot shift a
  caller's place in the sequence.

- `limit` (integer, optional)
  Page size. Defaults to `100`, and at most `1000`. A request for more is rejected rather
  than quietly clamped.

- `format` (string, optional)
  One of `json` or `csv`. Overrides the `Accept` header, for callers working from a
  browser address bar who cannot set one.

## Content negotiation

The response format is chosen by the `Accept` header, and `format` overrides it.
`text/csv` must be both wanted and preferred: `Accept: text/csv;q=0` is a refusal, and
`Accept: application/json, text/csv;q=0.1` gets JSON. A tie goes to JSON.

CSV carries a header row and exactly the fields JSON carries, in the same order and
rendered the same way. It is streamed from a server-side cursor rather than buffered, so
a large export cannot exhaust server memory — and so the response carries no
`content-length`.

```bash
curl -H 'Accept: text/csv' \
  'http://localhost:8000/v1/position-reports?mmsi=311486000&limit=2'
```

```csv
report_id,mmsi,reported_at,nav_status,speed_knots,course_degrees,heading_degrees,rate_of_turn,latitude,longitude
1916,311486000,2013-07-01T17:29:00Z,0,15.3,101,102,,38.2366,10.82863
1931,311486000,2013-07-01T17:43:00Z,0,15.3,101,102,,38.20821,11.00047
```

Because a CSV body cannot carry `next_cursor`, a caller paging through CSV takes the
`report_id` of the last row and passes it as `after`. Unlike JSON, there is no signal
that a page is the last one; an exhausted collection returns a header row and nothing
else.

## Errors

Every failure returns an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem
document under `application/problem+json` — validation, not found, wrong method and
unhandled server failures alike. There is no second error format to branch on.

`detail` is prose for a person. Structured detail lives in `errors`, one entry per
rejected parameter, each naming the parameter so a caller knows which to fix.

### 422 — a parameter is out of range

```json
{
  "type": "/problems/invalid-parameters",
  "title": "Invalid parameters",
  "status": 422,
  "detail": "The request could not be understood as it stands.",
  "instance": "/v1/position-reports?min_latitude=100&radius_nautical_miles=60",
  "errors": [
    {
      "parameter": "min_latitude",
      "detail": "Input should be less than or equal to 90"
    }
  ]
}
```

Note that this response reports only `min_latitude`, although the request also gave a
radius without a centre. Rules about individual parameters are checked before rules about
the request as a whole, so a request breaking both is told about the parameters first.
Fixing them and asking again returns the second problem.

### 422 — a rule about the whole request

An entry carries no `parameter` when no single parameter is at fault:

```json
{
  "type": "/problems/invalid-parameters",
  "title": "Invalid parameters",
  "status": 422,
  "detail": "The request could not be understood as it stands.",
  "instance": "/v1/position-reports?radius_nautical_miles=60",
  "errors": [
    {
      "detail": "a circle needs all three parts; missing centre_latitude, centre_longitude"
    }
  ]
}
```

### 429 — the rate limit

Ten requests per minute per client IP, counted outside the API process so the limit holds
across however many instances are running. The response carries a retry hint and the
limit headers:

```http
HTTP/1.1 429 Too Many Requests
retry-after: 56
ratelimit-limit: 10
ratelimit-remaining: 0
ratelimit-reset: 56
```

```json
{
  "type": "about:blank",
  "title": "Too Many Requests",
  "status": 429,
  "detail": "At most 10 requests are served per client per minute. Try again in 56 seconds.",
  "instance": "/v1/position-reports?limit=1"
}
```

Refused requests are recorded in the request log alongside served ones. If the limiter
itself is unreachable the API serves the request rather than refusing it, and says so in
its own logs — see
[ADR-0007](adr/0007-the-rate-limiter-fails-open.md).

### 500 — an unhandled failure

The same document shape, saying nothing about the cause. The cause goes to the API's log,
where an operator can reach it and a caller cannot.

## Field reference

Each object in `items`:

- `report_id` (integer)
  The identifier the report carried from the feed, unique across all reports and
  ascending in the order they were received. Also the paging cursor.

- `mmsi` (integer)
  The vessel's nine-digit Maritime Mobile Service Identity. The system holds no other
  vessel attribute — no name, type or dimensions.

- `reported_at` (string)
  The time the report claims for itself, ISO-8601 UTC. Untrusted: coarse, and frequently
  shared by many reports covering a wide area.

- `nav_status` (integer)
  The AIS navigational status the vessel declared — under way, at anchor, moored,
  aground. Self-declared, and therefore only as reliable as the crew setting it.

- `speed_knots` (number, nullable)
  Speed over the ground, in knots. `null` where AIS reported the value unavailable.

- `course_degrees` (integer, nullable)
  The direction the vessel is travelling over the ground.

- `heading_degrees` (integer, nullable)
  The direction the bow is pointing, which differs from the course whenever wind or
  current pushes the vessel sideways. `null` where AIS reported it unavailable.

- `rate_of_turn` (integer, nullable)
  Degrees per minute. `null` in every record of the supplied feed.

- `latitude` (number)
  Canonical, as reported.

- `longitude` (number)
  Canonical, as reported. The geography used for radius search is derived from these two
  rather than stored alongside them, so the two cannot disagree.
