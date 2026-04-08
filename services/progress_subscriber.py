from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

import redis.asyncio as redis
from starlette.requests import Request

from config.logger import get_logger
from models.progress import ProgressEvent, ProgressStage

logger = get_logger(__name__)

STATE_KEY_PREFIX = "progress_state"
CHANNEL_PREFIX = "progress"
HEARTBEAT_INTERVAL = 15  # seconds


class ProgressSubscriber:
    """Async Redis client for subscribing to progress events from API."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: redis.Redis | None = None

    async def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        return self._client

    def _state_key(self, track_id: str) -> str:
        return f"{STATE_KEY_PREFIX}:{track_id}"

    def _channel(self, track_id: str) -> str:
        return f"{CHANNEL_PREFIX}:{track_id}"

    async def get_current_state(self, track_id: str) -> dict[str, Any] | None:
        """Get the current progress state for a track."""
        try:
            client = await self._get_client()
            value = await client.get(self._state_key(track_id))
            if value is None:
                return None
            return json.loads(value)
        except (redis.RedisError, json.JSONDecodeError) as e:
            logger.warning(
                "Failed to get progress state",
                track_id=track_id,
                error=str(e),
            )
            return None

    async def stream_progress(
        self, track_id: str, request: Request
    ) -> AsyncGenerator[str, None]:
        """Generate SSE-formatted progress events for a track."""
        pubsub: redis.client.PubSub | None = None

        try:
            # First, send current state if available (for late joiners)
            current_state = await self.get_current_state(track_id)

            if current_state:
                event = ProgressEvent.from_dict(current_state)
                yield event.to_sse()

                # If already completed or failed, we're done
                if current_state["stage"] in (
                    ProgressStage.COMPLETED.value,
                    ProgressStage.FAILED.value,
                ):
                    return

            # Subscribe to the progress channel
            client = await self._get_client()
            pubsub = client.pubsub()
            await pubsub.subscribe(self._channel(track_id))

            # Listen for messages with heartbeat
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    logger.debug("Client disconnected", track_id=track_id)
                    break

                try:
                    # Wait for message with timeout for heartbeat
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                        timeout=HEARTBEAT_INTERVAL,
                    )

                    if message is not None and message["type"] == "message":
                        data = json.loads(message["data"])
                        event = ProgressEvent.from_dict(data)
                        yield event.to_sse()

                        # Close connection on terminal states
                        if data["stage"] in (
                            ProgressStage.COMPLETED.value,
                            ProgressStage.FAILED.value,
                        ):
                            return

                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield ": heartbeat\n\n"

        except redis.RedisError as e:
            logger.error(
                "Redis subscription error",
                track_id=track_id,
                error=str(e),
            )
            # Send error event before closing
            error_event = ProgressEvent.create(
                track_id=track_id,
                stage=ProgressStage.FAILED,
                message="Connection error",
                error="Lost connection to progress stream",
            )
            yield error_event.to_sse()

        finally:
            if pubsub:
                try:
                    await pubsub.unsubscribe(self._channel(track_id))
                    await pubsub.close()
                except redis.RedisError:
                    pass

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
