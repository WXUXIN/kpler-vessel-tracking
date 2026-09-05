"""The Position Report API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, Field

from vessel_tracking.domain import PositionReport
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore, ReportFilter

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000

# Why there is no sort parameter, said where a caller will look for one. The endpoint
# description carries it into the OpenAPI document (ADR-0006).
ORDERING = """Position Reports are ordered by Report ID, the sequence in which the feed
received them, and no alternative ordering is offered.

Reported Time cannot support one: it is what a Vessel *said* the time was, it is coarse
- every value in the supplied feed falls on a minute boundary - and it repeats, with one
value shared by 272 reports covering the whole geographic range. Ordering by it would
neither reconstruct a voyage nor page stably, because a page boundary falling inside a
group of equal Reported Times has no defined place to resume from.

Report ID ascends strictly in receipt order, which makes it both the honest ordering and
a cursor that cannot drift."""


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
    """One page of Position Reports, and where to resume.

    `next_cursor` is null exactly when the collection is exhausted, so a caller pages
    until it disappears rather than until a page comes back short.
    """

    items: list[PositionReportResource]
    next_cursor: int | None = None

    @classmethod
    def of(
        cls, reports: Sequence[PositionReport], limit: int
    ) -> PositionReportPage:
        """Build a page from one report more than the page holds.

        Asking the datastore for the extra report is what lets the cursor be null
        exactly when the collection is exhausted: a full page and a final page are
        otherwise the same thing, and the caller is sent back for an empty one.
        """
        page = reports[:limit]
        return cls(
            items=[PositionReportResource.of(report) for report in page],
            next_cursor=page[-1].report_id if len(reports) > limit else None,
        )


class ReportQuery(BaseModel):
    """The collection endpoint's query parameters, as one object.

    Held apart from ReportFilter so the datastore stays free of the web layer, and
    gathered here so that adding a filter is one field rather than an edit in four
    places. FastAPI still publishes each field as its own query parameter, which is
    what lets a validation error name the bound that was wrong.
    """

    mmsi: list[int] = Field(
        default_factory=list,
        description="Repeat the parameter to ask about several Vessels.",
    )
    reported_from: datetime | None = Field(
        None, description="Start of the interval, inclusive."
    )
    reported_to: datetime | None = Field(
        None, description="End of the interval, exclusive."
    )
    min_latitude: float | None = Field(None, ge=-90, le=90, description="Southern bound.")
    max_latitude: float | None = Field(None, ge=-90, le=90, description="Northern bound.")
    min_longitude: float | None = Field(
        None, ge=-180, le=180, description="Western bound."
    )
    max_longitude: float | None = Field(
        None, ge=-180, le=180, description="Eastern bound."
    )
    after: int | None = Field(
        None,
        description="Resume after this Report ID, taken from the previous page's next_cursor.",
    )
    limit: int = Field(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Page size. Defaults to {DEFAULT_PAGE_SIZE}, at most {MAX_PAGE_SIZE}.",
    )

    def filters(self) -> ReportFilter:
        """The same request as the datastore sees it."""
        return ReportFilter(
            mmsis=self.mmsi,
            after_report_id=self.after,
            reported_from=_as_utc(self.reported_from),
            reported_to=_as_utc(self.reported_to),
            min_latitude=self.min_latitude,
            max_latitude=self.max_latitude,
            min_longitude=self.min_longitude,
            max_longitude=self.max_longitude,
        )


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
    @app.get(
        "/v1/position-reports",
        response_model=PositionReportPage,
        summary="List Position Reports",
        description=ORDERING,
    )
    def list_position_reports(
        request: Request, query: Annotated[ReportQuery, Query()]
    ) -> PositionReportPage:
        # One report more than the page, so that "is there another page" is answered by
        # the datastore rather than guessed from a page that happens to look full.
        reports = request.app.state.store.list_reports(query.limit + 1, query.filters())
        return PositionReportPage.of(reports, query.limit)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
