import asyncio
import time
from typing import Any, Coroutine, TypeVar

from rq import get_current_job

from config.logger import get_logger
from models.job import JobStatus
from models.progress import ProgressStage
from models.request import ProcessingJobRequest
from services.audio_cache import AudioCache
from services.audio_processor import AudioProcessor
from services.progress_publisher import ProgressPublisher
from services.storage import get_storage_client
from utils.redis_cache import RedisCache
from workers.globals import get_global_separator_provider


T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run async code in sync context, reusing event loop if possible."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in async context - shouldn't happen in RQ worker
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(coro)

    # Create new loop or get existing one
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    return loop.run_until_complete(coro)


logger = get_logger(__name__)


def process_audio_job(job_request: ProcessingJobRequest) -> dict[str, Any]:
    job = get_current_job()
    if job:
        logger.info("Starting RQ job", job_id=job.id, track_id=job_request.track_id)

    track_id = job_request.track_id
    request_ids: list[str] = [job_request.request_id] if job_request.request_id else []
    cache_manager: AudioCache | None = None
    publisher: ProgressPublisher | None = None

    # Initialize progress publisher if Redis is available
    if job_request.cache_manager_config:
        publisher = ProgressPublisher(job_request.cache_manager_config.redis_config.url)

    def emit_progress(stage: str) -> None:
        """Callback for AudioProcessor to emit progress events."""
        if publisher:
            stage_map = {
                "downloading": ProgressStage.DOWNLOADING,
                "separating": ProgressStage.SEPARATING,
                "uploading": ProgressStage.UPLOADING,
            }
            if stage in stage_map:
                publisher.publish_progress(track_id, stage_map[stage])

    try:
        # Emit queued/started progress
        if publisher:
            publisher.publish_progress(track_id, ProgressStage.QUEUED, message="Job started")

        storage = get_storage_client(
            account_id=job_request.storage_config.account_id,
            access_key_id=job_request.storage_config.access_key_id,
            secret_access_key=job_request.storage_config.secret_access_key,
            bucket_name=job_request.storage_config.bucket_name,
            public_domain=job_request.storage_config.public_domain,
        )

        global_separator = get_global_separator_provider()
        audio_processor = AudioProcessor(
            storage,
            models_dir=job_request.directory_config.models,
            working_dir=job_request.directory_config.working,
            separator_provider=global_separator,
        )

        # Only initialize model if we don't have a pre-initialized provider
        if not global_separator:
            audio_processor.initialize_model()

        if job_request.cache_manager_config:
            redis_cache = RedisCache(job_request.cache_manager_config.redis_config.url)
            cache_manager = AudioCache(redis_cache, storage)

        # Get all request_ids that subscribed to this URL (may include more than initial)
        if cache_manager and job_request.cache_key:
            try:
                cached_request_ids = run_async(cache_manager.get_request_ids(job_request.cache_key))
                if cached_request_ids:
                    request_ids = cached_request_ids
            except RuntimeError as e:
                logger.warning(f"Failed to get request_ids: {e}")

        logger.info(
            "Starting audio processing job",
            track_id=track_id,
            audio_url=job_request.audio_url[:50]
            + ("..." if len(job_request.audio_url) > 50 else ""),
        )

        result = audio_processor.process_audio(
            track_id=track_id,
            audio_url=job_request.audio_url,
            max_file_size_mb=job_request.max_file_size_mb,
            processing_timeout=job_request.processing_timeout,
            progress_callback=emit_progress,
        )

        logger.info("Audio processing completed successfully", track_id=track_id)

        success_payload = {
            "result": result,
            "progress": 100,
            "created_at": time.time(),
            "status": JobStatus.COMPLETED.value,
            "track_id": track_id,
            "request_ids": request_ids,
            "audio_url": job_request.audio_url,
        }

        if cache_manager and job_request.cache_key:
            try:
                run_async(
                    cache_manager.cache_completed_result(
                        job_request.cache_key, job_request.audio_url, result
                    )
                )
            except RuntimeError as e:
                logger.error(f"Failed to cache result: {e}")

        # Emit completed progress with result
        if publisher:
            publisher.publish_progress(
                track_id, ProgressStage.COMPLETED, result=success_payload
            )

        return success_payload

    # Expected failure modes during audio processing:
    # - ConnectionError/TimeoutError: network issues during download or upload
    # - RuntimeError: separation failures, download errors, storage errors
    # - OSError/FileNotFoundError: filesystem issues, missing audio files
    # - ValueError: invalid file sizes, unsupported formats, empty files
    except (
        ConnectionError,
        TimeoutError,
        RuntimeError,
        OSError,
        FileNotFoundError,
        ValueError,
    ) as e:
        error_msg = str(e)
        logger.error(
            "Audio processing failed", track_id=track_id, error=error_msg, exc_info=True
        )

        # Clear the processing cache entry so new requests can retry
        if cache_manager and job_request.cache_key:
            try:
                run_async(cache_manager.clear_processing_entry(job_request.cache_key))
            except RuntimeError as cache_error:
                logger.warning("Failed to clear cache on failure: %s", cache_error)

        # Emit failed progress
        if publisher:
            publisher.publish_progress(track_id, ProgressStage.FAILED, error=error_msg)

        raise RuntimeError(error_msg) from e
