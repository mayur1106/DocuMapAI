from __future__ import annotations

import json
import shutil
import uuid
from json import JSONDecodeError
from pathlib import Path, PureWindowsPath
from typing import Iterable

import fitz
from fastapi import UploadFile

from app.config import Settings, get_settings
from app.models import ActivityLogResponse, DocumentRecord, DocumentStatus, DocumentWorkflow, Heading, JobRecord, JobState, utc_now


class StorageError(RuntimeError):
    pass


class PDFValidationError(StorageError):
    pass


def ensure_storage(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.documents_index_path.parent.mkdir(parents=True, exist_ok=True)
    if not settings.documents_index_path.exists():
        _atomic_write_json(settings.documents_index_path, {})
    if not settings.jobs_index_path.exists():
        _atomic_write_json(settings.jobs_index_path, {})
    if not settings.activity_log_path.exists():
        settings.activity_log_path.touch()


def save_upload_file(
    upload: UploadFile,
    settings: Settings | None = None,
    workflow: DocumentWorkflow = DocumentWorkflow.TOC,
) -> DocumentRecord:
    settings = settings or get_settings()
    ensure_storage(settings)

    original_name = Path(upload.filename or "document.pdf").name
    if not original_name.lower().endswith(".pdf"):
        raise PDFValidationError("Uploaded file must use a .pdf extension.")

    document_id = uuid.uuid4().hex
    target_path = settings.upload_dir / f"{document_id}.pdf"

    with target_path.open("wb") as destination:
        shutil.copyfileobj(upload.file, destination)

    max_bytes = settings.max_upload_mb * 1024 * 1024
    if target_path.stat().st_size == 0:
        target_path.unlink(missing_ok=True)
        raise PDFValidationError("Uploaded PDF is empty.")
    if target_path.stat().st_size > max_bytes:
        target_path.unlink(missing_ok=True)
        raise PDFValidationError(f"Uploaded PDF exceeds {settings.max_upload_mb} MB.")

    try:
        validate_pdf(target_path)
    except PDFValidationError:
        target_path.unlink(missing_ok=True)
        raise

    record = DocumentRecord(
        id=document_id,
        original_filename=original_name,
        original_path=target_path,
        workflow=workflow,
    )
    upsert_document(record, settings)
    log_activity(
        action="document_uploaded",
        status="success",
        message=f"Uploaded {original_name} to {workflow.value} workflow.",
        document=record,
        metadata={"path": str(target_path), "size_bytes": target_path.stat().st_size},
        settings=settings,
    )
    return record


def validate_pdf(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            header = handle.read(5)
        if header != b"%PDF-":
            raise PDFValidationError("Uploaded file does not have a valid PDF header.")

        with fitz.open(path) as document:
            if document.page_count < 1:
                raise PDFValidationError("PDF does not contain any pages.")
            if document.is_encrypted:
                raise PDFValidationError("Encrypted PDFs are not supported.")
    except PDFValidationError:
        raise
    except Exception as exc:
        raise PDFValidationError("Uploaded file is not a readable PDF.") from exc


def get_document(document_id: str, settings: Settings | None = None) -> DocumentRecord:
    settings = settings or get_settings()
    documents = _read_documents(settings)
    data = documents.get(document_id)
    if not data:
        raise KeyError(document_id)
    return _document_from_data(data, settings)


def list_documents(settings: Settings | None = None) -> list[DocumentRecord]:
    settings = settings or get_settings()
    documents = _read_documents(settings)
    records = [_document_from_data(item, settings) for item in documents.values()]
    return sorted(records, key=lambda record: record.updated_at, reverse=True)


def delete_document(document_id: str, settings: Settings | None = None) -> DocumentRecord:
    settings = settings or get_settings()
    documents = _read_documents(settings)
    data = documents.pop(document_id, None)
    if not data:
        raise KeyError(document_id)

    record = _document_from_data(data, settings)
    for path in (record.original_path, record.output_path, record.toc_path, record.process_log_path, record.xml_path, record.xml_stats_path):
        if path:
            path.unlink(missing_ok=True)

    _atomic_write_json(settings.documents_index_path, documents)
    log_activity(
        action="document_deleted",
        status="success",
        message=f"Deleted {record.original_filename}.",
        document=record,
        settings=settings,
    )
    return record


def upsert_document(record: DocumentRecord, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    ensure_storage(settings)
    documents = _read_documents(settings)
    record.updated_at = utc_now()
    documents[record.id] = record.model_dump(mode="json")
    _atomic_write_json(settings.documents_index_path, documents)


def update_document_status(
    document_id: str,
    status: DocumentStatus,
    *,
    output_path: Path | None = None,
    toc_path: Path | None = None,
    process_log_path: Path | None = None,
    error: str | None = None,
    settings: Settings | None = None,
) -> DocumentRecord:
    settings = settings or get_settings()
    record = get_document(document_id, settings)
    record.status = status
    record.error = error
    if output_path is not None:
        record.output_path = output_path
    if toc_path is not None:
        record.toc_path = toc_path
    if process_log_path is not None:
        record.process_log_path = process_log_path
    upsert_document(record, settings)
    log_activity(
        action="document_status_updated",
        status="error" if status == DocumentStatus.FAILED else "info",
        message=f"{record.original_filename} status changed to {status.value}.",
        document=record,
        metadata={"status": status.value, "error": error},
        settings=settings,
    )
    return record


def save_toc(document_id: str, headings: Iterable[Heading], settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    ensure_storage(settings)
    toc_path = settings.output_dir / f"{document_id}_toc.json"
    payload = [heading.model_dump(mode="json") for heading in headings]
    _atomic_write_json(toc_path, payload)
    try:
        record = get_document(document_id, settings)
        log_activity(
            action="toc_saved",
            status="success",
            message=f"Saved TOC data for {record.original_filename}.",
            document=record,
            metadata={"heading_count": len(payload), "path": str(toc_path)},
            settings=settings,
        )
    except KeyError:
        log_activity(
            action="toc_saved",
            status="warning",
            message=f"Saved TOC data for missing document {document_id}.",
            document_id=document_id,
            metadata={"heading_count": len(payload), "path": str(toc_path)},
            settings=settings,
        )
    return toc_path


def save_process_log(
    document_id: str,
    log_path: Path,
    settings: Settings | None = None,
) -> DocumentRecord:
    settings = settings or get_settings()
    record = get_document(document_id, settings)
    record.process_log_path = log_path
    upsert_document(record, settings)
    log_activity(
        action="process_log_saved",
        status="info",
        message=f"Saved process diagnostics log for {record.original_filename}.",
        document=record,
        metadata={"path": str(log_path)},
        settings=settings,
    )
    return record


def save_xml_conversion(
    document_id: str,
    *,
    xml_path: Path,
    stats_path: Path,
    settings: Settings | None = None,
) -> DocumentRecord:
    settings = settings or get_settings()
    record = get_document(document_id, settings)
    record.xml_path = xml_path
    record.xml_stats_path = stats_path
    record.status = DocumentStatus.READY
    record.error = None
    upsert_document(record, settings)
    log_activity(
        action="xml_conversion_ready",
        status="success",
        message=f"XML conversion ready for {record.original_filename}.",
        document=record,
        metadata={"xml_path": str(xml_path), "stats_path": str(stats_path)},
        settings=settings,
    )
    return record


def save_xml_stats(document_id: str, stats: dict, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    ensure_storage(settings)
    stats_path = settings.output_dir / f"{document_id}_xml_stats.json"
    _atomic_write_json(stats_path, stats)
    log_activity(
        action="xml_stats_saved",
        status="success",
        message=f"Saved XML statistics for document {document_id}.",
        document_id=document_id,
        metadata={"path": str(stats_path), "page_count": stats.get("page_count")},
        settings=settings,
    )
    return stats_path


def load_xml_stats(document_id: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    record = get_document(document_id, settings)
    if not record.xml_stats_path or not record.xml_stats_path.exists():
        raise FileNotFoundError(f"XML stats have not been generated for document {document_id}.")
    with record.xml_stats_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Stored XML stats are not valid.")
    return payload


def load_toc(document_id: str, settings: Settings | None = None) -> list[Heading]:
    settings = settings or get_settings()
    record = get_document(document_id, settings)
    if not record.toc_path or not record.toc_path.exists():
        raise FileNotFoundError(f"TOC has not been generated for document {document_id}.")
    with record.toc_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return [Heading.model_validate(item) for item in payload]


def create_job(record: JobRecord, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    ensure_storage(settings)
    jobs = _read_jobs(settings)
    jobs[record.id] = record.model_dump(mode="json")
    _atomic_write_json(settings.jobs_index_path, jobs)
    log_activity(
        action="job_saved",
        status="info",
        message=f"Saved {record.backend} job {record.id} with status {record.status.value}.",
        document_id=record.document_id,
        metadata={"job_id": record.id, "backend": record.backend, "status": record.status.value},
        settings=settings,
    )


def get_local_job(job_id: str, settings: Settings | None = None) -> JobRecord:
    settings = settings or get_settings()
    jobs = _read_jobs(settings)
    data = jobs.get(job_id)
    if not data:
        raise KeyError(job_id)
    return JobRecord.model_validate(data)


def update_local_job(
    job_id: str,
    status: JobState,
    *,
    result: dict | None = None,
    error: str | None = None,
    settings: Settings | None = None,
) -> JobRecord:
    settings = settings or get_settings()
    record = get_local_job(job_id, settings)
    record.status = status
    record.result = result
    record.error = error
    record.updated_at = utc_now()
    create_job(record, settings)
    return record


def log_activity(
    *,
    action: str,
    message: str,
    status: str = "info",
    document: DocumentRecord | None = None,
    document_id: str | None = None,
    filename: str | None = None,
    workflow: DocumentWorkflow | str | None = None,
    metadata: dict | None = None,
    settings: Settings | None = None,
) -> ActivityLogResponse:
    settings = settings or get_settings()
    ensure_storage(settings)
    entry = ActivityLogResponse(
        id=uuid.uuid4().hex,
        created_at=utc_now(),
        action=action,
        status=status,
        message=message,
        document_id=document.id if document else document_id,
        filename=document.original_filename if document else filename,
        workflow=document.workflow if document else workflow,
        metadata=metadata or {},
    )
    with settings.activity_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return entry


def list_activity_logs(limit: int = 80, settings: Settings | None = None) -> list[ActivityLogResponse]:
    settings = settings or get_settings()
    ensure_storage(settings)
    if not settings.activity_log_path.exists():
        return []

    lines = settings.activity_log_path.read_text(encoding="utf-8").splitlines()
    entries: list[ActivityLogResponse] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entries.append(ActivityLogResponse.model_validate(json.loads(line)))
        except (JSONDecodeError, ValueError):
            continue
        if len(entries) >= limit:
            break
    return entries


def _read_documents(settings: Settings) -> dict:
    ensure_storage(settings)
    return _read_json_index(settings.documents_index_path)


def _read_jobs(settings: Settings) -> dict:
    ensure_storage(settings)
    return _read_json_index(settings.jobs_index_path)


def _document_from_data(data: dict, settings: Settings) -> DocumentRecord:
    record = DocumentRecord.model_validate(data)
    return _normalize_document_paths(record, settings)


def _normalize_document_paths(record: DocumentRecord, settings: Settings) -> DocumentRecord:
    record.original_path = _resolve_path(record.original_path, settings.upload_dir, fallback_name=f"{record.id}.pdf")
    record.output_path = _resolve_path(record.output_path, settings.output_dir)
    record.toc_path = _resolve_path(record.toc_path, settings.output_dir)
    record.process_log_path = _resolve_path(record.process_log_path, settings.output_dir)
    record.xml_path = _resolve_path(record.xml_path, settings.output_dir)
    record.xml_stats_path = _resolve_path(record.xml_stats_path, settings.output_dir)
    return record


def _resolve_path(path: Path | None, base_dir: Path, fallback_name: str | None = None) -> Path | None:
    if path is None:
        return None
    if path.exists():
        return path

    raw_path = str(path)
    name = PureWindowsPath(raw_path).name if "\\" in raw_path else path.name
    candidates = [base_dir / name]
    if fallback_name:
        candidates.append(base_dir / fallback_name)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return path


def _read_json_index(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        _atomic_write_json(path, {})
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except JSONDecodeError:
        backup_path = path.with_suffix(f"{path.suffix}.bad")
        path.replace(backup_path)
        _atomic_write_json(path, {})
        return {}

    if not isinstance(payload, dict):
        backup_path = path.with_suffix(f"{path.suffix}.bad")
        path.replace(backup_path)
        _atomic_write_json(path, {})
        return {}

    return payload


def _atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)
