"""Subscribes to the broker and writes Position Reports to the datastore."""

from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime
from decimal import Decimal
from types import FrameType
from typing import Any

from confluent_kafka import Consumer, KafkaError, Message, Producer

from vessel_tracking.domain import RejectedReport
from vessel_tracking.ingest import BatchWriter
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore

log = logging.getLogger("vessel_tracking.consumer")

POLL_SECONDS = 0.5
DEAD_LETTER_FLUSH_SECONDS = 10.0


def _json_default(value: Any) -> str | float:
    """Values JSON cannot carry directly.

    A decoded Position Report reaches this topic when the datastore refuses one, and
    it carries a timestamp and a Decimal that the raw feed messages never do.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _delivery_report(error: KafkaError | None, message: Message) -> None:
    """Publishing a Rejected Report can fail; failing quietly would not be honest."""
    if error is not None:
        log.error(json.dumps({"event": "dead_letter_failed", "detail": str(error)}))


class KafkaDeadLetters:
    """Publishes Rejected Reports to the dead-letter topic.

    The value is self-describing - the message as it arrived, and why it was rejected -
    so the topic can be read with any consumer without a schema to hand. Nothing is
    keyed: a malformed MMSI is one of the things that lands here, so it is no basis for
    a partition.
    """

    def __init__(self, producer: Producer, topic: str) -> None:
        self._producer = producer
        self._topic = topic

    def send(self, rejected: RejectedReport) -> None:
        self._producer.produce(
            self._topic,
            value=json.dumps(
                {"reason": rejected.reason, "message": rejected.message},
                default=_json_default,
            ).encode(),
            on_delivery=_delivery_report,
        )
        self._producer.poll(0)

    def flush(self) -> int:
        """Block until the Rejected Reports produced so far are on the topic.

        Returns how many are still undelivered, because the caller must not move
        offsets over a remainder: a Rejected Report counted but never delivered is a
        silently dropped one, which is the thing CONTEXT.md defines the term against.
        """
        undelivered: int = self._producer.flush(DEAD_LETTER_FLUSH_SECONDS)
        if undelivered:
            log.error(
                json.dumps(
                    {"event": "dead_letters_undelivered", "count": undelivered}
                )
            )
        return undelivered


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
            # Auto-commit moves offsets on a timer, regardless of what reached the
            # datastore, which is exactly the guarantee ADR-0004 rules out. Offsets
            # move here only after the transaction that wrote the batch.
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([settings.kafka_topic])

    dead_letters = KafkaDeadLetters(
        Producer(
            {
                "bootstrap.servers": settings.kafka_bootstrap_servers,
                "enable.idempotence": True,
                "acks": "all",
            }
        ),
        settings.kafka_dead_letter_topic,
    )
    # Waiting out a datastore is right while running and wrong while shutting down:
    # on the way out the batch is left for the next run, which still has it on the topic.
    writer = BatchWriter(
        store,
        dead_letters,
        settings.ingest_batch_size,
        stopping=lambda: not _running,
    )

    uncommitted = False
    reported = (0, 0)
    deadline = time.monotonic() + settings.ingest_batch_seconds
    last_record = time.monotonic()
    stopped_by = "signal"

    def progress() -> None:
        log.info(
            json.dumps(
                {
                    "event": "ingest_progress",
                    "written": writer.written,
                    "rejected": writer.rejected,
                }
            )
        )

    def checkpoint() -> None:
        """Write what is pending, make it durable, and only then move the offsets.

        This order is the whole of ADR-0004. A crash before the commit replays messages
        that the Report ID primary key then discards; a crash after it would lose them.
        The batch write can stall here indefinitely waiting for the datastore, which is
        deliberate - the unwritten messages are still on the topic.

        Offsets also stay put while a Rejected Report is undelivered, so the record that
        produced it comes back rather than vanishing along with it.
        """
        nonlocal uncommitted, deadline, reported
        writer.flush()
        undelivered = dead_letters.flush()
        if uncommitted and not undelivered:
            consumer.commit(asynchronous=False)
            uncommitted = False
        # Reported against the totals rather than against this flush: a batch that
        # filled on the size bound was already written from inside add().
        totals = (writer.written, writer.rejected)
        if totals != reported:
            progress()
            reported = totals
        deadline = time.monotonic() + settings.ingest_batch_seconds

    def handle(payload: bytes) -> bool:
        """Decode and accumulate one record. Returns whether a batch reached the store."""
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as undecodable:
            # Unusable before it is even a message, so it can never succeed: a poison
            # message like any other, and set aside the same way (ADR-0004).
            writer.reject(
                payload.decode("utf-8", "replace"),
                f"payload is not JSON: {undecodable}",
            )
            return False
        return bool(writer.add(body)) # for each message, add to the batch and return True if the batch was flushed to the store

    try:
        while _running:
            message = consumer.poll(POLL_SECONDS)
            wrote = False
            if message is not None:
                error = message.error()
                if error is None:
                    last_record = time.monotonic()
                    uncommitted = True
                    payload = message.value()
                    if payload is not None:
                        wrote = handle(payload)
                elif error.code() != KafkaError._PARTITION_EOF:
                    log.error(
                        json.dumps({"event": "broker_error", "detail": str(error)})
                    )
            now = time.monotonic()
            # The size bound fires the moment a batch fills; the time bound catches
            # everything else, including a run of records that were all rejected.
            if wrote or now >= deadline:
                checkpoint()
            if (
                settings.idle_timeout_seconds
                and now - last_record >= settings.idle_timeout_seconds
            ):
                stopped_by = "idle"
                break
    finally:
        try:
            checkpoint()
        except Exception as incomplete:
            # Shutdown reports what it could not finish and then finishes anyway.
            # Anything unwritten kept its place on the topic, because the offsets that
            # would have passed over it were never committed.
            log.error(
                json.dumps(
                    {"event": "shutdown_incomplete", "detail": str(incomplete)}
                )
            )
        consumer.close()
        store.close()
        # The run states both counts plainly: over the supplied feed this is 2,696
        # written and 0 rejected, because the feed contains no invalid records.
        log.info(
            json.dumps(
                {
                    "event": "ingest_summary",
                    "written": writer.written,
                    "rejected": writer.rejected,
                    "stopped_by": stopped_by,
                }
            )
        )
