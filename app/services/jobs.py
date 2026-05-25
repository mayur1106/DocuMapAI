from __future__ import annotations

import uuid

from app.config import Settings, get_settings
from app.models import DocumentStatus, GenerateResponse, JobRecord, StatusResponse
from app.services import local_queue, storage
from app.workers.tasks import generate_toc_task, hyperlink_existing_toc_task


def enqueue_generation(
    document_id: str,
    settings: Settings | None = None,
) -> GenerateResponse:
    return _enqueue_task(
        document_id=document_id,
        task=generate_toc_task,
        task_name="TOC generation",
        settings=settings,
    )


def enqueue_existing_toc_linking(
    document_id: str,
    settings: Settings | None = None,
) -> GenerateResponse:
    return _enqueue_task(
        document_id=document_id,
        task=hyperlink_existing_toc_task,
        task_name="existing TOC hyperlinking",
        settings=settings,
    )


def _enqueue_task(
    *,
    document_id: str,
    task,
    task_name: str,
    settings: Settings | None = None,
) -> GenerateResponse:
    settings = settings or get_settings()
    local_queue.ensure_worker_started(settings)
    job_id = uuid.uuid4().hex
    storage.create_job(JobRecord(id=job_id, document_id=document_id, backend="local"))
    local_queue.enqueue(job_id, document_id, task, task_name)
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
