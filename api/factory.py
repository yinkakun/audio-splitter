from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.logger import get_logger
from services.audio_cache import AudioCache
from services.dependencies import services
from services.progress_subscriber import ProgressSubscriber
from utils.rate_limiter import RateLimiter
from utils.redis_cache import RedisCache
from workers.job_queue import JobQueue

logger = get_logger(__name__)


async def initialize_services(config, storage) -> None:
    """Initialize all services: Redis, storage, and cache."""
    # Initialize Redis-based services
    if config.redis_enabled:
        try:
            default_queue_name = config.queue_names[0] if config.queue_names else "default"
            services.queue_manager = JobQueue(config.redis_url, default_queue_name=default_queue_name)
            services.rate_limiter = RateLimiter(config.redis_url)
            services.redis_cache = RedisCache(config.redis_url)
            services.cache_manager = AudioCache(services.redis_cache, storage)
            services.progress_subscriber = ProgressSubscriber(config.redis_url)
            logger.info("Redis services initialized")
        except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
            services.rate_limiter = None
            services.redis_cache = None
            services.queue_manager = None
            services.cache_manager = None
            services.progress_subscriber = None
            logger.error(f"Failed to initialize Redis services: {e}")
    else:
        logger.warning("Redis not configured - Redis services disabled")

    # Verify storage connectivity
    try:
        await storage.file_exists_async("_startup_test")
        logger.info("R2 storage connectivity verified")
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Storage startup validation failed: {e}")


def configure_middleware(app: FastAPI, _config) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


def create_lifespan_manager(config, storage):
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await initialize_services(config, storage)
        yield
        await services.close()
        await storage.close()

    return lifespan


def create_base_app(config) -> FastAPI:
    return FastAPI(
        version="1.0.0",
        title="Audio Separator API",
        description="API for separating audio from yt-dlp compatible URLs into stems",
        docs_url="/docs" if config.debug else None,
        redoc_url="/redoc" if config.debug else None,
    )
