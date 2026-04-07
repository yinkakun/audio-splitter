import time
import uuid as uuid_module
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from config.constants import (
    DEFAULT_HEALTH_CHECK_RATE_LIMIT_REQUESTS,
    DEFAULT_HEALTH_CHECK_RATE_LIMIT_WINDOW,
)
from config.logger import get_logger
from models.job import JobStatus
from models.request import AudioCacheConfig, ProcessingJobRequest, RedisConfig, StorageConfig
from services.audio_cache import AudioCache
from services.dependencies import CacheManagerDep, QueueManagerDep, RateLimiterDep
from utils.exceptions import RateLimitExceededError, ServiceUnavailableError, ValidationError
from utils.rate_limiter import RateLimiter, parse_rate_limit
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
    search_query: str = Field(..., min_length=1, max_length=200)

    @field_validator("search_query")
    @classmethod
    def validate_search_query(cls, v):
        return sanitize_input(v, 200)


class HealthResponse(BaseModel):
    status: str
    timestamp: float
    services: Dict[str, bool]


def register_error_handlers(app: FastAPI):
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(ValidationError)
    async def validation_exception_handler(_: Request, exc: ValidationError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": str(exc)},
        )

    @app.exception_handler(ServiceUnavailableError)
    async def service_unavailable_handler(_: Request, exc: ServiceUnavailableError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": str(exc)},
        )

    @app.exception_handler(RateLimitExceededError)
    async def rate_limit_handler(_: Request, exc: RateLimitExceededError):
        return JSONResponse(
            content={"error": str(exc)},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after > 0 else {},
        )

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
    async def _verify_api_key(request: Request):
        authorization = request.headers.get("Authorization")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = authorization.replace("Bearer ", "")
        if not token or token != config.api_secret_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def _apply_rate_limit(
        request: Request, rate_limit: str, rate_limiter: Optional[RateLimiter] = RateLimiterDep
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
            logger.warning(f"Rate limit exceeded for {client_ip}: {rate_limit}")
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
        queue_manager: Optional[JobQueue] = QueueManagerDep,
        cache_manager: Optional[AudioCache] = CacheManagerDep,
    ):
        await _verify_api_key(request)

        await _apply_rate_limit(request, config.rate_limits.requests)
        try:
            search_query = sanitize_input(
                separation_request.search_query,
                config.input_limits.max_search_query_length,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Input validation failed: {str(e)}"
            ) from e

        if cache_manager:
            cached_result = await cache_manager.get_cached_or_processing(search_query)
            if cached_result:
                logger.info(f"Returning cached/processing result for: {search_query[:50]}")
                return cached_result

        track_id = str(uuid_module.uuid4())

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        cache_key = ""
        if cache_manager:
            cache_key = await cache_manager.mark_processing_start(search_query, track_id)

        storage_config = StorageConfig(
            account_id=config.r2_storage.account_id,
            access_key_id=config.r2_storage.access_key_id,
            secret_access_key=config.r2_storage.secret_access_key,
            bucket_name=config.r2_storage.bucket_name,
            public_domain=config.r2_storage.public_domain,
        )

        cache_manager_config = None
        if cache_manager:
            cache_manager_config = AudioCacheConfig(redis_config=RedisConfig(url=config.redis_url))

        job_request = ProcessingJobRequest(
            track_id=track_id,
            search_query=search_query,
            max_file_size_mb=config.input_limits.max_file_size_mb,
            processing_timeout=config.processing.timeout,
            webhook_url=config.webhook_url,
            webhook_secret=config.webhook_secret,
            cache_key=cache_key,
            storage_config=storage_config,
            models_dir=config.models_dir,
            working_dir=config.working_dir,
            cache_manager_config=cache_manager_config,
        )

        queue_manager.enqueue_job(job_request)

        logger.info(
            "Accepted job %s for search query: %s",
            track_id,
            search_query[:50] + ("..." if len(search_query) > 50 else ""),
        )

        return {
            "track_id": track_id,
            "status": JobStatus.PROCESSING.value,
            "message": "Audio separation started",
        }

    @app.get("/health", response_model=HealthResponse)
    async def health_check(rate_limiter: Optional[RateLimiter] = RateLimiterDep):
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
            health_status["services"]["storage_operational"] = False
            logger.error("Storage health check failed: %s", e)

        if rate_limiter:
            try:
                # Test rate limiter connectivity by checking if a request is allowed
                _, _ = await rate_limiter.is_allowed(
                    "health_check",
                    DEFAULT_HEALTH_CHECK_RATE_LIMIT_REQUESTS,
                    DEFAULT_HEALTH_CHECK_RATE_LIMIT_WINDOW,
                )
                health_status["services"]["rate_limiting_operational"] = True
            except (ConnectionError, TimeoutError, OSError) as e:
                health_status["services"]["rate_limiting_operational"] = False
                logger.error("Redis rate limiter health check failed: %s", e)

        return HealthResponse(**health_status)

    @app.get("/job/{track_id}")
    async def get_job_status(
        request: Request, track_id: str, queue_manager: Optional[JobQueue] = QueueManagerDep
    ):
        await _verify_api_key(request)

        if not validate_track_id(track_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid track ID format"
            )

        if not queue_manager:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue unavailable - Redis not configured",
            )

        try:
            job_status = queue_manager.get_job_status(track_id)
            return job_status
        except Exception as e:
            logger.error(f"Failed to get job status for {track_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve job status",
            ) from e

    @app.get("/queue/info")
    async def get_queue_info(request: Request, queue_manager: Optional[JobQueue] = QueueManagerDep):
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
            logger.error(f"Failed to get queue info: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve queue information",
            ) from e
