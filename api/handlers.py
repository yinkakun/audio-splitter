import time
import uuid as uuid_module
from typing import Dict

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from config.logger import get_logger
from models.job import JobStatus
from models.request import (
    AudioCacheConfig,
    DirectoryConfig,
    ProcessingJobRequest,
    RedisConfig,
    StorageConfig,
)
from services.audio_cache import AudioCache
from services.dependencies import (
    CacheManagerDep,
    ProgressSubscriberDep,
    QueueManagerDep,
    RateLimiterDep,
)
from services.progress_subscriber import ProgressSubscriber
from utils.rate_limiter import RateLimiter, parse_rate_limit
from utils.job_access_token import JobAccessTokenManager
from workers.job_queue import JobQueue

logger = get_logger(__name__)


def sanitize_input(input_str: str, max_length: int) -> str:
    if not isinstance(input_str, str):
        raise ValueError("Input must be a string")

    cleaned = input_str.strip()
    if not cleaned:
        raise ValueError("Input cannot be empty")

    if len(cleaned) > max_length:
        raise ValueError(f"Input too long (max {max_length} characters)")

    return cleaned


def validate_track_id(track_id: str) -> bool:
    try:
        uuid_module.UUID(track_id)
        return True
    except (ValueError, TypeError):
        return False


class SeparationRequest(BaseModel):
    audio_url: str = Field(..., min_length=1, max_length=500)
    request_id: str = Field(..., min_length=1, max_length=100)

    @field_validator("audio_url")
    @classmethod
    def validate_audio_url(cls, v):
        sanitized = sanitize_input(v, 500)
        # Basic URL validation - yt-dlp will handle the actual compatibility check
        if not sanitized.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return sanitized

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, v):
        return sanitize_input(v, 100)


class HealthResponse(BaseModel):
    status: str
    timestamp: float
    services: Dict[str, bool]


class CacheStatsResponse(BaseModel):
    total_entries: int


class CacheClearResponse(BaseModel):
    deleted_count: int
    message: str


def register_error_handlers(app: FastAPI):
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": str(exc)},
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(_: Request, exc: Exception):
        logger.error("Unexpected error: %s", str(exc), exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error"},
        )


def register_routes(app: FastAPI, config, storage):
    job_access_token_manager = JobAccessTokenManager(
        config.effective_job_access_token_secret,
        config.job_access_token_ttl_seconds,
    )

    async def _verify_api_key(request: Request):
        authorization = request.headers.get("Authorization")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = authorization.removeprefix("Bearer ")
        if not token or token != config.api_secret_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def _build_job_access_payload(track_id: str) -> dict[str, str | int]:
        access_token, expires_at = job_access_token_manager.create_token(track_id)
        return {
            "access_token": access_token,
            "access_token_expires_at": expires_at,
        }

    async def _verify_job_access(request: Request, track_id: str) -> None:
        token = request.query_params.get("access_token")
        if not token:
            authorization = request.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                token = authorization.removeprefix("Bearer ")

        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing job access token",
            )

        try:
            job_access_token_manager.verify_token(token, track_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc

    async def _apply_rate_limit(
        request: Request, rate_limit: str, rate_limiter: RateLimiter | None = None
    ):
        if not rate_limiter:
            return

        try:
            limit_requests, window_seconds = parse_rate_limit(rate_limit)
        except ValueError as e:
            logger.warning(str(e))
            return

        client_ip = request.client.host if request.client else "unknown"

        allowed, info = await rate_limiter.is_allowed(client_ip, limit_requests, window_seconds)

        if not allowed:
            logger.warning("Rate limit exceeded for %s: %s", client_ip, rate_limit)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "Rate limit exceeded",
                    "limit": info.limit,
                    "remaining": info.remaining,
                    "reset_time": info.reset_time,
                    "retry_after": info.retry_after,
                },
                headers={"Retry-After": str(info.retry_after)},
            )

    @app.post("/separate-audio", status_code=status.HTTP_202_ACCEPTED)
    async def separate_audio(
        request: Request,
        separation_request: SeparationRequest,
        queue_manager: JobQueue | None = QueueManagerDep,
        cache_manager: AudioCache | None = CacheManagerDep,
        rate_limiter: RateLimiter | None = RateLimiterDep,
    ):
        await _verify_api_key(request)

        await _apply_rate_limit(request, config.rate_limit_requests, rate_limiter)
        try:
            audio_url = sanitize_input(
                separation_request.audio_url,
                config.max_url_length,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input validation failed: {str(e)}"
            ) from e

        request_id = separation_request.request_id

        if cache_manager:
            cached_result = await cache_manager.get_cached_or_processing(audio_url)
            if cached_result:
                # Add this request as a subscriber if URL is still processing
                if cached_result.get("status") == "processing":
                    await cache_manager.add_subscriber(audio_url, request_id)
                logger.info("Returning cached/processing result for: %s", audio_url[:50])
                cached_result.update(_build_job_access_payload(cached_result["track_id"]))
                return cached_result

        track_id = str(uuid_module.uuid4())

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        cache_key = ""
        if cache_manager:
            cache_key = await cache_manager.mark_processing_start(audio_url, track_id, request_id)

        storage_config = StorageConfig(
            account_id=config.cloudflare_account_id,
            access_key_id=config.r2_access_key_id,
            secret_access_key=config.r2_secret_access_key,
            bucket_name=config.r2_bucket_name,
            public_domain=config.r2_public_domain,
        )

        cache_manager_config = None
        if cache_manager:
            cache_manager_config = AudioCacheConfig(redis_config=RedisConfig(url=config.redis_url))

        job_request = ProcessingJobRequest(
            track_id=track_id,
            audio_url=audio_url,
            max_file_size_mb=config.max_file_size_mb,
            processing_timeout=config.processing_timeout,
            cache_key=cache_key,
            storage_config=storage_config,
            directory_config=DirectoryConfig(models=config.models_dir, working=config.working_dir),
            cache_manager_config=cache_manager_config,
            request_id=request_id,
        )

        queue_manager.enqueue_job(job_request, job_timeout=config.processing_timeout)

        logger.info(
            "Accepted job %s for URL: %s",
            track_id,
            audio_url[:50] + ("..." if len(audio_url) > 50 else ""),
        )

        return {
            "track_id": track_id,
            "request_id": request_id,
            "status": JobStatus.PROCESSING.value,
            "message": "Audio separation started",
            **_build_job_access_payload(track_id),
        }

    @app.get("/health", response_model=HealthResponse)
    async def health_check(rate_limiter: RateLimiter | None = RateLimiterDep):
        health_status = {
            "status": "healthy",
            "timestamp": time.time(),
            "services": {
                "storage_operational": False,
                "rate_limiting_operational": False,
            },
        }

        try:
            await storage.file_exists_async("_health_check")
            health_status["services"]["storage_operational"] = True
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error("Storage health check failed: %s", e)

        if rate_limiter:
            try:
                # Test rate limiter connectivity by checking if a request is allowed
                _, _ = await rate_limiter.is_allowed(
                    "health_check",
                    config.health_check_rate_limit_requests,
                    config.health_check_rate_limit_window,
                )
                health_status["services"]["rate_limiting_operational"] = True
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.error("Redis rate limiter health check failed: %s", e)

        return HealthResponse(**health_status)

    @app.get("/job/{track_id}")
    async def get_job_status(
        request: Request, track_id: str, queue_manager: JobQueue | None = QueueManagerDep
    ):
        if not validate_track_id(track_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid track ID format"
            )

        await _verify_job_access(request, track_id)

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        try:
            job_status = queue_manager.get_job_status(track_id)
            return job_status
        except Exception as e:
            logger.error("Failed to get job status for %s: %s", track_id, e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve job status",
            ) from e

    @app.get("/job/{track_id}/stream")
    async def stream_job_progress(
        request: Request,
        track_id: str,
        progress_subscriber: ProgressSubscriber | None = ProgressSubscriberDep,
    ):
        """Stream job progress via Server-Sent Events."""
        if not validate_track_id(track_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid track ID format"
            )

        await _verify_job_access(request, track_id)

        if not progress_subscriber:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Progress streaming unavailable - Redis not configured",
            )

        return StreamingResponse(
            progress_subscriber.stream_progress(track_id, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/queue/info")
    async def get_queue_info(request: Request, queue_manager: JobQueue | None = QueueManagerDep):
        await _verify_api_key(request)

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        try:
            queue_info = queue_manager.get_queue_info()
            return queue_info
        except Exception as e:
            logger.error("Failed to get queue info: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve queue information",
            ) from e

    @app.get("/cache/stats", response_model=CacheStatsResponse)
    async def get_cache_stats(
        request: Request,
        cache_manager: AudioCache | None = CacheManagerDep,
    ):
        """Get cache statistics."""
        await _verify_api_key(request)

        if not cache_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Cache unavailable - Redis not configured",
            )

        try:
            stats = await cache_manager.get_cache_stats()
            return CacheStatsResponse(**stats)
        except Exception as e:
            logger.error("Failed to get cache stats: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve cache statistics",
            ) from e

    @app.delete("/cache", response_model=CacheClearResponse)
    async def clear_cache(
        request: Request,
        cache_manager: AudioCache | None = CacheManagerDep,
    ):
        """Clear all audio cache entries."""
        await _verify_api_key(request)

        if not cache_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Cache unavailable - Redis not configured",
            )

        try:
            deleted_count = await cache_manager.clear_all_cache()
            return CacheClearResponse(
                deleted_count=deleted_count,
                message=f"Successfully cleared {deleted_count} cache entries",
            )
        except Exception as e:
            logger.error("Failed to clear cache: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to clear cache",
            ) from e
