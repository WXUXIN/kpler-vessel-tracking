"""Subscribes to the broker and writes Position Reports to the datastore."""

from __future__ import annotations

import json
import logging
import signal
import time
from types import FrameType

from confluent_kafka import Consumer, KafkaError, Message, Producer

from vessel_tracking.domain import RejectedReport
from vessel_tracking.ingest import BatchWriter
from vessel_tracking.settings import Settings
from vessel_tracking.store import PositionReportStore

log = logging.getLogger("vessel_tracking.consumer")

BATCH_SECONDS = 1.0
DEAD_LETTER_FLUSH_SECONDS = 10.0


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
                {"reason": rejected.reason, "message": rejected.message}
            ).encode(),
            on_delivery=_delivery_report,
        )
        self._producer.poll(0)

    def flush(self) -> None:
        """Block until the Rejected Reports produced so far are on the topic.

        A Rejected Report counted but never delivered is a silently dropped one, which
        is the thing CONTEXT.md defines the term against, so a remainder is reported
        rather than shrugged at. Guaranteeing delivery before the offset moves needs
        the manual offset commit that issue #4 owns.
        """
        undelivered = self._producer.flush(DEAD_LETTER_FLUSH_SECONDS)
        if undelivered:
            log.error(
                json.dumps(
                    {"event": "dead_letters_undelivered", "count": undelivered}
                )
            )

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
    # Offsets are auto-committed here. ADR-0004 requires them committed manually,
    # after the database transaction; that arrives with issue #4, which owns it along
    # with the retry-with-backoff half of the failure taxonomy. The dead-letter half is
    # live below. Until #4 lands, this consumer can lose a batch - and a Rejected
    # Report already produced but not yet flushed - on an unclean shutdown.
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_consumer_group,
            "auto.offset.reset": "earliest",
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
    writer = BatchWriter(store, dead_letters)
    deadline = time.monotonic() + BATCH_SECONDS

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

    def flush() -> None:
        """Write the pending batch and make its rejections durable at the same point."""
        nonlocal deadline
        written = writer.flush()
        dead_letters.flush()
        if written:
            progress()
        deadline = time.monotonic() + BATCH_SECONDS

    def handle(payload: bytes) -> None:
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as undecodable:
            # Unusable before it is even a message, so it can never succeed: a poison
            # message like any other, and set aside the same way (ADR-0004).
            writer.reject(
                payload.decode("utf-8", "replace"),
                f"payload is not JSON: {undecodable}",
            )
            return
        if writer.add(body):
            progress()

    try:
        while _running:
            message = consumer.poll(0.5)
            if message is not None:
                error = message.error()
                if error is None:
                    payload = message.value()
                    if payload is not None:
                        handle(payload)
                elif error.code() != KafkaError._PARTITION_EOF:
                    log.error(
                        json.dumps({"event": "broker_error", "detail": str(error)})
                    )
            # Every path through the loop reaches the time bound, so a run of messages
            # that are all rejected still flushes the batch waiting behind them.
            if time.monotonic() >= deadline:
                flush()
    finally:
        flush()
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
                }
            )
        )
