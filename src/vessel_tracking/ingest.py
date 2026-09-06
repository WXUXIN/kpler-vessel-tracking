"""The ingest seam: raw messages in, Position Reports written, counts reported.

The consumer drives BatchWriter from the broker; the ingest seam tests drive the same
BatchWriter from the producer's message stream, which is what "faked broker" means.
Both paths share this code, so the seam tests cover what the consumer actually runs.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from vessel_tracking.domain import (
    InvalidReport,
    PositionReport,
    RejectedReport,
    from_message,
)
from vessel_tracking.store import (
    PositionReportStore,
    TransientFailure,
    UnstorableReport,
)

log = logging.getLogger("vessel_tracking.ingest")

DEFAULT_BATCH_SIZE = 500
RETRY_INITIAL_SECONDS = 0.5
RETRY_MAXIMUM_SECONDS = 30.0
RETRY_MULTIPLIER = 2.0


@dataclass(frozen=True, slots=True)
class Backoff:
    """How long to wait between attempts at a write that failed transiently.

    Doubling, capped, and never exhausted: ADR-0004 requires the consumer to wait out
    a datastore that is not there rather than discard reports it could have written.
    Sleeping is injected so that tests can watch the schedule without living through it.
    """

    initial_seconds: float = RETRY_INITIAL_SECONDS
    maximum_seconds: float = RETRY_MAXIMUM_SECONDS
    sleep: Callable[[float], None] = time.sleep

    def wait(self, attempt: int) -> float:
        """Wait before the given attempt, counting from one. Returns the delay waited."""
        delay = min(
            self.initial_seconds * RETRY_MULTIPLIER ** (attempt - 1),
            self.maximum_seconds,
        )
        self.sleep(delay)
        return delay


def _never() -> bool:
    """The default stop signal: a writer nobody is shutting down waits indefinitely."""
    return False


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
        backoff: Backoff = Backoff(),
        stopping: Callable[[], bool] = _never,
    ) -> None:
        self._store = store
        self._dead_letters = dead_letters
        self._batch_size = batch_size
        self._backoff = backoff
        self._stopping = stopping
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
        """Decode and accumulate one record. Returns whether a batch reached the store."""
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
        added = self._write(self._batch)
        self._batch = []
        self._written += added
        return added

    def _write(self, batch: list[PositionReport]) -> int:
        """Write the batch, waiting out a datastore that is not there.

        A transient failure is retried indefinitely and never dead-lettered: it is not
        the batch's fault, and no number of retries makes the data worse. Blocking here
        stalls the whole consumer, which is the intended behaviour rather than an
        oversight - availability is not traded for data loss (ADR-0004). The exception
        is shutdown, where the batch is left unwritten: its offsets never moved, so the
        records are still on the topic for the next run to collect.
        """
        attempt = 0
        while True:
            try:
                return self._store.insert_many(batch)
            except UnstorableReport:
                if len(batch) == 1:
                    raise
                return self._write_separately(batch)
            except TransientFailure as failure:
                if self._stopping():
                    raise
                attempt += 1
                delay = self._backoff.wait(attempt)
                log.warning(
                    json.dumps(
                        {
                            "event": "write_retry",
                            "attempt": attempt,
                            "waited_seconds": delay,
                            "pending": len(batch),
                            "detail": str(failure),
                        }
                    )
                )

    def _write_separately(self, batch: list[PositionReport]) -> int:
        """Write a refused batch report by report, setting aside only what is refused.

        One row the datastore will not have must not cost the other 499 their place,
        and it must not be retried forever either. The batch is written in a single
        transaction, so a refusal rolls the whole thing back and everything good in it
        is written again here.
        """
        written = 0
        for report in batch:
            try:
                written += self._write([report])
            except UnstorableReport as unstorable:
                # The raw message is long gone - it was decoded to get this far - so
                # what reaches the topic is the decoded report and the refusal.
                self.reject(asdict(report), f"the datastore refused it: {unstorable}")
        return written


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
