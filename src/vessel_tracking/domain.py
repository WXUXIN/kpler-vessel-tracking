"""The Position Report and the AIS wire decoding that produces one.

Definitions for the vocabulary used here live in CONTEXT.md.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

# AIS transmits Speed as an integer ten times the value in knots (ADR-0002).
SPEED_SCALE = Decimal(10)

# The AIS sentinels for "not available", which decode to null rather than to an absurd
# reading. Neither appears in the supplied feed, so these come from the specification
# rather than from the data (ADR-0002).
SPEED_UNAVAILABLE = 1023
HEADING_UNAVAILABLE = 511

# The validation rules, defined here and nowhere else. The consumer rejects messages
# against them; the producer builds its injected malformed messages to violate them.
LATITUDE_LIMIT = 90.0
LONGITUDE_LIMIT = 180.0
MMSI_MIN, MMSI_MAX = 100_000_000, 999_999_999


class InvalidReport(ValueError):
    """A message that can never be stored, however often it is retried.

    A poison message in the sense of ADR-0004: dead-lettered and counted, never
    retried. Transient failures are a different class entirely and never raise this.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RejectedReport:
    """An inbound message that failed validation and was never written.

    Counted and set aside rather than silently dropped: this is what goes to the
    dead-letter topic, carrying the message as it arrived and why it was rejected.
    """

    message: Any
    reason: str


@dataclass(frozen=True, slots=True)
class PositionReport:
    """A single observation of a Vessel received from AIS.

    A record of what arrived, never a claim about where the Vessel truly was.
    """

    report_id: int
    mmsi: int
    reported_at: datetime
    nav_status: int
    speed_knots: Decimal | None
    course_degrees: int | None
    heading_degrees: int | None
    rate_of_turn: int | None
    latitude: float
    longitude: float


# A rejection reason is read by a person, so it names the domain concept rather than
# the wire field it arrived in - several of those names are on CONTEXT.md's avoid list.
# The dead-lettered message carries the wire field itself for anyone who needs it.
_CONCEPTS: Mapping[str, str] = {
    "stationId": "report id",
    "mmsi": "mmsi",
    "timestamp": "reported time",
    "status": "navigational status",
    "speed": "speed",
    "course": "course",
    "heading": "heading",
    "rot": "rate of turn",
    "lat": "latitude",
    "lon": "longitude",
}

Number = TypeVar("Number", int, float)


def _required(
    message: Mapping[str, Any], field: str, parse: Callable[[Any], Number]
) -> Number:
    concept = _CONCEPTS[field]
    try:
        value = message[field]
    except KeyError:
        raise InvalidReport(f"{concept} is missing") from None
    try:
        return parse(value)
    except (TypeError, ValueError):
        raise InvalidReport(f"{concept} is not a number: {value!r}") from None


def _required_int(message: Mapping[str, Any], field: str) -> int:
    return _required(message, field, int)


def _required_float(message: Mapping[str, Any], field: str) -> float:
    return _required(message, field, float)


def _optional_int(message: Mapping[str, Any], field: str) -> int | None:
    """AIS fields arrive absent, empty, or numeric; only the last is a value."""
    value = message.get(field)
    if value is None or value == "":
        return None
    return _required_int(message, field)


def _reported_time(message: Mapping[str, Any]) -> datetime:
    """Reported Time arrives as Unix epoch seconds and is stored as a real timestamp."""
    seconds = _required_int(message, "timestamp")
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OSError, OverflowError, ValueError):
        raise InvalidReport(f"reported time is out of range: {seconds}") from None


def from_message(message: Mapping[str, Any]) -> PositionReport:
    """Decode a raw feed message into a Position Report in natural units.

    The feed's `stationId` becomes the Report ID: it is unique across the feed and
    ascending in receipt order, and does not identify a receiving station (ADR-0001).

    Raises InvalidReport if the message breaks a rule above, which makes it a Rejected
    Report rather than a Position Report.
    """
    latitude = _required_float(message, "lat")
    if not -LATITUDE_LIMIT <= latitude <= LATITUDE_LIMIT:
        raise InvalidReport(f"latitude out of range: {latitude}")

    longitude = _required_float(message, "lon")
    if not -LONGITUDE_LIMIT <= longitude <= LONGITUDE_LIMIT:
        raise InvalidReport(f"longitude out of range: {longitude}")

    mmsi = _required_int(message, "mmsi")
    if not MMSI_MIN <= mmsi <= MMSI_MAX:
        raise InvalidReport(f"mmsi is not a nine-digit identifier: {mmsi}")

    speed = _optional_int(message, "speed")
    if speed is not None and speed < 0:
        raise InvalidReport(f"speed is negative: {speed}")
    if speed == SPEED_UNAVAILABLE:
        speed = None

    heading = _optional_int(message, "heading")
    if heading == HEADING_UNAVAILABLE:
        heading = None

    return PositionReport(
        report_id=_required_int(message, "stationId"),
        mmsi=mmsi,
        reported_at=_reported_time(message),
        nav_status=_required_int(message, "status"),
        speed_knots=None if speed is None else Decimal(speed) / SPEED_SCALE,
        course_degrees=_optional_int(message, "course"),
        heading_degrees=heading,
        rate_of_turn=_optional_int(message, "rot"),
        latitude=latitude,
        longitude=longitude,
    )


# A message that passes validation, and the base for the malformed ones below. Its MMSI
# is nine valid digits belonging to no Vessel in the feed, so injected junk is
# distinguishable from real traffic on the dead-letter topic.
_VALID_TEMPLATE: Mapping[str, Any] = {
    "mmsi": 999999999,
    "status": 0,
    "stationId": 0,
    "speed": 180,
    "lon": 15.4415,
    "lat": 42.75178,
    "course": 144,
    "heading": 144,
    "rot": "",
    "timestamp": 1372683960,
}

# Report IDs for injected messages, far clear of the feed's own range (81-3396) so that
# a dead-lettered report is never confused with a real one.
INJECTED_REPORT_ID_BASE = 900_000_000

# Each entry breaks exactly one rule defined above, expressed in terms of that rule so
# the injected messages cannot drift from what the consumer actually enforces.
_VIOLATIONS: tuple[tuple[str, Any], ...] = (
    ("lat", LATITUDE_LIMIT + 1.0),
    ("lon", LONGITUDE_LIMIT + 1.0),
    ("mmsi", MMSI_MIN // 10),
    ("speed", -1),
    ("timestamp", "half past three"),
)


def malformed_messages(count: int) -> Iterator[Mapping[str, Any]]:
    """Messages built to fail validation, one broken rule each, cycling through them.

    The supplied feed contains no invalid records, so the rejection path is only
    demonstrable against messages made for the purpose.
    """
    for index in range(count):
        field, value = _VIOLATIONS[index % len(_VIOLATIONS)]
        yield {
            **_VALID_TEMPLATE,
            "stationId": INJECTED_REPORT_ID_BASE + index,
            field: value,
        }
