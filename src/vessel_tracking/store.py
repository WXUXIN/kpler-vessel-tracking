"""Position Report storage.

Hand-written SQL over psycopg (ADR-0005). Filter values always reach the database as
parameters and are never interpolated into query text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from vessel_tracking.domain import PositionReport

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
        """Write a batch in one transaction. Returns the number of rows actually added."""
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
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(_INSERT, rows)
            return cur.rowcount

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
