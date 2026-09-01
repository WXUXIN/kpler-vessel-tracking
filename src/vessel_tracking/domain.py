"""The Position Report and the AIS wire decoding that produces one.

Definitions for the vocabulary used here live in CONTEXT.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

# AIS transmits Speed as an integer ten times the value in knots (ADR-0002).
SPEED_SCALE = Decimal(10)


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


def _optional_int(value: Any) -> int | None:
    """AIS fields arrive absent, empty, or numeric; only the last is a value."""
    if value is None or value == "":
        return None
    return int(value)


def from_message(message: Mapping[str, Any]) -> PositionReport:
    """Decode a raw feed message into a Position Report in natural units.

    The feed's `stationId` becomes the Report ID: it is unique across the feed and
    ascending in receipt order, and does not identify a receiving station (ADR-0001).
    """
    speed = _optional_int(message.get("speed"))
    return PositionReport(
        report_id=int(message["stationId"]),
        mmsi=int(message["mmsi"]),
        reported_at=datetime.fromtimestamp(int(message["timestamp"]), UTC),
        nav_status=int(message["status"]),
        speed_knots=None if speed is None else Decimal(speed) / SPEED_SCALE,
        course_degrees=_optional_int(message.get("course")),
        heading_degrees=_optional_int(message.get("heading")),
        rate_of_turn=_optional_int(message.get("rot")),
        latitude=float(message["lat"]),
        longitude=float(message["lon"]),
    )
