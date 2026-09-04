"""Configuration, read from the environment through one object."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VT_", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql://vessel:vessel@localhost:5432/vessel_tracking"
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic: str = "ais.position-reports"
    kafka_dead_letter_topic: str = "ais.position-reports.dead-letter"
    kafka_consumer_group: str = "vessel-tracking"
    feed_path: Path = Path("ship_positions.json")
    producer_rate: float = 200.0
