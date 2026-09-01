"""Subscribes to the broker and writes Position Reports to the datastore."""

from __future__ import annotations

import json
import logging
import signal
import time
from types import FrameType

from confluent_kafka import Consumer, KafkaError

from vessel_tracking.domain import PositionReport, from_message
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore

log = logging.getLogger("vessel_tracking.consumer")

BATCH_SIZE = 500
BATCH_SECONDS = 1.0

_running = True


def _stop(signum: int, frame: FrameType | None) -> None:
    global _running
    _running = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    settings = Settings()
    store = PositionReportStore(settings.database_url)
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_consumer_group,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([settings.kafka_topic])

    written = 0
    batch: list[PositionReport] = []
    deadline = time.monotonic() + BATCH_SECONDS

    def flush() -> None:
        nonlocal written, batch, deadline
        if batch:
            written += store.insert_many(batch)
            batch = []
            log.info(json.dumps({"event": "ingest_progress", "written": written}))
        deadline = time.monotonic() + BATCH_SECONDS

    try:
        while _running:
            message = consumer.poll(0.5)
            if message is None:
                if time.monotonic() >= deadline:
                    flush()
                continue
            error = message.error()
            if error is not None:
                if error.code() != KafkaError._PARTITION_EOF:
                    log.error(
                        json.dumps({"event": "broker_error", "detail": str(error)})
                    )
                continue
            payload = message.value()
            if payload is None:
                continue
            batch.append(from_message(json.loads(payload)))
            if len(batch) >= BATCH_SIZE or time.monotonic() >= deadline:
                flush()
    finally:
        flush()
        consumer.close()
        store.close()
        log.info(json.dumps({"event": "ingest_summary", "written": written}))
