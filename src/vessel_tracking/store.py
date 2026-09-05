"""Position Report storage.

Hand-written SQL over psycopg (ADR-0005). Filter values always reach the database as
parameters and are never interpolated into query text.
"""

from __future__ import annotations

from collections.abc import Sequence
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


class PositionReportStore:
    def __init__(self, dsn: str) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=5, open=True)

    def close(self) -> None:
        self._pool.close()

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
                cur.executemany(_INSERT, rows)
                return cur.rowcount
        except psycopg.OperationalError as failure:
            # PoolTimeout is one of these: the datastore is out of reach either way.
            raise TransientFailure(str(failure)) from failure
        except psycopg.Error as failure:
            raise UnstorableReport(str(failure)) from failure

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
        """Position Reports in Report ID order - the trustworthy sequence (ADR-0006)."""
        fragments, params = filters.conditions()
        # Only the fragments reach the statement text, and they are fixed strings from
        # ReportFilter itself. Everything a caller supplied travels as a parameter.
        where = f" WHERE {' AND '.join(fragments)}" if fragments else ""
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(PositionReport)) as cur:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM position_report{where}"
                    " ORDER BY report_id LIMIT %s",
                    (*params, limit),
                )
                return cur.fetchall()
