"""Shared computation behind the dataset observations.

Imported by both `explore_feed.py`, which prints a markdown report, and
`explore_feed.ipynb`, which adds charts. Keeping the arithmetic here means the two
cannot drift apart and quote different numbers.

Standard library only, so `explore_feed.py` runs anywhere with no install.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Record = dict[str, Any]

FEED = Path(__file__).resolve().parent.parent / "ship_positions.json"
KM_PER_NAUTICAL_MILE = 1.852
MOVING_SPEED_THRESHOLD = 5  # wire units; below this, implied interval is meaningless


def load(path: Path = FEED) -> list[Record]:
    return json.loads(path.read_text())


def haversine_km(a: Record, b: Record) -> float:
    radius = 6371.0
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M")


def vessels(records: list[Record]) -> list[int]:
    return sorted({r["mmsi"] for r in records})


def track(records: list[Record], mmsi: int, ordering: str) -> list[Record]:
    """One Vessel's reports, ordered by 'stationId' or by 'timestamp'."""
    rows = [r for r in records if r["mmsi"] == mmsi]
    if ordering == "stationId":
        return sorted(rows, key=lambda r: r["stationId"])
    return sorted(rows, key=lambda r: (r["timestamp"], r["stationId"]))


def shape(records: list[Record]) -> dict[str, Any]:
    timestamps = [r["timestamp"] for r in records]
    return {
        "records": len(records),
        "vessels": len(vessels(records)),
        "distinct_station_ids": len({r["stationId"] for r in records}),
        "fields": len(records[0]),
        "span_hours": (max(timestamps) - min(timestamps)) / 3600,
        "first": utc(min(timestamps)),
        "last": utc(max(timestamps)),
    }


def report_id_facts(records: list[Record]) -> dict[str, Any]:
    station_ids = [r["stationId"] for r in records]
    return {
        "unique": len(set(station_ids)) == len(records),
        "minimum": min(station_ids),
        "maximum": max(station_ids),
        "ascending_in_file_order": station_ids == sorted(station_ids),
    }


def conflicts(records: list[Record]) -> dict[str, Any]:
    """Reports sharing an MMSI and a Reported Time but disagreeing about position."""
    grouped: dict[tuple[int, int], list[Record]] = defaultdict(list)
    for record in records:
        grouped[(record["mmsi"], record["timestamp"])].append(record)
    repeated = {k: v for k, v in grouped.items() if len(v) > 1}
    separations = sorted(haversine_km(v[0], v[1]) for v in repeated.values())
    widest = max(repeated.values(), key=lambda v: haversine_km(v[0], v[1]))
    return {
        "distinct_pairs": len(grouped),
        "repeated_keys": len(repeated),
        "exact_duplicates": sum(1 for v in repeated.values() if all(x == v[0] for x in v)),
        "busiest_key_size": max(len(v) for v in repeated.values()),
        "records_lost": len(records) - len(grouped),
        "share_lost": (len(records) - len(grouped)) / len(records),
        "separations_km": separations,
        "widest_pair": widest[:2],
    }


def timestamp_facts(records: list[Record]) -> dict[str, Any]:
    timestamps = [r["timestamp"] for r in records]
    distinct = sorted(set(timestamps))
    per_value = Counter(timestamps)
    busiest, busiest_count = per_value.most_common(1)[0]
    return {
        "distinct": len(distinct),
        "all_gaps_minute_multiples": all(
            (b - a) % 60 == 0 for a, b in zip(distinct, distinct[1:])
        ),
        "busiest": busiest,
        "busiest_label": utc(busiest),
        "busiest_count": busiest_count,
        "counts": per_value,
    }


def ordering_comparison(records: list[Record]) -> list[dict[str, Any]]:
    """Hop distances along each Vessel's track under each candidate ordering."""
    rows = []
    for mmsi in vessels(records):
        for ordering in ("timestamp", "stationId"):
            ordered = track(records, mmsi, ordering)
            hops = sorted(haversine_km(a, b) for a, b in zip(ordered, ordered[1:]))
            rows.append(
                {
                    "mmsi": mmsi,
                    "ordering": ordering,
                    "total_path_km": sum(hops),
                    "median_hop_km": statistics.median(hops),
                    "p95_hop_km": hops[int(0.95 * len(hops))],
                }
            )
    return rows


def implied_intervals(records: list[Record]) -> dict[int, float]:
    """Seconds each report would need to reach the next, at its own reported Speed.

    Computed in stationId order. If that ordering is the true receipt sequence, these
    should land near the 60-second grid the timestamps use.
    """
    result = {}
    for mmsi in vessels(records):
        ordered = track(records, mmsi, "stationId")
        intervals = [
            haversine_km(a, b) / KM_PER_NAUTICAL_MILE / (a["speed"] / 10) * 3600
            for a, b in zip(ordered, ordered[1:])
            if a["speed"] > MOVING_SPEED_THRESHOLD
        ]
        result[mmsi] = statistics.median(intervals)
    return result


def field_ranges(records: list[Record]) -> dict[str, Any]:
    return {
        "speed": (min(r["speed"] for r in records), max(r["speed"] for r in records)),
        "course": (min(r["course"] for r in records), max(r["course"] for r in records)),
        "heading": (min(r["heading"] for r in records), max(r["heading"] for r in records)),
        "rot": dict(Counter(r["rot"] for r in records)),
        "status": dict(Counter(r["status"] for r in records)),
    }
