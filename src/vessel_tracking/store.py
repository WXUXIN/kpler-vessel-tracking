"""Position Report storage.

Hand-written SQL over psycopg (ADR-0005). Filter values always reach the database as
parameters and are never interpolated into query text.
"""

from __future__ import annotations

from collections.abc import Sequence
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

    def list_reports(self, limit: int) -> list[PositionReport]:
        """Position Reports in Report ID order - the trustworthy sequence (ADR-0006)."""
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(PositionReport)) as cur:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM position_report"
                    " ORDER BY report_id LIMIT %s",
                    (limit,),
                )
                return cur.fetchall()
