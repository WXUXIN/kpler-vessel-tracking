"""The HTTP seam: request -> datastore -> response, against real PostgreSQL."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from vessel_tracking.api import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, create_app
from vessel_tracking.domain import PositionReport

FEED_SIZE = 2696

# More pages than any test here should need. A guard, not a limit.
PAGE_LIMIT = 200


def pages(client: TestClient, **params: Any) -> Iterator[dict[str, Any]]:
    """Each page in turn, walking the cursor the way a caller has to.

    Counted rather than `while True`: a cursor that stops advancing should fail the
    test that asked for it rather than hang the suite.
    """
    cursor: int | None = None
    for _ in range(PAGE_LIMIT):
        response = client.get(
            "/v1/position-reports",
            params={**params, **({"after": cursor} if cursor is not None else {})},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        yield page
        cursor = page["next_cursor"]
        if cursor is None:
            return
    raise AssertionError(f"the cursor never ran out after {PAGE_LIMIT} pages")


def fetch_all(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    """Every matching Position Report, paged the way a caller has to page."""
    return [
        item
        for page in pages(client, **{"limit": MAX_PAGE_SIZE, **params})
        for item in page["items"]
    ]


def test_the_collection_is_empty_before_anything_is_ingested(
    client: TestClient,
) -> None:
    body = client.get("/v1/position-reports").json()

    assert body["items"] == []
    assert body["next_cursor"] is None


def test_ingested_position_reports_are_served(ingested_client: TestClient) -> None:
    assert len(fetch_all(ingested_client)) == FEED_SIZE


def test_position_reports_are_served_in_natural_units(
    ingested_client: TestClient,
) -> None:
    """No caller should have to know Speed is transmitted ten times too large."""
    first = ingested_client.get("/v1/position-reports").json()["items"][0]

    assert first["report_id"] == 81
    assert first["mmsi"] == 247039300
    assert first["speed_knots"] == 18.0
    assert first["reported_at"].startswith("2013-07-01T13:06:00")
    assert first["rate_of_turn"] is None


def test_position_reports_are_served_in_report_id_order(
    ingested_client: TestClient,
) -> None:
    """Receipt sequence, not Reported Time, which the data cannot support (ADR-0006)."""
    ids = [item["report_id"] for item in fetch_all(ingested_client)]

    assert ids == sorted(ids)


# The feed's three Vessels and how many Position Reports each carries. Named by where
# they sail, because that is all the data supports: the system holds no Vessel
# attributes beyond the MMSI, so any ship's name here would be invented.
NORTHERN_VESSEL = 247039300
EASTERN_VESSEL = 311040700
WESTERN_VESSEL = 311486000
REPORTS_PER_VESSEL = {NORTHERN_VESSEL: 869, EASTERN_VESSEL: 967, WESTERN_VESSEL: 860}


def test_reports_can_be_narrowed_to_one_vessel(ingested_client: TestClient) -> None:
    items = fetch_all(ingested_client, mmsi=NORTHERN_VESSEL)

    assert len(items) == REPORTS_PER_VESSEL[NORTHERN_VESSEL]
    assert {item["mmsi"] for item in items} == {NORTHERN_VESSEL}


def test_several_vessels_can_be_asked_about_at_once(
    ingested_client: TestClient,
) -> None:
    """One repeated parameter, so there is no comma-separated list to parse."""
    items = fetch_all(ingested_client, mmsi=[NORTHERN_VESSEL, EASTERN_VESSEL])

    assert len(items) == REPORTS_PER_VESSEL[NORTHERN_VESSEL] + REPORTS_PER_VESSEL[EASTERN_VESSEL]
    assert {item["mmsi"] for item in items} == {NORTHERN_VESSEL, EASTERN_VESSEL}


# One instant in the feed, carrying 119 Position Reports, with 601 strictly before it.
BOUNDARY = "2013-07-01T17:34:00Z"
BEFORE_BOUNDARY = 601
FROM_BOUNDARY = 2095


def test_the_time_interval_is_half_open(ingested_client: TestClient) -> None:
    """Adjacent windows must neither overlap nor double-count.

    The two windows either side of one instant partition the feed exactly. The 119
    reports standing on the boundary belong to the window that starts there, never to
    the one that ends there.
    """
    starting = fetch_all(ingested_client, reported_from=BOUNDARY)
    ending = fetch_all(ingested_client, reported_to=BOUNDARY)

    assert len(starting) == FROM_BOUNDARY
    assert len(ending) == BEFORE_BOUNDARY
    assert len(starting) + len(ending) == FEED_SIZE
    assert not {i["report_id"] for i in starting} & {i["report_id"] for i in ending}


# A box that clips the northern Vessel's track rather than enclosing it, so the bounds
# have to do work. A box drawn loosely around a whole track proves nothing: any single
# bound satisfies it, and three could be dropped unnoticed.
CLIPPING_BOX = {
    "min_latitude": 41.0,
    "max_latitude": 43.0,
    "min_longitude": 15.0,
    "max_longitude": 17.5,
}


def test_reports_can_be_narrowed_to_a_bounding_box(
    ingested_client: TestClient,
) -> None:
    """Four separately named bounds, so a bad one can be named back to the caller."""
    items = fetch_all(ingested_client, **CLIPPING_BOX)

    assert len(items) == 537
    assert {item["mmsi"] for item in items} == {NORTHERN_VESSEL}


def test_each_bound_of_the_box_narrows_in_its_own_direction(
    ingested_client: TestClient,
) -> None:
    """Four bounds, four directions, each exercised alone against data spanning it.

    The feed has Position Reports either side of the 42nd parallel and of longitude 17,
    so a bound wired to the wrong column, or the wrong way round, changes these answers.
    """

    def coordinates(axis: str, **params: float) -> list[float]:
        return [item[axis] for item in fetch_all(ingested_client, **params)]

    north = coordinates("latitude", min_latitude=42.0)
    south = coordinates("latitude", max_latitude=42.0)
    east = coordinates("longitude", min_longitude=17.0)
    west = coordinates("longitude", max_longitude=17.0)

    assert (len(north), len(south)) == (373, 2323)
    assert (len(east), len(west)) == (1270, 1426)
    assert min(north) >= 42.0 and max(south) <= 42.0
    assert min(east) >= 17.0 and max(west) <= 17.0


WINDOW_END = "2013-07-01T17:40:00Z"


def test_both_bounds_together_make_a_closed_window(
    ingested_client: TestClient,
) -> None:
    """The ordinary case, and the one the composite index serves.

    Narrower than either bound alone, which is what proves both are applied rather
    than the last one winning.
    """
    items = fetch_all(ingested_client, reported_from=BOUNDARY, reported_to=WINDOW_END)

    assert len(items) == 1060
    assert len(items) < FROM_BOUNDARY


def test_filters_combine_in_one_request(ingested_client: TestClient) -> None:
    """A specific question is a single call, and the filters narrow together."""
    items = fetch_all(
        ingested_client, mmsi=NORTHERN_VESSEL, reported_from=BOUNDARY, **CLIPPING_BOX
    )

    assert {item["mmsi"] for item in items} == {NORTHERN_VESSEL}
    assert all(41.0 <= item["latitude"] <= 43.0 for item in items)
    assert all(item["reported_at"] >= "2013-07-01T17:34:00" for item in items)
    assert 0 < len(items) < 537


def test_combining_filters_narrows_rather_than_widens(
    ingested_client: TestClient,
) -> None:
    """The box holds one Vessel, so asking for another one inside it finds none."""
    items = fetch_all(ingested_client, mmsi=EASTERN_VESSEL, **CLIPPING_BOX)

    assert items == []


def test_a_bound_without_a_timezone_is_read_as_utc(
    ingested_client: TestClient,
) -> None:
    """Ordinary ISO-8601 input works without timezone boilerplate.

    Reading a naive bound in the server's local zone would answer a different question
    from the one asked. Documenting the assumption belongs to the error contract ticket;
    the reading has to be right the moment a time filter exists.
    """
    naive = fetch_all(ingested_client, reported_from="2013-07-01T17:34:00")

    assert len(naive) == FROM_BOUNDARY


def test_a_page_is_bounded_even_when_nothing_is_asked_for(
    ingested_client: TestClient,
) -> None:
    """A caller who asks for everything gets a page, not 2,696 reports."""
    body = ingested_client.get("/v1/position-reports").json()

    assert len(body["items"]) == DEFAULT_PAGE_SIZE
    assert body["next_cursor"] == body["items"][-1]["report_id"]


def test_the_cursor_seeks_past_the_reports_already_seen(
    ingested_client: TestClient,
) -> None:
    """Keyset, not offset: the next page starts after a Report ID, not at a row number."""
    first = ingested_client.get(
        "/v1/position-reports", params={"limit": 10}
    ).json()
    second = ingested_client.get(
        "/v1/position-reports", params={"limit": 10, "after": first["next_cursor"]}
    ).json()

    assert [i["report_id"] for i in second["items"]] == sorted(
        i["report_id"] for i in second["items"]
    )
    assert min(i["report_id"] for i in second["items"]) > first["next_cursor"]
    assert not {i["report_id"] for i in first["items"]} & {
        i["report_id"] for i in second["items"]
    }


def test_paging_a_filtered_set_returns_every_match_exactly_once(
    ingested_client: TestClient,
) -> None:
    """The property keyset paging exists for: no drift, no duplication, no gaps."""
    ids: list[int] = []
    seen = 0

    for page in pages(ingested_client, mmsi=NORTHERN_VESSEL, limit=50):
        ids.extend(item["report_id"] for item in page["items"])
        seen += 1

    assert len(ids) == REPORTS_PER_VESSEL[NORTHERN_VESSEL]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)
    assert seen == 18  # 869 reports in pages of 50


def test_page_size_has_a_documented_maximum(ingested_client: TestClient) -> None:
    """A caller can size their own batching, and cannot ask for the whole table."""
    at_maximum = ingested_client.get(
        "/v1/position-reports", params={"limit": MAX_PAGE_SIZE}
    )
    beyond = ingested_client.get(
        "/v1/position-reports", params={"limit": MAX_PAGE_SIZE + 1}
    )

    assert at_maximum.status_code == 200
    assert len(at_maximum.json()["items"]) == MAX_PAGE_SIZE
    assert beyond.status_code == 422


def test_no_alternative_ordering_is_offered_and_the_reason_is_published(
    client: TestClient,
) -> None:
    """A caller looking for a sort parameter finds the reason there isn't one."""
    endpoint = client.get("/openapi.json").json()["paths"]["/v1/position-reports"]["get"]

    offered = {parameter["name"] for parameter in endpoint["parameters"]}
    assert not offered & {"sort", "sort_by", "order", "order_by", "ordering"}
    assert "Report ID" in endpoint["description"]
    assert "Reported Time" in endpoint["description"]


def test_a_last_page_that_is_exactly_full_still_ends_the_paging(
    ingested_client: TestClient,
) -> None:
    """The boundary keyset paging is easy to get wrong: an exactly-full final page.

    869 reports in pages of 79 divides exactly, so the last page comes back full. A
    full page is indistinguishable from one with more behind it unless the datastore is
    asked for one row beyond the page, which is what stops the caller being sent back
    for an empty one.
    """
    sizes = [
        len(page["items"])
        for page in pages(ingested_client, mmsi=NORTHERN_VESSEL, limit=79)
    ]

    assert sizes == [79] * 11  # 869 = 11 x 79, so every page is full, including the last
    assert sum(sizes) == REPORTS_PER_VESSEL[NORTHERN_VESSEL]


def problem(response: Any, status: int) -> dict[str, Any]:
    """Assert the one error shape, and hand back the document for closer inspection."""
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status
    assert body["title"]
    assert body["type"]
    assert body["instance"]
    return body


def test_an_invalid_parameter_returns_a_problem_document(client: TestClient) -> None:
    """One shape, not the framework's native one alongside it."""
    document = problem(
        client.get("/v1/position-reports", params={"limit": MAX_PAGE_SIZE + 1}), 422
    )

    assert document["errors"][0]["parameter"] == "limit"
    assert isinstance(document["detail"], str)  # prose here, structure in `errors`


def test_the_problem_document_names_the_bound_that_was_wrong(
    client: TestClient,
) -> None:
    """Four separately named bounds exist so that this answer can be given."""
    document = problem(
        client.get(
            "/v1/position-reports",
            params={"min_latitude": 100, "max_longitude": 999},
        ),
        422,
    )

    named = {error["parameter"] for error in document["errors"]}
    assert named == {"min_latitude", "max_longitude"}
    assert all(error["detail"] for error in document["errors"])


def test_an_unknown_path_returns_the_same_problem_shape(client: TestClient) -> None:
    """A caller handling errors programmatically meets one format, not two."""
    document = problem(client.get("/v1/no-such-collection"), 404)

    assert "errors" not in document


def test_a_failure_inside_the_api_returns_the_same_problem_shape() -> None:
    """Server failures use the same document, and leak nothing about the cause."""

    class BrokenStore:
        def list_reports(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("password=hunter2 at 10.0.0.4")

    with TestClient(create_app(BrokenStore()), raise_server_exceptions=False) as broken:
        response = broken.get("/v1/position-reports")

    document = problem(response, 500)
    assert "hunter2" not in response.text


def test_the_utc_assumption_is_documented(client: TestClient) -> None:
    """Documented rather than silent: a caller reads it where they pass the bound."""
    schema = client.get("/openapi.json").json()
    endpoint = schema["paths"]["/v1/position-reports"]["get"]
    described = " ".join(
        parameter.get("description", "")
        for parameter in endpoint["parameters"]
        if parameter["name"].startswith("reported_")
    )

    assert "UTC" in described


def test_a_bad_value_in_a_repeated_parameter_names_the_parameter(
    client: TestClient,
) -> None:
    """Not the position it sat at: a caller cannot fix a parameter called "0"."""
    document = problem(client.get("/v1/position-reports", params={"mmsi": "abc"}), 422)

    assert [error["parameter"] for error in document["errors"]] == ["mmsi"]


def test_the_wrong_method_keeps_the_headers_the_status_requires(
    client: TestClient,
) -> None:
    """One shape everywhere, but not at the cost of a header the RFC requires."""
    response = client.post("/v1/position-reports")

    problem(response, 405)
    assert "GET" in response.headers["allow"]


def test_the_published_schema_offers_only_the_problem_media_type(
    client: TestClient,
) -> None:
    """A generated client should expect what the wire returns, and nothing else."""
    responses = client.get("/openapi.json").json()["paths"]["/v1/position-reports"][
        "get"
    ]["responses"]

    for status in ("422", "500"):
        content = responses[status]["content"]
        assert list(content) == ["application/problem+json"]
        assert content["application/problem+json"]["schema"]["$ref"].endswith("/Problem")


def csv_rows(response: Any) -> list[list[str]]:
    """The CSV body as rows, header included."""
    return list(csv.reader(io.StringIO(response.text)))


def test_an_accept_header_selects_csv(ingested_client: TestClient) -> None:
    response = ingested_client.get(
        "/v1/position-reports", headers={"accept": "text/csv"}, params={"limit": 3}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert len(csv_rows(response)) == 4  # a header row and three reports


def test_a_query_parameter_overrides_the_header(ingested_client: TestClient) -> None:
    """So that a browser address bar, which cannot set Accept, can still ask for CSV."""
    response = ingested_client.get(
        "/v1/position-reports",
        headers={"accept": "application/json"},
        params={"format": "csv", "limit": 3},
    )

    assert response.headers["content-type"].startswith("text/csv")


def test_csv_and_json_carry_the_same_fields_and_values(
    ingested_client: TestClient,
) -> None:
    """Switching format must not change the data or how it is rendered."""
    window = {"mmsi": NORTHERN_VESSEL, "reported_from": BOUNDARY, "limit": 20}
    items = ingested_client.get("/v1/position-reports", params=window).json()["items"]
    rows = csv_rows(
        ingested_client.get("/v1/position-reports", params={**window, "format": "csv"})
    )

    header, records = rows[0], rows[1:]
    assert header == list(items[0])
    assert len(records) == len(items)
    for record, item in zip(records, items):
        assert record == ["" if item[field] is None else str(item[field]) for field in header]


def test_csv_renders_absent_values_as_empty_fields(
    ingested_client: TestClient,
) -> None:
    """Rate of Turn is empty in every supplied record and must not become "None"."""
    rows = csv_rows(
        ingested_client.get(
            "/v1/position-reports", params={"format": "csv", "limit": 5}
        )
    )

    rate_of_turn = rows[0].index("rate_of_turn")
    assert {row[rate_of_turn] for row in rows[1:]} == {""}


def test_csv_honours_the_same_ordering_and_paging(
    ingested_client: TestClient,
) -> None:
    rows = csv_rows(
        ingested_client.get(
            "/v1/position-reports",
            params={"format": "csv", "mmsi": NORTHERN_VESSEL, "limit": 10, "after": 81},
        )
    )

    report_id = rows[0].index("report_id")
    ids = [int(row[report_id]) for row in rows[1:]]
    assert len(ids) == 10
    assert ids == sorted(ids)
    assert min(ids) > 81


def test_a_failure_before_the_first_row_still_gets_a_problem_document() -> None:
    """The status is already sent once streaming starts, so it is chosen before that.

    The datastore is touched, and the first report pulled, while a proper error is still
    possible. Only a failure part way through an export can truncate a 200.
    """

    class BrokenStore:
        def stream_reports(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the datastore fell over")

    with TestClient(create_app(BrokenStore()), raise_server_exceptions=False) as broken:
        response = broken.get("/v1/position-reports", params={"format": "csv"})

    problem(response, 500)


def test_both_formats_render_time_as_utc_whatever_the_session_says() -> None:
    """A timestamptz arrives in the session's timezone, and must not leave in it.

    Constructed directly because a database session's timezone cannot be varied through
    the seam, and the seam's own container happens to run in UTC - which is exactly what
    would hide this.
    """
    from vessel_tracking.api import PositionReportResource

    eastern = timezone(timedelta(hours=2))
    report = PositionReport(
        report_id=81,
        mmsi=NORTHERN_VESSEL,
        reported_at=datetime(2013, 7, 1, 15, 6, tzinfo=eastern),
        nav_status=0,
        speed_knots=Decimal("18.0"),
        course_degrees=144,
        heading_degrees=144,
        rate_of_turn=None,
        latitude=42.75,
        longitude=15.44,
    )

    rendered = PositionReportResource.of(report).model_dump(mode="json")

    assert rendered["reported_at"] == "2013-07-01T13:06:00Z"


def test_the_schema_offers_both_formats(client: TestClient) -> None:
    served = client.get("/openapi.json").json()["paths"]["/v1/position-reports"]["get"][
        "responses"
    ]["200"]["content"]

    assert set(served) == {"application/json", "text/csv"}


def test_a_refused_format_is_not_served(ingested_client: TestClient) -> None:
    """`text/csv;q=0` says do not send CSV, which is the opposite of asking for it."""
    response = ingested_client.get(
        "/v1/position-reports", headers={"accept": "text/csv;q=0"}, params={"limit": 1}
    )

    assert response.headers["content-type"].startswith("application/json")


def test_the_format_the_caller_prefers_wins(ingested_client: TestClient) -> None:
    """Tolerating CSV is not preferring it, and a tie goes to the documented default."""
    grudging = ingested_client.get(
        "/v1/position-reports",
        headers={"accept": "application/json, text/csv;q=0.1"},
        params={"limit": 1},
    )
    keen = ingested_client.get(
        "/v1/position-reports",
        headers={"accept": "text/csv;q=0.9, application/json;q=0.1"},
        params={"limit": 1},
    )
    indifferent = ingested_client.get(
        "/v1/position-reports", headers={"accept": "*/*"}, params={"limit": 1}
    )

    assert grudging.headers["content-type"].startswith("application/json")
    assert keen.headers["content-type"].startswith("text/csv")
    assert indifferent.headers["content-type"].startswith("application/json")


def test_csv_is_streamed_rather_than_buffered(ingested_client: TestClient) -> None:
    """A buffered body has to be measured first, so its absence is the observable sign.

    The rows themselves come from a server-side cursor, which the seam cannot see; what
    it can see is that the API never held the whole export to count it.
    """
    response = ingested_client.get(
        "/v1/position-reports", params={"format": "csv", "limit": MAX_PAGE_SIZE}
    )

    assert response.status_code == 200
    assert "content-length" not in response.headers
    assert len(csv_rows(response)) == MAX_PAGE_SIZE + 1
