"""Publishes the AIS feed to the broker, one Position Report per message.

Messages are published in feed order - which is Report ID order, the receipt sequence -
and keyed by MMSI so that one Vessel's reports stay ordered within a partition.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from confluent_kafka import Producer

from vessel_tracking.domain import malformed_messages
from vessel_tracking.feed import read_feed
from vessel_tracking.settings import Settings

log = logging.getLogger("vessel_tracking.producer")

# Injected malformed messages are spread through the feed rather than appended, so a
# run demonstrates valid Position Reports still being written either side of a rejection.
INJECT_EVERY = 100


def messages_to_publish(
    feed: Path, inject_invalid: int = 0
) -> Iterator[Mapping[str, Any]]:
    """Everything this producer puts on the topic, in the order it publishes.

    The feed in file order - which is Report ID order - with `inject_invalid` malformed
    messages spread through it. The feed itself contains no invalid records, so the
    rejection path is only demonstrable against messages made for the purpose.
    """
    malformed = malformed_messages(inject_invalid)
    for position, message in enumerate(read_feed(feed), start=1):
        yield message
        if position % INJECT_EVERY == 0:
            injected = next(malformed, None)
            if injected is not None:
                yield injected
    # More injections asked for than the feed had room to space out.
    yield from malformed


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
    parser.add_argument(
        "--inject-invalid",
        type=int,
        default=0,
        help="publish this many malformed messages alongside the feed",
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
    for message in messages_to_publish(args.feed, args.inject_invalid):
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
    # Feed records and injected junk are counted apart, so a run that demonstrates the
    # rejection path still states plainly how much of the feed it published.
    log.info(
        json.dumps(
            {
                "event": "feed_published",
                "published": published - args.inject_invalid,
                "injected": args.inject_invalid,
                "feed": str(args.feed),
            }
        )
    )
