from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import Annotated

import fitz
from fastapi import FastAPI, File, HTTPException, Path, Query, Response, UploadFile, status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from app.config import get_settings
from app.models import (
    ActivityLogResponse,
    DocumentResponse,
    DocumentStatus,
    DocumentWorkflow,
    ErrorResponse,
    GenerateResponse,
    StatusResponse,
    TocEntryResponse,
    TocRevisionDryRunResponse,
    UploadResponse,
    XmlConversionResponse,
    XmlStatsResponse,
)
from app.services import jobs, local_queue, storage
from app.services.pdf_xml_parser import convert_pdf_to_xml, read_xml_preview
from app.services.storage import PDFValidationError
from app.services.toc_revision_dry_run import build_toc_revision_dry_run
from app.utils.logger import configure_logging, get_logger


configure_logging()
logger = get_logger(__name__)
settings = get_settings()

OPENAPI_TAGS = [
    {
        "name": "Activity",
        "description": "Inspect recent system activity and audit events.",
    },
    {
        "name": "Documents",
        "description": "Upload source PDFs and download processed PDFs.",
    },
    {
        "name": "TOC Generation",
        "description": "Generate, monitor, and inspect detected table of contents data.",
    },
    {
        "name": "Existing TOC Linking",
        "description": "Repair visible TOC pages that already exist in the source PDF.",
    },
    {
        "name": "PDF to XML",
        "description": "Convert uploaded PDFs into layout-preserving XML and inspect conversion statistics.",
    },
]

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Backend service for text-based PDFs that repairs existing visible TOC links when TOC pages "
        "are present, or generates clickable TOC pages from MEL item tables when no visible TOC "
        "exists. It also adds sidebar bookmarks and links EICAS MEL item references when targets "
        "exist in the same PDF."
    ),
    summary="Repair or generate clickable PDF tables of contents and bookmarks.",
    openapi_tags=OPENAPI_TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={
        "name": "PDF TOC Generator API",
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "http://cmtdigi.com:5174"
    ],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    storage.ensure_storage(settings)
    local_queue.ensure_worker_started(settings)


@app.get(
    "/activity",
    response_model=list[ActivityLogResponse],
    tags=["Activity"],
    summary="List recent system activity",
    description="Returns recent upload, queue, processing, conversion, download, delete, and failure events.",
    response_description="Recent activity log entries.",
)
async def list_activity(
    limit: Annotated[int, Query(ge=1, le=250, description="Maximum number of activity entries to return.")] = 80,
) -> list[ActivityLogResponse]:
    return storage.list_activity_logs(limit=limit, settings=settings)


@app.post(
    "/upload",
    response_model=UploadResponse,
    tags=["Documents"],
    summary="Upload a source PDF",
    description=(
        "Uploads and validates a PDF, stores the original file locally, and creates a document record. "
        "The PDF must contain extractable text and must not be encrypted."
    ),
    response_description="Created document record.",
    responses={
        400: {"model": ErrorResponse, "description": "The uploaded file is not a valid supported PDF."},
        500: {"model": ErrorResponse, "description": "The service could not save the uploaded PDF."},
    },
)
async def upload_pdf(
    file: Annotated[
        UploadFile,
        File(
            description="Text-based PDF file to process. Scanned PDFs without embedded text are not OCRed.",
            media_type="application/pdf",
        ),
    ],
    workflow: Annotated[
        DocumentWorkflow,
        Query(description="Dashboard workflow that owns this upload."),
    ] = DocumentWorkflow.TOC,
) -> UploadResponse:
    try:
        record = storage.save_upload_file(file, settings, workflow)
    except PDFValidationError as exc:
        storage.log_activity(
            action="document_upload_failed",
            status="error",
            message=f"Upload rejected for {file.filename or 'document.pdf'}: {exc}",
            filename=file.filename,
            workflow=workflow,
            metadata={"error": str(exc)},
            settings=settings,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to save uploaded PDF")
        storage.log_activity(
            action="document_upload_failed",
            status="error",
            message=f"Upload failed for {file.filename or 'document.pdf'}.",
            filename=file.filename,
            workflow=workflow,
            metadata={"error": str(exc)},
            settings=settings,
        )
        raise HTTPException(status_code=500, detail="Failed to save uploaded PDF.") from exc

    return UploadResponse(
        document_id=record.id,
        filename=record.original_filename,
        status=record.status,
        workflow=record.workflow,
    )


@app.get(
    "/documents",
    response_model=list[DocumentResponse],
    tags=["Documents"],
    summary="List uploaded documents",
    description="Returns uploaded documents and processing availability for the dashboard.",
    response_description="Uploaded document records.",
)
async def list_documents(
    workflow: Annotated[
        DocumentWorkflow | None,
        Query(description="Optional workflow filter. Use toc for TOC/hyperlinking or xml for PDF conversion."),
    ] = None,
) -> list[DocumentResponse]:
    return [
        DocumentResponse(
            id=record.id,
            filename=record.original_filename,
            status=record.status,
            workflow=record.workflow,
            created_at=record.created_at,
            updated_at=record.updated_at,
            page_count=_page_count(record.original_path),
            created_by="System",
            updated_by="Worker" if record.output_path or record.xml_path else "System",
            has_output=bool(record.output_path and record.output_path.exists()),
            has_toc=bool(record.toc_path and record.toc_path.exists()),
            has_xml=bool(record.xml_path and record.xml_path.exists()),
            error=record.error,
        )
        for record in storage.list_documents(settings)
        if workflow is None or record.workflow == workflow
    ]


@app.delete(
    "/documents/{document_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    tags=["Documents"],
    summary="Delete a document",
    description="Deletes the document record and removes the original PDF, generated PDF, and TOC JSON files from local storage.",
    responses={
        204: {"description": "Document deleted."},
        404: {"model": ErrorResponse, "description": "The document ID does not exist."},
        409: {"model": ErrorResponse, "description": "The document is currently processing."},
    },
)
async def delete_document(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> Response:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc

    if record.status in {DocumentStatus.QUEUED, DocumentStatus.PROCESSING}:
        raise HTTPException(status_code=409, detail="Cannot delete a document while it is queued or processing.")

    try:
        storage.delete_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc
    except Exception as exc:
        logger.exception("Failed to delete document %s", document_id)
        raise HTTPException(status_code=500, detail="Failed to delete document.") from exc

    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


def _page_count(pdf_path) -> int | None:
    try:
        with fitz.open(pdf_path) as document:
            return len(document)
    except Exception:
        logger.warning("Unable to read page count for %s", pdf_path)
        return None


@app.get(
    "/dry-run/toc/{document_id}",
    response_model=TocRevisionDryRunResponse,
    tags=["TOC Generation"],
    summary="Dry-run TOC revision and LEP impact",
    description=(
        "Analyzes the uploaded PDF and predicts what TOC, revision/date, hyperlink, and LEP updates "
        "would be required. This endpoint is read-only: it does not modify the source PDF, generated "
        "PDF, document record, jobs, activity log, or stored TOC JSON."
    ),
    response_description="Predicted TOC revision and LEP impact report.",
    responses={
        404: {"model": ErrorResponse, "description": "The document ID does not exist."},
        409: {"model": ErrorResponse, "description": "The document belongs to a different workflow."},
        500: {"model": ErrorResponse, "description": "The dry-run analysis failed."},
    },
)
async def dry_run_toc_revision(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> TocRevisionDryRunResponse:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc

    if record.workflow != DocumentWorkflow.TOC:
        raise HTTPException(status_code=409, detail="This file belongs to the PDF conversion workflow.")
    if not record.original_path.exists():
        raise HTTPException(status_code=404, detail="Original PDF file is not available.")

    try:
        return build_toc_revision_dry_run(
            record,
            settings=settings,
        )
    except Exception as exc:
        logger.exception("Failed to build TOC revision dry run for document %s", document_id)
        raise HTTPException(status_code=500, detail=f"Unable to build dry run: {exc}") from exc


@app.post(
    "/convert-xml/{document_id}",
    response_model=XmlConversionResponse,
    tags=["PDF to XML"],
    summary="Convert uploaded PDF to XML",
    description=(
        "Extracts a layout-preserving XML representation of the uploaded PDF. The XML includes "
        "document metadata, pages, text blocks, lines, spans, fonts, positions, image blocks, links, "
        "annotations, and conversion statistics."
    ),
    response_description="Generated XML conversion statistics.",
    responses={
        404: {"model": ErrorResponse, "description": "The document ID does not exist."},
        500: {"model": ErrorResponse, "description": "The PDF could not be converted to XML."},
    },
)
async def convert_xml(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> XmlConversionResponse:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc
    if record.workflow != DocumentWorkflow.XML:
        raise HTTPException(status_code=409, detail="This file belongs to the TOC and hyperlinking workflow.")

    try:
        storage.log_activity(
            action="xml_conversion_queued",
            status="info",
            message=f"Queued XML conversion for {record.original_filename}.",
            document=record,
            settings=settings,
        )
        storage.update_document_status(document_id, DocumentStatus.QUEUED, error=None, settings=settings)
        storage.log_activity(
            action="xml_conversion_started",
            status="info",
            message=f"Started XML conversion for {record.original_filename}.",
            document=record,
            settings=settings,
        )
        storage.update_document_status(document_id, DocumentStatus.PROCESSING, error=None, settings=settings)
        xml_path = settings.output_dir / f"{document_id}.xml"
        stats = convert_pdf_to_xml(
            document_id=document_id,
            filename=record.original_filename,
            pdf_path=record.original_path,
            xml_path=xml_path,
        )
        stats_path = storage.save_xml_stats(document_id, stats, settings)
        storage.save_xml_conversion(document_id, xml_path=xml_path, stats_path=stats_path, settings=settings)
        storage.log_activity(
            action="xml_conversion_completed",
            status="success",
            message=f"Completed XML conversion for {record.original_filename}.",
            document=record,
            metadata={"xml_path": str(xml_path), "page_count": stats.get("page_count")},
            settings=settings,
        )
        return XmlConversionResponse(**stats, xml_path=str(xml_path))
    except Exception as exc:
        storage.update_document_status(document_id, DocumentStatus.FAILED, error=str(exc), settings=settings)
        storage.log_activity(
            action="xml_conversion_failed",
            status="error",
            message=f"XML conversion failed for {record.original_filename}.",
            document=record,
            metadata={"error": str(exc)},
            settings=settings,
        )
        logger.exception("Failed to convert document %s to XML", document_id)
        raise HTTPException(status_code=500, detail=f"Failed to convert PDF to XML: {exc}") from exc


@app.get(
    "/xml/{document_id}/stats",
    response_model=XmlStatsResponse,
    tags=["PDF to XML"],
    summary="Get PDF-to-XML conversion statistics",
    response_description="Stored XML conversion statistics.",
    responses={
        404: {"model": ErrorResponse, "description": "The document or XML stats were not found."},
    },
)
async def get_xml_stats(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> XmlStatsResponse:
    try:
        stats = storage.load_xml_stats(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="XML has not been generated for this document.") from exc
    return XmlStatsResponse(**stats)


@app.get(
    "/xml/{document_id}/preview",
    response_class=PlainTextResponse,
    tags=["PDF to XML"],
    summary="Preview generated XML",
    description="Returns the beginning of the generated XML file for fast dashboard preview.",
    responses={
        404: {"model": ErrorResponse, "description": "The document or XML file was not found."},
    },
)
async def preview_xml(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> PlainTextResponse:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc

    if not record.xml_path or not record.xml_path.exists():
        raise HTTPException(status_code=404, detail="XML has not been generated for this document.")

    return PlainTextResponse(read_xml_preview(record.xml_path), media_type="application/xml")


@app.get(
    "/xml/{document_id}/download",
    tags=["PDF to XML"],
    summary="Download generated XML",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/xml": {}}, "description": "Generated XML file."},
        404: {"model": ErrorResponse, "description": "The document or XML file was not found."},
    },
)
async def download_xml(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> FileResponse:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc

    if not record.xml_path or not record.xml_path.exists():
        raise HTTPException(status_code=404, detail="XML has not been generated for this document.")

    filename = _download_filename(record.original_filename, suffix="_xml", extension=".xml")
    storage.log_activity(
        action="xml_downloaded",
        status="success",
        message=f"Downloaded XML for {record.original_filename} as {filename}.",
        document=record,
        metadata={"download_filename": filename},
        settings=settings,
    )
    return FileResponse(record.xml_path, media_type="application/xml", filename=filename)


@app.post(
    "/generate-toc/{document_id}",
    response_model=GenerateResponse,
    tags=["TOC Generation"],
    summary="Start smart TOC processing",
    description=(
        "Starts PDF TOC processing for an uploaded document. If visible TOC pages already exist, "
        "the service preserves them and repairs their links. If no visible TOC exists, it generates "
        "new clickable TOC pages from MEL ITEM + DESCRIPTION table rows, with layout-based heading "
        "detection as a fallback. "
        "Jobs are queued using an in-process Python worker queue."
    ),
    response_description="Queued generation job.",
    responses={
        404: {"model": ErrorResponse, "description": "The document ID does not exist."},
        503: {"model": ErrorResponse, "description": "The job could not be queued."},
    },
)
async def generate_toc(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> GenerateResponse:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc
    if record.workflow != DocumentWorkflow.TOC:
        raise HTTPException(status_code=409, detail="This file belongs to the PDF conversion workflow.")

    try:
        return jobs.enqueue_generation(document_id, settings)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to enqueue TOC generation: {exc}") from exc


@app.post(
    "/hyperlink-existing-toc/{document_id}",
    response_model=GenerateResponse,
    tags=["Existing TOC Linking"],
    summary="Hyperlink an existing visible TOC",
    description=(
        "Preserves the original PDF pages, detects existing global and chapter-level TOC pages, "
        "removes bad link annotations from those TOC pages, and overlays internal clickable links "
        "that jump to the matching target pages. EICAS MEL item references are also linked when "
        "the matching item exists in the same PDF. This endpoint is intended for PDFs that already "
        "have visible TOC pages."
    ),
    response_description="Queued existing-TOC hyperlinking job.",
    responses={
        404: {"model": ErrorResponse, "description": "The document ID does not exist."},
        503: {"model": ErrorResponse, "description": "The job could not be queued."},
    },
)
async def hyperlink_existing_toc(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> GenerateResponse:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc
    if record.workflow != DocumentWorkflow.TOC:
        raise HTTPException(status_code=409, detail="This file belongs to the PDF conversion workflow.")

    try:
        return jobs.enqueue_existing_toc_linking(document_id, settings)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to enqueue existing TOC hyperlinking: {exc}") from exc


@app.get(
    "/status/{job_id}",
    response_model=StatusResponse,
    tags=["TOC Generation", "Existing TOC Linking"],
    summary="Get generation job status",
    description="Returns the current state of a TOC generation job and includes the result after completion.",
    response_description="Current job status.",
    responses={
        404: {"model": ErrorResponse, "description": "The job ID does not exist."},
        503: {"model": ErrorResponse, "description": "The job backend is unavailable."},
    },
)
async def status(
    job_id: Annotated[str, Path(description="Job ID returned by the generate endpoint.")],
) -> StatusResponse:
    try:
        return jobs.get_generation_status(job_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to read job status: {exc}") from exc


@app.get(
    "/toc/{document_id}",
    response_model=list[TocEntryResponse],
    tags=["TOC Generation"],
    summary="Get generated TOC JSON",
    description=(
        "Returns the detected hierarchical table of contents for a processed document. "
        "Page numbers refer to the original PDF pages before inserted TOC pages."
    ),
    response_description="Detected TOC entries.",
    responses={
        404: {"model": ErrorResponse, "description": "The document or generated TOC was not found."},
    },
)
async def get_toc(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> list[TocEntryResponse]:
    try:
        headings = storage.load_toc(document_id, settings)
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="TOC has not been generated for this document.") from exc
    storage.log_activity(
        action="toc_viewed",
        status="info",
        message=f"Viewed TOC data for {record.original_filename}.",
        document=record,
        metadata={"heading_count": len(headings)},
        settings=settings,
    )
    return [
        TocEntryResponse(
            title=heading.title,
            level=heading.level,
            page=heading.page,
            confidence=heading.confidence,
        )
        for heading in headings
    ]


@app.get(
    "/download/{document_id}",
    tags=["Documents"],
    summary="Download the processed PDF",
    description=(
        "Downloads the final PDF after processing has completed. The file either preserves repaired "
        "visible TOC pages or includes inserted clickable TOC pages, plus sidebar PDF bookmarks."
    ),
    response_class=FileResponse,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Generated PDF file.",
        },
        404: {"model": ErrorResponse, "description": "The document or generated PDF was not found."},
    },
)
async def download(
    document_id: Annotated[str, Path(description="Document ID returned by the upload endpoint.")],
) -> FileResponse:
    try:
        record = storage.get_document(document_id, settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc

    if not record.output_path or not record.output_path.exists():
        raise HTTPException(status_code=404, detail="Generated PDF is not available.")

    filename = _download_filename(record.original_filename, suffix="_linked", extension=".pdf")
    storage.log_activity(
        action="pdf_downloaded",
        status="success",
        message=f"Downloaded linked PDF for {record.original_filename} as {filename}.",
        document=record,
        metadata={"download_filename": filename},
        settings=settings,
    )
    return FileResponse(
        record.output_path,
        media_type="application/pdf",
        filename=filename,
    )


def _download_filename(original_filename: str, *, suffix: str, extension: str) -> str:
    original_name = PureWindowsPath(original_filename or "document").name
    stem = original_name.rsplit(".", 1)[0] if "." in original_name else original_name
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" ._")
    if not safe_stem:
        safe_stem = "document"
    return f"{safe_stem}{suffix}{extension}"
