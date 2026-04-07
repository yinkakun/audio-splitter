from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.logger import get_logger
from services.audio_cache import AudioCache
from services.dependencies import services
from utils.rate_limiter import RateLimiter
from utils.redis_cache import RedisCache
from workers.job_queue import JobQueue

logger = get_logger(__name__)


def setup_services(config):
    if config.redis_enabled:
        try:
            services.queue_manager = JobQueue(config.redis_url)
            queue_info = services.queue_manager.get_queue_info()
            logger.info(f"Queue info at startup: {queue_info}")
            services.rate_limiter = RateLimiter(config.redis_url)
            services.redis_cache = RedisCache(config.redis_url)
        except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
            services.rate_limiter = None
            services.redis_cache = None
            services.queue_manager = None
            logger.error(f"Failed to initialize Redis services: {e}")
    else:
        logger.warning("Redis not configured - Redis services disabled")


async def initialize_storage(storage):
    """Initialize and verify storage connectivity"""
    try:
        await storage.file_exists_async("_startup_test")
        logger.info("R2 storage connectivity verified")
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.error(f"Storage startup validation failed: {e}")


def setup_cache_manager(storage) -> None:
    if services.redis_cache:
        services.cache_manager = AudioCache(services.redis_cache, storage)


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
        # Startup
        setup_services(config)

        await initialize_storage(storage)
        setup_cache_manager(storage)

        # Shutdown
        yield

        await services.close()
        await storage.close()

    return lifespan


def create_base_app(config) -> FastAPI:
    return FastAPI(
        version="1.0.0",
        title="YouTube Audio Separator API",
        description="API for separating YouTube audio into stems",
        docs_url="/docs" if config.debug else None,
        redoc_url="/redoc" if config.debug else None,
    )
