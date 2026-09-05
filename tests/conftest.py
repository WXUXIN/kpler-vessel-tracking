import json
import pathlib
import uuid
from collections.abc import Iterator, Mapping
from typing import Any, Callable

import psycopg
import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from vessel_tracking.domain import RejectedReport
from vessel_tracking.limits import Limits
from vessel_tracking.settings import Settings
from vessel_tracking.ingest import ingest_messages
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


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """A real Redis, for the same reason PostgreSQL is real.

    A fixed window is one INCR and one EXPIRE racing each other; a fake would accept
    any ordering of those, which is precisely the part worth testing.
    """
    with RedisContainer("redis:7-alpine") as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


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
        conn.execute("TRUNCATE position_report, request_log")
        conn.commit()
    store = PositionReportStore(dsn)
    yield store
    store.close()


@pytest.fixture
def client(store: PositionReportStore, redis_url: str) -> Iterator[TestClient]:
    from vessel_tracking.api import create_app

    # A fresh namespace per test, so one test cannot spend another's allowance, and an
    # allowance high enough that paging through a result is not mistaken for abuse.
    # The limit itself has its own client below.
    limits = Limits(url=redis_url, allowance=1000, namespace=f"test-{uuid.uuid4()}")
    with TestClient(create_app(store, limits=limits)) as test_client:
        yield test_client


@pytest.fixture
def limited_client(store: PositionReportStore, redis_url: str) -> Iterator[TestClient]:
    """A client held to the allowance the system actually ships with."""
    from vessel_tracking.api import create_app

    limits = Limits(
        url=redis_url,
        allowance=Settings().rate_limit_allowance,
        namespace=f"test-{uuid.uuid4()}",
    )
    with TestClient(create_app(store, limits=limits)) as test_client:
        yield test_client


@pytest.fixture
def ingested_client(
    client: TestClient,
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Callable[..., Iterator[Mapping[str, Any]]],
) -> TestClient:
    """The API with the whole feed behind it.

    Most HTTP seam tests want the same 2,696 Position Reports in place and differ only
    in the request they make.
    """
    ingest_messages(published_stream(), store, dead_letters)
    return client
