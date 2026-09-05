"""The Position Report API."""

from __future__ import annotations

import logging

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from vessel_tracking.domain import PositionReport
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore, ReportFilter

log = logging.getLogger("vessel_tracking.api")

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


# RFC 9457. One shape for every failure, because two error formats are worse than one:
# a caller who has to branch on which one arrived is not being handled programmatically.
PROBLEM_MEDIA_TYPE = "application/problem+json"

# A type is a URI *identifying* a problem, not a page to fetch. "about:blank" means the
# problem is fully described by the status code; anything carrying extension members
# gets its own, so a caller can tell from the type that `errors` will be there.
BLANK_PROBLEM = "about:blank"
INVALID_PARAMETERS = "/problems/invalid-parameters"


# Declared on every route that can fail, so a generated client expects the shape the
# wire actually returns rather than the framework's default.
class InvalidParameter(BaseModel):
    """One rejected query parameter, named so the caller knows which to fix."""

    parameter: str
    detail: str


class Problem(BaseModel):
    """An RFC 9457 problem document: the only error body this API returns."""

    type: str = BLANK_PROBLEM
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[InvalidParameter] | None = None


PROBLEM_RESPONSE: dict[str, Any] = {
    "model": Problem,
    "description": "An RFC 9457 problem document.",
    "content": {PROBLEM_MEDIA_TYPE: {}},
}


def _status_phrase(status: int) -> str:
    """The IANA phrase for a status, or a plain title for a code that has none."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def _occurrence(request: Request) -> str:
    """What actually failed, not merely where.

    RFC 9457 asks instance to identify the occurrence. The path alone is the same on
    every rejected request to a collection; the query string is the part that varied.
    """
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _parameter_name(location: Sequence[Any]) -> str:
    """The query parameter a caller can actually act on.

    Pydantic locates a failure as ("query", "mmsi") for a scalar and
    ("query", "mmsi", 0) for one bad value inside a repeated parameter. The name is what
    the caller wrote; the trailing index is a position inside their own repetition, and
    naming a parameter "0" back to them helps nobody.
    """
    return str(location[1]) if len(location) > 1 else str(location[0])


def _problem_response(
    problem: Problem, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """The document, plus whatever headers the status itself requires.

    A 405 without Allow, or a 503 without Retry-After, is a worse answer than the
    framework's default would have been (RFC 9110).
    """
    return JSONResponse(
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        content=problem.model_dump(exclude_none=True),
        headers=dict(headers) if headers else None,
    )


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
        None,
        description="Start of the interval, inclusive. A value without a timezone is read as UTC.",
    )
    reported_to: datetime | None = Field(
        None,
        description="End of the interval, exclusive. A value without a timezone is read as UTC.",
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

    A naive bound compared in some other zone answers a question the caller did not ask,
    so ordinary ISO-8601 input works without timezone boilerplate. The assumption is
    stated on both interval parameters, where a caller passes them.
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

    @app.exception_handler(RequestValidationError)
    async def invalid_parameters(
        request: Request, failure: RequestValidationError
    ) -> JSONResponse:
        """Validation detail inside the problem document, not beside it.

        """
        return _problem_response(
            Problem(
                type=INVALID_PARAMETERS,
                title="Invalid parameters",
                status=422,
                detail="The request could not be understood as it stands.",
                instance=_occurrence(request),
                errors=[
                    InvalidParameter(
                        parameter=_parameter_name(error["loc"]),
                        detail=str(error["msg"]),
                    )
                    for error in failure.errors()
                ],
            )
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_problem(
        request: Request, failure: StarletteHTTPException
    ) -> JSONResponse:
        return _problem_response(
            Problem(
                title=_status_phrase(failure.status_code),
                status=failure.status_code,
                detail=str(failure.detail) if failure.detail else None,
                instance=_occurrence(request),
            ),
            headers=failure.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_problem(request: Request, failure: Exception) -> JSONResponse:
        """The same document for a failure nobody anticipated.

        The cause is logged rather than returned: a caller can do nothing with it, and a
        stack trace or a connection string on the wire is a gift to the wrong reader.
        """
        log.exception("unhandled request failure", exc_info=failure)
        return _problem_response(
            Problem(
                title=HTTPStatus.INTERNAL_SERVER_ERROR.phrase,
                status=500,
                detail="The request could not be served. The failure has been logged.",
                instance=_occurrence(request),
            )
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
        responses={
            422: PROBLEM_RESPONSE,
            500: PROBLEM_RESPONSE,
        },
    )
    def list_position_reports(
        request: Request, query: Annotated[ReportQuery, Query()]
    ) -> PositionReportPage:
        # One report more than the page, so that "is there another page" is answered by
        # the datastore rather than guessed from a page that happens to look full.
        reports = request.app.state.store.list_reports(query.limit + 1, query.filters())
        return PositionReportPage.of(reports, query.limit)

    def problem_only_openapi() -> dict[str, Any]:
        """The generated document, saying only what these routes actually return.

        Declaring a response `model` makes FastAPI advertise application/json. These
        routes answer only with a problem document, and a schema promising both formats
        is the very thing this error contract exists to remove - in the one artefact a
        client is generated from.
        """
        if not app.openapi_schema:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                summary=app.summary,
                routes=app.routes,
            )
            for path in schema.get("paths", {}).values():
                for operation in path.values():
                    for response in operation.get("responses", {}).values():
                        content = response.get("content", {})
                        if PROBLEM_MEDIA_TYPE in content and "application/json" in content:
                            content[PROBLEM_MEDIA_TYPE] = content.pop("application/json")
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = problem_only_openapi  # type: ignore[method-assign]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
