"""AIS decoding cases the supplied feed does not contain.

The one place these tests reach beneath a seam, as the spec allows: the unavailable
sentinels and an empty Rate of Turn are awkward to stage through a full ingest run and
cheap to enumerate directly. Their values come from the AIS specification, not the data.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from vessel_tracking.domain import from_message

# A message that decodes cleanly; each case below varies one field of it.
VALID: dict[str, Any] = {
    "mmsi": 247039300,
    "status": 0,
    "stationId": 81,
    "speed": 180,
    "lon": 15.4415,
    "lat": 42.75178,
    "course": 144,
    "heading": 144,
    "rot": "",
    "timestamp": 1372683960,
}


@pytest.mark.parametrize(
    ("field", "wire_value", "attribute", "expected"),
    [
        ("heading", 511, "heading_degrees", None),  # AIS: heading unavailable
        ("heading", 0, "heading_degrees", 0),
        ("heading", 359, "heading_degrees", 359),
        ("speed", 1023, "speed_knots", None),  # AIS: speed unavailable
        ("speed", 0, "speed_knots", Decimal("0.0")),
        ("speed", 199, "speed_knots", Decimal("19.9")),
        ("rot", "", "rate_of_turn", None),  # empty in every supplied record
        ("rot", 0, "rate_of_turn", 0),
        ("rot", -127, "rate_of_turn", -127),
    ],
)
def test_ais_wire_values_decode_to_natural_units(
    field: str, wire_value: Any, attribute: str, expected: Any
) -> None:
    report = from_message({**VALID, field: wire_value})

    assert getattr(report, attribute) == expected
