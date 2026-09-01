"""The ingest seam: feed file -> messages -> processed -> datastore.

The broker is faked by passing the producer's message stream straight into the
consumer's batch processor. Everything asserted here is externally observable:
rows in the datastore, and the counters the pipeline reports.
"""

import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from vessel_tracking.feed import read_feed
from vessel_tracking.ingest import ingest_messages
from vessel_tracking.store import PositionReportStore

FEED_SIZE = 2696


def test_the_whole_feed_reaches_the_datastore(
    store: PositionReportStore, feed_path: pathlib.Path
) -> None:
    result = ingest_messages(read_feed(feed_path), store)

    assert result.written == FEED_SIZE
    assert store.count() == FEED_SIZE


def test_replaying_the_feed_writes_nothing_new(
    store: PositionReportStore, feed_path: pathlib.Path
) -> None:
    """At-least-once delivery must not corrupt the store (ADR-0004)."""
    ingest_messages(read_feed(feed_path), store)

    result = ingest_messages(read_feed(feed_path), store)

    assert result.written == 0
    assert store.count() == FEED_SIZE


def test_ais_wire_encodings_are_normalised_on_write(
    store: PositionReportStore, feed_path: pathlib.Path
) -> None:
    """Speed to knots, Reported Time to a real timestamp, absent Rate of Turn to null."""
    ingest_messages(read_feed(feed_path), store)

    report = store.get(81)

    assert report is not None
    assert report.speed_knots == Decimal("18.0")  # wire value was 180
    assert report.reported_at == datetime(2013, 7, 1, 13, 6, tzinfo=UTC)
    assert report.rate_of_turn is None  # wire value was the empty string
    assert report.mmsi == 247039300


def test_conflicting_reports_are_both_retained(
    store: PositionReportStore, feed_path: pathlib.Path
) -> None:
    """The store records observations rather than adjudicating between them (ADR-0001).

    Report 81 and report 113 share an MMSI and a Reported Time but disagree about
    position by some 160km. Keying on (mmsi, reported_at) would discard one of them.
    """
    ingest_messages(read_feed(feed_path), store)

    first, second = store.get(81), store.get(113)

    assert first is not None and second is not None
    assert first.mmsi == second.mmsi
    assert first.reported_at == second.reported_at
    assert (first.latitude, first.longitude) != (second.latitude, second.longitude)
