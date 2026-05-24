from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from app.config import Settings, get_settings
from app.models import JobState
from app.services import storage
from app.utils.logger import get_logger


logger = get_logger(__name__)

TaskCallable = Callable[[str], dict]
QueuedTask = tuple[str, str, TaskCallable, str]

_TASK_QUEUE: queue.Queue[QueuedTask] = queue.Queue()
_START_LOCK = threading.Lock()
_WORKER_STARTED = False


def ensure_worker_started(settings: Settings | None = None) -> None:
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    with _START_LOCK:
        if _WORKER_STARTED:
            return
        thread = threading.Thread(
            target=_worker_loop,
            kwargs={"settings": settings or get_settings()},
            name="pdf-toc-local-worker",
            daemon=True,
        )
        thread.start()
        _WORKER_STARTED = True
        logger.info("Started in-process local queue worker thread.")


def enqueue(job_id: str, document_id: str, task: TaskCallable, task_name: str) -> None:
    ensure_worker_started()
    _TASK_QUEUE.put((job_id, document_id, task, task_name))


def _worker_loop(*, settings: Settings) -> None:
    while True:
        job_id, document_id, task, task_name = _TASK_QUEUE.get()
        try:
            storage.update_local_job(job_id, JobState.STARTED, settings=settings)
            result = task(document_id)
            storage.update_local_job(job_id, JobState.FINISHED, result=result, settings=settings)
        except Exception as exc:
            logger.exception("Local %s job failed for document %s", task_name, document_id)
            storage.update_local_job(job_id, JobState.FAILED, error=str(exc), settings=settings)
        finally:
            _TASK_QUEUE.task_done()
