import json
import time
from typing import Any, Dict, Optional

import redis.asyncio as redis

from config.logger import get_logger

logger = get_logger(__name__)


class RedisCache:
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

    async def get_key(self, key: str) -> Optional[dict]:
        try:
            client = await self._ensure_connection()
            value = await client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except (redis.RedisError, json.JSONDecodeError, ConnectionError) as e:
            logger.warning(f"Redis get failed for key {key}: {e}")
            return None

    async def put_key(self, key: str, value: Dict[str, Any], ttl: Optional[int] = None) -> bool:
        try:
            client = await self._ensure_connection()
            json_value = json.dumps(value)
            if ttl:
                await client.setex(key, ttl, json_value)
            else:
                await client.set(key, json_value)
            return True
        except (redis.RedisError, ConnectionError) as e:
            logger.warning(f"Redis put failed for key {key}: {e}")
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching the given pattern. Returns count of deleted keys."""
        try:
            client = await self._ensure_connection()
            deleted_count = 0
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor, match=pattern, count=100)
                if keys:
                    deleted_count += await client.delete(*keys)
                if cursor == 0:
                    break
            logger.info("Deleted %d keys matching pattern: %s", deleted_count, pattern)
            return deleted_count
        except (redis.RedisError, ConnectionError) as e:
            logger.warning("Redis delete pattern failed for %s: %s", pattern, e)
            return 0

    async def count_keys(self, pattern: str) -> int:
        """Count keys matching the given pattern."""
        try:
            client = await self._ensure_connection()
            count = 0
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor, match=pattern, count=100)
                count += len(keys)
                if cursor == 0:
                    break
            return count
        except (redis.RedisError, ConnectionError) as e:
            logger.warning("Redis count keys failed for %s: %s", pattern, e)
            return 0
