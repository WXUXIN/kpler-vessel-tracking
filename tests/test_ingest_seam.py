"""The ingest seam: feed file -> messages -> processed -> datastore.

The broker is faked by passing the producer's message stream straight into the
consumer's batch processor. Everything asserted here is externally observable:
rows in the datastore, and the counters the pipeline reports.
"""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from itertools import chain
from typing import Any, Callable

import pytest
from conftest import RecordingDeadLetters
from vessel_tracking.domain import malformed_messages
from vessel_tracking.ingest import ingest_messages
from vessel_tracking.store import PositionReportStore

Stream = Callable[..., Iterator[Mapping[str, Any]]]

FEED_SIZE = 2696


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
