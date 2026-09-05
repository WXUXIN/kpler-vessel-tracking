"""The Compose smoke seam: the whole pipeline against a real broker.

The only test that exercises real Kafka. Marked so the default run excludes it.
"""

from __future__ import annotations

from collections.abc import Iterator

import json
import subprocess
import time
import urllib.request

import pytest

from vessel_tracking.api import MAX_PAGE_SIZE

FEED_SIZE = 2696
API = "http://localhost:8000/v1/position-reports"
INGEST_TIMEOUT_SECONDS = 180

pytestmark = pytest.mark.compose


def _compose(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )


def _stored_report_count() -> int:
    result = _compose(
        "exec", "-T", "postgres",
        "psql", "-U", "vessel", "-d", "vessel_tracking",
        "-tAc", "SELECT count(*) FROM position_report",
    )
    return int(result.stdout.strip())


def _served_report_count() -> int:
    """Every Position Report the API will hand over, paged the way a caller must.

    Pages are bounded, so counting one response counts one page. Paging to the end here
    also puts the cursor through a real HTTP round trip rather than a test client.
    """
    total = 0
    cursor: int | None = None
    while True:
        url = f"{API}?limit={MAX_PAGE_SIZE}" + (
            f"&after={cursor}" if cursor is not None else ""
        )
        with urllib.request.urlopen(url, timeout=30) as response:
            page = json.load(response)
        total += len(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return total


@pytest.fixture(scope="module")
def pipeline() -> Iterator[None]:
    _compose("down", "-v")
    _compose("up", "-d", "--build", "--wait")
    # The producer is a profiled job rather than a service, so `up --build` does not
    # rebuild it. Without this the test can run a stale producer against fresh services.
    _compose("build", "producer")
    yield
    _compose("down", "-v")


def test_the_feed_travels_end_to_end(pipeline: None) -> None:
    """Producer publishes 2,696, consumer writes 2,696, the API returns them."""
    assert _served_report_count() == 0

    published = _compose("run", "--rm", "producer", "vt-producer", "--rate", "0")
    # Application logs reach stderr, which is also where compose forwards container output.
    assert '"published": 2696' in published.stdout + published.stderr

    deadline = time.monotonic() + INGEST_TIMEOUT_SECONDS
    served = 0
    while time.monotonic() < deadline:
        served = _served_report_count()
        if served >= FEED_SIZE:
            break
        time.sleep(2)

    assert _stored_report_count() == FEED_SIZE
    assert served == FEED_SIZE


def test_a_consumer_given_an_idle_timeout_stops_on_its_own(pipeline: None) -> None:
    """A demonstration run terminates by itself rather than needing to be killed.

    The feed has already been consumed by the time this runs, so the consumer finds
    nothing, waits out its idle period and reports why it stopped. A non-zero exit
    fails here, because _compose checks it.
    """
    run = _compose(
        "run", "--rm",
        "-e", "VT_IDLE_TIMEOUT_SECONDS=5",
        "consumer", "vt-consumer",
        timeout=180,
    )

    assert '"stopped_by": "idle"' in run.stdout + run.stderr
