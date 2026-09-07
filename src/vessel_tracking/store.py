"""Position Report storage.

Hand-written SQL over psycopg (ADR-0005). Filter values always reach the database as
parameters and are never interpolated into query text.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from vessel_tracking.domain import PositionReport


class TransientFailure(Exception):
    """A datastore failure that a later attempt may well survive.

    The other half of ADR-0004's taxonomy, and the counterpart of the domain's
    InvalidReport: this one is retried with backoff and never dead-lettered, because
    discarding valid Position Reports over an outage that has nothing to do with them
    is precisely the failure the taxonomy exists to prevent.
    """


class UnstorableReport(Exception):
    """A write the datastore refuses however often it is retried.

    Poison at the row level rather than the message level: the report passed validation
    but the datastore will not have it - a value outside a column's range, say.
    Validation cannot anticipate every such case, so this lands on the dead-letter side
    of ADR-0004's taxonomy rather than the retry side.
    """


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """What a caller asked the collection to be narrowed to.

    Every field is optional and they combine with AND, so an unset field constrains
    nothing and the empty filter is the whole collection.
    """

    mmsis: Sequence[int] = ()
    after_report_id: int | None = None
    reported_from: datetime | None = None
    reported_to: datetime | None = None
    min_latitude: float | None = None
    max_latitude: float | None = None
    min_longitude: float | None = None
    max_longitude: float | None = None
    centre_latitude: float | None = None
    centre_longitude: float | None = None
    radius_metres: float | None = None

    def __post_init__(self) -> None:
        """A circle is all three of its parts or none of them.

        The API refuses a partial circle before it gets here, but a filter built any
        other way would fail quietly rather than loudly: a radius without a centre
        compares against NULL and matches nothing, and a centre without a radius is
        dropped. Both look exactly like a query that worked.
        """
        circle = (self.centre_latitude, self.centre_longitude, self.radius_metres)
        if any(part is not None for part in circle) and None in circle:
            raise ValueError("a circle needs a centre and a radius, or neither")

    def conditions(self) -> tuple[list[str], list[Any]]:
        """SQL fragments and the parameters that fill them, in step.

        Values never reach the statement text: each fragment carries a placeholder and
        its value travels beside it in the parameter list, so a filter value cannot
        become SQL however strange it is (ADR-0005).
        """
        fragments: list[str] = []
        params: list[Any] = []
        if self.mmsis:
            fragments.append("mmsi = ANY(%s)")
            params.append(list(self.mmsis))
        # Keyset, not offset: the page resumes at a position in the ordering rather
        # than counting rows, so inserts behind the cursor cannot shift a caller's
        # place. Report ID only ever ascends, so this never skips a report (ADR-0006).
        if self.after_report_id is not None:
            fragments.append("report_id > %s")
            params.append(self.after_report_id)
        # Half-open, so adjacent windows neither overlap nor double-count: a report
        # standing exactly on a bound belongs to the window that starts there.
        if self.reported_from is not None:
            fragments.append("reported_at >= %s")
            params.append(self.reported_from)
        if self.reported_to is not None:
            fragments.append("reported_at < %s")
            params.append(self.reported_to)
        # Four bounds rather than one packed value, so a rejected one can be named.
        # Inclusive on all four: a box is an area a caller drew, not a pair of windows
        # to tile, so there is no double-counting to avoid.
        for column, bound, comparison in (
            ("latitude", self.min_latitude, ">="),
            ("latitude", self.max_latitude, "<="),
            ("longitude", self.min_longitude, ">="),
            ("longitude", self.max_longitude, "<="),
        ):
            if bound is not None:
                fragments.append(f"{column} {comparison} %s")
                params.append(bound)
        # On the geography type ST_DWithin measures across the spheroid, so a circle
        # stays a circle at any latitude, and it can use the GiST index rather than
        # computing a distance for every row. ST_MakePoint takes x then y: longitude
        # before latitude, which is the opposite of how they are said aloud.
        if self.radius_metres is not None:
            fragments.append(
                "ST_DWithin(position,"
                " ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)"
            )
            params.extend(
                [self.centre_longitude, self.centre_latitude, self.radius_metres]
            )
        return fragments, params


_COLUMNS = """
    report_id, mmsi, reported_at, nav_status, speed_knots,
    course_degrees, heading_degrees, rate_of_turn, latitude, longitude
"""

# Conflicts on Report ID are ignored rather than updated: a redelivered Position Report
# is the same observation arriving twice, so discarding it is correct (ADR-0004).
_INSERT = f"""
    INSERT INTO position_report ({_COLUMNS})
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (report_id) DO NOTHING
"""


# How many rows a server-side cursor hands over at a time. At today's page cap of 1,000
# this is a handful of fetches rather than many: the cursor is here for the shape of the
# design, not because this volume needs it.
STREAM_CHUNK = 100


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One request as the log keeps it: what was asked, and what came back."""

    method: str
    path: str
    query: str | None
    client_ip: str
    status: int
    duration_ms: float
    response_bytes: int


_RECORD_REQUEST = """
    INSERT INTO request_log
        (method, path, query, client_ip, status, duration_ms, response_bytes)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


class PositionReportStore:
    def __init__(self, dsn: str) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=5, open=True)

    def close(self) -> None:
        self._pool.close()

    @staticmethod
    def _select(filters: ReportFilter) -> tuple[str, list[Any]]:
        """The one statement both the paged read and the export are built from."""
        fragments, params = filters.conditions()
        where = f" WHERE {' AND '.join(fragments)}" if fragments else ""

        # The order here is the trustworthy sequence (ADR-0006): Report ID only ever ascends, so a
        # caller that paginates through it sees every report once and only once, even if
        # new reports arrive while the pagination is in progress.
        return (
            f"SELECT {_COLUMNS} FROM position_report{where}"
            " ORDER BY report_id LIMIT %s", 
            params,
        )

    def insert_many(self, reports: Sequence[PositionReport]) -> int:
        """Write a batch in one transaction. Returns the number of rows actually added.

        Raises TransientFailure when the datastore is unreachable, so that the caller
        can tell an outage apart from a message that can never be stored. Only the
        write path classifies: the read paths belong to the API, whose error contract
        is a separate concern.
        """
        if not reports:
            return 0
        rows: list[tuple[Any, ...]] = [
            (
                report.report_id,
                report.mmsi,
                report.reported_at,
                report.nav_status,
                report.speed_knots,
                report.course_degrees,
                report.heading_degrees,
                report.rate_of_turn,
                report.latitude,
                report.longitude,
            )
            for report in reports
        ]
        try:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.executemany(_INSERT, rows) # execute the insert statement for each row in the batch
                return cur.rowcount
        except psycopg.OperationalError as failure:
            # PoolTimeout is one of these: the datastore is out of reach either way.
            raise TransientFailure(str(failure)) from failure
        except psycopg.Error as failure:
            raise UnstorableReport(str(failure)) from failure

    def stream_reports(
        self, limit: int, filters: ReportFilter = ReportFilter()
    ) -> Generator[PositionReport, None, None]:
        """Position Reports one at a time, from a cursor held on the server.

        The rows stay in the datastore until they are fetched, so exporting a large
        result costs this process a chunk at a time rather than the whole set. The
        A pooled connection is held for as long as the caller keeps iterating, so a
        caller that stops early must close the iterator - closing it unwinds these
        blocks and hands the connection back. This pool belongs to the API process
        alone; the consumer has its own, so an abandoned export cannot reach ingest.
        """
        statement, params = self._select(filters)
        with self._pool.connection() as conn:
            with conn.cursor(
                name="position_reports", row_factory=class_row(PositionReport)
            ) as cur:
                cur.itersize = STREAM_CHUNK
                cur.execute(statement, (*params, limit))
                yield from cur

    def record_request(self, record: RequestRecord) -> None:
        """Write one request to the log. Called off the response path."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                _RECORD_REQUEST,
                (
                    record.method,
                    record.path,
                    record.query,
                    record.client_ip,
                    record.status,
                    record.duration_ms,
                    record.response_bytes,
                ),
            )

    def count(self) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM position_report")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get(self, report_id: int) -> PositionReport | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(PositionReport)) as cur:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM position_report WHERE report_id = %s",
                    (report_id,),
                )
                return cur.fetchone()

    def list_reports(
        self, limit: int, filters: ReportFilter = ReportFilter()
    ) -> list[PositionReport]:
        """Position Reports in Report ID order - the trustworthy sequence (ADR-0006).

        Only fixed fragments from ReportFilter reach the statement text; everything a
        caller supplied travels as a parameter.
        """
        statement, params = self._select(filters)
        with self._pool.connection() as conn:

            # cur here represents one query's execution and results
            # class_row builds PositionReport instances from each row, so the caller gets a list of them
            with conn.cursor(row_factory=class_row(PositionReport)) as cur:
                cur.execute(statement, (*params, limit))
                return cur.fetchall() # we want all the rows at once, not a cursor, because this is a paged read rather than an export
