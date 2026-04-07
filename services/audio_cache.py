import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from config.logger import get_logger
from services.audio_processor import AudioProcessResult

logger = get_logger(__name__)


class CacheProtocol(Protocol):
    async def get_key(self, key: str) -> dict | None: ...
    async def put_key(self, key: str, value: dict[str, Any], ttl: int | None = None) -> bool: ...


@dataclass
class TTLConfig:
    cache: int = 30 * 24 * 3600  # 30 days
    processing: int = 3600  # 1 hour
    cleanup: int = 60  # 1 minute


@dataclass
class AudioResult:
    track_id: str
    stems_urls: dict[str, str]
    created_at: float
    processing_time: float

    def get_storage_paths(self) -> list[str]:
        """Extract bucket keys from public stem URLs."""
        storage_paths: list[str] = []
        for url in self.stems_urls.values():
            parsed = urlparse(url)
            storage_path = parsed.path.lstrip("/")
            if storage_path:
                storage_paths.append(storage_path)
        return storage_paths


@dataclass
class CacheEntry:
    status: str
    youtube_url: str
    created_at: float
    last_accessed: float
    processing_time: float
    result: AudioResult


class AudioCache:
    def __init__(
        self,
        cache_client: CacheProtocol,
        storage_client,
        ttl_config: TTLConfig | None = None,
    ):
        self.cache = cache_client
        self.storage = storage_client
        ttl = ttl_config or TTLConfig()
        self.cache_ttl = ttl.cache
        self.processing_ttl = ttl.processing
        self.cache_cleanup_ttl = ttl.cleanup

    def _extract_video_id(self, youtube_url: str) -> str:
        """Extract video ID from various YouTube URL formats."""
        # Match youtube.com/watch?v=VIDEO_ID
        match = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]+)", youtube_url)
        if match:
            return match.group(1)
        # Fallback: use the full URL
        return youtube_url.lower().strip()

    def _generate_cache_key(self, youtube_url: str) -> str:
        video_id = self._extract_video_id(youtube_url)
        hash_digest = hashlib.sha256(video_id.encode("utf-8")).hexdigest()
        return hash_digest[:32]

    def _parse_cache_response(self, raw_data: Any) -> dict[str, Any] | None:
        if not raw_data:
            return None

        try:
            # Unwrap "value" key if present
            data = raw_data.get("value", raw_data) if isinstance(raw_data, dict) else raw_data
            # Parse JSON string if needed
            return json.loads(data) if isinstance(data, str) else data
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            logger.warning(f"Failed to parse cache response: {e}")
            return None

    def _extract_stems_urls(self, result_data: dict[str, Any]) -> dict[str, str]:
        stems_urls = result_data.get("stems_urls")
        if isinstance(stems_urls, dict):
            return {
                stem_name: stem_url
                for stem_name, stem_url in stems_urls.items()
                if isinstance(stem_name, str) and isinstance(stem_url, str)
            }
        return {}

    async def _get_cache_entry(self, cache_key: str) -> CacheEntry | None:
        try:
            raw_data = await self.cache.get_key(f"audio:{cache_key}")
            if not raw_data:
                return None

            data = self._parse_cache_response(raw_data)
            if not data:
                logger.error(f"Failed to parse cache data for {cache_key}")
                return None

            if "status" not in data:
                logger.error(f"Cache entry missing 'status' key for {cache_key}: {data}")
                return None

            if data["status"] == "deleted":
                logger.debug(f"Cache entry marked as deleted for {cache_key}")
                return None

            if data["status"] == "processing":
                result = AudioResult(
                    processing_time=0.0,
                    stems_urls={},
                    track_id=data.get("track_id", ""),
                    created_at=data.get("created_at", time.time()),
                )

                return CacheEntry(
                    result=result,
                    processing_time=0.0,
                    status=data["status"],
                    youtube_url=data.get("youtube_url", data.get("search_query", "")),
                    created_at=data.get("created_at", time.time()),
                    last_accessed=data.get("last_accessed", data.get("created_at", time.time())),
                )

            if data["status"] == "completed" and "result" in data:
                result_data = data["result"]
                stems_urls = self._extract_stems_urls(result_data)
                if not stems_urls:
                    logger.warning(f"Completed cache entry missing stems for {cache_key}")
                    return None

                result = AudioResult(
                    created_at=result_data["created_at"],
                    processing_time=result_data["processing_time"],
                    stems_urls=stems_urls,
                    track_id=data.get("track_id", result_data.get("track_id", "")),
                )

                return CacheEntry(
                    result=result,
                    status=data["status"],
                    youtube_url=data.get("youtube_url", data.get("search_query", "")),
                    created_at=data.get("created_at", time.time()),
                    last_accessed=data.get("last_accessed", data.get("created_at", time.time())),
                    processing_time=data.get("processing_time", result_data["processing_time"]),
                )

            logger.warning(f"Invalid cache entry structure for {cache_key}: {data}")
            return None

        except RuntimeError as e:
            logger.warning(f"Failed to get cache entry for {cache_key}: {e}")
            return None
        except KeyError as e:
            logger.error(f"KeyError in cache entry {cache_key}: missing key {e}")
            return None

    async def _update_access_time(self, cache_key: str) -> None:
        try:
            raw_data = await self.cache.get_key(f"audio:{cache_key}")
            if not raw_data:
                return

            data = self._parse_cache_response(raw_data)
            if not data:
                logger.error(f"Failed to parse cache data for access time update: {cache_key}")
                return

            data["last_accessed"] = time.time()

            await self.cache.put_key(f"audio:{cache_key}", data, ttl=self.cache_ttl)

        except RuntimeError as e:
            logger.warning(f"Failed to update access time for {cache_key}: {e}")

    async def _files_exist(self, result: AudioResult) -> bool:
        try:
            stem_paths = result.get_storage_paths()
            if not stem_paths:
                return False
            file_checks = await asyncio.gather(
                *(self.storage.file_exists_async(stem_path) for stem_path in stem_paths)
            )
            return all(file_checks)
        except RuntimeError as e:
            logger.warning(f"Error checking file existence: {e}")
            return False

    async def _cache_result(self, cache_key: str, youtube_url: str, result: AudioResult) -> None:
        try:
            cache_data = {
                "status": "completed",
                "youtube_url": youtube_url,
                "created_at": result.created_at,
                "last_accessed": time.time(),
                "processing_time": result.processing_time,
                "result": {
                    "track_id": result.track_id,
                    "stems_urls": result.stems_urls,
                    "processing_time": result.processing_time,
                    "created_at": result.created_at,
                },
            }

            await self.cache.put_key(f"audio:{cache_key}", cache_data, ttl=self.cache_ttl)
            logger.info(f"Cached audio result for URL: {youtube_url[:50]}")
        except RuntimeError as e:
            logger.error(f"Failed to cache result for {cache_key}: {e}")

    async def _mark_processing(self, cache_key: str, youtube_url: str, track_id: str) -> None:
        try:
            processing_data = {
                "status": "processing",
                "youtube_url": youtube_url,
                "track_id": track_id,
                "created_at": time.time(),
                "last_accessed": time.time(),
            }

            await self.cache.put_key(f"audio:{cache_key}", processing_data, ttl=self.processing_ttl)
            logger.debug(f"Marked processing for cache key: {cache_key}")

        except RuntimeError as e:
            logger.error(f"Failed to mark processing for {cache_key}: {e}")

    async def get_cached_or_processing(self, youtube_url: str) -> dict[str, Any] | None:
        cache_key = self._generate_cache_key(youtube_url)
        logger.debug(f"Cache lookup for URL: {youtube_url[:50]} -> key: {cache_key}")

        cached = await self._get_cache_entry(cache_key)
        if not cached:
            logger.debug(f"No cache entry found for: {youtube_url[:50]}")
            return None

        if cached.status == "completed":
            if await self._files_exist(cached.result):
                await self._update_access_time(cache_key)
                logger.info(f"Cache hit for URL: {youtube_url[:50]}")

                return {
                    "status": "completed",
                    "track_id": cached.result.track_id,
                    "result": {
                        "stems_urls": cached.result.stems_urls,
                        "track_id": cached.result.track_id,
                    },
                    "processing_time": cached.result.processing_time,
                    "created_at": cached.result.created_at,
                    "cached": True,
                }

            logger.warning(f"Cache entry exists but files missing for: {youtube_url[:50]}")
            await self._remove_cache_entry(cache_key)
            return None

        if cached.status == "processing":
            if time.time() - cached.created_at < self.processing_ttl:
                logger.info(f"Request already processing for: {youtube_url[:50]}")
                return {
                    "status": "processing",
                    "track_id": cached.result.track_id if cached.result.track_id else "unknown",
                    "message": "Audio separation already in progress",
                }
            logger.info(f"Processing entry expired for: {youtube_url[:50]}")
            await self._remove_cache_entry(cache_key)
            return None

        logger.warning(f"Unknown cache status '{cached.status}' for: {youtube_url[:50]}")
        return None

    async def _remove_cache_entry(self, cache_key: str) -> None:
        try:
            await self.cache.put_key(
                f"audio:{cache_key}", {"status": "deleted"}, ttl=self.cache_cleanup_ttl
            )
        except RuntimeError as e:
            logger.warning(f"Failed to remove cache entry {cache_key}: {e}")

    async def mark_processing_start(self, youtube_url: str, track_id: str) -> str:
        cache_key = self._generate_cache_key(youtube_url)
        await self._mark_processing(cache_key, youtube_url, track_id)
        return cache_key

    async def cache_completed_result(
        self, cache_key: str, youtube_url: str, result: AudioProcessResult
    ) -> None:
        """Cache the completed processing result"""
        try:
            audio_result = AudioResult(
                track_id=result["track_id"],
                stems_urls=result.get("stems_urls", {}),
                processing_time=result.get("processing_time", 0.0),
                created_at=result.get("created_at", time.time()),
            )

            await self._cache_result(cache_key, youtube_url, audio_result)
            logger.info(f"Cached completed result for: {youtube_url[:50]}")

        except KeyError as e:
            logger.error(f"Missing required key in result data: {e}")
