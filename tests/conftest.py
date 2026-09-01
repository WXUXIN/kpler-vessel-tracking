import json
import pathlib
from collections.abc import Iterator, Mapping
from typing import Any, Callable

import psycopg
import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from vessel_tracking.feed import read_feed
from vessel_tracking.producer import encode
from vessel_tracking.store import PositionReportStore

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "001_schema.sql"


@pytest.fixture(scope="session")
def dsn() -> Iterator[str]:
    """A real PostgreSQL with PostGIS.

    The seam tests use a real database rather than a fake because the behaviour most
    worth testing - conflict handling on insert, and the filter predicates - is exactly
    what a fake would accept and a real database would reject.
    """
    with PostgresContainer("postgis/postgis:16-3.4", driver=None) as postgres:
        url = postgres.get_connection_url()
        with psycopg.connect(url) as conn:
            conn.execute(SCHEMA.read_text())
            conn.commit()
        yield url


@pytest.fixture
def feed_path() -> pathlib.Path:
    return ROOT / "ship_positions.json"


@pytest.fixture
def published_stream(
    feed_path: pathlib.Path,
) -> Callable[[], Iterator[Mapping[str, Any]]]:
    """The producer's message stream, as the consumer receives it.

    Faking the broker means standing in for the topic, not for the wire format:
    incremental JSON parsing yields Decimal coordinates, and only the producer's
    serialisation turns them into the numbers the real path carries.
    """

    def stream() -> Iterator[Mapping[str, Any]]:
        for message in read_feed(feed_path):
            yield json.loads(encode(message))

    return stream


@pytest.fixture
def store(dsn: str) -> Iterator[PositionReportStore]:
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE position_report")
        conn.commit()
    store = PositionReportStore(dsn)
    yield store
    store.close()


@pytest.fixture
def client(store: PositionReportStore) -> Iterator[TestClient]:
    from vessel_tracking.api import create_app

    with TestClient(create_app(store)) as test_client:
        yield test_client
