"""The Position Report API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel

from vessel_tracking.domain import PositionReport
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore, ReportFilter

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


def _as_utc(moment: datetime | None) -> datetime | None:
    """Read a bound without a timezone as UTC rather than as the server's local time.

    Documenting the assumption, and the error contract around bad input, belongs to the
    error ticket. Reading it consistently belongs here: a naive bound compared in some
    other zone answers a question the caller did not ask.
    """
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


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

    # The resource is a Position Report, not a position: it records what arrived from
    # AIS, never where a Vessel was. CONTEXT.md puts "position" on the avoid list for
    # exactly that reason, so the conventional-looking /v1/positions would assert
    # something the data cannot support.
    @app.get("/v1/position-reports", response_model=PositionReportPage)
    def list_position_reports(
        request: Request,
        mmsi: Annotated[
            list[int] | None,
            Query(description="Repeat the parameter to ask about several Vessels."),
        ] = None,
        reported_from: Annotated[
            datetime | None,
            Query(description="Start of the interval, inclusive."),
        ] = None,
        reported_to: Annotated[
            datetime | None,
            Query(description="End of the interval, exclusive."),
        ] = None,
        min_latitude: Annotated[
            float | None, Query(ge=-90, le=90, description="Southern bound.")
        ] = None,
        max_latitude: Annotated[
            float | None, Query(ge=-90, le=90, description="Northern bound.")
        ] = None,
        min_longitude: Annotated[
            float | None, Query(ge=-180, le=180, description="Western bound.")
        ] = None,
        max_longitude: Annotated[
            float | None, Query(ge=-180, le=180, description="Eastern bound.")
        ] = None,
    ) -> PositionReportPage:
        filters = ReportFilter(
            mmsis=mmsi or (),
            reported_from=_as_utc(reported_from),
            reported_to=_as_utc(reported_to),
            min_latitude=min_latitude,
            max_latitude=max_latitude,
            min_longitude=min_longitude,
            max_longitude=max_longitude,
        )
        reports = request.app.state.store.list_reports(MAX_RESULT_SIZE, filters)
        return PositionReportPage(
            items=[PositionReportResource.of(report) for report in reports]
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
