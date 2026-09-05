"""The Position Report API."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

import csv
import io
import time
from itertools import chain, islice
from collections.abc import (
    AsyncIterator,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
import anyio
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from vessel_tracking.domain import PositionReport
from vessel_tracking.limits import Decision, Limits
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore, ReportFilter, RequestRecord

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
    """One reason the request was rejected, named where a name applies.

    A parameter is absent when the rule was about the request as a whole rather than
    any single value the caller wrote.
    """

    parameter: str | None = None
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


# How many request records may be in flight at once. See RequestLog.record.
MAX_PENDING_RECORDS = 32


class RequestLog:
    """Writes request records without a request waiting for them.

    Each write is its own task, started once the response is on its way out. The tasks
    are held so that a shutdown can wait for them rather than cancelling records that
    have already been counted as kept.
    """

    def __init__(self, concurrency: int = MAX_PENDING_RECORDS) -> None:
        self._writing: set[asyncio.Task[None]] = set()
        self._concurrency = concurrency

    def record(self, store: PositionReportStore, record: RequestRecord) -> None:
        log.info(json.dumps({"event": "request", **dataclasses.asdict(record)}))
        if len(self._writing) >= self._concurrency:
            # An unreachable datastore parks each write in a worker thread until the
            # pool gives up. Left unbounded, enough of them exhaust the thread limiter
            # that ordinary requests - and the health probe - stop being served, so a
            # datastore outage would take the API with it. Records are dropped instead,
            # loudly, because losing a log line beats losing the service it describes.
            log.error(
                json.dumps(
                    {"event": "request_log_saturated", "pending": len(self._writing)}
                )
            )
            return
        task = asyncio.create_task(self._write(store, record))
        self._writing.add(task)
        task.add_done_callback(self._writing.discard)

    @staticmethod
    async def _write(store: PositionReportStore, record: RequestRecord) -> None:
        """A log that cannot be written is the operator's problem, not the caller's.

        The request has already been answered by the time this runs, so the only honest
        thing a failure here can do is say so.
        """
        try:
            await anyio.to_thread.run_sync(store.record_request, record)
        except Exception:
            log.exception("could not record a request")

    async def drain(self) -> None:
        """Wait for the writes, including any started while waiting for the others."""
        while self._writing:
            await asyncio.gather(*tuple(self._writing), return_exceptions=True)


# A container's liveness probe is not a caller. At one check every five seconds it
# spends twelve of a ten-request allowance per minute, and the API would then report
# itself unhealthy for correctly enforcing its own limit. Still recorded, just not
# counted: the log is a record of traffic, and this is traffic.
UNLIMITED_PATHS = frozenset({"/healthz"})

# The demo page under /ui and its assets are the same kind of exemption, extended to a
# prefix because it is a directory rather than one path. Only the data-plane calls the
# page itself makes to /v1/position-reports are meant to feel the limit.
UNLIMITED_PREFIX = "/ui/"


def _is_unlimited(path: str) -> bool:
    return path in UNLIMITED_PATHS or path == "/ui" or path.startswith(UNLIMITED_PREFIX)


class Traffic:
    """Rate limits, times and records every request.

    Raw ASGI rather than BaseHTTPMiddleware, which collects a response into memory
    before passing it on. That would quietly undo the CSV streaming in the one place it
    matters, so this watches the messages go past instead of holding them.
    """

    def __init__(self, app: ASGIApp, limits: Limits, requests: RequestLog) -> None:
        self._app = app
        self._limits = limits
        self._requests = requests

    async def _allowance(self, scope: Scope, client: str) -> Decision | None:
        """What the limiter says, or nothing at all if it cannot be asked.

        A limiter that is down fails open. The alternative turns an outage of the thing
        that protects capacity into an outage of the capacity itself, which is a worse
        answer to every caller in order to give a better one to none. It is logged at
        error, because for as long as it lasts the limit is not being applied.
        """
        if _is_unlimited(scope["path"]):
            return None
        try:
            return await self._limits.check(client)
        except Exception as unreachable:
            log.error(
                json.dumps(
                    {"event": "rate_limiter_unavailable", "detail": str(unreachable)}
                )
            )
            return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        client = scope["client"][0] if scope.get("client") else "unknown"
        started = time.perf_counter()
        status = 500
        written = 0

        async def watch(message: Message) -> None:
            nonlocal status, written
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                written += len(message.get("body", b""))
            await send(message)

        try:
            decision = await self._allowance(scope, client)
            if decision is None or decision.allowed:
                await self._app(scope, receive, watch)
            else:
                await _too_many_requests(scope, decision)(scope, receive, watch)
        finally:
            self._requests.record(
                scope["app"].state.store,
                RequestRecord(
                    method=scope["method"],
                    path=scope["path"],
                    query=scope["query_string"].decode() or None,
                    client_ip=client,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    response_bytes=written,
                ),
            )


def _too_many_requests(scope: Scope, decision: Decision) -> JSONResponse:
    """The same document as every other failure, plus what to do about this one."""
    return _problem_response(
        Problem(
            title=HTTPStatus.TOO_MANY_REQUESTS.phrase,
            status=429,
            detail=(
                f"At most {decision.allowance} requests are served per client "
                f"per minute. Try again in {decision.retry_after_seconds} seconds."
            ),
            instance=_occurrence(
                str(scope["path"]), scope["query_string"].decode()
            ),
        ),
        headers={
            "retry-after": str(decision.retry_after_seconds),
            "ratelimit-limit": str(decision.allowance),
            "ratelimit-remaining": str(decision.remaining),
            "ratelimit-reset": str(decision.retry_after_seconds),
        },
    )


def _status_phrase(status: int) -> str:
    """The IANA phrase for a status, or a plain title for a code that has none."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def _occurrence(path: str, query: str) -> str:
    """What actually failed, not merely where.

    RFC 9457 asks instance to identify the occurrence. The path alone is the same on
    every rejected request to a collection; the query string is the part that varied.
    """
    return f"{path}?{query}" if query else path


def _parameter_name(location: Sequence[Any]) -> str | None:
    """The query parameter a caller can actually act on, where there is one.

    Pydantic locates a failure as ("query", "mmsi") for a scalar, ("query", "mmsi", 0)
    for one bad value inside a repeated parameter, and ("query",) alone for a rule about
    the request rather than any single value. The name is what the caller wrote; the
    trailing index is a position inside their own repetition, and naming "0" or "query"
    back to someone who wrote neither helps nobody.
    """
    return str(location[1]) if len(location) > 1 else None


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
            # Normalised here rather than trusted from the connection: a timestamptz
            # comes back in the session's timezone, so a server configured otherwise
            # would render every report at an offset. Both formats read this field.
            reported_at=report.reported_at.astimezone(UTC),
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


# The international nautical mile: 1,852 metres exactly, by definition. Callers ask in
# nautical miles because that is what a chart is marked in; the geography type answers
# in metres, so the conversion happens once, here at the edge.
NAUTICAL_MILE_METRES = 1852.0

CSV_MEDIA_TYPE = "text/csv"
JSON_MEDIA_TYPE = "application/json"
Format = Literal["json", "csv"]
CSV_FIELDS = tuple(PositionReportResource.model_fields)


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
    centre_latitude: float | None = Field(
        None, ge=-90, le=90, description="Centre of the circle to search."
    )
    centre_longitude: float | None = Field(
        None, ge=-180, le=180, description="Centre of the circle to search."
    )
    radius_nautical_miles: float | None = Field(
        None,
        gt=0,
        description="Radius of the circle, measured across the sphere.",
    )
    after: int | None = Field(
        None,
        description="Resume after this Report ID, taken from the previous page's next_cursor.",
    )
    format: Format | None = Field(
        None,
        description=(
            "Overrides the Accept header, for callers who cannot set one. "
            "Defaults to JSON unless Accept asks for text/csv."
        ),
    )
    limit: int = Field(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Page size. Defaults to {DEFAULT_PAGE_SIZE}, at most {MAX_PAGE_SIZE}.",
    )

    @model_validator(mode="after")
    def _a_circle_is_all_or_nothing(self) -> ReportQuery:
        """Two thirds of a circle is not a smaller circle, it is an unanswerable ask.

        Silently ignoring a partial circle would answer a question the caller did not
        ask and look like it had worked, which is worse than refusing.
        """
        parts = {
            "centre_latitude": self.centre_latitude,
            "centre_longitude": self.centre_longitude,
            "radius_nautical_miles": self.radius_nautical_miles,
        }
        given = {name for name, value in parts.items() if value is not None}
        if given and len(given) < len(parts):
            missing = ", ".join(sorted(set(parts) - given))
            raise ValueError(f"a circle needs all three parts; missing {missing}")
        return self

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
            centre_latitude=self.centre_latitude,
            centre_longitude=self.centre_longitude,
            radius_metres=(
                None
                if self.radius_nautical_miles is None
                else self.radius_nautical_miles * NAUTICAL_MILE_METRES
            ),
        )


def _quality(accept: str, media_type: str) -> float:
    """How much the caller wants a media type: its q-value, or zero if unmentioned.

    Matches the type exactly, by its wildcard, or by */*. Not a general negotiation -
    this endpoint offers two representations, so all that matters is which of the two
    was preferred and whether either was refused outright.
    """
    kind, _, _ = media_type.partition("/")
    best = 0.0
    for offer in accept.split(","):
        name, *parameters = (token.strip() for token in offer.split(";"))
        if name.lower() not in (media_type, f"{kind}/*", "*/*"):
            continue
        weight = 1.0
        for parameter in parameters:
            if parameter.startswith("q="):
                try:
                    weight = float(parameter[2:])
                except ValueError:
                    weight = 0.0
        best = max(best, weight)
    return best


def _wants_csv(accept: str, override: Format | None) -> bool:
    """The query parameter wins: it exists for callers who cannot set a header.

    Otherwise CSV has to be both wanted and preferred. A tie goes to JSON, which is the
    documented default, and `text/csv;q=0` is a refusal rather than a request.
    """
    if override is not None:
        return override == "csv"
    wanted = _quality(accept, CSV_MEDIA_TYPE)
    return wanted > 0 and wanted > _quality(accept, JSON_MEDIA_TYPE)


def _csv_response(
    store: PositionReportStore, query: ReportQuery
) -> StreamingResponse:
    """Stream the export, but choose the status before the first byte goes out.

    Once a 200 is on the wire the error contract can no longer apply, so the datastore
    is reached and the first report pulled here, while a problem document is still
    possible. Only a failure part way through an export can truncate a response, which
    is a property of streaming rather than something left unhandled.
    """
    reports = store.stream_reports(query.limit, query.filters())
    try:
        first = list(islice(reports, 1))
    except BaseException:
        reports.close()
        raise
    return StreamingResponse(
        _csv_lines(first, reports),
        media_type=CSV_MEDIA_TYPE,
    )


def _csv_lines(
    first: Iterable[PositionReport], rest: Generator[PositionReport, None, None]
) -> Iterator[str]:
    """The same fields JSON serves, rendered once per report as they are pulled.

    Each row goes through PositionReportResource, so CSV cannot drift from JSON in
    either its columns or how it renders a value: both are the resource's own JSON
    form, and an absent value is an empty field rather than the word None.

    The remaining reports are taken as the generator itself rather than chained, so
    that closing this one closes that one: a chain cannot pass on a close, and the
    datastore connection would then be held until the collector happened to notice.
    """
    line = io.StringIO()
    writer = csv.writer(line)

    def emit() -> str:
        rendered = line.getvalue()
        line.seek(0)
        line.truncate()
        return rendered

    try:
        writer.writerow(CSV_FIELDS)
        yield emit()
        for report in chain(first, rest):
            row = PositionReportResource.of(report).model_dump(mode="json")
            writer.writerow([row[field] for field in CSV_FIELDS])
            yield emit()
    finally:
        rest.close()


def _as_utc(moment: datetime | None) -> datetime | None:
    """Read a bound without a timezone as UTC rather than as the server's local time.

    A naive bound compared in some other zone answers a question the caller did not ask,
    so ordinary ISO-8601 input works without timezone boilerplate. The assumption is
    stated on both interval parameters, where a caller passes them.
    """
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def create_app(
    store: PositionReportStore | None = None, limits: Limits | None = None
) -> FastAPI:
    """Build the application, and the entrypoint uvicorn calls with --factory.

    Being the entrypoint, it configures logging: uvicorn sets up its own loggers and
    leaves the root logger without handlers, so without this every request record would
    be built, formatted and then discarded. `force` is off so a host that has already
    configured logging keeps its own arrangement.

    A store and a limiter may be supplied, in which case the caller owns their
    lifetimes; otherwise they are opened from settings for the life of the application.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    owns_store = store is None
    owns_limits = limits is None
    limiter = limits or Limits(
        settings.redis_url,
        allowance=settings.rate_limit_allowance,
        window_seconds=settings.rate_limit_window_seconds,
    )
    requests = RequestLog()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = store or PositionReportStore(settings.database_url)
        try:
            yield
        finally:
            # Records already counted as kept are waited for rather than cancelled.
            await requests.drain()
            if owns_limits:
                await limiter.close()
            if owns_store:
                app.state.store.close()

    app = FastAPI(
        title="Vessel Tracking",
        summary="Position Reports observed from AIS.",
        lifespan=lifespan,
    )

    app.add_middleware(Traffic, limits=limiter, requests=requests)

    # A demo of the endpoints above, not an endpoint itself. See UNLIMITED_PREFIX.
    app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")

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
                instance=_occurrence(request.url.path, request.url.query),
                errors=[
                    InvalidParameter(
                        parameter=_parameter_name(error["loc"]),
                        # Pydantic prefixes a custom rule's message with "Value error,";
                        # the caller wrote the request, not the validator.
                        detail=str(error["msg"]).removeprefix("Value error, "),
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
                instance=_occurrence(request.url.path, request.url.query),
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
                instance=_occurrence(request.url.path, request.url.query),
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
            200: {
                "description": "A page of Position Reports.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/PositionReportPage"}
                    },
                    CSV_MEDIA_TYPE: {"schema": {"type": "string"}},
                },
            },
            422: PROBLEM_RESPONSE,
            500: PROBLEM_RESPONSE,
        },
    )
    def list_position_reports(
        request: Request, query: Annotated[ReportQuery, Query()]
    ) -> PositionReportPage | StreamingResponse:
        store = request.app.state.store
        if _wants_csv(request.headers.get("accept", ""), query.format):
            return _csv_response(store, query)
        # One report more than the page, so that "is there another page" is answered by
        # the datastore rather than guessed from a page that happens to look full.
        reports = store.list_reports(query.limit + 1, query.filters())
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
