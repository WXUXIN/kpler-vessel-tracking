"""Configuration, read from the environment through one object."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VT_", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql://vessel:vessel@localhost:5433/vessel_tracking"
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic: str = "ais.position-reports"
    kafka_dead_letter_topic: str = "ais.position-reports.dead-letter"
    kafka_consumer_group: str = "vessel-tracking"
    feed_path: Path = Path("ship_positions.json")
    producer_rate: float = 200.0

    # A batch is written when it reaches the size bound or the time bound, whichever
    # comes first, so a slow feed still makes visible progress.
    ingest_batch_size: int = 500
    ingest_batch_seconds: float = 1.0

    # Seconds without a record before the consumer stops. Zero runs forever, which is
    # what a service wants; a demonstration run or an end-to-end test sets a value so
    # that it terminates deterministically.
    idle_timeout_seconds: float = 0.0

    redis_url: str = "redis://localhost:6379/0"

    # Requests allowed per client per window. Ten a minute is deliberately low enough
    # that the limit can be demonstrated by hand rather than only under load.
    rate_limit_allowance: int = 10
    rate_limit_window_seconds: int = 60
