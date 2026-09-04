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
    with urllib.request.urlopen(API, timeout=30) as response:
        return len(json.load(response)["items"])


@pytest.fixture(scope="module")
def pipeline() -> Iterator[None]:
    _compose("down", "-v")
    _compose("up", "-d", "--build", "--wait")
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
