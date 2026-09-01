"""The HTTP seam: request -> datastore -> response, against real PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Callable

from fastapi.testclient import TestClient

from vessel_tracking.ingest import ingest_messages
from vessel_tracking.store import PositionReportStore

Stream = Callable[[], Iterator[Mapping[str, Any]]]

FEED_SIZE = 2696


def test_the_collection_is_empty_before_anything_is_ingested(
    client: TestClient,
) -> None:
    response = client.get("/v1/positions")

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_ingested_position_reports_are_served(
    client: TestClient, store: PositionReportStore, published_stream: Stream
) -> None:
    ingest_messages(published_stream(), store)

    response = client.get("/v1/positions")

    assert response.status_code == 200
    assert len(response.json()["items"]) == FEED_SIZE


def test_position_reports_are_served_in_natural_units(
    client: TestClient, store: PositionReportStore, published_stream: Stream
) -> None:
    """No caller should have to know Speed is transmitted ten times too large."""
    ingest_messages(published_stream(), store)

    first = client.get("/v1/positions").json()["items"][0]

    assert first["report_id"] == 81
    assert first["mmsi"] == 247039300
    assert first["speed_knots"] == 18.0
    assert first["reported_at"].startswith("2013-07-01T13:06:00")
    assert first["rate_of_turn"] is None


def test_position_reports_are_served_in_report_id_order(
    client: TestClient, store: PositionReportStore, published_stream: Stream
) -> None:
    """Receipt sequence, not Reported Time, which the data cannot support (ADR-0006)."""
    ingest_messages(published_stream(), store)

    ids = [item["report_id"] for item in client.get("/v1/positions").json()["items"]]

    assert ids == sorted(ids)
