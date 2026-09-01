"""The Position Report API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from pydantic import BaseModel

from vessel_tracking.domain import PositionReport
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore

# Serving the whole collection is safe only because a cap bounds it. Keyset paging
# replaces the cap in its own ticket; until then this is what stops an unbounded
# query exhausting memory.
MAX_RESULT_SIZE = 100_000


class PositionReportResource(BaseModel):
    """A Position Report as served: natural units, no wire encodings."""

    report_id: int
    mmsi: int
    reported_at: datetime
    nav_status: int
    speed_knots: float | None
    course_degrees: int | None
    heading_degrees: int | None
    rate_of_turn: int | None
    latitude: float
    longitude: float

    @classmethod
    def of(cls, report: PositionReport) -> PositionReportResource:
        return cls(
            report_id=report.report_id,
            mmsi=report.mmsi,
            reported_at=report.reported_at,
            nav_status=report.nav_status,
            speed_knots=(
                None if report.speed_knots is None else float(report.speed_knots)
            ),
            course_degrees=report.course_degrees,
            heading_degrees=report.heading_degrees,
            rate_of_turn=report.rate_of_turn,
            latitude=report.latitude,
            longitude=report.longitude,
        )


class PositionReportPage(BaseModel):
    items: list[PositionReportResource]


def create_app(store: PositionReportStore | None = None) -> FastAPI:
    """Build the application.

    A store may be supplied, in which case the caller owns its lifetime; otherwise one
    is opened from settings for the lifetime of the application.
    """
    owns_store = store is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = store or PositionReportStore(Settings().database_url)
        try:
            yield
        finally:
            if owns_store:
                app.state.store.close()

    app = FastAPI(
        title="Vessel Tracking",
        summary="Position Reports observed from AIS.",
        lifespan=lifespan,
    )

    @app.get("/v1/positions", response_model=PositionReportPage)
    def list_position_reports(request: Request) -> PositionReportPage:
        reports = request.app.state.store.list_reports(MAX_RESULT_SIZE)
        return PositionReportPage(
            items=[PositionReportResource.of(report) for report in reports]
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
