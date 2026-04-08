from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import redis

from config.logger import get_logger
from models.progress import ProgressEvent, ProgressStage, STAGE_MESSAGES, STAGE_PROGRESS

logger = get_logger(__name__)

STATE_KEY_PREFIX = "progress_state"
CHANNEL_PREFIX = "progress"
DEFAULT_STATE_TTL = 3600  # 1 hour


class ProgressPublisher:
    """Sync Redis client for publishing progress events from workers."""

    def __init__(self, redis_url: str, state_ttl: int = DEFAULT_STATE_TTL):
        self.redis_url = redis_url
        self.state_ttl = state_ttl
        self._connection: redis.Redis | None = None

    def _get_connection(self) -> redis.Redis:
        if self._connection is None:
            parsed_url = urlparse(self.redis_url)

            connection_kwargs: dict[str, Any] = {
                "socket_timeout": 10,
                "decode_responses": True,
                "socket_connect_timeout": 10,
            }

            if parsed_url.scheme == "rediss":
                connection_kwargs.update(
                    {
                        "ssl_cert_reqs": None,
                        "ssl_check_hostname": False,
                    }
                )

            self._connection = redis.from_url(self.redis_url, **connection_kwargs)

        return self._connection

    def _state_key(self, track_id: str) -> str:
        return f"{STATE_KEY_PREFIX}:{track_id}"

    def _channel(self, track_id: str) -> str:
        return f"{CHANNEL_PREFIX}:{track_id}"

    def publish_progress(
        self,
        track_id: str,
        stage: ProgressStage,
        message: str | None = None,
        progress: int | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Publish a progress event to Redis pub/sub and store state."""
        event = ProgressEvent.create(
            track_id=track_id,
            stage=stage,
            message=message,
            progress=progress,
            error=error,
            result=result,
        )

        event_json = json.dumps(event.to_dict())

        try:
            conn = self._get_connection()

            # Store current state for late-joining clients
            conn.setex(self._state_key(track_id), self.state_ttl, event_json)

            # Publish to channel for real-time subscribers
            conn.publish(self._channel(track_id), event_json)

            logger.debug(
                "Published progress event",
                track_id=track_id,
                stage=stage.value,
                progress=event.progress,
            )
            return True

        except redis.RedisError as e:
            logger.error(
                "Failed to publish progress event",
                track_id=track_id,
                stage=stage.value,
                error=str(e),
            )
            return False

    def get_current_state(self, track_id: str) -> dict[str, Any] | None:
        """Get the current progress state for a track."""
        try:
            conn = self._get_connection()
            value = conn.get(self._state_key(track_id))
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

    def cleanup_state(self, track_id: str, delay_seconds: int = 300) -> None:
        """Set expiry on progress state after completion."""
        try:
            conn = self._get_connection()
            conn.expire(self._state_key(track_id), delay_seconds)
        except redis.RedisError as e:
            logger.warning(
                "Failed to set state expiry",
                track_id=track_id,
                error=str(e),
            )

    def close(self) -> None:
        if self._connection:
            try:
                self._connection.close()
            except redis.RedisError:
                pass
            self._connection = None
