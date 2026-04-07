import json
import time

import redis.asyncio as redis

from config.logger import get_logger
from models.rate_limit import RateLimitInfo

logger = get_logger(__name__)


def parse_rate_limit(rate_limit: str) -> tuple[int, int]:
    """
    Parse rate limit string into requests and window seconds.

    Args:
        rate_limit: Rate limit string (e.g., "100 per hour", "5 per minute")

    Returns:
        tuple: (limit_requests, window_seconds)

    Raises:
        ValueError: If rate limit format is invalid
    """
    parts = rate_limit.lower().split()
    if len(parts) != 3 or parts[1] != "per":
        raise ValueError(f"Invalid rate limit format: {rate_limit}")

    limit_requests = int(parts[0])
    time_unit = parts[2]

    window_seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}.get(
        time_unit.rstrip("s"), 60
    )

    return limit_requests, window_seconds


class RateLimiter:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.client = None

    async def _ensure_connection(self) -> redis.Redis:
        if self.client is None:
            self.client = redis.from_url(
                self.redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5
            )
        return self.client

    async def close(self) -> None:
        if self.client:
            await self.client.close()
            self.client = None

    async def is_allowed(
        self, identifier: str, limit_requests: int, window_seconds: int
    ) -> tuple[bool, RateLimitInfo]:
        current_time = int(time.time())
        key = f"rate_limit:{identifier}:{current_time // window_seconds}"

        try:
            client = await self._ensure_connection()
            value = await client.get(key)

            if value is None:
                data = {"count": 0, "reset_time": current_time + window_seconds}
            else:
                data = json.loads(value)
        except (redis.RedisError, json.JSONDecodeError, ConnectionError) as e:
            logger.warning(f"Redis get failed for rate limit key {key}: {e}")
            # Fail open - allow request if Redis is down
            return True, RateLimitInfo(
                limit=limit_requests,
                remaining=limit_requests - 1,
                reset_time=current_time + window_seconds,
                retry_after=0,
            )

        count = data.get("count", 0)
        reset_time = data.get("reset_time", current_time + window_seconds)

        if count >= limit_requests:
            return False, RateLimitInfo(
                limit=limit_requests,
                remaining=0,
                reset_time=reset_time,
                retry_after=reset_time - current_time,
            )

        new_count = count + 1
        try:
            client = await self._ensure_connection()
            json_value = json.dumps({"count": new_count, "reset_time": reset_time})
            await client.setex(key, window_seconds + 60, json_value)  # Extra buffer time
        except (redis.RedisError, ConnectionError) as e:
            logger.warning(f"Redis put failed for rate limit key {key}: {e}")

        return True, RateLimitInfo(
            limit=limit_requests,
            remaining=max(0, limit_requests - new_count),
            reset_time=reset_time,
            retry_after=0,
        )
