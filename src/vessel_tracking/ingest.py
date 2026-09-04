"""The ingest seam: raw messages in, Position Reports written, counts reported.

The consumer drives BatchWriter from the broker; the ingest seam tests drive the same
BatchWriter from the producer's message stream, which is what "faked broker" means.
Both paths share this code, so the seam tests cover what the consumer actually runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from vessel_tracking.domain import (
    InvalidReport,
    PositionReport,
    RejectedReport,
    from_message,
)
from vessel_tracking.store import PositionReportStore

DEFAULT_BATCH_SIZE = 500


class DeadLetters(Protocol):
    """Where Rejected Reports go instead of being dropped.

    The consumer publishes to a dead-letter topic; the ingest seam tests record in
    memory. Neither the batch writer nor anything below it knows which it has.
    """

    def send(self, rejected: RejectedReport) -> None: ...


@dataclass(frozen=True, slots=True)
class IngestResult:
    written: int
    rejected: int


class BatchWriter:
    """Accumulates Position Reports and writes them in bounded batches.

    Adding flushes automatically once the batch is full; callers that also want a time
    bound call flush() themselves.
    """

    def __init__(
        self,
        store: PositionReportStore,
        dead_letters: DeadLetters,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._store = store
        self._dead_letters = dead_letters
        self._batch_size = batch_size
        self._batch: list[PositionReport] = []
        self._written = 0
        self._rejected = 0

    @property
    def written(self) -> int:
        return self._written

    @property
    def rejected(self) -> int:
        return self._rejected

    def reject(self, message: Any, reason: str) -> None:
        """Set a message aside as a Rejected Report: dead-lettered and counted.

        Public because a message can be unusable before it is ever a mapping - an
        undecodable payload - and that rejection must reach the same counter.
        """
        self._dead_letters.send(RejectedReport(message, reason))
        self._rejected += 1

    def add(self, message: Mapping[str, Any]) -> int:
        try:
            report = from_message(message)
        except InvalidReport as invalid:
            # Only a poison message reaches here. A transient failure - the datastore
            # unreachable, a reset connection - is a different class and must never be
            # dead-lettered (ADR-0004), so nothing broader is caught.
            self.reject(message, invalid.reason)
            return 0
        self._batch.append(report)
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
    dead_letters: DeadLetters,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> IngestResult:
    writer = BatchWriter(store, dead_letters, batch_size)
    for message in messages:
        writer.add(message)
    writer.flush()
    return IngestResult(written=writer.written, rejected=writer.rejected)
