"""The ingest seam: raw messages in, Position Reports written, counts reported.

The consumer feeds this from the broker; the ingest seam tests feed it from the feed
reader directly, which is what "faked broker" means here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from vessel_tracking.domain import PositionReport, from_message
from vessel_tracking.store import PositionReportStore

DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class IngestResult:
    written: int
    rejected: int = 0


def ingest_messages(
    messages: Iterable[Mapping[str, Any]],
    store: PositionReportStore,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> IngestResult:
    written = 0
    batch: list[PositionReport] = []
    for message in messages:
        batch.append(from_message(message))
        if len(batch) >= batch_size:
            written += store.insert_many(batch)
            batch.clear()
    written += store.insert_many(batch)
    return IngestResult(written=written)
