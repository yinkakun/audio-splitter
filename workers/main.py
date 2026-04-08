import os
import platform
import signal
import sys

from rq import Queue, SimpleWorker, Worker
from rq.worker_pool import WorkerPool

from config.config import Config
from config.logger import get_logger, setup_logging
from services.audio_processor import DefaultSeparatorProvider
from workers.globals import set_global_separator_provider
from workers.job_queue import JobQueue

logger = get_logger(__name__)


def signal_handler(signum, _frame):
    logger.info(f"Received signal {signum}, shutting down worker...")
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    config = Config()
    setup_logging(config.debug)

    if not config.redis_enabled:
        logger.error("Redis URL not configured. Set REDIS_URL environment variable.")
        sys.exit(1)

    worker_name = config.worker_name
    queue_names = config.queue_names
    worker_concurrency = config.worker_concurrency

    logger.info(
        "Starting RQ worker",
        worker_name=worker_name,
        queue_names=queue_names,
        concurrency=worker_concurrency,
    )

    try:
        separator_provider = DefaultSeparatorProvider(
            models_dir=config.models_dir,
            working_dir=config.working_dir,
            model_filename=f"{config.separation_model}.yaml",
            output_format=config.output_format,
            output_bitrate=config.output_bitrate,
        )
        separator_provider.initialize_model()
        set_global_separator_provider(separator_provider)
        logger.info(
            "Model initialized successfully",
            model=config.separation_model,
            output_format=config.output_format,
            output_bitrate=config.output_bitrate,
        )

        queue_manager = JobQueue(config.redis_url, default_queue_name=queue_names[0])
        redis_connection = queue_manager.get_redis_connection()

        queues = [Queue(name, connection=redis_connection) for name in queue_names]
        if worker_concurrency > 1:
            worker_pool = WorkerPool(
                queues=queues,
                connection=redis_connection,
                num_workers=worker_concurrency,
            )
            logger.info(
                "Worker pool started",
                worker_pool_name=worker_pool.name,
                worker_count=worker_concurrency,
                worker_pid=os.getpid(),
            )
            worker_pool.start()
        else:
            # Use SimpleWorker on macOS to avoid fork + MPS crashes
            # SimpleWorker runs jobs in the main process (no forking)
            worker_class = SimpleWorker if platform.system() == "Darwin" else Worker
            worker = worker_class(
                queues=queues,
                name=worker_name,
                connection=redis_connection,
            )

            logger.info(
                "Worker started",
                worker_name=worker.name,
                worker_pid=os.getpid(),
                worker_type=worker_class.__name__,
            )
            worker.work(with_scheduler=True)
    except (ConnectionError, RuntimeError) as e:
        logger.error(f"Worker failed to start: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
