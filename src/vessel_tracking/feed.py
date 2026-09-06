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

    This is "incremental": at no point does the function hold "all 2,696 records" in memory.
    It holds exactly one, plus a frozen position in the file. 
    
    Whether the file has 2,696 records or 2.6 billion, 
    read_feed uses the same tiny amount of memory — it's only ever looking at one record at a time.

    In the feed, the first message is:
    {
        "mmsi": 247039300,
        "status": 0,
        "stationId": 81,
        "speed": 180,
        "lon": 15.4415,
        "lat": 42.75178,
        "course": 144,
        "heading": 144,
        "rot": "",
        "timestamp": 1372683960
    }

    The message yielded here is the same, except `lat`/`lon` are Decimal rather than
    float: incremental JSON parsing yields Decimal for a number with a decimal point,
    which plain `json.dumps` cannot serialise. The producer's encode() function converts
    them to float before sending to the broker:
    """
    with path.open("rb") as handle:
        for message in ijson.items(handle, "item"):
            yield message
