from __future__ import annotations

import uuid

from fastapi import BackgroundTasks

from app.config import Settings, get_settings
from app.models import DocumentStatus, GenerateResponse, JobRecord, JobState, StatusResponse
from app.services import storage
from app.utils.logger import get_logger
from app.workers.tasks import generate_toc_task, hyperlink_existing_toc_task


logger = get_logger(__name__)


def enqueue_generation(
    document_id: str,
    background_tasks: BackgroundTasks,
    settings: Settings | None = None,
) -> GenerateResponse:
    return _enqueue_task(
        document_id=document_id,
        task=generate_toc_task,
        task_name="TOC generation",
        background_tasks=background_tasks,
        settings=settings,
    )


def enqueue_existing_toc_linking(
    document_id: str,
    background_tasks: BackgroundTasks,
    settings: Settings | None = None,
) -> GenerateResponse:
    return _enqueue_task(
        document_id=document_id,
        task=hyperlink_existing_toc_task,
        task_name="existing TOC hyperlinking",
        background_tasks=background_tasks,
        settings=settings,
    )


def _enqueue_task(
    *,
    document_id: str,
    task,
    task_name: str,
    background_tasks: BackgroundTasks,
    settings: Settings | None = None,
) -> GenerateResponse:
    settings = settings or get_settings()
    response: GenerateResponse
    try:
        from redis import Redis
        from rq import Queue

        redis = Redis.from_url(settings.redis_url)
        redis.ping()
        queue = Queue(settings.queue_name, connection=redis)
        job = queue.enqueue_call(
            func=task,
            args=(document_id,),
            timeout=settings.job_timeout_seconds,
            result_ttl=86400,
            failure_ttl=86400,
        )
        response = GenerateResponse(job_id=job.id, document_id=document_id, status="queued", backend="rq")
    except Exception as exc:
        if not settings.allow_inline_fallback:
            raise
        logger.warning("RQ is unavailable, falling back to local background task: %s", exc)
        job_id = uuid.uuid4().hex
        storage.create_job(JobRecord(id=job_id, document_id=document_id, backend="local"))
        background_tasks.add_task(_run_local_task, job_id, document_id, task, task_name)
        response = GenerateResponse(job_id=job_id, document_id=document_id, status="queued", backend="local")

    storage.update_document_status(document_id, DocumentStatus.QUEUED, error=None, settings=settings)
    try:
        document = storage.get_document(document_id, settings)
        storage.log_activity(
            action="toc_job_queued",
            status="success",
            message=f"Queued TOC processing for {document.original_filename}.",
            document=document,
            metadata={"job_id": response.job_id, "backend": response.backend},
            settings=settings,
        )
    except KeyError:
        storage.log_activity(
            action="toc_job_queued",
            status="warning",
            message=f"Queued TOC processing for missing document {document_id}.",
            document_id=document_id,
            metadata={"job_id": response.job_id, "backend": response.backend},
            settings=settings,
        )
    return response


def get_generation_status(job_id: str, settings: Settings | None = None) -> StatusResponse:
    settings = settings or get_settings()
    try:
        local_job = storage.get_local_job(job_id, settings)
        _sync_document_status_from_job(
            local_job.document_id,
            local_job.status.value,
            error=local_job.error,
            settings=settings,
        )
        return StatusResponse(
            job_id=local_job.id,
            document_id=local_job.document_id,
            status=local_job.status.value,
            backend=local_job.backend,
            result=local_job.result,
            error=local_job.error,
        )
    except KeyError:
        pass

    try:
        from redis import Redis
        from rq.job import Job
        from rq.exceptions import NoSuchJobError

        redis = Redis.from_url(settings.redis_url)
        job = Job.fetch(job_id, connection=redis)
        status = job.get_status(refresh=True)
        document_id = job.args[0] if job.args else None
        _sync_document_status_from_job(
            document_id,
            status,
            error=str(job.exc_info) if status == "failed" else None,
            settings=settings,
        )
        return StatusResponse(
            job_id=job.id,
            document_id=document_id,
            status=status,
            backend="rq",
            result=job.result if status == "finished" else None,
            error=str(job.exc_info) if status == "failed" else None,
        )
    except ModuleNotFoundError as exc:
        raise KeyError(job_id) from exc
    except NoSuchJobError as exc:
        raise KeyError(job_id) from exc


def _sync_document_status_from_job(
    document_id: str | None,
    job_status: str,
    *,
    error: str | None = None,
    settings: Settings,
) -> None:
    if not document_id:
        return

    status_map = {
        "queued": DocumentStatus.QUEUED,
        "scheduled": DocumentStatus.QUEUED,
        "deferred": DocumentStatus.QUEUED,
        "started": DocumentStatus.PROCESSING,
        "finished": DocumentStatus.READY,
        "failed": DocumentStatus.FAILED,
        "stopped": DocumentStatus.FAILED,
        "canceled": DocumentStatus.FAILED,
    }
    document_status = status_map.get(job_status)
    if document_status is None:
        return

    try:
        record = storage.get_document(document_id, settings)
    except KeyError:
        return

    if record.status == document_status and record.error == error:
        return
    if record.status == DocumentStatus.READY and document_status in {DocumentStatus.QUEUED, DocumentStatus.PROCESSING}:
        return

    storage.update_document_status(document_id, document_status, error=error, settings=settings)
    storage.log_activity(
        action="job_status_synced",
        status="error" if document_status == DocumentStatus.FAILED else "info",
        message=f"Synced job status {job_status} for {record.original_filename}.",
        document=record,
        metadata={"job_status": job_status, "document_status": document_status.value, "error": error},
        settings=settings,
    )


def _run_local_task(job_id: str, document_id: str, task, task_name: str) -> None:
    try:
        storage.update_local_job(job_id, JobState.STARTED)
        result = task(document_id)
        storage.update_local_job(job_id, JobState.FINISHED, result=result)
    except Exception as exc:
        logger.exception("Local %s job failed for document %s", task_name, document_id)
        storage.update_local_job(job_id, JobState.FAILED, error=str(exc))
