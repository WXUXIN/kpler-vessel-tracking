"""Rate limiting, held outside the API process.

A fixed window counted in Redis rather than in memory, so the limit holds however many
API instances are running: two processes each allowing ten requests a minute between
them allow twenty, which is not the limit anyone asked for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

DEFAULT_ALLOWANCE = 10
DEFAULT_WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Decision:
    """What the limiter made of one request, and what to tell the caller about it."""

    allowed: bool
    allowance: int
    remaining: int
    retry_after_seconds: int


class Limits:
    """A fixed window per client, counted in Redis.

    Fixed rather than sliding: a caller can spend a whole allowance at the end of one
    window and another at the start of the next, which is the known cost of the simpler
    scheme and is bounded at twice the rate for one instant.
    """

    def __init__(
        self,
        url: str,
        allowance: int = DEFAULT_ALLOWANCE,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        namespace: str = "ratelimit",
    ) -> None:
        self._redis: Redis = Redis.from_url(url)
        self._allowance = allowance
        self._window = window_seconds
        self._namespace = namespace

    async def check(self, client: str) -> Decision:
        """Count one request against a client's allowance for the window it lands in.

        The increment and the expiry go together in one round trip, so a counter cannot
        be left immortal by a crash between them.
        """
        now = int(time.time())
        window = now // self._window
        key = f"{self._namespace}:{client}:{window}"

        pipeline = self._redis.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, self._window)
        used, _ = await pipeline.execute()

        return Decision(
            allowed=used <= self._allowance,
            allowance=self._allowance,
            remaining=max(0, self._allowance - used),
            retry_after_seconds=self._window - (now % self._window),
        )

    async def close(self) -> None:
        await self._redis.aclose()
