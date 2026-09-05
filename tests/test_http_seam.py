"""The HTTP seam: request -> datastore -> response, against real PostgreSQL."""

from __future__ import annotations

from fastapi.testclient import TestClient

FEED_SIZE = 2696


def test_the_collection_is_empty_before_anything_is_ingested(
    client: TestClient,
) -> None:
    response = client.get("/v1/position-reports")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_ingested_position_reports_are_served(ingested_client: TestClient) -> None:
    response = ingested_client.get("/v1/position-reports")

    assert response.status_code == 200
    assert len(response.json()["items"]) == FEED_SIZE


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
    ids = [
        item["report_id"]
        for item in ingested_client.get("/v1/position-reports").json()["items"]
    ]

    assert ids == sorted(ids)


# The feed's three Vessels and how many Position Reports each carries. Named by where
# they sail, because that is all the data supports: the system holds no Vessel
# attributes beyond the MMSI, so any ship's name here would be invented.
NORTHERN_VESSEL = 247039300
EASTERN_VESSEL = 311040700
WESTERN_VESSEL = 311486000
REPORTS_PER_VESSEL = {NORTHERN_VESSEL: 869, EASTERN_VESSEL: 967, WESTERN_VESSEL: 860}


def test_reports_can_be_narrowed_to_one_vessel(ingested_client: TestClient) -> None:
    response = ingested_client.get("/v1/position-reports", params={"mmsi": NORTHERN_VESSEL})

    items = response.json()["items"]
    assert response.status_code == 200
    assert len(items) == REPORTS_PER_VESSEL[NORTHERN_VESSEL]
    assert {item["mmsi"] for item in items} == {NORTHERN_VESSEL}


def test_several_vessels_can_be_asked_about_at_once(
    ingested_client: TestClient,
) -> None:
    """One repeated parameter, so there is no comma-separated list to parse."""
    response = ingested_client.get(
        "/v1/position-reports", params={"mmsi": [NORTHERN_VESSEL, EASTERN_VESSEL]}
    )

    items = response.json()["items"]
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
    starting = ingested_client.get(
        "/v1/position-reports", params={"reported_from": BOUNDARY}
    ).json()["items"]
    ending = ingested_client.get(
        "/v1/position-reports", params={"reported_to": BOUNDARY}
    ).json()["items"]

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
    items = ingested_client.get(
        "/v1/position-reports", params=CLIPPING_BOX
    ).json()["items"]

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
        response = ingested_client.get("/v1/position-reports", params=params)
        return [item[axis] for item in response.json()["items"]]

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
    items = ingested_client.get(
        "/v1/position-reports",
        params={"reported_from": BOUNDARY, "reported_to": WINDOW_END},
    ).json()["items"]

    assert len(items) == 1060
    assert len(items) < FROM_BOUNDARY


def test_filters_combine_in_one_request(ingested_client: TestClient) -> None:
    """A specific question is a single call, and the filters narrow together."""
    items = ingested_client.get(
        "/v1/position-reports",
        params={
            "mmsi": NORTHERN_VESSEL,
            "reported_from": BOUNDARY,
            **CLIPPING_BOX,
        },
    ).json()["items"]

    assert {item["mmsi"] for item in items} == {NORTHERN_VESSEL}
    assert all(41.0 <= item["latitude"] <= 43.0 for item in items)
    assert all(item["reported_at"] >= "2013-07-01T17:34:00" for item in items)
    assert 0 < len(items) < 537


def test_combining_filters_narrows_rather_than_widens(
    ingested_client: TestClient,
) -> None:
    """The box holds one Vessel, so asking for another one inside it finds none."""
    items = ingested_client.get(
        "/v1/position-reports",
        params={**CLIPPING_BOX, "mmsi": EASTERN_VESSEL},
    ).json()["items"]

    assert items == []


def test_a_bound_without_a_timezone_is_read_as_utc(
    ingested_client: TestClient,
) -> None:
    """Ordinary ISO-8601 input works without timezone boilerplate.

    Reading a naive bound in the server's local zone would answer a different question
    from the one asked. Documenting the assumption belongs to the error contract ticket;
    the reading has to be right the moment a time filter exists.
    """
    naive = ingested_client.get(
        "/v1/position-reports", params={"reported_from": "2013-07-01T17:34:00"}
    ).json()["items"]

    assert len(naive) == FROM_BOUNDARY
