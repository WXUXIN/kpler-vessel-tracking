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
    """
    with path.open("rb") as handle:
        for message in ijson.items(handle, "item"):
            yield message
