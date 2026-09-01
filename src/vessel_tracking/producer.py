"""Publishes the AIS feed to the broker, one Position Report per message.

Messages are published in feed order - which is Report ID order, the receipt sequence -
and keyed by MMSI so that one Vessel's reports stay ordered within a partition.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from confluent_kafka import Producer

from vessel_tracking.feed import read_feed
from vessel_tracking.settings import Settings

log = logging.getLogger("vessel_tracking.producer")


def _json_default(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def encode(message: Mapping[str, Any]) -> bytes:
    """The exact bytes this producer puts on the topic.

    Incremental JSON parsing yields Decimal for the coordinate fields, which JSON
    cannot represent; they cross the wire as numbers.
    """
    return json.dumps(message, default=_json_default).encode()


def partition_key(message: Mapping[str, Any]) -> bytes:
    """One Vessel's reports share a key, so they stay ordered within a partition."""
    return str(message["mmsi"]).encode()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings()
    parser = argparse.ArgumentParser(description="Replay the AIS feed to the broker.")
    parser.add_argument("--feed", type=Path, default=settings.feed_path)
    parser.add_argument(
        "--rate",
        type=float,
        default=settings.producer_rate,
        help="messages per second; 0 publishes unthrottled",
    )
    args = parser.parse_args()

    producer = Producer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "enable.idempotence": True,
            "acks": "all",
        }
    )
    interval = 0.0 if args.rate <= 0 else 1.0 / args.rate

    published = 0
    for message in read_feed(args.feed):
        producer.produce(
            settings.kafka_topic,
            key=partition_key(message),
            value=encode(message),
        )
        producer.poll(0)
        published += 1
        if interval:
            time.sleep(interval)

    producer.flush()
    log.info(
        json.dumps(
            {"event": "feed_published", "published": published, "feed": str(args.feed)}
        )
    )
