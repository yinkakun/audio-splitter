import os
import signal
import sys

from rq import Queue, Worker
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
    queue_names = config.queue_names or ["default"]
    worker_concurrency = config.worker_concurrency

    logger.info(
        "Starting RQ worker",
        worker_name=worker_name,
        queue_names=queue_names,
        concurrency=worker_concurrency,
        redis_url=config.redis_url[:50] + "...",
    )

    try:
        separator_provider = DefaultSeparatorProvider(config.models_dir)
        separator_provider.initialize_model()
        set_global_separator_provider(separator_provider)
        logger.info("model initialized successfully")

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
            worker = Worker(
                queues=queues,
                name=worker_name,
                connection=redis_connection,
            )

            logger.info("Worker started", worker_name=worker.name, worker_pid=os.getpid())
            worker.work(with_scheduler=True)
    except (ConnectionError, RuntimeError) as e:
        logger.error(f"Worker failed to start: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Worker shutdown complete")


if __name__ == "__main__":
    main()
