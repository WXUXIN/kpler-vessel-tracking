"""Incremental reading of the raw AIS feed file."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import ijson


def read_feed(path: Path) -> Iterator[Mapping[str, Any]]:
    """Yield raw feed messages one at a time, in file order.

    Parsed incrementally rather than loaded whole: the sample feed is small, but a
    production feed file is not, and feed order is the receipt sequence we rely on.
    Eg. 
    In the feed, the first message is:
    {
        "stationId": 1,
        "mmsi": 123456789,
        "latitude": 12.3456789,
        "longitude": 98.7654321,
        "speed": 123,
        "heading": 456,
        "rateOfTurn": 7,
        "reportedTime": 1680000000
    }

    message that's yielded will be the same, except the coordinates are Decimal rather than float, since
    incremental JSON parsing yields Decimal for the coordinate fields, which JSON serialisation does not support. 
    The producer's encode() function converts them to float before sending to the broker:
    {
        "stationId": 1,
        "mmsi": 123456789,
        "lat": Decimal("12.3456789"),               
        "lon": Decimal("98.7654321"),
        "speed": 123,
        "heading": 456,
        "rateOfTurn": 7,
        "timestamp": 1680000000
    }
    """
    with path.open("rb") as handle:
        for message in ijson.items(handle, "item"):
            yield message
