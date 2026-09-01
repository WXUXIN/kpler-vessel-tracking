"""The ingest seam: raw messages in, Position Reports written, counts reported.

The consumer drives BatchWriter from the broker; the ingest seam tests drive the same
BatchWriter from the producer's message stream, which is what "faked broker" means.
Both paths share this code, so the seam tests cover what the consumer actually runs.
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


class BatchWriter:
    """Accumulates Position Reports and writes them in bounded batches.

    Adding flushes automatically once the batch is full; callers that also want a time
    bound call flush() themselves.
    """

    def __init__(
        self, store: PositionReportStore, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        self._store = store
        self._batch_size = batch_size
        self._batch: list[PositionReport] = []
        self._written = 0

    @property
    def written(self) -> int:
        return self._written

    def add(self, message: Mapping[str, Any]) -> int:
        self._batch.append(from_message(message))
        if len(self._batch) >= self._batch_size:
            return self.flush()
        return 0

    def flush(self) -> int:
        """Write the pending batch in one transaction. Returns rows added by this call."""
        if not self._batch:
            return 0
        added = self._store.insert_many(self._batch)
        self._batch = []
        self._written += added
        return added


def ingest_messages(
    messages: Iterable[Mapping[str, Any]],
    store: PositionReportStore,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> IngestResult:
    writer = BatchWriter(store, batch_size)
    for message in messages:
        writer.add(message)
    writer.flush()
    return IngestResult(written=writer.written)
