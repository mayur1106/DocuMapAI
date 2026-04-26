from __future__ import annotations

import argparse

from redis import Redis
from rq import Queue, SimpleWorker
from rq.timeouts import TimerDeathPenalty

from app.config import get_settings
from app.utils.logger import configure_logging


class WindowsSafeWorker(SimpleWorker):
    """RQ worker variant that avoids Unix-only fork and SIGALRM behavior."""

    death_penalty_class = TimerDeathPenalty


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PDF TOC RQ worker.")
    parser.add_argument("--url", default=None, help="Redis URL. Defaults to REDIS_URL/config.py.")
    parser.add_argument("--queue", default=None, help="Queue name. Defaults to QUEUE_NAME/config.py.")
    parser.add_argument("--burst", action="store_true", help="Process queued jobs and exit.")
    parser.add_argument("--logging-level", default="INFO", help="Worker logging level.")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    redis_url = args.url or settings.redis_url
    queue_name = args.queue or settings.queue_name

    connection = Redis.from_url(redis_url)
    queue = Queue(queue_name, connection=connection, default_timeout=settings.job_timeout_seconds)
    worker = WindowsSafeWorker([queue], connection=connection)
    worker.work(burst=args.burst, logging_level=args.logging_level)


if __name__ == "__main__":
    main()

