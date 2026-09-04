import json
import pathlib
from collections.abc import Iterator, Mapping
from typing import Any, Callable

import psycopg
import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from vessel_tracking.domain import RejectedReport
from vessel_tracking.producer import encode, messages_to_publish
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
) -> Callable[..., Iterator[Mapping[str, Any]]]:
    """The producer's message stream, as the consumer receives it.

    Faking the broker means standing in for the topic, not for the wire format:
    incremental JSON parsing yields Decimal coordinates, and only the producer's
    serialisation turns them into the numbers the real path carries. The stream comes
    from the producer's own function, injection option included, so the seam tests
    cover what the producer actually publishes.
    """

    def stream(inject_invalid: int = 0) -> Iterator[Mapping[str, Any]]:
        for message in messages_to_publish(feed_path, inject_invalid):
            yield json.loads(encode(message))

    return stream


class RecordingDeadLetters:
    """Stands in for the dead-letter topic.

    Faking the broker here means keeping what was published to it, so a test can
    assert on dead-letter contents the way an operator would read the topic.
    """

    def __init__(self) -> None:
        self.sent: list[RejectedReport] = []

    def send(self, rejected: RejectedReport) -> None:
        self.sent.append(rejected)

    @property
    def reasons(self) -> list[str]:
        return [rejected.reason for rejected in self.sent]


@pytest.fixture
def dead_letters() -> RecordingDeadLetters:
    return RecordingDeadLetters()


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
