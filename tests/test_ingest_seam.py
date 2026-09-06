"""The ingest seam: feed file -> messages -> processed -> datastore.

The broker is faked by passing the producer's message stream straight into the
consumer's batch processor. Everything asserted here is externally observable:
rows in the datastore, and the counters the pipeline reports.
"""

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from itertools import chain, islice
from typing import Any, Callable

import pytest
from conftest import RecordingDeadLetters
from vessel_tracking.domain import PositionReport, malformed_messages
from vessel_tracking.ingest import Backoff, BatchWriter, ingest_messages
from vessel_tracking.store import PositionReportStore, TransientFailure

Stream = Callable[..., Iterator[Mapping[str, Any]]]

FEED_SIZE = 2696


# --------------------------------------------------------------------------------------
# Whole-feed ingest
# --------------------------------------------------------------------------------------


def test_the_whole_feed_reaches_the_datastore(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    result = ingest_messages(published_stream(), store, dead_letters)

    assert result.written == FEED_SIZE
    assert store.count() == FEED_SIZE


def test_replaying_the_feed_writes_nothing_new(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """At-least-once delivery must not corrupt the store (ADR-0004)."""
    ingest_messages(published_stream(), store, dead_letters)

    result = ingest_messages(published_stream(), store, dead_letters)

    assert result.written == 0
    assert store.count() == FEED_SIZE


# --------------------------------------------------------------------------------------
# Normalisation and retention
# --------------------------------------------------------------------------------------


def test_ais_wire_encodings_are_normalised_on_write(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """Speed to knots, Reported Time to a real timestamp, absent Rate of Turn to null."""
    ingest_messages(published_stream(), store, dead_letters)

    report = store.get(81)

    assert report is not None
    assert report.speed_knots == Decimal("18.0")  # wire value was 180
    assert report.reported_at == datetime(2013, 7, 1, 13, 6, tzinfo=UTC)
    assert report.rate_of_turn is None  # wire value was the empty string
    assert report.mmsi == 247039300


def test_conflicting_reports_are_both_retained(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """The store records observations rather than adjudicating between them (ADR-0001).

    Report 81 and report 113 share an MMSI and a Reported Time but disagree about
    position by some 160km. Keying on (mmsi, reported_at) would discard one of them.
    """
    ingest_messages(published_stream(), store, dead_letters)

    first, second = store.get(81), store.get(113)

    assert first is not None and second is not None
    assert first.mmsi == second.mmsi
    assert first.reported_at == second.reported_at
    assert (first.latitude, first.longitude) != (second.latitude, second.longitude)


# --------------------------------------------------------------------------------------
# Rejection
# --------------------------------------------------------------------------------------


def test_an_invalid_report_is_rejected_rather_than_written(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """A Rejected Report is counted and set aside, never written (CONTEXT.md)."""
    messages = chain(published_stream(), malformed_messages(1))

    result = ingest_messages(messages, store, dead_letters)

    assert result.written == FEED_SIZE
    assert result.rejected == 1
    assert store.count() == FEED_SIZE


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("lat", 91.0, "latitude"),
        ("lon", 181.0, "longitude"),
        ("mmsi", 12345, "mmsi"),
        ("speed", -1, "speed"),
        ("timestamp", "half past three", "reported time"),
    ],
)
def test_a_report_breaking_a_rule_is_dead_lettered_with_its_reason(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
    field: str,
    value: Any,
    reason: str,
) -> None:
    """Bad data is inspectable rather than lost: the topic carries the message and why."""
    message = {**next(published_stream()), field: value}

    result = ingest_messages([message], store, dead_letters)

    assert result.rejected == 1
    assert result.written == 0
    assert store.count() == 0
    assert dead_letters.sent[0].message == message
    assert reason in dead_letters.sent[0].reason


def test_injected_malformed_messages_are_rejected_while_the_feed_is_written(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """Valid Position Reports keep being written while invalid ones are set aside.

    The supplied feed contains no invalid records, so the producer injects them; this
    is the whole rejection path exercised over a stream that is mostly good data.
    """
    result = ingest_messages(published_stream(inject_invalid=5), store, dead_letters)

    assert result.written == FEED_SIZE
    assert result.rejected == 5
    assert store.count() == FEED_SIZE
    # Five distinct reasons: what the producer injects really does break the rules the
    # consumer enforces, one rule each, rather than tripping the same one five times.
    assert len(set(dead_letters.reasons)) == 5


def test_the_supplied_feed_alone_is_rejected_nowhere(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """2,696 written and 0 rejected: the feed as supplied contains no invalid records."""
    result = ingest_messages(published_stream(), store, dead_letters)

    assert (result.written, result.rejected) == (FEED_SIZE, 0)
    assert dead_letters.sent == []


# --------------------------------------------------------------------------------------
# Crash recovery (ADR-0004)
# --------------------------------------------------------------------------------------


def test_a_batch_lost_to_a_crash_comes_back_on_replay(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """Nothing is lost when the consumer dies with a batch still in hand.

    Redelivery resumes at the last committed offset, not at the last written row. The
    writer dies holding 100 records that no committed batch covered, so redelivery
    restarts at 500 and those 100 come back with it.
    """
    crashed = BatchWriter(store, dead_letters, batch_size=250)
    for message in islice(published_stream(), 600):
        crashed.add(message)
    assert store.count() == 500  # two batches written; 100 died in hand

    redelivered = islice(published_stream(), 500, None)
    result = ingest_messages(redelivered, store, dead_letters)

    assert result.written == FEED_SIZE - 500
    assert store.count() == FEED_SIZE


def test_a_batch_written_before_a_crash_is_not_written_twice(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """Nothing is duplicated when the crash falls between the write and the commit.

    That window is the normal case, not the exceptional one: offsets move only after
    the datastore transaction, so the replayed messages are ones already stored, and
    the Report ID primary key discards them (ADR-0004).
    """
    crashed = BatchWriter(store, dead_letters, batch_size=250)
    for message in islice(published_stream(), 500):
        crashed.add(message)

    result = ingest_messages(published_stream(), store, dead_letters)

    assert result.written == FEED_SIZE - 500
    assert store.count() == FEED_SIZE


class FlakyStore(PositionReportStore):
    """A real store whose first few writes fail the way an outage does.

    Real PostgreSQL underneath, so the rows asserted on are real rows; only the
    failure is injected.
    """

    def __init__(self, dsn: str, failures: int) -> None:
        super().__init__(dsn)
        self._remaining = failures

    def insert_many(self, reports: Sequence[PositionReport]) -> int:
        if self._remaining:
            self._remaining -= 1
            raise TransientFailure("connection reset by peer")
        return super().insert_many(reports)


def test_a_transient_failure_is_waited_out_rather_than_dead_lettered(
    dsn: str,
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """The consumer stalls rather than skipping data it could have written (ADR-0004).

    Dead-lettering an outage would discard valid Position Reports over a fault that
    has nothing to do with them, so a transient failure is retried indefinitely and
    reaches the dead-letter topic never.
    """
    slept: list[float] = []
    flaky = FlakyStore(dsn, failures=3)
    try:
        writer = BatchWriter(
            flaky,
            dead_letters,
            batch_size=FEED_SIZE,
            backoff=Backoff(initial_seconds=0.5, sleep=slept.append),
        )
        for message in published_stream():
            writer.add(message)
        writer.flush()
    finally:
        flaky.close()

    assert writer.written == FEED_SIZE
    assert store.count() == FEED_SIZE
    assert dead_letters.sent == []
    assert slept == [0.5, 1.0, 2.0]


def test_a_report_the_datastore_refuses_does_not_take_its_batch_down(
    store: PositionReportStore,
    dead_letters: RecordingDeadLetters,
    published_stream: Stream,
) -> None:
    """A row the datastore will not have is poison too, isolated rather than retried.

    Speed 99999 is a non-negative wire value, so validation passes it, but it decodes
    to 9999.9 knots and overflows the column. Validation cannot anticipate every such
    case, so the write falls back to report by report and sets aside only the refused
    one - retrying it forever would wedge ingest, which is what ADR-0004 forbids.
    """
    refused = {**next(published_stream()), "stationId": 999999, "speed": 99999}

    result = ingest_messages(chain(published_stream(), [refused]), store, dead_letters)

    assert result.written == FEED_SIZE
    assert result.rejected == 1
    assert store.count() == FEED_SIZE
    assert "refused" in dead_letters.sent[0].reason
